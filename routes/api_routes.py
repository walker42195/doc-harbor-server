import os
from pathlib import Path
from flask import Blueprint, request, jsonify, Response, send_file
from auth import requires_auth
from helpers import safe_path, get_dir_size_recursive, _save_version
from config import ROOT
from db import get_db

bp = Blueprint("api", __name__)


@bp.route("/api/browse", defaults={"rel": ""}, strict_slashes=False)
@bp.route("/api/browse/<path:rel>")
@requires_auth
def api_browse(rel):
    # Non-admin with empty rel or "Users" → redirect to their home
    clean_rel = rel.strip("/")
    if request.user_role != "admin" and (not clean_rel or clean_rel == "Users"):
        rel = f"Users/{request.username}"

    path = safe_path(rel)
    if not path.is_dir():
        return jsonify({"error": "Directory not found"}), 404

    entries = []
    for p in path.iterdir():
        if p.name.startswith("."):
            continue
        try:
            stat = p.stat()
            entries.append({
                "name": p.name,
                "is_dir": p.is_dir(),
                "size": stat.st_size if p.is_file() else None,
                "mtime": stat.st_mtime,
                "rel": str(p.relative_to(ROOT)),
            })
        except Exception:
            continue
    return jsonify({"entries": entries})


@bp.route("/api/file/<path:rel>", methods=["GET"])
@requires_auth
def api_get_file(rel):
    path = safe_path(rel)
    if not path.is_file():
        return jsonify({"error": "File not found"}), 404
    try:
        from config import IMAGE_EXTENSIONS
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            return send_file(path)
        content = path.read_text(encoding="utf-8", errors="replace")
        return Response(content, mimetype="text/plain")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/file/<path:rel>", methods=["POST"])
@requires_auth
def api_save_file(rel):
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
    path = safe_path(rel)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            _save_version(path)
        if request.is_json:
            content = request.json.get("content", "")
        else:
            content = request.data.decode("utf-8")
        path.write_text(content, encoding="utf-8")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/size-analysis", defaults={"rel": ""})
@bp.route("/api/size-analysis/", defaults={"rel": ""})
@bp.route("/api/size-analysis/<path:rel>")
@requires_auth
def api_size_analysis(rel):
    path = safe_path(rel)
    if not path.is_dir():
        return jsonify({"error": "Mappen hittades inte"}), 404

    entries = []
    total_dir_size = 0
    try:
        for entry in os.scandir(path):
            if entry.name.startswith("."):
                continue
            entry_path = Path(entry.path)
            if entry.is_file(follow_symlinks=False):
                size = entry.stat().st_size
                total_dir_size += size
                entries.append({
                    "name": entry.name,
                    "is_dir": False,
                    "size": size,
                    "rel": str(entry_path.relative_to(ROOT)),
                    "count": None,
                })
            elif entry.is_dir(follow_symlinks=False):
                size, files_count, dirs_count = get_dir_size_recursive(entry_path)
                total_dir_size += size
                entries.append({
                    "name": entry.name,
                    "is_dir": True,
                    "size": size,
                    "rel": str(entry_path.relative_to(ROOT)),
                    "count": files_count + dirs_count,
                })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    entries.sort(key=lambda x: x["size"], reverse=True)
    return jsonify({"total_size": total_dir_size, "entries": entries})


