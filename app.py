"""MGT Voucher Management - application routes.

Structure:
    db.py         connection pool, query/execute/transaction helpers
    security.py   rate limiting, OTP, password policy, audit log
    vouchers.py   money arithmetic, payment status, voucher numbering
    pdfgen.py     printed payment voucher
    app.py        routes (this file)
"""

import io
import logging
import os
import re
import secrets
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from logging.handlers import RotatingFileHandler

import mysql.connector
import pandas as pd
from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from flask_wtf.csrf import CSRFError, CSRFProtect
from PIL import Image as PILImage
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# Must run before the local modules are imported, so anything that reads
# configuration sees the values from .env.
load_dotenv()

import notifications  # noqa: E402
import pdfgen  # noqa: E402
import security  # noqa: E402
from db import execute, query, transaction  # noqa: E402
from security import audit, now  # noqa: E402
from vouchers import (PAID_COL, PAID_JOIN, allocate_voucher_no, decorate,  # noqa: E402
                      money, money_or_zero)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PLACEHOLDER_KEYS = {"CHANGE_THIS_SECRET_KEY", "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET", ""}
_secret = os.getenv("SECRET_KEY", "")
if _secret in PLACEHOLDER_KEYS:
    if os.getenv("FLASK_ENV") == "test":
        _secret = secrets.token_hex(32)
    else:
        # Refuse to start rather than run with a key published in the
        # repository, which would let anyone forge a manager session.
        raise SystemExit(
            "SECRET_KEY is missing or still set to the placeholder.\n"
            "Generate one with:  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "and put it in your .env file as SECRET_KEY=<value>."
        )
app.secret_key = _secret

UPLOAD_FOLDER = os.path.abspath(os.getenv("UPLOAD_FOLDER", "uploads"))
PROFILE_FOLDER = os.path.join(UPLOAD_FOLDER, "profiles")
LEGACY_PROFILE_FOLDER = os.path.join(app.static_folder, "uploads", "profiles")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROFILE_FOLDER, exist_ok=True)

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "8"))
PER_PAGE = max(10, int(os.getenv("PER_PAGE", "50")))

app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set COOKIE_SECURE=0 only for local plain-HTTP development.
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    WTF_CSRF_TIME_LIMIT=None,
)

csrf = CSRFProtect(app)

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "pdf", "webp"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
PAYMENT_TYPES = ["Cash", "UPI", "Bank Transfer", "Cheque", "Other"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = os.getenv("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_handler = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"),
                               maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_handler.setLevel(logging.INFO)
app.logger.addHandler(_handler)
app.logger.setLevel(logging.INFO)
notifications.set_logger(app.logger)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^[0-9+\-\s()]{6,20}$")


def valid_email(email):
    return bool(email) and bool(EMAIL_RE.match(email.strip()))


def valid_phone(phone):
    return bool(phone) and bool(PHONE_RE.match(phone.strip()))


def field(name, max_length, required=False, label=None):
    """Read a form field, trim it, and enforce the column width.

    Previously an over-long value went straight to MySQL and raised a 500.
    """
    value = (request.form.get(name) or "").strip()
    label = label or name.replace("_", " ").title()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > max_length:
        raise ValueError(f"{label} must be {max_length} characters or fewer.")
    return value


def parse_date(value, label="Date", allow_future=False):
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    try:
        parsed = datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{label} must be a valid date.")
    if not allow_future and parsed > date.today():
        raise ValueError(f"{label} cannot be in the future.")
    if parsed < date.today() - timedelta(days=365 * 5):
        raise ValueError(f"{label} looks too far in the past.")
    return parsed.strftime("%Y-%m-%d")


def allowed_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _verify_upload(stream, ext):
    """Check the bytes match the claimed type, not just the file name."""
    head = stream.read(8)
    stream.seek(0)
    if ext == "pdf":
        if not head.startswith(b"%PDF-"):
            raise ValueError("That file is not a valid PDF.")
        return
    try:
        with PILImage.open(stream) as im:
            im.verify()
    except Exception:
        raise ValueError("That image could not be read. Upload a valid JPG, PNG or WEBP.")
    finally:
        stream.seek(0)


def employee_folder(name):
    return os.path.join(UPLOAD_FOLDER, "Employees", secure_filename(name or "") or "Employee")


def voucher_folder(employee, date_value, voucher_no):
    try:
        month = datetime.strptime(str(date_value)[:10], "%Y-%m-%d").strftime("%Y-%m")
    except ValueError:
        month = now().strftime("%Y-%m")
    path = os.path.join(employee_folder(employee), month, secure_filename(voucher_no))
    os.makedirs(path, exist_ok=True)
    return path


def validate_upload(file):
    """Check an upload before anything is written to the database.

    Returns True if there is a usable file. Raises ValueError with a message
    for the user if the file is present but unacceptable, so a bad bill never
    leaves a half-created voucher behind.
    """
    if not file or not file.filename:
        return False
    if not allowed_file(file.filename):
        raise ValueError("Unsupported file type. Upload a JPG, PNG, WEBP or PDF.")
    _verify_upload(file.stream, file.filename.rsplit(".", 1)[1].lower())
    return True


def save_upload(file, employee, date_value, voucher_no, prefix="Bill"):
    if not file or not file.filename:
        return ""
    if not allowed_file(file.filename):
        raise ValueError("Unsupported file type. Upload a JPG, PNG, WEBP or PDF.")
    ext = file.filename.rsplit(".", 1)[1].lower()
    _verify_upload(file.stream, ext)
    filename = f"{prefix}_{now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}.{ext}"
    path = os.path.join(voucher_folder(employee, date_value, voucher_no), filename)
    file.save(path)
    return os.path.relpath(path, UPLOAD_FOLDER).replace("\\", "/")


def safe_upload_path(relative):
    """Resolve a stored relative path, refusing anything outside the root."""
    if not relative:
        return None
    full = os.path.abspath(os.path.join(UPLOAD_FOLDER, relative))
    if os.path.commonpath([full, UPLOAD_FOLDER]) != UPLOAD_FOLDER:
        return None
    return full if os.path.isfile(full) else None


def save_profile_image(file, user_type, user_id):
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError("Profile photo must be a JPG, PNG or WEBP image.")
    _verify_upload(file.stream, ext)
    filename = f"{user_type}_{user_id}_{now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}.{ext}"
    file.save(os.path.join(PROFILE_FOLDER, filename))
    return filename


def current_user():
    if "user_cache" in g:
        return g.user_cache
    g.user_cache = None
    role, uid = session.get("role"), session.get("user_id")
    if role in ("employee", "manager") and uid:
        table = "employees" if role == "employee" else "managers"
        g.user_cache = query(f"SELECT * FROM {table} WHERE id=%s", (uid,), True)
    return g.user_cache


def profile_table(role):
    return "employees" if role == "employee" else "managers"


def profile_url():
    return url_for("employee_profile" if session.get("role") == "employee" else "manager_profile")


def login_required(role=None):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not session.get("user_id"):
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))
            user = current_user()
            # Disabling an account now ends its session at the next request
            # instead of leaving it live until the cookie is discarded.
            if not user or not user.get("is_active"):
                session.clear()
                flash("Your account is no longer active. Contact your manager.", "danger")
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                flash("You do not have access to that page.", "danger")
                return redirect(url_for("dashboard"))
            if user.get("must_change_password") and request.endpoint not in (
                    "change_password", "logout", "static"):
                return redirect(url_for("change_password"))
            return f(*a, **kw)
        return wrapper
    return deco


