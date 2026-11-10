import hashlib
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, Response
from auth import check_auth, create_token, verify_token, is_setup_needed, login_blocked
from db import get_db

bp = Blueprint("auth", __name__)


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
        if login_blocked(username):
            error = "För många misslyckade inloggningar. Vänta en stund och försök igen."
            return render_template("login.html", error=error, next=request.form.get("next", "")), 429
        authenticated, role = check_auth(username, password)
        if authenticated:
            session.permanent = True
            session["username"] = username
            session["role"] = role
            next_url = request.form.get("next") or ""
            # Bara interna sökvägar – annars kan inloggningen skicka vidare till en annan sajt.
            if not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url:
                next_url = url_for("file.index")
            return redirect(next_url)
        error = "Felaktigt användarnamn eller lösenord"
    return render_template("login.html", error=error, next=request.args.get("next", ""))


@bp.route("/logout")
def logout_view():
    session.clear()
    return redirect(url_for("auth.login_view"))


@bp.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    if is_setup_needed():
        return jsonify({"error": "Ingen admin-användare konfigurerad"}), 503
    if request.is_json:
        data = request.get_json() or {}
        username = data.get("username", "")
        password = data.get("password", "")
    else:
        username = request.form.get("username", "")
        password = request.form.get("password", "")
    if login_blocked(username):
        return jsonify({"error": "För många misslyckade inloggningar. Försök igen senare."}), 429
    authenticated, role = check_auth(username, password)
    if not authenticated:
        return jsonify({"error": "Felaktigt användarnamn eller lösenord"}), 401
    token = create_token(username)
    return jsonify({"token": token, "username": username, "role": role})


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
