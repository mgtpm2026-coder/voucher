"""Authentication hardening: rate limiting, OTP handling, audit logging.

Rate limits and OTP attempt counters live in MySQL rather than in process
memory on purpose - under gunicorn there are several worker processes, and
an in-memory counter would let an attacker get N times the allowed attempts
by spreading them across workers.
"""

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import string
from datetime import datetime, timedelta

from flask import request, session

from db import execute, query, transaction

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_MINUTES = 15

OTP_REQUEST_MAX = 3
OTP_REQUEST_WINDOW_MINUTES = 15

MIN_PASSWORD_LENGTH = 10

# Passwords seen constantly in Indian SME deployments plus the ones this
# repository used to ship. Cheap to check, and blocks the realistic guesses.
_WEAK_PASSWORDS = {
    "password", "password1", "password123", "changeme@123", "changeme123",
    "admin@123", "admin123", "welcome@123", "welcome123", "qwerty123",
    "12345678", "123456789", "1234567890", "india@123", "mgt@123",
    "voucher@123", "abcd1234", "iloveyou", "letmein123",
}


def now():
    """Single source of time for the application layer.

    MySQL's session time zone is pinned to the same offset in db.py, so
    NOW() and this function agree.
    """
    return datetime.now()


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------

def password_problem(password, username=None):
    """Return a human-readable reason the password is unacceptable, or None."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in _WEAK_PASSWORDS:
        return "That password is too common. Choose something harder to guess."
    if username and username.lower() in password.lower():
        return "Password must not contain your username."
    classes = sum(bool(p.search(password)) for p in (
        re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"),
        re.compile(r"[^A-Za-z0-9]"),
    ))
    if classes < 3:
        return ("Password must mix at least three of: lowercase, uppercase, "
                "digits, symbols.")
    return None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _prune(bucket, window_minutes):
    execute("DELETE FROM rate_limits WHERE bucket=%s AND attempted_at < %s",
            (bucket, now() - timedelta(minutes=window_minutes)))


def rate_limited(bucket, max_attempts, window_minutes):
    """True when `bucket` has already used up its allowance."""
    _prune(bucket, window_minutes)
    row = query("SELECT COUNT(*) c FROM rate_limits WHERE bucket=%s", (bucket,), True)
    return (row["c"] if row else 0) >= max_attempts


def record_attempt(bucket):
    execute("INSERT INTO rate_limits(bucket, attempted_at) VALUES(%s,%s)", (bucket, now()))


def clear_attempts(bucket):
    execute("DELETE FROM rate_limits WHERE bucket=%s", (bucket,))


def client_ip():
    # Only trust X-Forwarded-For when a proxy is declared, otherwise a caller
    # can spoof the header and dodge per-IP limits entirely.
    if os.getenv("TRUST_PROXY", "0") == "1":
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:45]
    return (request.remote_addr or "")[:45]


# ---------------------------------------------------------------------------
# OTP
# ---------------------------------------------------------------------------

def _otp_pepper():
    return (os.getenv("SECRET_KEY") or "").encode() or b"voucher-otp"


def hash_otp(code):
    return hashlib.sha256(_otp_pepper() + str(code).strip().encode()).hexdigest()


def generate_otp():
    return "".join(random.SystemRandom().choice(string.digits) for _ in range(6))


def create_otp(user_type, user_id, purpose, payload=None):
    """Create a one-time code and return the plaintext to send.

    Only the hash is stored, so a database leak does not hand over live
    codes. Any earlier pending code for the same purpose is invalidated so
    two codes are never valid at once.
    """
    code = generate_otp()
    execute("""UPDATE otp_codes SET consumed_at=%s
                WHERE user_type=%s AND user_id=%s AND purpose=%s AND consumed_at IS NULL""",
            (now(), user_type, user_id, purpose))
    execute("""INSERT INTO otp_codes(user_type,user_id,purpose,otp_code,payload,
                                     expires_at,attempts,created_at)
               VALUES(%s,%s,%s,%s,%s,%s,0,%s)""",
            (user_type, user_id, purpose, hash_otp(code), json.dumps(payload or {}),
             now() + timedelta(minutes=OTP_TTL_MINUTES), now()))
    return code


def verify_otp(user_type, user_id, purpose, code):
    """Return (payload, error). Consumes the code on success.

    Runs inside one transaction with SELECT ... FOR UPDATE so two parallel
    submissions cannot both consume the same code.
    """
    with transaction() as cur:
        cur.execute("""SELECT * FROM otp_codes
                        WHERE user_type=%s AND user_id=%s AND purpose=%s AND consumed_at IS NULL
                        ORDER BY id DESC LIMIT 1 FOR UPDATE""",
                    (user_type, user_id, purpose))
        row = cur.fetchone()
        if not row:
            return None, "No pending verification found. Please request a new code."
        if now() > row["expires_at"]:
            cur.execute("UPDATE otp_codes SET consumed_at=%s WHERE id=%s", (now(), row["id"]))
            return None, "That code has expired. Please request a new one."
        if row["attempts"] >= OTP_MAX_ATTEMPTS:
            cur.execute("UPDATE otp_codes SET consumed_at=%s WHERE id=%s", (now(), row["id"]))
            return None, "Too many incorrect attempts. Please request a new code."
        if not hmac.compare_digest(str(row["otp_code"]), hash_otp(code)):
            cur.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE id=%s", (row["id"],))
            left = OTP_MAX_ATTEMPTS - (row["attempts"] + 1)
            if left <= 0:
                cur.execute("UPDATE otp_codes SET consumed_at=%s WHERE id=%s", (now(), row["id"]))
                return None, "Too many incorrect attempts. Please request a new code."
            return None, f"Incorrect code. {left} attempt{'s' if left != 1 else ''} remaining."
        cur.execute("UPDATE otp_codes SET consumed_at=%s WHERE id=%s", (now(), row["id"]))
        return json.loads(row["payload"] or "{}"), None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _jsonable(value):
    from decimal import Decimal
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat(sep=" ")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def audit(action, entity, entity_id=None, before=None, after=None, actor=None):
    """Append one immutable record of who did what.

    Never raises: an audit write failing must not roll back the business
    action it describes, but it is logged loudly so the gap is visible.
    """
    try:
        if actor:
            a_type, a_id, a_name = actor
        else:
            a_type = session.get("role") or "system"
            a_id = session.get("user_id")
            a_name = session.get("name")
        clean = lambda d: json.dumps({k: _jsonable(v) for k, v in d.items()}) if d else None
        execute("""INSERT INTO audit_log(actor_type,actor_id,actor_name,action,entity,entity_id,
                                         before_json,after_json,ip,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (a_type, a_id, a_name, action, entity, entity_id,
                 clean(before), clean(after), client_ip(), now()))
    except Exception as exc:  # pragma: no cover - defensive
        from flask import current_app
        current_app.logger.error("AUDIT WRITE FAILED action=%s entity=%s id=%s: %s",
                                 action, entity, entity_id, exc)


def new_session_token():
    return secrets.token_urlsafe(24)