@app.context_processor
def template_globals():
    return {"current_user": current_user(), "role": session.get("role")}


@app.template_filter("rupees")
def rupees(value):
    return f"{money_or_zero(value):,.2f}"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.errorhandler(CSRFError)
def handle_csrf(e):
    app.logger.warning("CSRF rejected: %s %s from %s", request.method, request.path,
                       security.client_ip())
    flash("Your session expired or the form was stale. Please try again.", "danger")
    return redirect(request.referrer or url_for("dashboard")), 302


@app.errorhandler(413)
def handle_too_large(e):
    flash(f"That file is too large. The limit is {MAX_UPLOAD_MB} MB.", "danger")
    return redirect(request.referrer or url_for("dashboard")), 302


@app.errorhandler(404)
def handle_404(e):
    return render_template("error.html", code=404,
                           title="Page not found",
                           message="That page does not exist."), 404


@app.errorhandler(403)
def handle_403(e):
    return render_template("error.html", code=403,
                           title="Not allowed",
                           message="You do not have access to that item."), 403


@app.errorhandler(Exception)
def handle_500(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled error on %s %s", request.method, request.path)
    return render_template("error.html", code=500,
                           title="Something went wrong",
                           message="The error has been logged. Please try again, "
                                   "and tell your administrator if it keeps happening."), 500


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Username and password are required.", "danger")
            return render_template("login.html")

        ip_bucket = f"login-ip:{security.client_ip()}"
        user_bucket = f"login-user:{username.lower()}"
        if (security.rate_limited(user_bucket, security.LOGIN_MAX_ATTEMPTS,
                                  security.LOGIN_WINDOW_MINUTES)
                or security.rate_limited(ip_bucket, security.LOGIN_MAX_ATTEMPTS * 4,
                                         security.LOGIN_WINDOW_MINUTES)):
            app.logger.warning("Login throttled for %s from %s", username, security.client_ip())
            flash(f"Too many failed attempts. Try again in "
                  f"{security.LOGIN_WINDOW_MINUTES} minutes.", "danger")
            return render_template("login.html"), 429

        user = query("SELECT * FROM employees WHERE username=%s AND is_active=1", (username,), True)
        role = "employee"
        if not user:
            user = query("SELECT * FROM managers WHERE username=%s AND is_active=1",
                         (username,), True)
            role = "manager"

        if user and check_password_hash(user["password_hash"], password):
            security.clear_attempts(user_bucket)
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            session["role"] = role
            session["name"] = user["full_name"]
            audit("login", "user", user["id"],
                  actor=(role, user["id"], user["full_name"]))
            app.logger.info("Login ok: %s (%s)", username, role)
            flash(f"Welcome, {user['full_name']}!", "success")
            return redirect(url_for("dashboard"))

        security.record_attempt(user_bucket)
        security.record_attempt(ip_bucket)
        app.logger.info("Login failed: %s from %s", username, security.client_ip())
        # Deliberately identical message whether the username exists or not.
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    if session.get("user_id"):
        audit("logout", "user", session.get("user_id"))
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required()
def dashboard():
    return redirect(url_for("employee_dashboard" if session["role"] == "employee"
                            else "manager_dashboard"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required()
def change_password():
    user = current_user()
    table = profile_table(session["role"])
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not check_password_hash(user["password_hash"], current):
            flash("Your current password is not correct.", "danger")
        elif new != confirm:
            flash("The two new passwords do not match.", "danger")
        else:
            problem = security.password_problem(new, user["username"])
            if problem:
                flash(problem, "danger")
            else:
                execute(f"UPDATE {table} SET password_hash=%s, must_change_password=0 WHERE id=%s",
                        (generate_password_hash(new), user["id"]))
                audit("password_change", "user", user["id"])
                flash("Password updated.", "success")
                return redirect(url_for("dashboard"))
    return render_template("change_password.html",
                           forced=bool(user.get("must_change_password")),
                           min_length=security.MIN_PASSWORD_LENGTH)


# ---------------------------------------------------------------------------
# Files - receipts, payment proofs, profile photos
#
# Served by record id rather than by path taken from the URL, so there is no
# traversal surface and the ownership check is unavoidable.
# ---------------------------------------------------------------------------

def _send_upload(relative, download_name=None):
    path = safe_upload_path(relative)
    if not path:
        abort(404)
    return send_file(path, download_name=download_name or os.path.basename(path),
                     max_age=0, conditional=True)


@app.route("/voucher/<int:voucher_id>/receipt")
@login_required()
def voucher_receipt(voucher_id):
    v = query("SELECT id, employee_id, voucher_no, receipt FROM vouchers WHERE id=%s",
              (voucher_id,), True)
    if not v or not v["receipt"]:
        abort(404)
    if session["role"] == "employee" and v["employee_id"] != session["user_id"]:
        abort(403)
    ext = os.path.splitext(v["receipt"])[1]
    return _send_upload(v["receipt"], f"{v['voucher_no']}_bill{ext}")


@app.route("/payment/<int:payment_id>/proof")
@login_required()
def payment_proof(payment_id):
    p = query("""SELECT p.proof_path, p.voucher_id, v.employee_id, v.voucher_no
                   FROM payments p JOIN vouchers v ON v.id=p.voucher_id
                  WHERE p.id=%s""", (payment_id,), True)
    if not p or not p["proof_path"]:
        abort(404)
    if session["role"] == "employee" and p["employee_id"] != session["user_id"]:
        abort(403)
    ext = os.path.splitext(p["proof_path"])[1]
    return _send_upload(p["proof_path"], f"{p['voucher_no']}_payment{ext}")


@app.route("/profile/photo/<user_type>/<int:user_id>")
@login_required()
def profile_photo_file(user_type, user_id):
    if user_type not in ("employee", "manager"):
        abort(404)
    table = "employees" if user_type == "employee" else "managers"
    row = query(f"SELECT profile_image FROM {table} WHERE id=%s", (user_id,), True)
    if not row or not row["profile_image"]:
        abort(404)
    stored = row["profile_image"]
    # Photos uploaded before v3 were kept under static/uploads/profiles.
    legacy = os.path.abspath(os.path.join(app.static_folder, stored))
    if stored.startswith("uploads/profiles/") and os.path.isfile(legacy):
        return send_file(legacy, max_age=0, conditional=True)
    path = os.path.join(PROFILE_FOLDER, os.path.basename(stored))
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, max_age=0, conditional=True)


# ---------------------------------------------------------------------------
# Employee - dashboard and vouchers
# ---------------------------------------------------------------------------

def _voucher_rows(where="", params=(), limit=None, offset=0):
    sql = f"""SELECT v.*, e.full_name employee_name, e.department, {PAID_COL}
                FROM vouchers v
                JOIN employees e ON e.id = v.employee_id
                {PAID_JOIN}
                {where}
               ORDER BY v.date DESC, v.id DESC"""
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params = tuple(params) + (limit, offset)
    return [decorate(r, r["paid_to_date"]) for r in query(sql, tuple(params))]


def _summary(where="", params=()):
    row = query(f"""SELECT COUNT(*) cnt,
                           COALESCE(SUM(v.amount),0) claims,
                           COALESCE(SUM(COALESCE(pay.paid,0)),0) paid,
                           COALESCE(SUM(v.status='Pending'),0) pending,
                           COALESCE(SUM(v.status='Approved'),0) approved,
                           COALESCE(SUM(v.status='Rejected'),0) rejected
                      FROM vouchers v
                      JOIN employees e ON e.id = v.employee_id
                      {PAID_JOIN}
                      {where}""", tuple(params), True) or {}
    claims, paid = money_or_zero(row.get("claims")), money_or_zero(row.get("paid"))
    return {"count": row.get("cnt", 0), "total": claims, "claims": claims, "paid": paid,
            "balance": claims - paid, "pending": row.get("pending", 0) or 0,
            "approved": row.get("approved", 0) or 0, "rejected": row.get("rejected", 0) or 0}


@app.route("/employee")
@login_required("employee")
def employee_dashboard():
    rows = _voucher_rows("WHERE v.employee_id=%s", (session["user_id"],))
    summary = _summary("WHERE v.employee_id=%s", (session["user_id"],))
    months = {}
    for v in rows:
        key = str(v["date"])[:7]
        m = months.setdefault(key, {"total": money("0"), "paid": money("0"),
                                    "balance": money("0"), "vouchers": []})
        m["total"] += money_or_zero(v["amount"])
        m["paid"] += v["paid_amount"]
        m["balance"] += v["balance"]
        m["vouchers"].append(v)
    return render_template("employee_dashboard.html", vouchers=rows, summary=summary,
                           months=months)


@app.route("/voucher/new", methods=["GET", "POST"])
@login_required("employee")
def new_voucher():
    if request.method == "POST":
        try:
            amount = money(request.form.get("amount"))
            if amount <= 0:
                raise ValueError("Amount must be greater than zero.")
            if amount > money("9999999999"):
                raise ValueError("Amount is larger than this system supports.")
            date_value = parse_date(request.form.get("date"), "Date")
            payable_to = field("payable_to", 150)
            purpose = field("purpose", 255, required=True, label="Purpose")
            description = field("description", 5000, required=True, label="Description")
            mode = field("expense_payment_mode", 50)
            txn = field("transaction_id", 150)
            file = request.files.get("receipt")
            # Validated before the insert so a rejected bill cannot leave a
            # voucher behind with no receipt attached.
            has_receipt = validate_upload(file)

            # The number is allocated and the row inserted in one transaction,
            # so concurrent submissions queue instead of colliding.
            with transaction() as cur:
                no = allocate_voucher_no(cur)
                cur.execute("""INSERT INTO vouchers(voucher_no,employee_id,payable_to,date,purpose,
                                   description,amount,expense_payment_mode,transaction_id,receipt,
                                   status,created_at)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'','Pending',%s)""",
                            (no, session["user_id"], payable_to, date_value, purpose,
                             description, amount, mode, txn, now()))
                voucher_id = cur.lastrowid

            # Written to disk after the row exists, so a failed insert cannot
            # orphan a file on the filesystem.
            if has_receipt:
                path = save_upload(file, session["name"], date_value, no)
                execute("UPDATE vouchers SET receipt=%s WHERE id=%s", (path, voucher_id))

            audit("voucher_create", "voucher", voucher_id,
                  after={"voucher_no": no, "amount": amount, "purpose": purpose})
            flash(f"Voucher {no} submitted successfully.", "success")
            return redirect(url_for("employee_dashboard"))
        except ValueError as e:
            flash(str(e), "danger")
    return render_template("voucher_form.html", today=date.today().isoformat())


@app.route("/voucher/<int:voucher_id>/edit", methods=["GET", "POST"])
@login_required("employee")
def edit_voucher(voucher_id):
    v = query(f"""SELECT v.*, {PAID_COL} FROM vouchers v {PAID_JOIN} WHERE v.id=%s""",
              (voucher_id,), True)
    if not v or v["employee_id"] != session["user_id"]:
        flash("Voucher not found.", "danger")
        return redirect(url_for("employee_dashboard"))
    if v["status"] not in ("Pending", "Rejected"):
        flash("Only pending or rejected vouchers can be edited.", "danger")
        return redirect(url_for("employee_dashboard"))
    # A voucher that has money against it is a settled record, whatever its
    # status says. Editing it would let a paid claim be re-submitted.
    if money_or_zero(v["paid_to_date"]) > 0:
        flash("This voucher already has payments recorded and can no longer be edited. "
              "Ask your manager to review it.", "danger")
        return redirect(url_for("employee_dashboard"))

    if request.method == "POST":
        try:
            amount = money(request.form.get("amount"))
            if amount <= 0:
                raise ValueError("Amount must be greater than zero.")
            date_value = parse_date(request.form.get("date") or str(v["date"])[:10], "Date")
            payable_to = field("payable_to", 150)
            purpose = field("purpose", 255, required=True, label="Purpose")
            description = field("description", 5000, required=True, label="Description")
            mode = field("expense_payment_mode", 50)
            txn = field("transaction_id", 150)
            receipt = v["receipt"]
            file = request.files.get("receipt")
            if file and file.filename:
                receipt = save_upload(file, session["name"], date_value, v["voucher_no"])

            with transaction() as cur:
                cur.execute("""SELECT status, amount FROM vouchers WHERE id=%s FOR UPDATE""",
                            (voucher_id,))
                fresh = cur.fetchone()
                if not fresh or fresh["status"] not in ("Pending", "Rejected"):
                    raise ValueError("This voucher was just reviewed and can no longer be edited.")
                cur.execute("SELECT COALESCE(SUM(amount),0) paid FROM payments WHERE voucher_id=%s",
                            (voucher_id,))
                if money_or_zero(cur.fetchone()["paid"]) > 0:
                    raise ValueError("This voucher already has payments recorded.")
                was_rejected = fresh["status"] == "Rejected"
                cur.execute(f"""UPDATE vouchers SET payable_to=%s,date=%s,purpose=%s,description=%s,
                                   amount=%s,expense_payment_mode=%s,transaction_id=%s,receipt=%s,
                                   updated_at=%s
                                   {",status='Pending',reject_reason=NULL" if was_rejected else ""}
                                WHERE id=%s""",
                            (payable_to, date_value, purpose, description, amount, mode, txn,
                             receipt, now(), voucher_id))

            audit("voucher_edit", "voucher", voucher_id,
                  before={"amount": v["amount"], "purpose": v["purpose"], "status": v["status"]},
                  after={"amount": amount, "purpose": purpose})
            flash(f"Voucher {v['voucher_no']} updated"
                  + (" and resubmitted for approval." if was_rejected else "."), "success")
            return redirect(url_for("employee_dashboard"))
        except ValueError as e:
            flash(str(e), "danger")
    return render_template("voucher_form.html", voucher=v, today=date.today().isoformat())


@app.route("/voucher/<int:voucher_id>/pdf")
@login_required()
def voucher_pdf(voucher_id):
    v = query(f"""SELECT v.*, e.full_name employee_name, {PAID_COL}
                    FROM vouchers v JOIN employees e ON e.id=v.employee_id {PAID_JOIN}
                   WHERE v.id=%s""", (voucher_id,), True)
    if not v:
        abort(404)
    if session["role"] == "employee" and v["employee_id"] != session["user_id"]:
        abort(403)
    decorate(v, v["paid_to_date"])
    payments = query("""SELECT * FROM payments WHERE voucher_id=%s ORDER BY payment_date, id""",
                     (voucher_id,))
    buf = pdfgen.build_pdf(v, v["employee_name"], payments, app.static_folder,
                           safe_upload_path(v.get("receipt")))
    return send_file(buf, as_attachment=True, download_name=f"{v['voucher_no']}.pdf",
                     mimetype="application/pdf")


@app.route("/voucher/<int:voucher_id>/history")
@app.route("/manager/voucher/<int:voucher_id>/history")
@login_required()
def payment_history(voucher_id):
    v = query(f"""SELECT v.*, e.full_name employee_name, {PAID_COL}
                    FROM vouchers v JOIN employees e ON e.id=v.employee_id {PAID_JOIN}
                   WHERE v.id=%s""", (voucher_id,), True)
    if not v:
        abort(404)
    if session["role"] == "employee" and v["employee_id"] != session["user_id"]:
        abort(403)
    decorate(v, v["paid_to_date"])
    payments = query("""SELECT p.*, m.full_name recorded_by
                          FROM payments p LEFT JOIN managers m ON m.id=p.created_by
                         WHERE p.voucher_id=%s ORDER BY p.payment_date DESC, p.id DESC""",
                     (voucher_id,))
    return render_template("payment_history.html", voucher=v, payments=payments)


# ---------------------------------------------------------------------------
# Manager - approvals and payments
# ---------------------------------------------------------------------------

@app.route("/manager")
@login_required("manager")
def manager_dashboard():
    status = request.args.get("status", "")
    employee_id = request.args.get("employee_id", "")
    month = request.args.get("month", "")
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    where, params = "WHERE 1=1", []
    if status in ("Pending", "Approved", "Rejected"):
        where += " AND v.status=%s"
        params.append(status)
    if employee_id.isdigit():
        where += " AND v.employee_id=%s"
        params.append(int(employee_id))
    if re.fullmatch(r"\d{4}-\d{2}", month or ""):
        where += " AND DATE_FORMAT(v.date,'%%Y-%%m')=%s"
        params.append(month)

    total = (query(f"""SELECT COUNT(*) c FROM vouchers v
                       JOIN employees e ON e.id=v.employee_id {where}""",
                   tuple(params), True) or {}).get("c", 0)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    vouchers = _voucher_rows(where, params, limit=PER_PAGE, offset=(page - 1) * PER_PAGE)

    return render_template("manager_dashboard.html",
                           vouchers=vouchers,
                           summary=_summary(),
                           filtered_total=total,
                           page=page, pages=pages,
                           employees=query("SELECT id, full_name FROM employees "
                                           "WHERE is_active=1 ORDER BY full_name"),
                           payment_types=PAYMENT_TYPES,
                           filters={"status": status, "employee_id": employee_id, "month": month})


@app.route("/manager/voucher/<int:voucher_id>/approve", methods=["POST"])
@login_required("manager")
def approve(voucher_id):
    try:
        with transaction() as cur:
            cur.execute("SELECT * FROM vouchers WHERE id=%s FOR UPDATE", (voucher_id,))
            v = cur.fetchone()
            if not v:
                raise ValueError("Voucher not found.")
            if v["status"] != "Pending":
                raise ValueError(f"That voucher is already {v['status']}. "
                                 "Refresh the page to see its current state.")
            cur.execute("""UPDATE vouchers SET status='Approved', approved_at=%s,
                              approved_by=%s, approved_by_id=%s, reject_reason=NULL
                            WHERE id=%s AND status='Pending'""",
                        (now(), session["name"], session["user_id"], voucher_id))
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("manager_dashboard"))

    audit("voucher_approve", "voucher", voucher_id,
          before={"status": "Pending"}, after={"status": "Approved"})
    v = query("""SELECT v.*, e.full_name, e.email, e.phone FROM vouchers v
                   JOIN employees e ON e.id=v.employee_id WHERE v.id=%s""", (voucher_id,), True)
    if v:
        notifications.notify_employee(
            v, f"Voucher {v['voucher_no']} approved",
            f"Hi {v['full_name']}, your voucher {v['voucher_no']} for "
            f"Rs. {money_or_zero(v['amount']):,.2f} has been approved.")
    flash("Voucher approved successfully.", "success")
    return redirect(request.referrer or url_for("manager_dashboard"))


@app.route("/manager/voucher/<int:voucher_id>/reject", methods=["POST"])
@login_required("manager")
def reject(voucher_id):
    try:
        reason = field("reason", 2000, required=True, label="Rejection reason")
        with transaction() as cur:
            cur.execute("SELECT * FROM vouchers WHERE id=%s FOR UPDATE", (voucher_id,))
            v = cur.fetchone()
            if not v:
                raise ValueError("Voucher not found.")
            if v["status"] == "Rejected":
                raise ValueError("That voucher is already rejected.")
            cur.execute("SELECT COALESCE(SUM(amount),0) paid FROM payments WHERE voucher_id=%s",
                        (voucher_id,))
            paid = money_or_zero(cur.fetchone()["paid"])
            # Rejecting a voucher that has been paid used to leave the payments
            # attached and let the employee edit and resubmit a settled claim.
            if paid > 0:
                raise ValueError(
                    f"Rs. {paid:,.2f} has already been paid against this voucher, so it "
                    "cannot be rejected. Reverse the payments first if this is an error.")
            cur.execute("UPDATE vouchers SET status='Rejected', reject_reason=%s WHERE id=%s",
                        (reason, voucher_id))
            previous = v["status"]
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("manager_dashboard"))

    audit("voucher_reject", "voucher", voucher_id,
          before={"status": previous}, after={"status": "Rejected", "reason": reason})
    v = query("""SELECT v.*, e.full_name, e.email, e.phone FROM vouchers v
                   JOIN employees e ON e.id=v.employee_id WHERE v.id=%s""", (voucher_id,), True)
    if v:
        notifications.notify_employee(
            v, f"Voucher {v['voucher_no']} rejected",
            f"Hi {v['full_name']}, your voucher {v['voucher_no']} was rejected. Reason: {reason}")
    flash("Voucher rejected with reason recorded.", "warning")
    return redirect(request.referrer or url_for("manager_dashboard"))


