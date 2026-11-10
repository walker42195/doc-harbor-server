from datetime import datetime

from flask import Blueprint, Response, jsonify, render_template, request, session

import backup
from auth import requires_auth

bp = Blueprint("backup", __name__)


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _can_write():
    return request.user_role in ("admin", "read-write")


@bp.route("/backup")
@requires_auth
def backup_view():
    empty, reason = (backup.instance_is_empty() if request.user_role == "admin" else (False, ""))
    return render_template(
        "backup.html",
        is_admin=request.user_role == "admin",
        can_write=_can_write(),
        home_rel=str(backup.user_home(request.username).relative_to(backup.ROOT)),
        restore_allowed=empty,
        restore_reason=reason,
        chunk_size=backup.UPLOAD_CHUNK_BYTES,
    )


def _download(gen, filename, mimetype):
    resp = Response(gen, mimetype=mimetype, direct_passthrough=True)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@bp.route("/api/backup/export/<part>")
@requires_auth
def export_part(part):
    if part not in backup.PARTS:
        return _err("Okänd del", 404)
    username, role = request.username, request.user_role
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    name = f"doc-harbor_{part}_{backup.safe_username(username)}_{stamp}.zip"
    gen = backup.stream_archive(lambda f: backup.write_part_zip(f, part, username, role))
    return _download(gen, name, "application/zip")


@bp.route("/api/backup/export-full")
@requires_auth
def export_full():
    if request.user_role != "admin":
        return _err("Endast admin", 403)
    username = request.username
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    gen = backup.stream_archive(lambda f: backup.write_full_tar(f, username))
    return _download(gen, f"doc-harbor_FULLSTANDIG_{stamp}.tar.gz", "application/gzip")


@bp.route("/api/backup/restore-status")
@requires_auth
def restore_status():
    if request.user_role != "admin":
        return _err("Endast admin", 403)
    empty, reason = backup.instance_is_empty()
    return jsonify({"allowed": empty, "reason": reason})


# ── Uppladdning i bitar (klarar stora filer bakom Cloudflare) ─────────────────

@bp.route("/api/backup/upload", methods=["POST"])
@requires_auth
def upload_start():
    if not _can_write():
        return _err("Du har bara läsbehörighet.", 403)
    data = request.get_json(silent=True) or {}
    try:
        upload_id = backup.start_upload(request.username, str(data.get("filename", "")), int(data.get("size", 0)))
    except (backup.BackupError, ValueError) as e:
        return _err(str(e))
    return jsonify({"upload_id": upload_id, "chunk_size": backup.UPLOAD_CHUNK_BYTES})


@bp.route("/api/backup/upload/<upload_id>", methods=["PUT"])
@requires_auth
def upload_chunk(upload_id):
    if not _can_write():
        return _err("Du har bara läsbehörighet.", 403)
    try:
        offset = int(request.args.get("offset", "-1"))
        length = int(request.content_length or 0)
        received = backup.append_upload(request.username, upload_id, offset, request.stream, length)
    except (backup.BackupError, ValueError) as e:
        return _err(str(e))
    return jsonify({"received": received})


@bp.route("/api/backup/upload/<upload_id>", methods=["DELETE"])
@requires_auth
def upload_discard(upload_id):
    backup.discard_upload(request.username, upload_id)
    return jsonify({"success": True})


# ── Import och återställning (körs i bakgrunden) ──────────────────────────────

def _uploaded_archive():
    """Uppladdad fil: antingen en färdig bit-uppladdning eller en vanlig multipart-fil."""
    if "file" in request.files:
        return backup.store_file_upload(request.username, request.files["file"], request.content_length or 0)
    data = request.get_json(silent=True) or {}
    upload_id = str(data.get("upload_id", ""))
    return upload_id, backup.finished_upload(request.username, upload_id)


@bp.route("/api/backup/import", methods=["POST"])
@requires_auth
def import_part():
    if not _can_write():
        return _err("Du har bara läsbehörighet.", 403)
    username = request.username
    data = request.get_json(silent=True) or request.form
    mode = data.get("mode", "rename")
    try:
        upload_id, archive = _uploaded_archive()
    except backup.BackupError as e:
        return _err(str(e))

    def job(progress):
        try:
            return backup.import_part(archive, username, mode, progress)
        finally:
            backup.discard_upload(username, upload_id)

    return jsonify({"job_id": backup.start_job(username, "import", job)})


@bp.route("/api/backup/restore-full", methods=["POST"])
@requires_auth
def restore_full():
    if request.user_role != "admin":
        return _err("Endast admin", 403)
    data = request.get_json(silent=True) or request.form
    if str(data.get("confirm", "")).lower() not in ("1", "true", "yes", "ja"):
        return _err("Bekräfta återställningen.")
    empty, reason = backup.instance_is_empty()
    if not empty:
        return _err("Fullständig återställning kan bara göras i en tom instans. " + reason, 409)
    username = request.username
    try:
        upload_id, archive = _uploaded_archive()
    except backup.BackupError as e:
        return _err(str(e))

    def job(progress):
        try:
            return backup.restore_full(archive, progress)
        finally:
            backup.discard_upload(username, upload_id)

    return jsonify({"job_id": backup.start_job(username, "restore-full", job)})


@bp.route("/api/backup/jobs/<job_id>")
@requires_auth
def job_status(job_id):
    job = backup.get_job(job_id, request.username)
    if not job:
        return _err("Jobbet finns inte", 404)
    if job["kind"] == "restore-full" and job["state"] == "done":
        # Kontona kommer nu från backupen: logga in på nytt.
        session.clear()
    return jsonify(job)
