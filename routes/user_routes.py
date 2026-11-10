import sqlite3
from flask import Blueprint, render_template, request, redirect, url_for
from auth import requires_auth, hash_password, is_safe_username
from db import get_db

bp = Blueprint("user", __name__)


@bp.route("/users")
@requires_auth
def users_view():
    if request.user_role != "admin":
        from flask import abort
        abort(403)

    conn = get_db()
    users = [dict(r) for r in conn.execute(
        "SELECT username, role FROM users ORDER BY username ASC"
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

    if not username or not is_safe_username(username) or not password or role not in ("admin", "read-write", "read-only"):
        return redirect(url_for("user.users_view", error="Ogiltigt användarnamn eller ogiltiga värden"))

    p_hash, salt = hash_password(password)
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
            (username, p_hash, salt, role),
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

    p_hash, salt = hash_password(password)
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
        (p_hash, salt, username),
    )
    conn.commit()
    conn.close()

    if request.user_role == "admin":
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