@app.route("/manager/voucher/<int:voucher_id>/payment", methods=["POST"])
@login_required("manager")
def record_payment(voucher_id):
    proof_file = request.files.get("proof")
    try:
        amount = money(request.form.get("amount"))
        if amount <= 0:
            raise ValueError("Payment amount must be greater than zero.")
        payment_type = field("payment_type", 30, required=True, label="Payment type")
        if payment_type not in PAYMENT_TYPES:
            raise ValueError("Choose a valid payment type.")
        payment_date = parse_date(request.form.get("payment_date") or date.today().isoformat(),
                                  "Payment date")
        reference_no = field("reference_no", 150)
        remarks = field("remarks", 2000)
        has_proof = validate_upload(proof_file)

        # Read the balance and write the payment under one row lock, so two
        # managers cannot both spend the same remaining balance.
        with transaction() as cur:
            cur.execute("SELECT * FROM vouchers WHERE id=%s FOR UPDATE", (voucher_id,))
            v = cur.fetchone()
            if not v:
                raise ValueError("Voucher not found.")
            if v["status"] != "Approved":
                raise ValueError("Only approved vouchers can receive payments.")
            cur.execute("SELECT COALESCE(SUM(amount),0) paid FROM payments WHERE voucher_id=%s",
                        (voucher_id,))
            paid = money_or_zero(cur.fetchone()["paid"])
            balance = money_or_zero(v["amount"]) - paid
            if amount > balance:
                raise ValueError(f"Payment cannot exceed the remaining balance of "
                                 f"Rs. {balance:,.2f}.")
            path = ""
            if has_proof:
                cur.execute("SELECT full_name FROM employees WHERE id=%s", (v["employee_id"],))
                emp = cur.fetchone()
                path = save_upload(proof_file, emp["full_name"] if emp else "Employee",
                                   v["date"], v["voucher_no"], "Payment")
            cur.execute("""INSERT INTO payments(voucher_id,payment_date,amount,payment_type,
                               reference_no,remarks,proof_path,created_by,created_at)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (voucher_id, payment_date, amount, payment_type, reference_no,
                         remarks, path, session["user_id"], now()))
            payment_id = cur.lastrowid
            new_balance = balance - amount
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(request.referrer or url_for("manager_dashboard"))

    audit("payment_record", "payment", payment_id,
          after={"voucher_id": voucher_id, "amount": amount, "type": payment_type,
                 "reference": reference_no, "balance_after": new_balance})
    v = query("""SELECT v.*, e.full_name, e.email, e.phone FROM vouchers v
                   JOIN employees e ON e.id=v.employee_id WHERE v.id=%s""", (voucher_id,), True)
    if v:
        notifications.notify_employee(
            v, f"Payment recorded for {v['voucher_no']}",
            f"Hi {v['full_name']}, a payment of Rs. {amount:,.2f} was recorded against voucher "
            f"{v['voucher_no']}. Remaining balance: Rs. {new_balance:,.2f}.")
    flash(f"Payment of Rs. {amount:,.2f} recorded successfully.", "success")
    return redirect(request.referrer or url_for("manager_dashboard"))


@app.route("/manager/audit")
@login_required("manager")
def audit_trail():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    total = (query("SELECT COUNT(*) c FROM audit_log", (), True) or {}).get("c", 0)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    rows = query("""SELECT * FROM audit_log ORDER BY id DESC LIMIT %s OFFSET %s""",
                 (PER_PAGE, (page - 1) * PER_PAGE))
    return render_template("audit.html", rows=rows, page=page, pages=pages, total=total)


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _all_vouchers_with_payments():
    vouchers = _voucher_rows()
    payments = query("""SELECT voucher_id, payment_date, amount, payment_type, reference_no, remarks
                          FROM payments ORDER BY payment_date, id""")
    by_voucher = {}
    for p in payments:
        by_voucher.setdefault(p["voucher_id"], []).append(p)
    return vouchers, by_voucher


@app.route("/manager/export")
@login_required("manager")
def export_excel():
    vouchers, payments_by_voucher = _all_vouchers_with_payments()

    cols = ["Month", "Employee", "Department", "Payment Status", "Claim Amount",
            "Total Paid", "Balance", "Voucher Count"]
    rows = [[str(v["date"])[:7], v["employee_name"], v["department"] or "", v["payment_status"],
             float(money_or_zero(v["amount"])), float(v["paid_amount"]), float(v["balance"]), 1]
            for v in vouchers]
    raw = pd.DataFrame(rows, columns=cols)
    if raw.empty:
        summary = pd.DataFrame(columns=cols)
    else:
        summary = raw.groupby(["Month", "Employee", "Department", "Payment Status"],
                              as_index=False).agg(**{
            "Claim Amount": ("Claim Amount", "sum"), "Total Paid": ("Total Paid", "sum"),
            "Balance": ("Balance", "sum"), "Voucher Count": ("Voucher Count", "sum")})

    detail_rows = []
    for v in vouchers:
        pays = payments_by_voucher.get(v["id"], [])
        details = " | ".join(
            f"{str(p['payment_date'])[:10]}: Rs. {money_or_zero(p['amount']):,.2f} "
            f"({p['payment_type']}{', ' + p['reference_no'] if p['reference_no'] else ''})"
            for p in pays) or "No payment"
        types = ", ".join(sorted({p["payment_type"] for p in pays})) or "-"
        detail_rows.append([
            v["voucher_no"], v["employee_name"], v["department"] or "", str(v["date"])[:10],
            v["purpose"], v["description"] or "", float(money_or_zero(v["amount"])),
            float(v["paid_amount"]), float(v["balance"]), v["payment_status"], v["status"],
            types, details, v["payable_to"] or "", v["expense_payment_mode"] or "",
            v["transaction_id"] or "", v["approved_by"] or "", v["reject_reason"] or ""])
    dcols = ["Voucher No", "Employee", "Department", "Date", "Purpose", "Description",
             "Claim Amount", "Total Paid", "Balance", "Payment Status", "Approval Status",
             "Payment Types", "Payment Details", "Payable To", "Expense Payment Mode",
             "Transaction ID", "Approved By", "Reject Reason"]
    detail = pd.DataFrame(detail_rows, columns=dcols)

    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        summary.to_excel(writer, index=False, sheet_name="Summary")
        detail.to_excel(writer, index=False, sheet_name="Voucher Data")
        from openpyxl.styles import Alignment, Font, PatternFill
        fills = {"Fully Paid": "C6EFCE", "Partially Paid": "FFEB9C", "Not Paid": "FFC7CE",
                 "Approved": "C6EFCE", "Pending": "FFEB9C", "Rejected": "FFC7CE"}
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            headers = [c.value for c in ws[1]]
            for c in ws[1]:
                c.font = Font(bold=True)
                c.alignment = Alignment(horizontal="center")
            for row in ws.iter_rows(min_row=2):
                statuses = [row[headers.index(k)].value for k in ("Payment Status",
                                                                  "Approval Status")
                            if k in headers]
                found = next((x for x in statuses if x in fills), None)
                if found:
                    fill = PatternFill("solid", fgColor=fills[found])
                    for c in row:
                        c.fill = fill
            for col in ws.columns:
                letter = col[0].column_letter
                ws.column_dimensions[letter].width = min(
                    max(len(str(c.value or "")) for c in col) + 2, 45)
            for key in ("Claim Amount", "Total Paid", "Balance"):
                if key in headers:
                    idx = headers.index(key) + 1
                    for row in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
                        row[0].number_format = '#,##0.00'
    out.seek(0)
    audit("export_excel", "report", None, after={"vouchers": len(vouchers)})
    return send_file(out, as_attachment=True,
                     download_name=f"MGT_Voucher_Report_{now():%Y%m%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet")


@app.route("/manager/export-zip")
@login_required("manager")
def export_zip():
    vouchers, _ = _all_vouchers_with_payments()
    # Built in memory and streamed. The old version wrote a temp file per
    # export and never deleted it.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for v in vouchers:
            folder = (f"{secure_filename(v['employee_name'])}/"
                      f"{str(v['date'])[:7]}/{v['voucher_no']}/")
            payments = query("SELECT * FROM payments WHERE voucher_id=%s ORDER BY id", (v["id"],))
            pdf = pdfgen.build_pdf(v, v["employee_name"], payments, app.static_folder,
                                   safe_upload_path(v.get("receipt")))
            z.writestr(folder + f"{v['voucher_no']}.pdf", pdf.getvalue())
            receipt = safe_upload_path(v.get("receipt"))
            if receipt:
                z.write(receipt, folder + os.path.basename(receipt))
            for p in payments:
                proof = safe_upload_path(p.get("proof_path"))
                if proof:
                    z.write(proof, folder + os.path.basename(proof))
    buf.seek(0)
    audit("export_zip", "report", None, after={"vouchers": len(vouchers)})
    return send_file(buf, as_attachment=True,
                     download_name=f"MGT_Vouchers_{now():%Y%m%d}.zip", mimetype="application/zip")


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

@app.route("/employee/profile")
@login_required("employee")
def employee_profile():
    return render_template("employee_profile.html", user=current_user(),
                           min_length=security.MIN_PASSWORD_LENGTH)


@app.route("/manager/profile")
@login_required("manager")
def manager_profile():
    return render_template("manager_profile.html", user=current_user(),
                           min_length=security.MIN_PASSWORD_LENGTH)


@app.route("/profile/photo", methods=["POST"])
@login_required()
def profile_photo():
    table = profile_table(session["role"])
    try:
        filename = save_profile_image(request.files.get("photo"), session["role"],
                                      session["user_id"])
        if not filename:
            flash("Please choose an image to upload.", "danger")
        else:
            execute(f"UPDATE {table} SET profile_image=%s WHERE id=%s",
                    (filename, session["user_id"]))
            audit("profile_photo", "user", session["user_id"])
            flash("Profile photo updated.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(profile_url())


@app.route("/profile/request-otp", methods=["POST"])
@login_required()
def profile_request_otp():
    user = current_user()
    purpose = request.form.get("purpose")
    if purpose not in ("profile_update", "password_reset"):
        flash("Invalid request.", "danger")
        return redirect(profile_url())

    bucket = f"otp:{session['role']}:{session['user_id']}"
    if security.rate_limited(bucket, security.OTP_REQUEST_MAX,
                             security.OTP_REQUEST_WINDOW_MINUTES):
        flash(f"Too many codes requested. Try again in "
              f"{security.OTP_REQUEST_WINDOW_MINUTES} minutes.", "danger")
        return redirect(profile_url())

    phone = (user.get("phone") or "").strip()
    if not phone:
        flash("No mobile number is on file for your account, so a code cannot be sent. "
              "Ask your manager to add one first.", "danger")
        return redirect(profile_url())

    try:
        if purpose == "profile_update":
            new_email = (request.form.get("new_email") or "").strip()
            new_phone = (request.form.get("new_phone") or "").strip()
            if not new_email and not new_phone:
                raise ValueError("Enter a new email or mobile number to change.")
            if new_email and not valid_email(new_email):
                raise ValueError("Please enter a valid email address.")
            if new_phone and not valid_phone(new_phone):
                raise ValueError("Please enter a valid mobile number.")
            payload = {"email": new_email or user.get("email"),
                       "phone": new_phone or user.get("phone")}
        else:
            new_password = request.form.get("new_password") or ""
            problem = security.password_problem(new_password, user["username"])
            if problem:
                raise ValueError(problem)
            payload = {"password_hash": generate_password_hash(new_password)}
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(profile_url())

    code = security.create_otp(session["role"], session["user_id"], purpose, payload)
    security.record_attempt(bucket)
    # The code always goes to the number already on file, never the new one
    # being requested, so knowing a new number is not enough to take over.
    sent = notifications.send_otp(phone, code)
    if not sent:
        # Previously this claimed success even when nothing was delivered.
        flash("Could not send the verification code - the SMS/WhatsApp service is not "
              "configured or is unavailable. Contact your administrator.", "danger")
        app.logger.error("OTP delivery failed for %s %s", session["role"], session["user_id"])
        return redirect(profile_url())

    audit("otp_request", "user", session["user_id"], after={"purpose": purpose})
    flash(f"A verification code has been sent to the mobile number on file "
          f"(ending {phone[-4:]}).", "info")
    return redirect(profile_url() + f"?verify={purpose}")


@app.route("/profile/verify-otp", methods=["POST"])
@login_required()
def profile_verify_otp():
    table = profile_table(session["role"])
    purpose = request.form.get("purpose")
    if purpose not in ("profile_update", "password_reset"):
        flash("Invalid request.", "danger")
        return redirect(profile_url())
    payload, error = security.verify_otp(session["role"], session["user_id"], purpose,
                                         request.form.get("otp", ""))
    if error:
        flash(error, "danger")
        return redirect(profile_url() + f"?verify={purpose}")

    user = current_user()
    if purpose == "profile_update":
        execute(f"UPDATE {table} SET email=%s, phone=%s WHERE id=%s",
                (payload.get("email"), payload.get("phone"), session["user_id"]))
        audit("profile_update", "user", session["user_id"],
              before={"email": user.get("email"), "phone": user.get("phone")},
              after={"email": payload.get("email"), "phone": payload.get("phone")})
        flash("Contact information updated successfully.", "success")
    else:
        execute(f"UPDATE {table} SET password_hash=%s, must_change_password=0 WHERE id=%s",
                (payload.get("password_hash"), session["user_id"]))
        audit("password_reset", "user", session["user_id"])
        flash("Password reset successfully.", "success")
    return redirect(profile_url())


# ---------------------------------------------------------------------------
# Manager - employee accounts
# ---------------------------------------------------------------------------

@app.route("/manager/employees")
@login_required("manager")
def employees():
    rows = query("""SELECT e.*, COALESCE(vc.c, 0) voucher_count
                      FROM employees e
                      LEFT JOIN (SELECT employee_id, COUNT(*) c FROM vouchers
                                  GROUP BY employee_id) vc ON vc.employee_id = e.id
                     ORDER BY e.full_name""")
    return render_template("employees.html", employees=rows,
                           min_length=security.MIN_PASSWORD_LENGTH)


@app.route("/manager/employees/add", methods=["POST"])
@login_required("manager")
def add_employee():
    try:
        username = field("username", 100, required=True, label="Username")
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,100}", username):
            raise ValueError("Username may use letters, numbers, dot, dash and underscore only.")
        full_name = field("full_name", 150, required=True, label="Full name")
        email = field("email", 150)
        phone = field("phone", 30)
        department = field("department", 100)
        password = request.form.get("password") or ""
        problem = security.password_problem(password, username)
        if problem:
            raise ValueError(problem)
        if email and not valid_email(email):
            raise ValueError("Please enter a valid email address (e.g. name@company.in).")
        if phone and not valid_phone(phone):
            raise ValueError("Please enter a valid mobile number.")
        new_id = execute("""INSERT INTO employees(username,password_hash,full_name,email,phone,
                                department,is_active,must_change_password,created_at)
                            VALUES(%s,%s,%s,%s,%s,%s,1,1,%s)""",
                         (username, generate_password_hash(password), full_name,
                          email or None, phone or None, department or None, now()))
        audit("employee_create", "employee", new_id,
              after={"username": username, "full_name": full_name})
        flash(f"Employee created. {full_name} must change this password at first login.",
              "success")
    except ValueError as e:
        flash(str(e), "danger")
    except mysql.connector.IntegrityError:
        flash("That username already exists.", "danger")
    return redirect(url_for("employees"))


@app.route("/manager/employees/<int:employee_id>/edit", methods=["POST"])
@login_required("manager")
def edit_employee(employee_id):
    before = query("SELECT * FROM employees WHERE id=%s", (employee_id,), True)
    if not before:
        abort(404)
    try:
        full_name = field("full_name", 150, required=True, label="Full name")
        email = field("email", 150)
        phone = field("phone", 30)
        department = field("department", 100)
        if email and not valid_email(email):
            raise ValueError("Please enter a valid email address (e.g. name@company.in).")
        if phone and not valid_phone(phone):
            raise ValueError("Please enter a valid mobile number.")
        execute("""UPDATE employees SET full_name=%s, email=%s, phone=%s, department=%s
                    WHERE id=%s""",
                (full_name, email or None, phone or None, department or None, employee_id))
        audit("employee_edit", "employee", employee_id,
              before={"full_name": before["full_name"], "email": before["email"],
                      "phone": before["phone"], "department": before["department"]},
              after={"full_name": full_name, "email": email, "phone": phone,
                     "department": department})
        flash("Employee details updated.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("employees"))


@app.route("/manager/employees/<int:employee_id>/reset-password", methods=["POST"])
@login_required("manager")
def reset_employee_password(employee_id):
    emp = query("SELECT * FROM employees WHERE id=%s", (employee_id,), True)
    if not emp:
        abort(404)
    password = request.form.get("password") or ""
    problem = security.password_problem(password, emp["username"])
    if problem:
        flash(problem, "danger")
    else:
        execute("""UPDATE employees SET password_hash=%s, must_change_password=1 WHERE id=%s""",
                (generate_password_hash(password), employee_id))
        audit("employee_password_reset", "employee", employee_id)
        flash(f"Password reset. {emp['full_name']} must change it at next login.", "success")
    return redirect(url_for("employees"))


@app.route("/manager/employees/<int:employee_id>/delete", methods=["POST"])
@login_required("manager")
def delete_employee(employee_id):
    emp = query("SELECT * FROM employees WHERE id=%s", (employee_id,), True)
    if not emp:
        abort(404)
    try:
        execute("DELETE FROM employees WHERE id=%s", (employee_id,))
        audit("employee_delete", "employee", employee_id,
              before={"username": emp["username"], "full_name": emp["full_name"]})
        flash("Employee deleted.", "success")
    except mysql.connector.IntegrityError:
        flash("This employee has voucher history and can't be deleted - "
              "disable the account instead.", "danger")
    return redirect(url_for("employees"))


@app.route("/manager/employees/<int:employee_id>/toggle", methods=["POST"])
@login_required("manager")
def toggle_employee(employee_id):
    emp = query("SELECT * FROM employees WHERE id=%s", (employee_id,), True)
    if not emp:
        abort(404)
    execute("UPDATE employees SET is_active = NOT is_active WHERE id=%s", (employee_id,))
    audit("employee_toggle", "employee", employee_id,
          before={"is_active": emp["is_active"]}, after={"is_active": not emp["is_active"]})
    flash("Employee status updated.", "success")
    return redirect(url_for("employees"))


@app.route("/healthz")
def healthz():
    try:
        query("SELECT 1 AS ok", (), True)
        return {"status": "ok"}, 200
    except Exception as exc:
        app.logger.error("Health check failed: %s", exc)
        return {"status": "degraded"}, 503


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    if debug and os.getenv("ALLOW_DEBUG", "0") != "1":
        raise SystemExit(
            "FLASK_DEBUG=1 exposes an interactive console to anyone who can reach a "
            "traceback. If you really want it on this machine, also set ALLOW_DEBUG=1."
        )
    app.run(host=os.getenv("BIND_HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "5000")), debug=debug)
