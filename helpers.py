import os
import re
import logging
import shutil
import markdown
import bleach
from markupsafe import Markup
from pygments import highlight
from pygments.lexers import get_lexer_for_filename, TextLexer
from pygments.formatters import HtmlFormatter
from pathlib import Path
from datetime import datetime
from flask import request, abort, url_for

from config import ROOT, MAX_VERSIONS, TEXT_EXTENSIONS, IMAGE_EXTENSIONS


# ── Formatting helpers ───────────────────────────────────────────────────────

def fmt_size(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def sanitize_html(html_content):
    allowed_tags = [
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "br", "hr", "pre", "code", "blockquote",
        "ul", "ol", "li",
        "table", "thead", "tbody", "tr", "th", "td",
        "a", "img", "span", "div", "em", "strong", "i", "b", "del", "ins",
    ]
    allowed_attrs = {
        "a": ["href", "title", "target"],
        "img": ["src", "alt", "title", "width", "height"],
        "th": ["style", "align"],
        "td": ["style", "align"],
        "code": ["class"],
        "pre": ["class"],
        "div": ["class"],
        "span": ["class"],
    }
    return bleach.clean(html_content, tags=allowed_tags, attributes=allowed_attrs, strip=True)


def _markdown_render(text):
    html = markdown.markdown(text, extensions=["fenced_code", "tables", "toc", "nl2br"])
    return Markup(sanitize_html(html))


# ── Access control ───────────────────────────────────────────────────────────

def get_accessible_paths(username: str, role: str) -> list[Path]:
    if role == "admin":
        return [ROOT.resolve()]

    paths = []

    try:
        users_dir = (ROOT / "Users").resolve()
        users_dir.mkdir(parents=True, exist_ok=True)
        safe_username = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', username)
        user_dir = (users_dir / safe_username).resolve()
        user_dir.relative_to(users_dir)
        user_dir.mkdir(parents=True, exist_ok=True)
        paths.append(user_dir)
    except (ValueError, OSError) as e:
        logging.error(f"Error getting accessible user_dir for {username}: {e}")

    from db import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT g.id FROM groups g JOIN group_members gm ON g.id = gm.group_id WHERE gm.username = ?",
        (username,),
    ).fetchall()
    conn.close()

    for row in rows:
        group_dir = (ROOT / "Groups" / row["id"]).resolve()
        if group_dir.is_dir():
            paths.append(group_dir)

    return paths


def safe_path(rel: str) -> Path:
    rel = rel.lstrip("/")
    root_resolved = ROOT.resolve()
    path = (ROOT / rel).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError:
        abort(403)

    username = getattr(request, "username", None)
    user_role = getattr(request, "user_role", None)
    if username and user_role and user_role != "admin":
        accessible = get_accessible_paths(username, user_role)
        path_str = str(path)
        if not any(
            path == acc or path_str.startswith(str(acc) + "/")
            for acc in accessible
        ):
            abort(403)

    return path


# ── Navigation helpers ───────────────────────────────────────────────────────

def make_breadcrumbs(rel: str):
    parts = [p for p in rel.split("/") if p]

    username = getattr(request, "username", None)
    user_role = getattr(request, "user_role", None)

    # Strip "Users/<username>" prefix from visual display for non-admin users
    display_offset = 0
    if user_role and user_role != "admin" and username:
        if len(parts) >= 2 and parts[0].lower() == "users":
            p1 = parts[1].lower()
            u = username.lower()
            if p1 == u or p1.startswith(f"{u}@") or p1.startswith(u) or u.startswith(p1):
                display_offset = 2

    crumbs = [("doc-harbor", "/")]
    for i, part in enumerate(parts):
        href = "/browse/" + "/".join(parts[: i + 1])
        if i < display_offset:
            continue
        display_part = part
        if i == 1 and parts[0] == "Groups":
            from db import get_db
            try:
                conn = get_db()
                row = conn.execute("SELECT name FROM groups WHERE id = ?", (part,)).fetchone()
                conn.close()
                if row:
                    display_part = row["name"]
            except Exception:
                pass
        crumbs.append((display_part, href))

    return crumbs


def parent_url(rel: str) -> str:
    if "/" in rel:
        parent = rel.rsplit("/", 1)[0]
        return url_for("file.browse_view", rel=parent)
    return url_for("file.index")


# ── Version helpers ──────────────────────────────────────────────────────────

def _versions_dir(path: Path) -> Path:
    return path.parent / ".versions" / path.name


