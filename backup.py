"""Backup och återställning för doc-harbor.

Tre delar kan exporteras och importeras var för sig, som .zip:
  - files   Filer: användarens hemmapp Users/<namn>/ (utom Todos/ och Kanban/).
            För admin även alla mappar och filer i roten utom Users/ och Groups/.
  - todo    Att-göra-listor: Users/<namn>/Todos/*.txt
  - kanban  Kanban-tavlor:   Users/<namn>/Kanban/*.json

Arkiven innehåller aldrig något användarnamn i sökvägarna (allt ligger under
data/), så en import hamnar ALLTID under den inloggade användarens hemmapp,
oavsett vem som exporterade. Ingen import kan skriva utanför Users/<inloggad>/.

Admin kan dessutom ta en fullständig backup (.tar.gz) av hela instansen: alla
filer, grupper, versionshistorik och databasen. Den kan bara återställas i en
tom instans (se instance_is_empty).
"""

import io
import json
import logging
import os
import queue
import re
import secrets
import shutil
import sqlite3
import stat
import tarfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from config import APP_VERSION, DB_PATH, ROOT

FORMAT = "doc-harbor-backup"
FULL_FORMAT = "doc-harbor-full-backup"
FORMAT_VERSION = 1
MANIFEST = "doc-harbor-backup.json"
FULL_MANIFEST = "doc-harbor-full-backup.json"
DATA_PREFIX = "data/"
FULL_DB_NAME = "database/doc-harbor.db"
PARTS = ("files", "todo", "kanban")
PART_LABELS = {"files": "Filer", "todo": "Att-göra-listor", "kanban": "Kanban-tavlor"}

# Tempmapp för export/import. Bör ligga på samma filsystem/mount som ROOT och
# DB_PATH (filerna flyttas med os.replace). Produktionen: TMP_DIR=/data/tmp.
TMP_DIR = Path(os.environ.get("TMP_DIR", str(ROOT / ".doc-harbor-backup-tmp"))).resolve()
HOME_RESERVED = {"Todos", "Kanban"}
ROOT_EXCLUDE = {"Users", "Groups"}
NAME_RE = re.compile(r"[^a-zA-Z0-9_@.+-]")

MAX_IMPORT_BYTES = int(os.environ.get("BACKUP_MAX_IMPORT_MB", "20480")) * 1024 * 1024
MAX_IMPORT_FILES = int(os.environ.get("BACKUP_MAX_IMPORT_FILES", "200000"))
MAX_TODO_BYTES = 5 * 1024 * 1024
MAX_KANBAN_BYTES = 20 * 1024 * 1024
FREE_SPACE_MARGIN = 200 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 32 * 1024 * 1024  # under Cloudflares gräns på 100 MB per anrop
STREAM_CHUNK = 256 * 1024

DEFAULT_TODO_NAME = "todo.txt"
DEFAULT_KANBAN_NAME = "Projekt.json"


class BackupError(Exception):
    """Fel som visas för användaren."""


_full_restore_lock = threading.Lock()


# ── Sökvägar ──────────────────────────────────────────────────────────────────

def safe_username(username: str) -> str:
    return NAME_RE.sub("_", username)


def user_home(username: str) -> Path:
    users = (ROOT / "Users").resolve()
    home = (users / safe_username(username)).resolve()
    home.relative_to(users)  # skydd: får aldrig hamna utanför Users/
    return home


def part_target(part: str, username: str) -> Path:
    home = user_home(username)
    if part == "files":
        return home
    if part == "todo":
        return home / "Todos"
    if part == "kanban":
        return home / "Kanban"
    raise BackupError(f"Okänd del: {part}")


