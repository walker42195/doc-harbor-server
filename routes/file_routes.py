import os
import shutil
from pathlib import Path
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, abort, Response, send_file, jsonify,
)
from auth import requires_auth
from helpers import (
    safe_path, make_breadcrumbs, parent_url,
    _save_version, _list_versions, _versions_dir,
    render_file, ensure_user_default_structure,
)
from config import ROOT

bp = Blueprint("file", __name__)

FLAT_SKIP_DIRS = {
    ".versions", ".git", "node_modules", ".dart_tool",
    "build", "dist", "__pycache__", ".gradle", ".idea",
    ".vscode", ".pub-cache", "vendor", ".cache",
}
FLAT_MAX = 2000


@bp.route("/")
@requires_auth
def index():
    if request.user_role != "admin":
        ensure_user_default_structure(request.username)
        return redirect(url_for("file.browse_view", rel=f"Users/{request.username}"))
    return browse_view("")


@bp.route("/browse/", defaults={"rel": ""})
@bp.route("/browse/<path:rel>")
@requires_auth
def browse_view(rel):
    if request.username:
        ensure_user_default_structure(request.username)
    clean_rel = rel.strip("/")
    if request.user_role != "admin" and (not clean_rel or clean_rel == "Users"):
        return redirect(url_for("file.browse_view", rel=f"Users/{request.username}"))
    path = safe_path(rel)
    if path.is_dir():
        sort_by = request.args.get("sort", "name")
        order = request.args.get("order", "asc")
        view_mode = request.args.get("view", "tree")

        if sort_by not in ("name", "size", "mtime"):
            sort_by = "name"
        if order not in ("asc", "desc"):
            order = "asc"
        if view_mode not in ("tree", "flat", "size"):
            view_mode = "tree"

        flat_truncated = False
        entries = []

        if view_mode == "flat":
            for root_dir, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in FLAT_SKIP_DIRS and not d.startswith(".")]
                for file in files:
                    if file.startswith("."):
                        continue
                    if len(entries) >= FLAT_MAX:
                        flat_truncated = True
                        break
                    filepath = Path(root_dir) / file
                    try:
                        stat = filepath.stat()
                        name_rel = filepath.relative_to(path)
                        entries.append({
                            "name": str(name_rel),
                            "is_dir": False,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                            "rel": str(filepath.relative_to(ROOT)),
                        })
                    except (FileNotFoundError, ValueError):
                        continue
                if flat_truncated:
                    break
        else:
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
                except (FileNotFoundError, ValueError):
                    continue

        reverse = order == "desc"

        def sort_key(item):
            if sort_by == "name":
                return item["name"].lower()
            elif sort_by == "size":
                return item["size"] if item["size"] is not None else -1
            elif sort_by == "mtime":
                return item["mtime"]
            return item["name"].lower()

        if view_mode == "tree":
            dirs_list = [e for e in entries if e["is_dir"]]
            files_list = [e for e in entries if not e["is_dir"]]
            dirs_list.sort(key=sort_key, reverse=reverse)
            files_list.sort(key=sort_key, reverse=reverse)
            entries = dirs_list + files_list
        else:
            entries.sort(key=sort_key, reverse=reverse)

        up = parent_url(rel) if rel else None
        return render_template(
            "directory.html",
            entries=entries,
            rel=rel,
            up=up,
            crumbs=make_breadcrumbs(rel),
            view_mode=view_mode,
            sort_by=sort_by,
            order=order,
            flat_truncated=flat_truncated if view_mode == "flat" else False,
            flat_max=FLAT_MAX,
        )
    if path.is_file():
        return render_file(path, rel)
    abort(404)


@bp.route("/image/<path:rel>")
@requires_auth
def image_view(rel):
    path = safe_path(rel)
    if not path.is_file():
        abort(404)
    return send_file(path)


