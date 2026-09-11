"""
Notification helpers — SMS, WhatsApp, and Email.

These are REAL integrations (Twilio for SMS/WhatsApp, SMTP for Email) but
they only fire if you provide credentials in your .env file. Without
credentials, each function safely logs to the console instead of crashing,
so the app keeps working while you're setting providers up.

Add to your .env (see .env.example):

    # --- SMS / WhatsApp (Twilio) ---
    TWILIO_ACCOUNT_SID=
    TWILIO_AUTH_TOKEN=
    TWILIO_SMS_FROM=            # e.g. +14155550100  (a Twilio phone number)
    TWILIO_WHATSAPP_FROM=       # e.g. whatsapp:+14155238886 (Twilio sandbox or approved sender)

    # --- Email (SMTP) ---
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=you@yourcompany.in
    SMTP_PASSWORD=app-password-here
    SMTP_FROM=you@yourcompany.in

Then: pip install twilio   (only needed for SMS/WhatsApp; email uses the
standard library and needs no extra install.)
"""

import os
import smtplib
from email.mime.text import MIMEText

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_SMS_FROM = os.getenv("TWILIO_SMS_FROM")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM")

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "")


def _twilio_client():
    if not (TWILIO_SID and TWILIO_TOKEN):
        return None
    try:
        from twilio.rest import Client
        return Client(TWILIO_SID, TWILIO_TOKEN)
    except ImportError:
        print("[notifications] 'twilio' package not installed — run: pip install twilio")
        return None


def send_sms(to_phone, message):
    if not to_phone:
        return False
    client = _twilio_client()
    if not client or not TWILIO_SMS_FROM:
        print(f"[SMS not sent — Twilio not configured] to={to_phone}: {message}")
        return False
    try:
        client.messages.create(body=message, from_=TWILIO_SMS_FROM, to=to_phone)
        return True
    except Exception as e:
        print(f"[SMS error] {e}")
        return False


def send_whatsapp(to_phone, message):
    if not to_phone:
        return False
    client = _twilio_client()
    if not client or not TWILIO_WHATSAPP_FROM:
        print(f"[WhatsApp not sent — Twilio not configured] to={to_phone}: {message}")
        return False
    try:
        client.messages.create(body=message, from_=TWILIO_WHATSAPP_FROM, to=f"whatsapp:{to_phone}")
        return True
    except Exception as e:
        print(f"[WhatsApp error] {e}")
        return False


def send_email(to_email, subject, message):
    if not to_email:
        return False
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        print(f"[Email not sent — SMTP not configured] to={to_email}: {subject}")
        return False
    try:
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[Email error] {e}")
        return False


def notify_employee(employee, subject, message, whatsapp=True, sms=True, email=True):
    """Send a voucher/payment update to an employee across configured channels.
    `employee` is a dict-like row with 'email' and 'phone' keys.
    Never raises — failures are logged, not propagated, so a missing
    provider never breaks the approve/reject/payment flow."""
    results = {}
    try:
        if email:
            results["email"] = send_email(employee.get("email"), subject, message)
        if whatsapp:
            results["whatsapp"] = send_whatsapp(employee.get("phone"), message)
        if sms and not results.get("whatsapp"):
            results["sms"] = send_sms(employee.get("phone"), message)
    except Exception as e:
        print(f"[notify_employee error] {e}")
    return results


def send_otp(phone, otp_code):
    """OTP is sent by SMS (falls back to WhatsApp if SMS isn't configured)."""
    message = f"Your MGT Voucher Portal verification code is {otp_code}. It expires in 10 minutes. Do not share this code."
    if not send_sms(phone, message):
        send_whatsapp(phone, message)