def free_space(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def tmp_dir() -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_DIR


def _zip_datetime(ts: float):
    dt = datetime.fromtimestamp(max(ts, 315532800))  # zip klarar inte före 1980
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


# ── Export: vilka filer ingår ─────────────────────────────────────────────────

def _walk(base: Path, skip_top=frozenset(), include_hidden=False, exclude=()):
    """Ger ("dir"|"file", path, rel_posix, stat) under base.

    Följer aldrig symlänkar och tar bara med vanliga filer och mappar, så inget
    utanför base kan läcka ut i ett arkiv.
    """
    exclude = {p.resolve() for p in exclude}
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        rel_dir = d.relative_to(base)
        keep = []
        for n in sorted(dirnames):
            p = d / n
            if (not include_hidden and n.startswith(".")) or p.is_symlink() or p.resolve() in exclude:
                continue
            if rel_dir == Path(".") and n in skip_top:
                continue
            keep.append(n)
        dirnames[:] = keep
        if rel_dir != Path("."):
            try:
                yield "dir", d, rel_dir.as_posix(), d.lstat()
            except OSError:
                pass
        for n in sorted(filenames):
            if not include_hidden and n.startswith("."):
                continue
            p = d / n
            if p.resolve() in exclude:
                continue
            try:
                st = p.lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield "file", p, (rel_dir / n).as_posix(), st


def iter_part_entries(part: str, username: str, role: str):
    """Ger ("dir"|"file", path, arkivnamn, stat) för en del."""
    home = user_home(username)
    if part == "files":
        sources = [(home, "", HOME_RESERVED)]
        if role == "admin":
            home_top = {p.name for p in home.iterdir()} if home.is_dir() else set()
            for p in sorted(ROOT.iterdir()):
                if p.name.startswith(".") or p.name in ROOT_EXCLUDE or p.is_symlink():
                    continue
                name = p.name
                if name in home_top:
                    name = f"{p.name} (rot)"
                if p.is_dir():
                    sources.append((p, name + "/", frozenset()))
                elif p.is_file():
                    yield "file", p, DATA_PREFIX + name, p.lstat()
        for base, prefix, skip in sources:
            if not base.is_dir():
                continue
            if prefix:
                yield "dir", base, DATA_PREFIX + prefix.rstrip("/"), base.lstat()
            for kind, p, rel, st in _walk(base, skip_top=skip):
                yield kind, p, DATA_PREFIX + prefix + rel, st
        return

    folder = part_target(part, username)
    suffix = ".txt" if part == "todo" else ".json"
    if not folder.is_dir():
        return
    for p in sorted(folder.iterdir()):
        if p.name.startswith(".") or p.suffix.lower() != suffix:
            continue
        st = p.lstat()
        if stat.S_ISREG(st.st_mode):
            yield "file", p, DATA_PREFIX + p.name, st


def _manifest(part: str, username: str, count: int, size: int) -> dict:
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "part": part,
        "created": datetime.now().isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
        "exported_by": username,
        "file_count": count,
        "total_bytes": size,
    }


