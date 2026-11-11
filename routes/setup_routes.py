from flask import Blueprint, render_template, request, redirect, url_for, session
from auth import hash_password, is_setup_needed, is_weak_password
from db import get_db

bp = Blueprint("setup", __name__)


@bp.route("/setup", methods=["GET", "POST"])
def setup_view():
    if not is_setup_needed():
        return redirect(url_for("file.index"))

    if request.method == "POST":
        password = request.form.get("password", "").strip()
        if not password:
            return render_template("setup.html", error="Lösenordet kan inte vara tomt")

        p_hash, salt = hash_password(password)
        conn = get_db()
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role, pw_weak) VALUES (?, ?, ?, ?, ?)",
            ("admin", p_hash, salt, "admin", 1 if is_weak_password(password) else 0),
        )
        conn.commit()
        conn.close()
        session.permanent = True
        session["username"] = "admin"
        session["role"] = "admin"
        session["mfa"] = False
        return redirect(url_for("file.index"))

    return render_template("setup.html")
