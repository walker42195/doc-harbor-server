import re
import sqlite3
from flask import Blueprint, render_template, request, redirect, url_for
from auth import requires_auth, hash_password, is_safe_username, is_weak_password, revoke_logins
from db import get_db

bp = Blueprint("user", __name__)


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


@bp.route("/users")
@requires_auth
def users_view():
    if request.user_role != "admin":
        from flask import abort
        abort(403)

    conn = get_db()
    users = [dict(r) for r in conn.execute(
        "SELECT username, role, email, mfa_email, pw_weak FROM users ORDER BY username ASC"
    ).fetchall()]
    conn.close()

    return render_template(
        "users.html",
        users=users,
        success=request.args.get("success", ""),
        error=request.args.get("error", ""),
    )


@bp.route("/users/create", methods=["POST"])
@requires_auth
def create_user():
    if request.user_role != "admin":
        from flask import abort
        abort(403)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    role = request.form.get("role", "read-only").strip()
    email = request.form.get("email", "").strip().lower()
    if not email and "@" in username:
        email = username.lower()
    # Kodkrav är standard när kontot har en e-postadress. Formuläret skickar
    # mfa_email=0/1; äldre klienter (appen) skickar inget och får standardvärdet.
    if "mfa_form" in request.form:  # webbformuläret: okryssad ruta skickas inte alls
        mfa = request.form.get("mfa_email") in ("1", "on", "true")
    else:
        mfa = request.form.get("mfa_email", "1" if email else "0") in ("1", "on", "true")

    if not username or not is_safe_username(username) or not password or role not in ("admin", "read-write", "read-only"):
        return redirect(url_for("user.users_view", error="Ogiltigt användarnamn eller ogiltiga värden"))
    if email and not _valid_email(email):
        return redirect(url_for("user.users_view", error="Ogiltig e-postadress"))
    if mfa and not email:
        return redirect(url_for("user.users_view", error="Kodverifiering kräver en e-postadress"))

    p_hash, salt = hash_password(password)
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role, email, mfa_email, pw_weak) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, p_hash, salt, role, email or None, 1 if mfa else 0, 1 if is_weak_password(password) else 0),
        )
        conn.commit()
        conn.close()
        from helpers import ensure_user_default_structure
        ensure_user_default_structure(username)
        return redirect(url_for("user.users_view", success=f"Användare '{username}' skapad"))
    except sqlite3.IntegrityError:
        return redirect(url_for("user.users_view", error="Användarnamnet är redan upptaget"))
    except Exception as e:
        return redirect(url_for("user.users_view", error=str(e)))


@bp.route("/users/delete", methods=["POST"])
@requires_auth
def delete_user():
    if request.user_role != "admin":
        from flask import abort
        abort(403)

    username = request.form.get("username", "").strip()
    if username == request.username:
        return redirect(url_for("user.users_view", error="Du kan inte ta bort dig själv"))

    conn = get_db()
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.execute("DELETE FROM tokens WHERE username = ?", (username,))
    conn.execute("DELETE FROM group_members WHERE username = ?", (username,))
    conn.execute("DELETE FROM login_codes WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return redirect(url_for("user.users_view", success=f"Användare '{username}' borttagen"))


@bp.route("/users/change-password", methods=["POST"])
@requires_auth
def change_password():
    target_user = request.form.get("username", "").strip()
    if target_user and request.user_role == "admin":
        username = target_user
    else:
        username = request.username

    password = request.form.get("password", "").strip()

    if not password:
        if request.user_role == "admin":
            return redirect(url_for("user.users_view", error="Lösenordet kan inte vara tomt"))
        else:
            from flask import abort
            abort(400)

    if len(password) < 8:
        if request.user_role == "admin":
            return redirect(url_for("user.users_view", error="Lösenordet måste vara minst 8 tecken"))
        from flask import abort
        abort(400)

    p_hash, salt = hash_password(password)
    conn = get_db()
    cur = conn.execute(
        "UPDATE users SET password_hash = ?, salt = ?, pw_weak = ? WHERE username = ?",
        (p_hash, salt, 1 if is_weak_password(password) else 0, username),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return redirect(url_for("user.users_view", error="Användaren finns inte"))

    if request.user_role == "admin":
        if username != request.username:
            # Återställning av någon annans lösenord: kontot loggas ut överallt
            # (appar och webben) och måste logga in med det nya lösenordet.
            revoke_logins(username)
            return redirect(url_for(
                "user.users_view",
                success=f"Lösenordet för '{username}' har återställts. Kontot är utloggat på alla enheter.",
            ))
        return redirect(url_for("user.users_view", success=f"Lösenordet för '{username}' har uppdaterats"))
    return redirect(url_for("file.index"))


@bp.route("/users/update-role", methods=["POST"])
@requires_auth
def update_user_role():
    if request.user_role != "admin":
        from flask import abort
        abort(403)

    username = request.form.get("username", "").strip()
    role = request.form.get("role", "").strip()

    if username == request.username:
        return redirect(url_for("user.users_view", error="Du kan inte ändra din egen roll"))

    if role not in ("admin", "read-write", "read-only"):
        return redirect(url_for("user.users_view", error="Ogiltig roll"))

    conn = get_db()
    conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
    conn.commit()
    conn.close()
    return redirect(url_for("user.users_view", success=f"Rollen för '{username}' har ändrats till '{role}'"))


@bp.route("/users/update-security", methods=["POST"])
@requires_auth
def update_user_security():
    """Admin: e-postadress och kodkrav (kod via e-post vid inloggning) per konto."""
    if request.user_role != "admin":
        from flask import abort
        abort(403)

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    mfa = request.form.get("mfa_email", "0") in ("1", "on", "true")

    if email and not _valid_email(email):
        return redirect(url_for("user.users_view", error="Ogiltig e-postadress"))
    if mfa and not email:
        return redirect(url_for("user.users_view", error=f"'{username}' behöver en e-postadress för kodverifiering"))

    conn = get_db()
    row = conn.execute("SELECT mfa_email FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        conn.close()
        return redirect(url_for("user.users_view", error="Användaren finns inte"))
    conn.execute(
        "UPDATE users SET email = ?, mfa_email = ? WHERE username = ?",
        (email or None, 1 if mfa else 0, username),
    )
    conn.commit()
    conn.close()

    # Slår admin på kodkrav för sitt eget konto gäller den nuvarande sessionen fortfarande:
    # admin har redan loggat in och ska inte kastas ut mitt i inställningen.
    if mfa and username == request.username:
        from flask import session
        session["mfa"] = True

    state = "på" if mfa else "av"
    return redirect(url_for("user.users_view", success=f"'{username}': kodverifiering via e-post är {state}"))