@bp.route("/raw/<path:rel>")
@requires_auth
def raw_html_view(rel):
    path = safe_path(rel)
    if not path.is_file():
        abort(404)
    if path.suffix.lower() != ".html":
        abort(400)
    content = path.read_text(encoding="utf-8", errors="replace")
    if "<html" not in content.lower():
        content = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<style>'
            'body{margin:1rem 1.5rem;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;font-size:1rem;line-height:1.7;color:#1f2328;}'
            'h1{font-size:1.9rem;border-bottom:1px solid #d0d7de;padding-bottom:.4rem;margin:0 0 1rem}'
            'h2{font-size:1.4rem;border-bottom:1px solid #d0d7de;padding-bottom:.3rem;margin:1.75rem 0 .75rem}'
            'h3{font-size:1.15rem;margin:1.5rem 0 .5rem}'
            'p{margin:.75rem 0}'
            'a{color:#0969da}'
            'img{max-width:100%}'
            'table{border-collapse:collapse;width:100%;margin:1rem 0}'
            'th,td{border:1px solid #d0d7de;padding:.45rem .75rem}'
            'th{background:#f6f8fa;font-weight:600}'
            'pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:1rem;overflow-x:auto}'
            'code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.875em}'
            'blockquote{border-left:4px solid #d0d7de;padding:.25rem 1rem;color:#57606a;margin:1rem 0}'
            '</style></head><body>'
            + content
            + '</body></html>'
        )
    return Response(content, mimetype="text/html")


@bp.route("/edit/<path:rel>", methods=["GET"])
@requires_auth
def edit_view(rel):
    if request.user_role == "read-only":
        abort(403)
    path = safe_path(rel)
    if not path.is_file():
        abort(404)
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        abort(400)
    return render_template(
        "edit.html",
        rel=rel,
        crumbs=make_breadcrumbs(rel),
        up=parent_url(rel),
        content=text,
        file_ext=path.suffix.lower(),
        version_count=len(_list_versions(path)),
    )


@bp.route("/edit/<path:rel>", methods=["POST"])
@requires_auth
def save_view(rel):
    if request.user_role == "read-only":
        abort(403)
    path = safe_path(rel)
    if not path.is_file():
        abort(404)
    _save_version(path)
    path.write_text(request.form.get("content", ""), encoding="utf-8")
    return redirect(url_for("file.browse_view", rel=rel))


@bp.route("/history/<path:rel>")
@requires_auth
def history_view(rel):
    path = safe_path(rel)
    if not path.is_file():
        abort(404)

    versions = _list_versions(path)
    v = request.args.get("v", "")
    version_content = None
    version_label = None
    version_mtime = None

    if v:
        if ".." in v or "/" in v or "\\" in v:
            abort(400)
        vfile = _versions_dir(path) / v
        if vfile.is_file():
            try:
                version_content = vfile.read_text(encoding="utf-8", errors="replace")
                version_mtime = vfile.stat().st_mtime
                from datetime import datetime
                try:
                    dt = datetime.strptime(v, "%Y-%m-%dT%H-%M-%S")
                    version_label = dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    version_label = v
            except Exception:
                pass

    return render_template(
        "history.html",
        rel=rel,
        crumbs=make_breadcrumbs(rel),
        up=url_for("file.browse_view", rel=rel),
        versions=versions,
        current_v=v,
        version_content=version_content,
        version_label=version_label,
        version_mtime=version_mtime,
        file_ext=path.suffix.lower(),
    )


@bp.route("/restore/<path:rel>", methods=["POST"])
@requires_auth
def restore_view(rel):
    if request.user_role == "read-only":
        abort(403)
    path = safe_path(rel)
    if not path.is_file():
        abort(404)
    v = request.form.get("version", "")
    if not v or ".." in v or "/" in v or "\\" in v:
        abort(400)
    vfile = _versions_dir(path) / v
    if not vfile.is_file():
        abort(404)
    _save_version(path)
    shutil.copy2(vfile, path)
    return redirect(url_for("file.browse_view", rel=rel))


@bp.route("/delete/<path:rel>", methods=["POST"])
@requires_auth
def delete_view(rel):
    if request.user_role == "read-only":
        abort(403)
    path = safe_path(rel)
    if not path.exists():
        abort(404)
    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    return redirect(url_for("file.browse_view", rel=parent_rel))


@bp.route("/new", methods=["POST"])
@requires_auth
def new_file():
    if request.user_role == "read-only":
        abort(403)
    dir_rel = request.form.get("dir_rel", "").strip("/")
    filename = request.form.get("filename", "").strip()
    if not filename or any(c in filename for c in '/\\<>:"|?*\x00'):
        abort(400)
    if not Path(filename).suffix:
        filename += ".html"
    dir_path = safe_path(dir_rel) if dir_rel else ROOT
    if not dir_path.is_dir():
        abort(404)
    file_path = (dir_path / filename).resolve()
    try:
        file_path.relative_to(ROOT)
    except ValueError:
        abort(403)
    if not file_path.exists():
        file_path.write_text("", encoding="utf-8")
    return redirect(url_for("file.edit_view", rel=str(file_path.relative_to(ROOT))))