def write_part_zip(fileobj, part: str, username: str, role: str):
    """Skriver en dels zip-arkiv till fileobj (som kan vara en ström)."""
    count = 0
    total = 0
    with zipfile.ZipFile(fileobj, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as zf:
        for kind, path, arc, st in iter_part_entries(part, username, role):
            if kind == "dir":
                zi = zipfile.ZipInfo(arc + "/", date_time=_zip_datetime(st.st_mtime))
                zi.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(zi, b"")
                continue
            zi = zipfile.ZipInfo(arc, date_time=_zip_datetime(st.st_mtime))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o100644 << 16
            try:
                with open(path, "rb") as src, zf.open(zi, "w", force_zip64=st.st_size > 2**31 - 2**24) as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            except OSError as e:
                logging.warning("Backup: hoppar över %s: %s", path, e)
                continue
            count += 1
            total += st.st_size
        zf.writestr(MANIFEST, json.dumps(_manifest(part, username, count, total), ensure_ascii=False, indent=2))


# ── Fullständig backup ────────────────────────────────────────────────────────

class _ExactReader:
    """Läser exakt n byte; fyller ut med nollor om filen krympt under exporten."""

    def __init__(self, f, n):
        self.f, self.left = f, n

    def read(self, size=-1):
        if self.left <= 0:
            return b""
        if size < 0 or size > self.left:
            size = self.left
        data = self.f.read(size)
        if not data:
            data = b"\0" * size
        self.left -= len(data)
        return data


def _snapshot_db(dest: Path):
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def write_full_tar(fileobj, username: str):
    """Hela instansen: alla filer (även dolda, t.ex. versionshistorik) och databasen."""
    snap = tmp_dir() / f"db-snapshot-{secrets.token_hex(6)}.db"
    _snapshot_db(snap)
    db_files = {DB_PATH.resolve(), Path(str(DB_PATH) + "-journal").resolve(),
                Path(str(DB_PATH) + "-wal").resolve(), Path(str(DB_PATH) + "-shm").resolve()}
    try:
        with tarfile.open(fileobj=fileobj, mode="w|gz", format=tarfile.PAX_FORMAT, compresslevel=6) as tf:
            manifest = json.dumps({
                "format": FULL_FORMAT,
                "format_version": FORMAT_VERSION,
                "created": datetime.now().isoformat(timespec="seconds"),
                "app_version": APP_VERSION,
                "exported_by": username,
            }, ensure_ascii=False, indent=2).encode()
            ti = tarfile.TarInfo(FULL_MANIFEST)
            ti.size, ti.mtime, ti.mode = len(manifest), time.time(), 0o644
            tf.addfile(ti, io.BytesIO(manifest))

            with open(snap, "rb") as f:
                ti = tarfile.TarInfo(FULL_DB_NAME)
                ti.size, ti.mtime, ti.mode = snap.stat().st_size, time.time(), 0o600
                tf.addfile(ti, f)

            for kind, path, rel, st in _walk(ROOT, include_hidden=True, exclude=[TMP_DIR, *db_files]):
                ti = tarfile.TarInfo(DATA_PREFIX + rel)
                ti.mtime = st.st_mtime
                if kind == "dir":
                    ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                    tf.addfile(ti)
                    continue
                ti.size, ti.mode = st.st_size, 0o644
                try:
                    f = open(path, "rb")
                except OSError as e:
                    logging.warning("Fullständig backup: hoppar över %s: %s", path, e)
                    continue
                with f:
                    tf.addfile(ti, _ExactReader(f, st.st_size))
    finally:
        snap.unlink(missing_ok=True)


# ── Strömning av arkiv till webbläsaren ───────────────────────────────────────

class _QueueWriter(io.RawIOBase):
    """Fil-liknande objekt som skickar det som skrivs vidare via en kö."""

    def __init__(self, q, cancelled):
        super().__init__()
        self.q, self.cancelled = q, cancelled
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        if self.cancelled.is_set():
            raise BrokenPipeError("Nedladdningen avbröts")
        self.buf += b
        if len(self.buf) >= STREAM_CHUNK:
            self._push()
        return len(b)

    def _push(self):
        if not self.buf:
            return
        data, self.buf = bytes(self.buf), bytearray()
        while True:
            if self.cancelled.is_set():
                raise BrokenPipeError("Nedladdningen avbröts")
            try:
                self.q.put(data, timeout=1)
                return
            except queue.Full:
                continue

    def flush(self):
        self._push()


def stream_archive(write_fn):
    """Kör write_fn(fileobj) i en tråd och ger arkivet bit för bit (begränsat minne)."""
    q = queue.Queue(maxsize=16)
    cancelled = threading.Event()
    done = object()

    def run():
        w = _QueueWriter(q, cancelled)
        try:
            write_fn(w)
            w.flush()
        except BrokenPipeError:
            pass
        except Exception:
            logging.exception("Backup-export misslyckades")
        finally:
            while not cancelled.is_set():
                try:
                    q.put(done, timeout=1)
                    break
                except queue.Full:
                    continue

    threading.Thread(target=run, daemon=True, name="backup-export").start()
    try:
        while True:
            item = q.get()
            if item is done:
                return
            yield item
    finally:
        cancelled.set()


# ── Import av en del ──────────────────────────────────────────────────────────

def _clean_parts(rel: str):
    """Delar upp en sökväg ur ett arkiv och avvisar allt som kan peka ut ur målmappen."""
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = [p for p in PurePosixPath(rel).parts if p not in ("",)]
    if not parts or any(p in (".", "..") or len(p.encode()) > 255 for p in parts):
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return parts


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _unique_name(dest: Path, restricted: bool) -> Path:
    stem, suffix = dest.stem, dest.suffix
    for n in range(1, 10000):
        if restricted:
            name = f"{stem}-import{'' if n == 1 else f'-{n}'}{suffix}"
        else:
            name = f"{stem} (import{'' if n == 1 else f' {n}'}){suffix}"
        cand = dest.with_name(name)
        if not cand.exists():
            return cand
    raise BackupError(f"Hittar inget ledigt namn för {dest.name}")


def _is_default_placeholder(path: Path) -> bool:
    """Exempel-listan/-tavlan som skapas automatiskt för nya konton."""
    try:
        if path.name == DEFAULT_TODO_NAME and path.parent.name == "Todos":
            return path.read_text(encoding="utf-8").startswith("[ ] Välkommen till doc-harbor Todo!") and path.stat().st_size < 300
        if path.name == DEFAULT_KANBAN_NAME and path.parent.name == "Kanban":
            data = json.loads(path.read_text(encoding="utf-8"))
            cards = [c.get("id") for col in data.get("columns", []) for c in col.get("cards", [])]
            return cards == ["card-1", "card-2", "card-3"] and "Välkommen till Kanban!" in path.read_text(encoding="utf-8")
    except Exception:
        return False
    return False


def _same_content(path: Path, zf, info) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != info.file_size:
            return False
        with open(path, "rb") as a, zf.open(info) as b:
            while True:
                x, y = a.read(1 << 20), b.read(1 << 20)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


def read_manifest_zip(zf) -> dict:
    try:
        m = json.loads(zf.read(MANIFEST).decode("utf-8"))
    except KeyError:
        raise BackupError("Filen är inte en doc-harbor-backup (manifest saknas).")
    except Exception:
        raise BackupError("Backupens manifest är skadat.")
    if m.get("format") != FORMAT:
        raise BackupError("Filen är inte en doc-harbor-backup.")
    if int(m.get("format_version", 0)) > FORMAT_VERSION:
        raise BackupError("Backupen är gjord med en nyare version av doc-harbor.")
    if m.get("part") not in PARTS:
        raise BackupError("Backupen innehåller en okänd del.")
    return m


def import_part(archive: Path, username: str, mode: str, progress=lambda **kw: None) -> dict:
    """Importerar en dels zip till den inloggade användarens hemmapp.

    mode: "rename"    – befintliga filer behålls, importerade får nytt namn
          "overwrite" – befintliga skrivs över (gamla versionen sparas i historiken)
    """
    from helpers import _save_version  # sen import: helpers kräver Flask-kontext vid import

    if mode not in ("rename", "overwrite"):
        raise BackupError("Ogiltigt läge för import.")
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise BackupError("Filen är inte en giltig zip-fil.")

    with zf:
        manifest = read_manifest_zip(zf)
        part = manifest["part"]
        target = part_target(part, username)
        target.mkdir(parents=True, exist_ok=True)
        restricted = part in ("todo", "kanban")

        infos = [i for i in zf.infolist() if i.filename != MANIFEST]
        if len(infos) > MAX_IMPORT_FILES:
            raise BackupError(f"Backupen har för många poster ({len(infos)}).")
        total = sum(i.file_size for i in infos)
        if total > MAX_IMPORT_BYTES:
            raise BackupError("Backupen är större än vad som tillåts att importera.")
        if total + FREE_SPACE_MARGIN > free_space(target):
            raise BackupError("Det finns inte tillräckligt med diskutrymme för importen.")

        result = {"part": part, "label": PART_LABELS[part], "exported_by": manifest.get("exported_by"),
                  "target": str(target.relative_to(ROOT)), "imported": 0, "renamed": 0,
                  "overwritten": 0, "identical": 0, "skipped": [], "bytes": 0}
        files = [i for i in infos if not i.is_dir()]
        progress(phase="import", done=0, total=len(files))

        def skip(name, why):
            if len(result["skipped"]) < 200:
                result["skipped"].append({"name": name, "reason": why})

        # Mappar först, så att även tomma mappar följer med.
        for info in infos:
            if not info.is_dir():
                continue
            if part != "files":
                continue
            parts = _clean_parts(info.filename[len(DATA_PREFIX):].rstrip("/")) if info.filename.startswith(DATA_PREFIX) else None
            if not parts or any(p.startswith(".") for p in parts):
                continue
            if parts[0] in HOME_RESERVED:
                parts[0] = f"{parts[0]} (importerad)"
            d = target.joinpath(*parts)
            if _inside(d, target) and not d.exists():
                try:
                    d.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass

        done = 0
        for info in files:
            done += 1
            if done % 25 == 0:
                progress(phase="import", done=done, total=len(files))
            name = info.filename
            if not name.startswith(DATA_PREFIX):
                skip(name, "ligger utanför data/")
                continue
            parts = _clean_parts(name[len(DATA_PREFIX):])
            if not parts:
                skip(name, "otillåten sökväg")
                continue
            if any(p.startswith(".") for p in parts):
                skip(name, "dold fil")
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                skip(name, "symbolisk länk")
                continue

            if part == "files":
                if parts[0] in HOME_RESERVED:
                    parts[0] = f"{parts[0]} (importerad)"
            else:
                if len(parts) != 1:
                    skip(name, "undermappar stöds inte här")
                    continue
                want = ".txt" if part == "todo" else ".json"
                stem, suffix = os.path.splitext(parts[0])
                if suffix.lower() != want:
                    skip(name, f"bara {want}-filer kan importeras")
                    continue
                limit = MAX_TODO_BYTES if part == "todo" else MAX_KANBAN_BYTES
                if info.file_size > limit:
                    skip(name, "för stor")
                    continue
                raw = zf.read(info)
                try:
                    text = raw.decode("utf-8")
                    if part == "kanban":
                        data = json.loads(text)
                        if not isinstance(data, dict) or not isinstance(data.get("columns"), list):
                            raise ValueError
                except (UnicodeDecodeError, ValueError):
                    skip(name, "ogiltigt innehåll")
                    continue
                parts = [NAME_RE.sub("_", stem) + want]

            dest = target.joinpath(*parts)
            if not _inside(dest.parent, target):
                skip(name, "otillåten sökväg")
                continue

            if dest.exists() or dest.is_symlink():
                if dest.is_symlink() or not dest.is_file():
                    dest = _unique_name(dest, restricted)
                    result["renamed"] += 1
                elif _same_content(dest, zf, info):
                    result["identical"] += 1
                    continue
                elif restricted and _is_default_placeholder(dest):
                    pass  # ersätt exempel-listan/-tavlan utan att räknas som konflikt
                elif mode == "overwrite":
                    _save_version(dest)
                    result["overwritten"] += 1
                else:
                    dest = _unique_name(dest, restricted)
                    result["renamed"] += 1

            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                skip(name, "en fil med samma namn som mappen finns redan")
                continue
            if not _inside(dest.parent, target):
                skip(name, "otillåten sökväg")
                continue
            tmp = dest.parent / f".import-{secrets.token_hex(6)}"
            try:
                with zf.open(info) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                ts = time.mktime(info.date_time + (0, 0, -1))
                os.utime(tmp, (ts, ts))
                os.replace(tmp, dest)
            except Exception as e:
                tmp.unlink(missing_ok=True)
                skip(name, f"kunde inte skrivas ({e.__class__.__name__})")
                continue
            result["imported"] += 1
            result["bytes"] += info.file_size

        progress(phase="import", done=len(files), total=len(files))
        return result


# ── Fullständig återställning ─────────────────────────────────────────────────

def instance_is_empty() -> tuple[bool, str]:
    """Fullständig återställning tillåts bara i en tom instans: högst ett konto,
    inga grupper och inga filer utöver de exempelfiler som skapas automatiskt."""
    from db import get_db
    conn = get_db()
    try:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    finally:
        conn.close()
    if users > 1:
        return False, f"Instansen har redan {users} konton."
    if groups > 0:
        return False, f"Instansen har redan {groups} grupper."
    db_files = {DB_PATH.resolve(), Path(str(DB_PATH) + "-journal").resolve(),
                Path(str(DB_PATH) + "-wal").resolve(), Path(str(DB_PATH) + "-shm").resolve()}
    for kind, path, rel, st in _walk(ROOT, include_hidden=True, exclude=[TMP_DIR, *db_files]):
        if kind == "file" and not _is_default_placeholder(path):
            return False, f"Instansen innehåller redan filer (t.ex. {rel})."
    return True, ""


def restore_full(archive: Path, progress=lambda **kw: None) -> dict:
    if not _full_restore_lock.acquire(blocking=False):
        raise BackupError("En fullständig återställning pågår redan.")
    try:
        return _restore_full(archive, progress)
    finally:
        _full_restore_lock.release()


def _restore_full(archive: Path, progress) -> dict:
    ok, why = instance_is_empty()
    if not ok:
        raise BackupError("Fullständig återställning kan bara göras i en tom instans. " + why)

    staging = tmp_dir() / f"restore-{secrets.token_hex(6)}"
    staging.mkdir()
    try:
        try:
            tf = tarfile.open(archive, "r:gz")
        except (tarfile.TarError, OSError):
            raise BackupError("Filen är inte en giltig fullständig backup (.tar.gz).")
        files = 0
        size = 0
        manifest = None
        with tf:
            for i, m in enumerate(tf):
                if i == 0:
                    if m.name != FULL_MANIFEST or not m.isfile():
                        raise BackupError("Filen är inte en fullständig doc-harbor-backup.")
                    manifest = json.loads(tf.extractfile(m).read().decode("utf-8"))
                    if manifest.get("format") != FULL_FORMAT:
                        raise BackupError("Filen är inte en fullständig doc-harbor-backup.")
                    if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
                        raise BackupError("Backupen är gjord med en nyare version av doc-harbor.")
                    continue
                if not (m.isfile() or m.isdir()):
                    continue  # länkar och specialfiler återställs aldrig
                if m.name != FULL_DB_NAME:
                    if not m.name.startswith(DATA_PREFIX) or not _clean_parts(m.name[len(DATA_PREFIX):]):
                        continue
                if m.isfile():
                    if m.size + FREE_SPACE_MARGIN > free_space(staging):
                        raise BackupError("Diskutrymmet tog slut under återställningen.")
                    files += 1
                    size += m.size
                try:
                    tf.extract(m, staging, filter="data")
                except tarfile.TarError as e:
                    raise BackupError(f"Ogiltig post i backupen: {m.name} ({e.__class__.__name__})")
                if files % 50 == 0:
                    progress(phase="unpack", done=files, total=0, bytes=size)

        db_new = staging / FULL_DB_NAME
        if not db_new.is_file():
            raise BackupError("Backupen saknar databasen.")
        chk = sqlite3.connect(db_new)
        try:
            if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("Databasen i backupen är skadad.")
            chk.execute("SELECT COUNT(*) FROM users").fetchone()
        except sqlite3.DatabaseError:
            raise BackupError("Databasen i backupen är skadad.")
        finally:
            chk.close()

        # Kontrollera igen precis innan något flyttas på plats.
        ok, why = instance_is_empty()
        if not ok:
            raise BackupError("Instansen ändrades under återställningen. " + why)

        progress(phase="install", done=0, total=files)
        data_root = staging / "data"
        moved = 0
        if data_root.is_dir():
            for kind, path, rel, st in _walk(data_root, include_hidden=True):
                dest = ROOT / rel
                if kind == "dir":
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, dest)  # skriver bara över exempelfiler (instansen är tom)
                moved += 1
                if moved % 100 == 0:
                    progress(phase="install", done=moved, total=files)

        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
        os.replace(db_new, DB_PATH)
        from db import init_db
        init_db()
        progress(phase="install", done=files, total=files)
        return {"files": moved, "bytes": size, "exported_by": manifest.get("exported_by"),
                "created": manifest.get("created"), "app_version": manifest.get("app_version")}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ── Uppladdning i bitar och bakgrundsjobb ─────────────────────────────────────

