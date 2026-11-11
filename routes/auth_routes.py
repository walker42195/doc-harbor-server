import hashlib
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, Response
from auth import (
    requires_auth, auth_epoch, check_auth, create_token, verify_token, is_setup_needed, login_blocked,
    requires_mfa, start_login_challenge, resend_login_code, verify_login_code, LoginCodeError,
)
from db import get_db

bp = Blueprint("auth", __name__)


def _safe_next(next_url: str) -> str:
    # Bara interna sökvägar – annars kan inloggningen skicka vidare till en annan sajt.
    if not next_url or not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url:
        return url_for("file.index")
    return next_url


def _finish_web_login(username: str, role: str, mfa: bool, next_url: str):
    session.clear()
    session.permanent = True
    session["username"] = username
    session["role"] = role
    session["mfa"] = mfa
    session["epoch"] = auth_epoch(username)
    return redirect(_safe_next(next_url))


@bp.route("/login", methods=["GET", "POST"])
def login_view():
    if not is_setup_needed() and session.get("username"):
        return redirect(url_for("file.index"))
    if is_setup_needed():
        return redirect(url_for("setup.setup_view"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next", "")
        if login_blocked(username):
            error = "För många misslyckade inloggningar. Vänta en stund och försök igen."
            return render_template("login.html", error=error, next=next_url), 429
        authenticated, role = check_auth(username, password)
        if authenticated:
            if not requires_mfa(username):
                return _finish_web_login(username, role, False, next_url)
            try:
                challenge, email_hint = start_login_challenge(username)
            except LoginCodeError as e:
                return render_template("login.html", error=str(e), next=next_url), 503
            session.clear()
            session["mfa_pending"] = {"challenge": challenge, "next": next_url, "email": email_hint}
            return redirect(url_for("auth.login_verify_view"))
        error = "Felaktigt användarnamn eller lösenord"
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@bp.route("/login/verify", methods=["GET", "POST"])
def login_verify_view():
    pending = session.get("mfa_pending")
    if not pending:
        return redirect(url_for("auth.login_view"))
    error = None
    info = request.args.get("info", "")
    if request.method == "POST":
        try:
            username, role = verify_login_code(pending["challenge"], request.form.get("code", ""))
        except LoginCodeError as e:
            error = str(e)
            # Utgången eller förbrukad inloggning: börja om från lösenordet.
            if e.restart:
                session.pop("mfa_pending", None)
                return render_template("login.html", error=error, next=pending.get("next", "")), 401
        else:
            return _finish_web_login(username, role, True, pending.get("next", ""))
    return render_template("login_verify.html", error=error, info=info, email=pending.get("email", ""))


@bp.route("/login/resend", methods=["POST"])
def login_resend_view():
    pending = session.get("mfa_pending")
    if not pending:
        return redirect(url_for("auth.login_view"))
    try:
        email_hint = resend_login_code(pending["challenge"])
    except LoginCodeError as e:
        return render_template("login_verify.html", error=str(e), info="", email=pending.get("email", ""))
    return redirect(url_for("auth.login_verify_view", info=f"En ny kod har skickats till {email_hint}."))


@bp.route("/logout")
def logout_view():
    session.clear()
    return redirect(url_for("auth.login_view"))


def _request_data() -> dict:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


@bp.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    if is_setup_needed():
        return jsonify({"error": "Ingen admin-användare konfigurerad"}), 503
    data = _request_data()
    username = data.get("username", "")
    password = data.get("password", "")
    if login_blocked(username):
        return jsonify({"error": "För många misslyckade inloggningar. Försök igen senare."}), 429
    authenticated, role = check_auth(username, password)
    if not authenticated:
        return jsonify({"error": "Felaktigt användarnamn eller lösenord"}), 401
    if not requires_mfa(username):
        token = create_token(username)
        return jsonify({"token": token, "username": username, "role": role})
    # Klienter som inte klarar kodsteget (appar före 2.3.13) får ett begripligt fel.
    if str(data.get("mfa", "")).lower() not in ("1", "true"):
        return jsonify({"error": "Kontot kräver kodverifiering via e-post – uppdatera appen."}), 403
    try:
        challenge, email_hint = start_login_challenge(username)
    except LoginCodeError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"mfa_required": True, "challenge": challenge, "email": email_hint})


@bp.route("/api/auth/verify", methods=["POST"])
def api_auth_verify():
    data = _request_data()
    try:
        username, role = verify_login_code(data.get("challenge", ""), str(data.get("code", "")))
    except LoginCodeError as e:
        return jsonify({"error": str(e), "restart": e.restart}), 401
    token = create_token(username, mfa=True)
    return jsonify({"token": token, "username": username, "role": role})


@bp.route("/api/auth/resend", methods=["POST"])
def api_auth_resend():
    data = _request_data()
    try:
        email_hint = resend_login_code(data.get("challenge", ""))
    except LoginCodeError as e:
        return jsonify({"error": str(e), "restart": e.restart}), 429
    return jsonify({"success": True, "email": email_hint})


@bp.route("/api/auth/me")
@requires_auth
def api_auth_me():
    """Vem är inloggad? Appen kontrollerar sitt sparade token med detta vid start."""
    return jsonify({"username": request.username, "role": request.user_role})


@bp.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        conn = get_db()
        conn.execute("DELETE FROM tokens WHERE token_hash = ?", (token_hash,))
        conn.commit()
        conn.close()
    return jsonify({"success": True})