@bp.route("/mkdir", methods=["POST"])
@requires_auth
def new_dir():
    if request.user_role == "read-only":
        abort(403)
    dir_rel = request.form.get("dir_rel", "").strip("/")
    dirname = request.form.get("dirname", "").strip()
    if not dirname or any(c in dirname for c in '/\\<>:"|?*\x00'):
        abort(400)
    dir_path = safe_path(dir_rel) if dir_rel else ROOT
    if not dir_path.is_dir():
        abort(404)
    new_path = (dir_path / dirname).resolve()
    try:
        new_path.relative_to(ROOT)
    except ValueError:
        abort(403)
    new_path.mkdir(exist_ok=True)
    return redirect(url_for("file.browse_view", rel=dir_rel))


@bp.route("/upload", defaults={"rel": ""}, methods=["POST"])
@bp.route("/upload/", defaults={"rel": ""}, methods=["POST"])
@bp.route("/upload/<path:rel>", methods=["POST"])
@requires_auth
def upload_files(rel):
    if request.user_role == "read-only":
        return jsonify({"error": "Behörighet saknas"}), 403
    path = safe_path(rel)
    if not path.is_dir():
        return jsonify({"error": "Målmappen hittades inte"}), 404
    if "files" not in request.files:
        return jsonify({"error": "Inga filer mottogs"}), 400

    mtime_ms = request.form.get("mtime")
    mtime = None
    if mtime_ms:
        try:
            mtime = float(mtime_ms) / 1000.0
        except ValueError:
            pass

    for file in request.files.getlist("files"):
        if not file or file.filename == "":
            continue
        filename = os.path.basename(file.filename)
        dest_path = (path / filename).resolve()
        try:
            dest_path.relative_to(ROOT)
        except ValueError:
            continue
        if dest_path.is_file():
            _save_version(dest_path)
        file.save(dest_path)

        if mtime is not None:
            try:
                os.utime(dest_path, (mtime, mtime))
            except Exception:
                pass
    return jsonify({"success": True})


@bp.route("/move", methods=["POST"])
@requires_auth
def move_items():
    if request.user_role == "read-only":
        return jsonify({"error": "Behörighet saknas"}), 403
    data = request.get_json() or {}
    sources = data.get("sources", [])
    dest_dir_rel = data.get("dest_dir", "").strip("/")

    if not sources or not isinstance(sources, list):
        return jsonify({"error": "Inga filer/mappar angivna för flytt"}), 400

    dest_dir = safe_path(dest_dir_rel) if dest_dir_rel else ROOT
    if not dest_dir.is_dir():
        return jsonify({"error": "Målmappen hittades inte"}), 404

    errors = []
    moved = []
    for src_rel in sources:
        src_rel = src_rel.strip("/")
        if not src_rel:
            continue
        src_path = safe_path(src_rel)
        if not src_path.exists():
            errors.append(f"Hittade inte '{src_rel}'")
            continue
        dest_path = (dest_dir / src_path.name).resolve()
        try:
            dest_path.relative_to(ROOT)
        except ValueError:
            errors.append(f"Ogiltig destination för '{src_path.name}'")
            continue
        if dest_path.exists():
            errors.append(f"'{src_path.name}' finns redan i målmappen")
            continue
        if src_path.is_dir() and dest_path.is_relative_to(src_path):
            errors.append(f"Kan inte flytta '{src_path.name}' till en undermapp till sig själv")
            continue
        try:
            shutil.move(str(src_path), str(dest_path))
            src_vdir = _versions_dir(src_path)
            if src_vdir.is_dir():
                dest_vdir = _versions_dir(dest_path)
                dest_vdir.parent.mkdir(parents=True, exist_ok=True)
                if dest_vdir.exists():
                    shutil.rmtree(dest_vdir)
                shutil.move(str(src_vdir), str(dest_vdir))
            moved.append(src_rel)
        except Exception as e:
            errors.append(f"Misslyckades att flytta '{src_path.name}': {str(e)}")

    if errors:
        status = 270 if moved else 400
        return jsonify({"success": bool(moved), "moved": moved, "errors": errors}), status
    return jsonify({"success": True, "moved": moved})