@bp.route("/api/groups")
@requires_auth
def api_groups():
    conn = get_db()
    if request.user_role == "admin":
        rows = conn.execute("""
            SELECT g.id, g.name, COUNT(gm.username) AS member_count, NULL AS my_role
            FROM groups g
            LEFT JOIN group_members gm ON g.id = gm.group_id
            GROUP BY g.id
            ORDER BY g.name
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT g.id, g.name, COUNT(gm2.username) AS member_count, my.role AS my_role
            FROM groups g
            JOIN group_members my ON g.id = my.group_id AND my.username = ?
            LEFT JOIN group_members gm2 ON g.id = gm2.group_id
            GROUP BY g.id
            ORDER BY g.name
        """, (request.username,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/users")
@requires_auth
def api_users():
    if request.user_role != "admin":
        return jsonify({"error": "Forbidden: Admin access required"}), 403
    conn = get_db()
    rows = conn.execute("SELECT username, role FROM users ORDER BY username").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/users/<username>/groups")
@requires_auth
def api_user_groups(username):
    if request.user_role != "admin":
        return jsonify({"error": "Forbidden: Admin access required"}), 403

    conn = get_db()
    all_groups = conn.execute("SELECT id, name, description FROM groups ORDER BY name").fetchall()
    user_groups = {
        row["group_id"]
        for row in conn.execute(
            "SELECT group_id FROM group_members WHERE username = ?",
            (username,),
        ).fetchall()
    }
    conn.close()

    result = []
    for g in all_groups:
        result.append({
            "id": g["id"],
            "name": g["name"],
            "description": g["description"],
            "is_member": g["id"] in user_groups
        })

    return jsonify(result)


@bp.route("/api/groups/create", methods=["POST"])
@requires_auth
def api_create_group():
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
    if request.is_json:
        data = request.get_json() or {}
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
    else:
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()

    if not name:
        return jsonify({"error": "Gruppnamn krävs"}), 400

    import uuid
    from datetime import datetime
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
    return jsonify({"success": True, "group_id": group_id})


@bp.route("/api/groups/<group_id>/members")
@requires_auth
def api_group_members(group_id):
    conn = get_db()
    if request.user_role != "admin":
        is_member = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
            (group_id, request.username),
        ).fetchone()
        if not is_member:
            conn.close()
            return jsonify({"error": "Forbidden: Not a member of this group"}), 403

    rows = conn.execute(
        "SELECT username, role FROM group_members WHERE group_id = ? ORDER BY role DESC, username ASC",
        (group_id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/groups/<group_id>/invite", methods=["POST"])
@requires_auth
def api_group_invite(group_id):
    from routes.group_routes import _is_owner_or_admin
    if not _is_owner_or_admin(group_id):
        return jsonify({"error": "Forbidden: Only owners and admins can invite members"}), 403

    if request.is_json:
        data = request.get_json() or {}
        member_email = data.get("username", "").strip().lower()
    else:
        member_email = request.form.get("username", "").strip().lower()

    if not member_email:
        return jsonify({"error": "Användarnamn krävs"}), 400

    conn = get_db()
    if not conn.execute("SELECT 1 FROM users WHERE username = ?", (member_email,)).fetchone():
        conn.close()
        return jsonify({"error": f"Användaren '{member_email}' finns inte"}), 404

    if conn.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND username = ?",
        (group_id, member_email),
    ).fetchone():
        conn.close()
        return jsonify({"error": "Användaren är redan med i gruppen"}), 400

    group_row = conn.execute("SELECT name FROM groups WHERE id = ?", (group_id,)).fetchone()
    group_name = group_row["name"] if group_row else "En grupp"

    from datetime import datetime
    conn.execute(
        "INSERT INTO group_members (group_id, username, role, joined_at) VALUES (?, ?, ?, ?)",
        (group_id, member_email, "member", datetime.now().timestamp()),
    )
    conn.commit()
    conn.close()

    try:
        from routes.group_routes import _send_invite_email
        _send_invite_email(member_email, group_name, request.username)
    except Exception:
        pass

    return jsonify({"success": True})


@bp.route("/api/groups/<group_id>/remove", methods=["POST"])
@requires_auth
def api_group_remove(group_id):
    from routes.group_routes import _is_owner_or_admin
    if not _is_owner_or_admin(group_id):
        return jsonify({"error": "Forbidden: Only owners and admins can remove members"}), 403

    if request.is_json:
        data = request.get_json() or {}
        member = data.get("username", "").strip()
    else:
        member = request.form.get("username", "").strip()

    if not member:
        return jsonify({"error": "Användarnamn krävs"}), 400

    conn = get_db()
    conn.execute(
        "DELETE FROM group_members WHERE group_id = ? AND username = ?",
        (group_id, member),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})