def _upload_dir(username: str) -> Path:
    d = tmp_dir() / "uploads" / safe_username(username)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_old_uploads(max_age=24 * 3600):
    base = TMP_DIR / "uploads"
    if not base.is_dir():
        return
    now = time.time()
    for p in base.glob("*/*"):
        try:
            if now - p.stat().st_mtime > max_age:
                p.unlink()
        except OSError:
            pass


def upload_paths(username: str, upload_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise BackupError("Ogiltig uppladdning.")
    d = _upload_dir(username)
    return d / f"{upload_id}.part", d / f"{upload_id}.json"


def start_upload(username: str, filename: str, size: int) -> str:
    if size <= 0:
        raise BackupError("Filen är tom.")
    if size > MAX_IMPORT_BYTES:
        raise BackupError("Filen är för stor.")
    # Uppladdningen + uppackningen ska få plats.
    if size * 2 + FREE_SPACE_MARGIN > free_space(TMP_DIR):
        raise BackupError("Det finns inte tillräckligt med diskutrymme på servern.")
    cleanup_old_uploads()
    upload_id = secrets.token_hex(16)
    part, meta = upload_paths(username, upload_id)
    part.write_bytes(b"")
    meta.write_text(json.dumps({"filename": filename[:200], "size": size}))
    return upload_id


def store_file_upload(username: str, filestorage, declared_size: int) -> tuple[str, Path]:
    """Sparar en vanlig multipart-uppladdning (för API-klienter) som en färdig uppladdning."""
    if declared_size * 2 + FREE_SPACE_MARGIN > free_space(TMP_DIR):
        raise BackupError("Det finns inte tillräckligt med diskutrymme på servern.")
    cleanup_old_uploads()
    upload_id = secrets.token_hex(16)
    part, meta = upload_paths(username, upload_id)
    written = 0
    with open(part, "wb") as out:
        while True:
            data = filestorage.stream.read(1024 * 1024)
            if not data:
                break
            written += len(data)
            if written > MAX_IMPORT_BYTES:
                out.close()
                part.unlink(missing_ok=True)
                raise BackupError("Filen är för stor.")
            out.write(data)
    if written == 0:
        part.unlink(missing_ok=True)
        raise BackupError("Filen är tom.")
    meta.write_text(json.dumps({"filename": (filestorage.filename or "")[:200], "size": written}))
    return upload_id, part


def append_upload(username: str, upload_id: str, offset: int, stream, length: int) -> int:
    part, meta = upload_paths(username, upload_id)
    if not part.is_file():
        raise BackupError("Uppladdningen finns inte (den kan ha gått ut).")
    info = json.loads(meta.read_text())
    current = part.stat().st_size
    if offset != current:
        raise BackupError(f"Fel position i uppladdningen (servern har {current} byte).")
    if length > UPLOAD_CHUNK_BYTES * 2 or current + length > info["size"]:
        raise BackupError("Uppladdningen är större än angivet.")
    with open(part, "ab") as f:
        left = length
        while left > 0:
            data = stream.read(min(left, 1024 * 1024))
            if not data:
                break
            f.write(data)
            left -= len(data)
    return part.stat().st_size


def finished_upload(username: str, upload_id: str) -> Path:
    part, meta = upload_paths(username, upload_id)
    if not part.is_file():
        raise BackupError("Uppladdningen finns inte (den kan ha gått ut).")
    info = json.loads(meta.read_text())
    if part.stat().st_size != info["size"]:
        raise BackupError("Uppladdningen är inte komplett.")
    return part


def discard_upload(username: str, upload_id: str):
    try:
        for p in upload_paths(username, upload_id):
            p.unlink(missing_ok=True)
    except BackupError:
        pass


_jobs: dict = {}
_jobs_lock = threading.Lock()


def start_job(owner: str, kind: str, fn) -> str:
    job_id = secrets.token_hex(12)
    job = {"id": job_id, "owner": owner, "kind": kind, "state": "running",
           "progress": {}, "result": None, "error": None, "started": time.time()}
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if v["state"] != "running" and time.time() - v["started"] > 3600]:
            _jobs.pop(jid, None)
        _jobs[job_id] = job

    def progress(**kw):
        job["progress"] = kw

    def run():
        try:
            job["result"] = fn(progress)
            job["state"] = "done"
        except BackupError as e:
            job["error"] = str(e)
            job["state"] = "error"
        except Exception as e:
            logging.exception("Backup-jobb %s misslyckades", kind)
            job["error"] = f"Oväntat fel: {e.__class__.__name__}"
            job["state"] = "error"

    threading.Thread(target=run, daemon=True, name=f"backup-{kind}").start()
    return job_id


def get_job(job_id: str, owner: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["owner"] != owner:
        return None
    return {k: v for k, v in job.items() if k != "owner"}
