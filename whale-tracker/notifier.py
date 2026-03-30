"""
Email notifier using Python's built-in smtplib (no extra packages required).
Supports Gmail SMTP (smtp.gmail.com:587 with STARTTLS).

Usage:
    from notifier import send_alert_email
    send_alert_email("[ALERT] Whale bought X", "Body text here")

If EMAIL_SENDER is not configured the function is a no-op (only terminal output).
"""

import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send_alert_email(subject: str, body: str) -> None:
    """
    Send an alert email via Gmail SMTP.

    Priority mapping (caller sets the subject prefix):
        [URGENT]  — 🚨 multi-wallet convergence
        [ALERT]   — 🔴 new token buy
        [WARNING] — ⚠️  large sell

    If EMAIL_SENDER is empty, this function silently returns without sending.
    Any SMTP error is logged to stdout but does NOT raise — monitoring continues.
    """
    if not EMAIL_SENDER:
        return

    if not EMAIL_PASSWORD or not EMAIL_RECEIVER:
        print("[notifier] EMAIL_PASSWORD or EMAIL_RECEIVER not set — skipping email.")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER

        msg.attach(MIMEText(body, "plain", "utf-8"))

        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())

        print(f"[notifier] Email sent: {subject}")

    except Exception as e:
        print(f"[notifier] Failed to send email: {e}")