def _save_version(path: Path):
    if not path.is_file():
        return
    vdir = _versions_dir(path)
    vdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    shutil.copy2(path, vdir / ts)
    versions = sorted(vdir.iterdir(), key=lambda p: p.name)
    for old in versions[:-MAX_VERSIONS]:
        old.unlink(missing_ok=True)


def _list_versions(path: Path):
    vdir = _versions_dir(path)
    if not vdir.is_dir():
        return []
    result = []
    for vfile in sorted(vdir.iterdir(), key=lambda p: p.name, reverse=True):
        try:
            dt = datetime.strptime(vfile.name, "%Y-%m-%dT%H-%M-%S")
            label = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            label = vfile.name
        result.append({
            "name": vfile.name,
            "label": label,
            "size": vfile.stat().st_size,
            "mtime": vfile.stat().st_mtime
        })
    return result


# ── Directory size ───────────────────────────────────────────────────────────

def get_dir_size_recursive(path: Path):
    total_size = 0
    files_count = 0
    dirs_count = 0
    try:
        for entry in os.scandir(path):
            if entry.name.startswith("."):
                continue
            if entry.is_file(follow_symlinks=False):
                total_size += entry.stat().st_size
                files_count += 1
            elif entry.is_dir(follow_symlinks=False):
                sub_size, sub_files, sub_dirs = get_dir_size_recursive(Path(entry.path))
                total_size += sub_size
                files_count += sub_files
                dirs_count += sub_dirs + 1
    except Exception:
        pass
    return total_size, files_count, dirs_count


# ── File rendering helper ────────────────────────────────────────────────────

def render_file(path: Path, rel: str):
    from flask import render_template, send_file
    suffix = path.suffix.lower()
    crumbs = make_breadcrumbs(rel)
    up = parent_url(rel)
    version_count = len(_list_versions(path))

    if suffix in IMAGE_EXTENSIONS:
        return render_template(
            "file.html", rel=rel, crumbs=crumbs, up=up,
            content_type="image", is_editable=False, version_count=version_count,
        )

    if suffix in TEXT_EXTENSIONS or suffix == "":
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            abort(400)

        if suffix == ".md":
            html = markdown.markdown(text, extensions=["fenced_code", "tables", "toc", "nl2br"])
            sanitized = sanitize_html(html)
            return render_template(
                "file.html", rel=rel, crumbs=crumbs, up=up,
                content_type="markdown", content=sanitized, is_editable=True,
                version_count=version_count,
            )

        if suffix == ".html":
            return render_template(
                "file.html", rel=rel, crumbs=crumbs, up=up,
                content_type="html", is_editable=True,
                version_count=version_count,
            )

        try:
            lexer = get_lexer_for_filename(path.name)
        except Exception:
            lexer = TextLexer()
        formatter = HtmlFormatter(linenos=True, cssclass="source")
        highlighted = text
        try:
            highlighted = highlight(text, lexer, formatter)
        except Exception:
            pass
        pygments_css = HtmlFormatter(style="friendly").get_style_defs(".source")
        return render_template(
            "file.html", rel=rel, crumbs=crumbs, up=up,
            content_type="code", content=highlighted,
            pygments_css=pygments_css, is_editable=True,
            version_count=version_count,
        )

    return send_file(path, as_attachment=True)


def ensure_user_default_structure(username: str):
    """Ensures that a user's home directory Users/<username> exists and contains default Todos and Kanban structures."""
    if not username:
        return
    import json
    safe_username = re.sub(r'[^a-zA-Z0-9_@.+-]', '_', username)
    user_dir = (ROOT / "Users" / safe_username).resolve()
    user_dir.mkdir(parents=True, exist_ok=True)

    # 1. Ensure Todos directory and default list
    todo_dir = user_dir / "Todos"
    todo_dir.mkdir(parents=True, exist_ok=True)
    txt_files = list(todo_dir.glob("*.txt"))
    if not txt_files:
        default_todo = todo_dir / "todo.txt"
        default_todo.write_text(
            "[ ] Välkommen till doc-harbor Todo!\n"
            "[ ] Skapa en ny uppgift genom att skriva nedan.\n"
            "[x] Detta är en avklarad uppgift.",
            encoding="utf-8"
        )

    # 2. Ensure Kanban directory and default board
    kanban_dir = user_dir / "Kanban"
    kanban_dir.mkdir(parents=True, exist_ok=True)
    json_files = list(kanban_dir.glob("*.json"))
    if not json_files:
        default_kanban = kanban_dir / "Projekt.json"
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
        default_kanban.write_text(json.dumps(default_data, ensure_ascii=False, indent=2), encoding="utf-8")

