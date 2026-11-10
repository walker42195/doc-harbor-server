import secrets
import smtplib
from email.message import EmailMessage
from datetime import datetime
from flask import Blueprint, abort, render_template, request, redirect, url_for, session
from auth import hash_password, is_safe_username
from db import get_db
from config import ROOT, registration_enabled

bp = Blueprint("register", __name__)


@bp.before_request
def _registration_gate():
    # Självregistrering är avstängd om inte REGISTRATION_ENABLED=true.
    if not registration_enabled():
        abort(404)

import os

SMTP_HOST = os.environ.get("SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
FROM_EMAIL = os.environ.get("FROM_EMAIL", "noreply@localhost")
CODE_TTL = 900  # 15 minutes
MAX_CODE_ATTEMPTS = 5
_wrong_codes: dict[str, int] = {}


def _send_verification_email(to_email: str, code: str):
    msg = EmailMessage()
    msg["Subject"] = "Verifiera din e-post — doc-harbor"
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(
        f"Din verifieringskod för doc-harbor är: {code}\n\n"
        f"Koden är giltig i 15 minuter.\n\n"
        f"Om du inte begärde ett konto, ignorera detta meddelande."
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
        s.send_message(msg)


@bp.route("/register", methods=["GET", "POST"])
def register_view():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email or not is_safe_username(email) or "@" not in email or "." not in email.split("@")[-1]:
            error = "Ange en giltig e-postadress"
        else:
            conn = get_db()
            existing = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (email,)
            ).fetchone()
            conn.close()
            if existing:
                error = "E-postadressen är redan registrerad"
            else:
                code = str(secrets.randbelow(10 ** 6)).zfill(6)
                _wrong_codes.pop(email, None)
                now = datetime.now().timestamp()
                conn = get_db()
                conn.execute("DELETE FROM email_verifications WHERE email = ?", (email,))
                conn.execute(
                    "INSERT INTO email_verifications (email, code, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (email, code, now, now + CODE_TTL),
                )
                conn.commit()
                conn.close()
                try:
                    _send_verification_email(email, code)
                except Exception:
                    error = "Kunde inte skicka verifieringsmail. Kontakta administratören."
                else:
                    return redirect(url_for("register.verify_view", email=email))

    return render_template("register.html", error=error)


@bp.route("/verify", methods=["GET", "POST"])
def verify_view():
    email = request.args.get("email", "").strip().lower()
    error = None

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        code = request.form.get("code", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if password != confirm:
            error = "Lösenorden matchar inte"
        elif len(password) < 8:
            error = "Lösenordet måste vara minst 8 tecken"
        else:
            now = datetime.now().timestamp()
            conn = get_db()
            row = conn.execute(
                "SELECT code, expires_at FROM email_verifications WHERE email = ? ORDER BY created_at DESC LIMIT 1",
                (email,),
            ).fetchone()

            if not row:
                error = "Ingen verifieringskod hittades. Börja om registreringen."
            elif now > row["expires_at"]:
                conn.execute("DELETE FROM email_verifications WHERE email = ?", (email,))
                conn.commit()
                conn.close()
                error = "Koden har gått ut. Börja om registreringen."
            elif not secrets.compare_digest(row["code"], code):
                # Max fem gissningar per kod – sedan måste registreringen börja om.
                _wrong_codes[email] = _wrong_codes.get(email, 0) + 1
                if _wrong_codes[email] >= MAX_CODE_ATTEMPTS:
                    _wrong_codes.pop(email, None)
                    conn.execute("DELETE FROM email_verifications WHERE email = ?", (email,))
                    conn.commit()
                    error = "För många felaktiga koder. Börja om registreringen."
                else:
                    error = "Felaktig verifieringskod"
                conn.close()
            else:
                p_hash, salt = hash_password(password)
                try:
                    # Sanity check to prevent traversal
                    user_dir = (ROOT / "Users" / email).resolve()
                    user_dir.relative_to(ROOT / "Users")
                    
                    conn.execute(
                        "INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
                        (email, p_hash, salt, "read-write"),
                    )
                    conn.execute("DELETE FROM email_verifications WHERE email = ?", (email,))
                    conn.commit()
                    user_dir.mkdir(parents=True, exist_ok=True)
                    conn.close()
                    from helpers import ensure_user_default_structure
                    ensure_user_default_structure(email)
                    session.permanent = True
                    session["username"] = email
                    session["role"] = "read-write"
                    return redirect(url_for("file.browse_view", rel=f"Users/{email}"))
                except Exception:
                    conn.close()
                    error = "Kontot kunde inte skapas. E-postadressen är möjligen redan registrerad."

    return render_template("verify.html", error=error, email=email)
