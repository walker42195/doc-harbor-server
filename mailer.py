"""Utskick av e-post (verifieringskoder, inloggningskoder, gruppinbjudningar).

Inställningar via miljövariabler:
  SMTP_HOST   (standard: localhost)
  SMTP_PORT   (standard: 25)
  FROM_EMAIL  (standard: noreply@localhost)
"""
import os
import smtplib
from email.message import EmailMessage


def send_mail(to_email: str, subject: str, body: str) -> None:
    """Skickar ett textmejl. Kastar ett undantag om servern inte tar emot det."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("FROM_EMAIL", "noreply@localhost")
    msg["To"] = to_email
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "localhost")
    port = int(os.environ.get("SMTP_PORT", "25"))
    with smtplib.SMTP(host, port, timeout=10) as s:
        s.send_message(msg)


def mask_email(email: str) -> str:
    """f***@sandgren.one – visas vid inloggning utan att hela adressen avslöjas."""
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"
