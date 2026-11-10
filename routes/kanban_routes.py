from flask import Blueprint, render_template, request, jsonify, abort
from auth import requires_auth
from config import ROOT
import os
import re
import json

bp = Blueprint("kanban", __name__)

def _get_user_kanban_dir(username: str):
    if not username:
        abort(400)
    safe_username = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', username)
    kanban_dir = (ROOT / "Users" / safe_username / "Kanban").resolve()
    kanban_dir.mkdir(parents=True, exist_ok=True)
    return kanban_dir

def _ensure_default_board(kanban_dir):
    json_files = list(kanban_dir.glob("*.json"))
    if not json_files:
        default_file = kanban_dir / "Projekt.json"
        default_data = {
            "title": "Projekt",
            "columns": [
                {
                    "id": "todo",
                    "title": "Att göra",
                    "cards": [
                        {"id": "card-1", "title": "Välkommen till Kanban!", "description": "Dra och släpp kort mellan kolumner med musen eller fingret."}
                    ]
                },
                {
                    "id": "in_progress",
                    "title": "Pågår",
                    "cards": [
                        {"id": "card-2", "title": "Pågående uppgift", "description": "Här samlas det som håller på att bearbetas."}
                    ]
                },
                {
                    "id": "done",
                    "title": "Klart",
                    "cards": [
                        {"id": "card-3", "title": "Slutförd uppgift", "description": "Färdiga moment hamnar här."}
                    ]
                }
            ]
        }
        default_file.write_text(json.dumps(default_data, ensure_ascii=False, indent=2), encoding="utf-8")
        json_files = [default_file]
    return json_files

@bp.route("/kanban")
@requires_auth
def kanban_view():
    username = request.username
    kanban_dir = _get_user_kanban_dir(username)
    _ensure_default_board(kanban_dir)
    return render_template("kanban.html", username=username)

@bp.route("/api/kanban", methods=["GET"])
@requires_auth
def api_list_kanban():
    username = request.username
    kanban_dir = _get_user_kanban_dir(username)
    files = _ensure_default_board(kanban_dir)
    boards = [f.stem for f in files]
    boards.sort(key=str.lower)
    return jsonify({"boards": boards})

@bp.route("/api/kanban/<name>", methods=["GET"])
@requires_auth
def api_get_kanban(name):
    username = request.username
    kanban_dir = _get_user_kanban_dir(username)
    safe_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    filepath = (kanban_dir / f"{safe_name}.json").resolve()
    if not filepath.is_relative_to(kanban_dir):
        return jsonify({"error": "Unauthorized path"}), 403
    if not filepath.is_file():
        return jsonify({"error": "Kanban board not found"}), 404
    
    content = filepath.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(content)
    except Exception:
        data = {"title": safe_name, "columns": []}
    return jsonify(data)

@bp.route("/api/kanban/<name>", methods=["POST"])
@requires_auth
def api_save_kanban(name):
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
    username = request.username
    kanban_dir = _get_user_kanban_dir(username)
    safe_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    filepath = (kanban_dir / f"{safe_name}.json").resolve()
    if not filepath.is_relative_to(kanban_dir):
        return jsonify({"error": "Unauthorized path"}), 403
    
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON"}), 400
        
    filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"success": True})

@bp.route("/api/kanban/create", methods=["POST"])
@requires_auth
def api_create_kanban():
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
    username = request.username
    kanban_dir = _get_user_kanban_dir(username)
    
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip() or request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
        
    safe_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    filepath = (kanban_dir / f"{safe_name}.json").resolve()
    if not filepath.is_relative_to(kanban_dir):
        return jsonify({"error": "Unauthorized path"}), 403
    if filepath.is_file():
        return jsonify({"error": "Kanban board already exists"}), 400
        
    initial_data = {
        "title": safe_name,
        "columns": [
            {"id": "todo", "title": "Att göra", "cards": []},
            {"id": "in_progress", "title": "Pågår", "cards": []},
            {"id": "done", "title": "Klart", "cards": []}
        ]
    }
    filepath.write_text(json.dumps(initial_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"success": True, "name": safe_name})

@bp.route("/api/kanban/<name>/rename", methods=["POST"])
@requires_auth
def api_rename_kanban(name):
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
    username = request.username
    kanban_dir = _get_user_kanban_dir(username)
    safe_old_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    old_filepath = (kanban_dir / f"{safe_old_name}.json").resolve()
    if not old_filepath.is_relative_to(kanban_dir) or not old_filepath.is_file():
        return jsonify({"error": "Board not found"}), 404
        
    data = request.get_json(silent=True) or {}
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return jsonify({"error": "New name required"}), 400
        
    safe_new_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', new_name)
    new_filepath = (kanban_dir / f"{safe_new_name}.json").resolve()
    if not new_filepath.is_relative_to(kanban_dir):
        return jsonify({"error": "Unauthorized path"}), 403
    if new_filepath.is_file() and safe_new_name != safe_old_name:
        return jsonify({"error": "Target board already exists"}), 400
        
    try:
        board_data = json.loads(old_filepath.read_text(encoding="utf-8"))
        board_data["title"] = safe_new_name
    except Exception:
        board_data = {"title": safe_new_name, "columns": []}
        
    old_filepath.unlink()
    new_filepath.write_text(json.dumps(board_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"success": True, "name": safe_new_name})

@bp.route("/api/kanban/<name>", methods=["DELETE"])
@requires_auth
def api_delete_kanban(name):
    if request.user_role == "read-only":
        return jsonify({"error": "Forbidden: Read-only access"}), 403
    username = request.username
    kanban_dir = _get_user_kanban_dir(username)
    safe_name = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', name)
    filepath = (kanban_dir / f"{safe_name}.json").resolve()
    if not filepath.is_relative_to(kanban_dir):
        return jsonify({"error": "Unauthorized path"}), 403
    if filepath.is_file():
        os.remove(filepath)
    return jsonify({"success": True})
