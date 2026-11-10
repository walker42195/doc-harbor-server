from flask import Blueprint, render_template, request, jsonify, abort, Response
from auth import requires_auth
from config import ROOT
import os
import re

bp = Blueprint("todo", __name__)

def _get_user_todo_dir(username: str):
    if not username:
        abort(400)
    # Ensure safe path name without traversal
    safe_username = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', username)
    todo_dir = (ROOT / "Users" / safe_username / "Todos").resolve()
    todo_dir.mkdir(parents=True, exist_ok=True)
    return todo_dir

def _ensure_default_list(todo_dir):
    txt_files = list(todo_dir.glob("*.txt"))
    if not txt_files:
        default_file = todo_dir / "todo.txt"
        default_file.write_text(
            "[ ] Välkommen till doc-harbor Todo!\n"
            "[ ] Skapa en ny uppgift genom att skriva nedan.\n"
            "[x] Detta är en avklarad uppgift.",
            encoding="utf-8"
        )
        txt_files = [default_file]
    return txt_files

@bp.route("/todo")
@requires_auth
def todo_view():
    username = request.username
    todo_dir = _get_user_todo_dir(username)
    _ensure_default_list(todo_dir)
    return render_template("todo.html", username=username)

@bp.route("/api/todo", methods=["GET"])
@requires_auth
def api_list_todos():
    username = request.username
    todo_dir = _get_user_todo_dir(username)
    files = _ensure_default_list(todo_dir)
    
    # Get names without extension
    lists = [f.stem for f in files]
    lists.sort(key=str.lower)
    return jsonify({"lists": lists})

@bp.route("/api/todo/<name>", methods=["GET"])
@requires_auth
def api_get_todo(name):
    username = request.username
    todo_dir = _get_user_todo_dir(username)
    
    # Secure name to prevent path traversal
    safe_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    filepath = (todo_dir / f"{safe_name}.txt").resolve()
    if not filepath.is_relative_to(todo_dir):
        return jsonify({"error": "Unauthorized path"}), 403
        
    if not filepath.is_file():
        return jsonify({"error": "Todo list not found"}), 404
        
    content = filepath.read_text(encoding="utf-8", errors="replace")
    return Response(content, mimetype="text/plain")

@bp.route("/api/todo/<name>", methods=["POST"])
@requires_auth
def api_save_todo(name):
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
        
    username = request.username
    todo_dir = _get_user_todo_dir(username)
    
    safe_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    filepath = (todo_dir / f"{safe_name}.txt").resolve()
    if not filepath.is_relative_to(todo_dir):
        return jsonify({"error": "Unauthorized path"}), 403
        
    if request.is_json:
        content = request.json.get("content", "")
    else:
        content = request.data.decode("utf-8")
        
    filepath.write_text(content, encoding="utf-8")
    return jsonify({"success": True})

@bp.route("/api/todo/create", methods=["POST"])
@requires_auth
def api_create_todo():
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
        
    username = request.username
    todo_dir = _get_user_todo_dir(username)
    
    if request.is_json:
        name = request.json.get("name", "").strip()
    else:
        name = request.form.get("name", "").strip()
        
    if not name:
        return jsonify({"error": "Name required"}), 400
        
    safe_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    filepath = (todo_dir / f"{safe_name}.txt").resolve()
    if not filepath.is_relative_to(todo_dir):
        return jsonify({"error": "Unauthorized path"}), 403
        
    if filepath.is_file():
        return jsonify({"error": "Todo list already exists"}), 400
        
    filepath.write_text("", encoding="utf-8")
    return jsonify({"success": True, "name": safe_name})

@bp.route("/api/todo/<name>", methods=["DELETE"])
@requires_auth
def api_delete_todo(name):
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
        
    username = request.username
    todo_dir = _get_user_todo_dir(username)
    
    safe_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    filepath = (todo_dir / f"{safe_name}.txt").resolve()
    if not filepath.is_relative_to(todo_dir):
        return jsonify({"error": "Unauthorized path"}), 403
        
    if filepath.is_file():
        os.remove(filepath)
        
    return jsonify({"success": True})

@bp.route("/api/todo/<name>/rename", methods=["POST"])
@requires_auth
def api_rename_todo(name):
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
        
    username = request.username
    todo_dir = _get_user_todo_dir(username)
    
    safe_old_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    old_filepath = (todo_dir / f"{safe_old_name}.txt").resolve()
    if not old_filepath.is_relative_to(todo_dir) or not old_filepath.is_file():
        return jsonify({"error": "Todo list not found"}), 404
        
    data = request.get_json(silent=True) or {}
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return jsonify({"error": "New name required"}), 400
        
    safe_new_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', new_name)
    new_filepath = (todo_dir / f"{safe_new_name}.txt").resolve()
    if not new_filepath.is_relative_to(todo_dir):
        return jsonify({"error": "Unauthorized path"}), 403
    if new_filepath.is_file() and safe_new_name != safe_old_name:
        return jsonify({"error": "Target list already exists"}), 400
        
    os.rename(old_filepath, new_filepath)
    return jsonify({"success": True, "name": safe_new_name})

