"""Notification helpers - SMS, WhatsApp and Email.

Real integrations (Twilio for SMS/WhatsApp, SMTP for email) that stay quiet
when unconfigured: every function returns True/False rather than raising, so
a missing provider never breaks an approval.

Configuration is read at call time, not at import time. The previous version
read os.getenv() while the module was being imported, which happened before
app.py called load_dotenv() - so credentials in .env were silently ignored
and notifications never went out even when fully configured.

Add to your .env (see .env.example):

    TWILIO_ACCOUNT_SID=
    TWILIO_AUTH_TOKEN=
    TWILIO_SMS_FROM=            # e.g. +14155550100
    TWILIO_WHATSAPP_FROM=       # e.g. +14155238886

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=you@yourcompany.in
    SMTP_PASSWORD=app-password-here
    SMTP_FROM=you@yourcompany.in

SMS/WhatsApp also needs:  pip install twilio
"""

import logging
import os
import smtplib
from email.message import EmailMessage

_log = logging.getLogger("notifications")


def set_logger(logger):
    """Point this module at the Flask app logger."""
    global _log
    _log = logger


def _env(name, default=""):
    return (os.getenv(name) or default).strip()


def _twilio_client():
    sid, token = _env("TWILIO_ACCOUNT_SID"), _env("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        return None
    try:
        from twilio.rest import Client
    except ImportError:
        _log.warning("twilio package not installed - run: pip install twilio")
        return None
    try:
        return Client(sid, token)
    except Exception as e:
        _log.error("Twilio client could not be created: %s", e)
        return None


def _normalise(phone):
    """Twilio needs E.164. A bare 10-digit Indian number gets +91."""
    p = "".join(ch for ch in str(phone or "") if ch.isdigit() or ch == "+")
    if not p:
        return ""
    if p.startswith("+"):
        return p
    if len(p) == 10:
        return f"{_env('DEFAULT_COUNTRY_CODE', '+91')}{p}"
    if len(p) > 10 and not p.startswith("+"):
        return f"+{p}"
    return p


def send_sms(to_phone, message):
    to_phone = _normalise(to_phone)
    if not to_phone:
        return False
    client, sender = _twilio_client(), _env("TWILIO_SMS_FROM")
    if not client or not sender:
        _log.info("SMS not sent (Twilio not configured) to=%s", to_phone)
        return False
    try:
        client.messages.create(body=message, from_=sender, to=to_phone)
        _log.info("SMS sent to %s", to_phone)
        return True
    except Exception as e:
        _log.error("SMS failed to %s: %s", to_phone, e)
        return False


def send_whatsapp(to_phone, message):
    to_phone = _normalise(to_phone)
    if not to_phone:
        return False
    client, sender = _twilio_client(), _env("TWILIO_WHATSAPP_FROM")
    if not client or not sender:
        _log.info("WhatsApp not sent (Twilio not configured) to=%s", to_phone)
        return False
    if not sender.startswith("whatsapp:"):
        sender = f"whatsapp:{sender}"
    try:
        client.messages.create(body=message, from_=sender, to=f"whatsapp:{to_phone}")
        _log.info("WhatsApp sent to %s", to_phone)
        return True
    except Exception as e:
        _log.error("WhatsApp failed to %s: %s", to_phone, e)
        return False


def send_email(to_email, subject, message):
    if not to_email:
        return False
    host, user, password = _env("SMTP_HOST"), _env("SMTP_USER"), _env("SMTP_PASSWORD")
    if not (host and user and password):
        _log.info("Email not sent (SMTP not configured) to=%s subject=%s", to_email, subject)
        return False
    port = int(_env("SMTP_PORT", "587") or 587)
    sender = _env("SMTP_FROM") or user
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        msg.set_content(message)
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(msg)
        _log.info("Email sent to %s", to_email)
        return True
    except Exception as e:
        _log.error("Email failed to %s: %s", to_email, e)
        return False


def notify_employee(employee, subject, message, whatsapp=True, sms=True, email=True):
    """Send a voucher or payment update across the configured channels.

    Never raises - a provider outage must not roll back an approval.
    """
    results = {}
    try:
        if email:
            results["email"] = send_email(employee.get("email"), subject, message)
        if whatsapp:
            results["whatsapp"] = send_whatsapp(employee.get("phone"), message)
        if sms and not results.get("whatsapp"):
            results["sms"] = send_sms(employee.get("phone"), message)
    except Exception as e:  # pragma: no cover - defensive
        _log.error("notify_employee failed: %s", e)
    return results


def send_otp(phone, otp_code):
    """Deliver a verification code. Returns True only if something was sent.

    The caller must check this. The previous version always reported success
    to the user even when no provider was configured and nothing went out.
    """
    message = (f"Your MGT Voucher Portal verification code is {otp_code}. "
               "It expires in 10 minutes. Do not share this code.")
    if send_sms(phone, message):
        return True
    return send_whatsapp(phone, message)