@bp.route("/rename/<path:rel>", methods=["POST"])
@requires_auth
def rename_item(rel):
    if request.user_role == "read-only":
        abort(403)
    path = safe_path(rel)
    if not path.exists():
        abort(404)
    new_name = request.form.get("new_name", "").strip()
    if not new_name or any(c in new_name for c in '/\\<>:"|?*\x00'):
        abort(400)
    new_path = (path.parent / new_name).resolve()
    try:
        new_path.relative_to(ROOT)
    except ValueError:
        abort(403)
    if new_path.exists():
        abort(400)
    try:
        shutil.move(str(path), str(new_path))
        vdir = _versions_dir(path)
        if vdir.is_dir():
            new_vdir = _versions_dir(new_path)
            new_vdir.parent.mkdir(parents=True, exist_ok=True)
            if new_vdir.exists():
                shutil.rmtree(new_vdir)
            shutil.move(str(vdir), str(new_vdir))
    except Exception:
        abort(500)
    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return redirect(url_for("file.browse_view", rel=parent_rel))


@bp.route("/delete-batch", methods=["POST"])
@requires_auth
def delete_batch():
    if request.user_role == "read-only":
        return jsonify({"error": "Behörighet saknas"}), 403
    data = request.get_json() or {}
    targets = data.get("targets", [])
    if not targets or not isinstance(targets, list):
        return jsonify({"error": "Inga filer/mappar angivna"}), 400

    errors = []
    deleted = []
    for rel in targets:
        rel = rel.strip("/")
        if not rel:
            continue
        path = safe_path(rel)
        if not path.exists():
            continue
        try:
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            deleted.append(rel)
        except Exception as e:
            errors.append(f"Kunde inte radera '{path.name}': {str(e)}")

    if errors:
        status = 270 if deleted else 400
        return jsonify({"success": bool(deleted), "deleted": deleted, "errors": errors}), status
    return jsonify({"success": True, "deleted": deleted})


@bp.route("/paste", methods=["POST"])
@requires_auth
def paste_items():
    if request.user_role == "read-only":
        return jsonify({"error": "Behörighet saknas"}), 403
    data = request.get_json() or {}
    action = data.get("action")
    sources = data.get("sources", [])
    dest_dir_rel = data.get("dest_dir", "").strip("/")

    if not action or action not in ("copy", "cut"):
        return jsonify({"error": "Ogiltig åtgärd"}), 400
    if not sources or not isinstance(sources, list):
        return jsonify({"error": "Inga filer/mappar angivna"}), 400

    dest_dir = safe_path(dest_dir_rel) if dest_dir_rel else ROOT
    if not dest_dir.is_dir():
        return jsonify({"error": "Målmappen hittades inte"}), 404

    errors = []
    success_count = 0
    for src_rel in sources:
        src_rel = src_rel.strip("/")
        if not src_rel:
            continue
        src_path = safe_path(src_rel)
        if not src_path.exists():
            errors.append(f"Hittade inte '{src_rel}'")
            continue
        dest_path = (dest_dir / src_path.name).resolve()
        try:
            dest_path.relative_to(ROOT)
        except ValueError:
            errors.append(f"Ogiltig destination för '{src_path.name}'")
            continue
        if dest_path.exists():
            errors.append(f"'{src_path.name}' finns redan i målmappen")
            continue
        if src_path.is_dir() and dest_path.is_relative_to(src_path):
            errors.append(f"Kan inte flytta/kopiera '{src_path.name}' till en undermapp till sig själv")
            continue
        try:
            if action == "cut":
                shutil.move(str(src_path), str(dest_path))
                src_vdir = _versions_dir(src_path)
                if src_vdir.is_dir():
                    dest_vdir = _versions_dir(dest_path)
                    dest_vdir.parent.mkdir(parents=True, exist_ok=True)
                    if dest_vdir.exists():
                        shutil.rmtree(dest_vdir)
                    shutil.move(str(src_vdir), str(dest_vdir))
            else:
                if src_path.is_file():
                    shutil.copy2(str(src_path), str(dest_path))
                    src_vdir = _versions_dir(src_path)
                    if src_vdir.is_dir():
                        dest_vdir = _versions_dir(dest_path)
                        dest_vdir.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(str(src_vdir), str(dest_vdir), dirs_exist_ok=True)
                elif src_path.is_dir():
                    shutil.copytree(str(src_path), str(dest_path))
            success_count += 1
        except Exception as e:
            errors.append(f"Misslyckades med '{src_path.name}': {str(e)}")

    if errors:
        status = 270 if success_count else 400
        return jsonify({"success": bool(success_count), "count": success_count, "errors": errors}), status
    return jsonify({"success": True, "count": success_count})
