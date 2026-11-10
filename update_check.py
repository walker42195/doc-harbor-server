"""Anonym uppdateringskoll.

En gång per dygn frågar servern doc-harbor.sandgren.one om det finns en nyare
version, så att admin kan få veta det. Anropet skickar bara två saker:

  id  – ett slumpat UUID som skapas första gången (sparas i system_settings)
  v   – serverns versionsnummer

Inget om användare, filer eller innehåll skickas. Stäng av med UPDATE_CHECK=false.
"""

import json
import logging
import os
import threading
import urllib.parse
import urllib.request
import uuid

from config import APP_VERSION
from db import get_db

CHECK_URL = os.environ.get(
    "UPDATE_CHECK_URL", "https://doc-harbor.sandgren.one/server-version.json"
)
FIRST_DELAY = 60          # sekunder efter start
INTERVAL = 24 * 60 * 60   # en gång per dygn
TIMEOUT = 5

log = logging.getLogger(__name__)

_state = {"latest": None, "url": None}
_started = False
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("UPDATE_CHECK", "true").strip().lower() not in ("0", "false", "no", "off")


def _instance_id() -> str:
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM system_settings WHERE key = 'instance_id'").fetchone()
        if row:
            return row["value"]
        new_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('instance_id', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (new_id,),
        )
        conn.commit()
        row = conn.execute("SELECT value FROM system_settings WHERE key = 'instance_id'").fetchone()
        return row["value"]
    finally:
        conn.close()


def _version_tuple(v: str) -> tuple:
    parts = []
    for p in str(v).strip().split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def check_once() -> None:
    query = urllib.parse.urlencode({"id": _instance_id(), "v": APP_VERSION})
    req = urllib.request.Request(
        f"{CHECK_URL}?{query}", headers={"User-Agent": f"doc-harbor-server/{APP_VERSION}"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read(64 * 1024).decode("utf-8"))
    latest = str(data.get("version", "")).strip()
    url = str(data.get("url", "")).strip()
    if latest:
        _state["latest"] = latest
        _state["url"] = url if url.startswith("https://") else None


def _loop(stop: threading.Event) -> None:
    if stop.wait(FIRST_DELAY):
        return
    while True:
        try:
            check_once()
        except Exception as e:  # nätverksfel o.d. ska aldrig störa servern
            log.debug("Uppdateringskollen misslyckades: %s", e)
        if stop.wait(INTERVAL):
            return


def start() -> None:
    """Startar bakgrundstråden en gång per process (om den inte är avstängd)."""
    global _started
    with _lock:
        if _started or not enabled():
            return
        _started = True
    threading.Thread(target=_loop, args=(threading.Event(),), name="update-check", daemon=True).start()


def status() -> dict:
    latest = _state["latest"]
    available = bool(latest) and _version_tuple(latest) > _version_tuple(APP_VERSION)
    return {
        "update_available": available,
        "latest_version": latest if available else None,
        "latest_url": _state["url"] if available else None,
    }
