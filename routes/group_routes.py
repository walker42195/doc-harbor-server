import uuid
import shutil
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, abort
from auth import requires_auth
from db import get_db
from config import ROOT

bp = Blueprint("group", __name__)


def _is_owner_or_admin(group_id: str) -> bool:
    if request.user_role == "admin":
        return True
    conn = get_db()
    row = conn.execute(
        "SELECT role FROM group_members WHERE group_id = ? AND username = ?",
        (group_id, request.username),
    ).fetchone()
    conn.close()
    return row and row["role"] == "owner"


@bp.route("/groups")
@requires_auth
def groups_view():
    conn = get_db()
    if request.user_role == "admin":
        rows = conn.execute("""
            SELECT g.id, g.name, g.description, g.created_by, g.created_at,
                   COUNT(gm.username) AS member_count,
                   NULL AS my_role
            FROM groups g
            LEFT JOIN group_members gm ON g.id = gm.group_id
            GROUP BY g.id
            ORDER BY g.name
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT g.id, g.name, g.description, g.created_by, g.created_at,
                   COUNT(gm2.username) AS member_count,
                   my.role AS my_role
            FROM groups g
            JOIN group_members my ON g.id = my.group_id AND my.username = ?
            LEFT JOIN group_members gm2 ON g.id = gm2.group_id
            GROUP BY g.id
            ORDER BY g.name
        """, (request.username,)).fetchall()

    # Fetch members for each group (for display in management view)
    groups = []
    for row in rows:
        g = dict(row)
        members = conn.execute(
            "SELECT username, role FROM group_members WHERE group_id = ? ORDER BY role DESC, username ASC",
            (g["id"],),
        ).fetchall()
        g["members"] = [dict(m) for m in members]
        groups.append(g)

    # All users (for invite form)
    all_users = [dict(r) for r in conn.execute(
        "SELECT username FROM users ORDER BY username"
    ).fetchall()]
    conn.close()

    return render_template(
        "groups.html",
        groups=groups,
        all_users=all_users,
        success=request.args.get("success", ""),
        error=request.args.get("error", ""),
    )


@bp.route("/groups/create", methods=["POST"])
@requires_auth
def create_group():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()

    if not name:
        return redirect(url_for("group.groups_view", error="Gruppnamn krävs"))

    group_id = str(uuid.uuid4())
    now = datetime.now().timestamp()

    conn = get_db()
    conn.execute(
        "INSERT INTO groups (id, name, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (group_id, name, description, request.username, now),
    )
    conn.execute(
        "INSERT INTO group_members (group_id, username, role, joined_at) VALUES (?, ?, ?, ?)",
        (group_id, request.username, "owner", now),
    )
    conn.commit()
    conn.close()

    (ROOT / "Groups" / group_id).mkdir(parents=True, exist_ok=True)
    return redirect(url_for("group.groups_view", success=f"Gruppen '{name}' skapad"))


def _send_invite_email(to_email: str, group_name: str, inviter_name: str):
    import os
    import smtplib
    from email.message import EmailMessage

    smtp_host = os.environ.get("SMTP_HOST", "localhost")
    smtp_port = int(os.environ.get("SMTP_PORT", "25"))
    from_email = os.environ.get("FROM_EMAIL", "noreply@localhost")

    msg = EmailMessage()
    msg["Subject"] = f"Du har lagts till i gruppen '{group_name}' — doc-harbor"
    msg["From"] = from_email
    msg["To"] = to_email
    msg.set_content(
        f"Hej!\n\n"
        f"Användaren {inviter_name} har lagt till dig i gruppen '{group_name}' på doc-harbor.\n\n"
        f"Du kan nu komma åt gruppens delade filer under avdelningen 'Grupper' i appen eller webbgränssnittet."
    )
    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
        s.send_message(msg)


@bp.route("/groups/<group_id>/invite", methods=["POST"])
@requires_auth
def invite_member(group_id):
    if not _is_owner_or_admin(group_id):
        abort(403)

    member_email = request.form.get("username", "").strip().lower()
    conn = get_db()

    if not conn.execute("SELECT 1 FROM users WHERE username = ?", (member_email,)).fetchone():
        conn.close()
        return redirect(url_for("group.groups_view", error=f"Användaren '{member_email}' finns inte"))

    if conn.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
        (group_id, member_email),
    ).fetchone():
        conn.close()
        return redirect(url_for("group.groups_view", error="Användaren är redan med i gruppen"))

    # Fetch group name
    group_row = conn.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
    group_name = group_row["name"] if group_row else "En grupp"

    conn.execute(
        "INSERT INTO group_members (group_id, username, role, joined_at) VALUES (?, ?, ?, ?)",
        (group_id, member_email, "member", datetime.now().timestamp()),
    )
    conn.commit()
    conn.close()

    try:
        _send_invite_email(member_email, group_name, request.username)
    except Exception:
        # Suppress email failures so the database update still succeeds
        pass

    return redirect(url_for("group.groups_view", success=f"'{member_email}' tillagd i gruppen"))


@bp.route("/groups/<group_id>/remove", methods=["POST"])
@requires_auth
def remove_member(group_id):
    if not _is_owner_or_admin(group_id):
        abort(403)

    member = request.form.get("username", "").strip()
    conn = get_db()
    conn.execute(
        "DELETE FROM group_members WHERE group_id = ? AND username = ?",
        (group_id, member),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("group.groups_view", success="Användaren borttagen från gruppen"))


@bp.route("/groups/<group_id>/delete", methods=["POST"])
@requires_auth
def delete_group(group_id):
    if not _is_owner_or_admin(group_id):
        abort(403)

    conn = get_db()
    group = conn.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not group:
        conn.close()
        abort(404)

    conn.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()
    conn.close()

    group_dir = ROOT / "Groups" / group_id
    if group_dir.is_dir():
        shutil.rmtree(group_dir)

    return redirect(url_for("group.groups_view", success=f"Gruppen '{group['name']}' borttagen"))


@bp.route("/groups/<group_id>/rename", methods=["POST"])
@requires_auth
def rename_group(group_id):
    if not _is_owner_or_admin(group_id):
        abort(403)

    new_name = request.form.get("name", "").strip()
    if not new_name:
        return redirect(url_for("group.groups_view", error="Gruppnamn kan inte vara tomt"))

    conn = get_db()
    conn.execute("UPDATE groups SET name = ? WHERE id = ?", (new_name, group_id))
    conn.commit()
    conn.close()
    return redirect(url_for("group.groups_view", success=f"Gruppen döpt om till '{new_name}'"))
