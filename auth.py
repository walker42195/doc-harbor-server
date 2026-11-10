import os
import hashlib
import hmac
import secrets
import functools
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from flask import request, redirect, url_for, Response, session
from db import get_db


from pathlib import Path


def sync_admin_password_from_env():
    admin_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not admin_pw:
        return

    build_time_file = Path("/app/.build_time")
    current_build = build_time_file.read_text().strip() if build_time_file.exists() else "dev"

    conn = get_db()
    cursor = conn.execute("SELECT id FROM users WHERE username = 'admin'")
    admin_user = cursor.fetchone()

    last_build_row = conn.execute("SELECT value FROM system_settings WHERE key = 'last_admin_reset_build'").fetchone()
    last_build = last_build_row["value"] if last_build_row else None

    # Reset on initial setup (no admin), or when container image was rebuilt
    is_new_build = (current_build != "dev" and current_build != last_build)

    if not admin_user:
        pw_hash, salt = hash_password(admin_pw)
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role) VALUES ('admin', ?, ?, 'admin')",
            (pw_hash, salt),
        )
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('last_admin_reset_build', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (current_build,),
        )
        conn.commit()
    elif is_new_build:
        pw_hash, salt = hash_password(admin_pw)
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, role = 'admin' WHERE username = 'admin'",
            (pw_hash, salt),
        )
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('last_admin_reset_build', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (current_build,),
        )
        conn.commit()
    conn.close()


def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return key.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return hmac.compare_digest(key.hex(), password_hash)


# ── Skydd mot lösenordsgissning ──────────────────────────────────────────────
# Misslyckade inloggningar räknas per användarnamn och per klient-IP under ett
# rullande fönster. Över gränsen nekas inloggning (även med rätt lösenord) tills
# fönstret har passerat. Räknarna ligger i minnet och nollställs vid omstart.
FAIL_WINDOW = 15 * 60
MAX_FAILS_PER_USER = 20
MAX_FAILS_PER_IP = 50
_fails = defaultdict(deque)
_fails_lock = threading.Lock()


def _client_ip() -> str:
    try:
        return (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr
            or "?"
        )
    except RuntimeError:  # utanför en request
        return "?"


def _recent(key, now):
    q = _fails[key]
    while q and q[0] < now - FAIL_WINDOW:
        q.popleft()
    if not q:
        _fails.pop(key, None)
    return len(q)


def login_blocked(username: str) -> bool:
    now = time.monotonic()
    with _fails_lock:
        return (
            _recent(("u", username.lower()), now) >= MAX_FAILS_PER_USER
            or _recent(("ip", _client_ip()), now) >= MAX_FAILS_PER_IP
        )


def _record_failure(username: str) -> None:
    now = time.monotonic()
    with _fails_lock:
        _fails[("u", username.lower())].append(now)
        _fails[("ip", _client_ip())].append(now)


def check_auth(username: str, password: str):
    if login_blocked(username or ""):
        return False, None
    conn = get_db()
    cursor = conn.execute(
        "SELECT password_hash, salt, role FROM users WHERE username = ?", (username,)
    )
    user = cursor.fetchone()
    conn.close()
    if user and verify_password(password, user["password_hash"], user["salt"]):
        return True, user["role"]
    _record_failure(username or "")
    return False, None


def create_token(username: str) -> str:
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now().timestamp()
    conn = get_db()
    conn.execute(
        "INSERT INTO tokens (token_hash, username, created_at, last_used) VALUES (?, ?, ?, ?)",
        (token_hash, username, now, now),
    )
    conn.commit()
    conn.close()
    return token


def verify_token(token: str):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = get_db()
    cursor = conn.execute(
        "SELECT t.username, u.role FROM tokens t JOIN users u ON t.username = u.username WHERE t.token_hash = ?",
        (token_hash,),
    )
    row = cursor.fetchone()
    if row:
        conn.execute(
            "UPDATE tokens SET last_used = ? WHERE token_hash = ?",
            (datetime.now().timestamp(), token_hash),
        )
        conn.commit()
    conn.close()
    return (row["username"], row["role"]) if row else (None, None)


def is_setup_needed() -> bool:
    conn = get_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except Exception:
        count = 0
    finally:
        conn.close()
    return count == 0


def _is_api_request() -> bool:
    return request.path.startswith("/api/")


def requires_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if is_setup_needed():
            return redirect(url_for("setup.setup_view"))

        if session.get("username"):
            # Rollen läses om från databasen: ändrad roll eller borttaget konto
            # gäller direkt, inte först vid nästa inloggning.
            conn = get_db()
            row = conn.execute(
                "SELECT role FROM users WHERE username = ?", (session["username"],)
            ).fetchone()
            conn.close()
            if row:
                session["role"] = row["role"]
                request.username = session["username"]
                request.user_role = row["role"]
                return f(*args, **kwargs)
            session.clear()

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            username, role = verify_token(token)
            if username:
                # Set cookie/session for subsequent web requests (like WebView form POSTs)
                session["username"] = username
                session["role"] = role
                request.username = username
                request.user_role = role
                return f(*args, **kwargs)
            return Response(
                "Ogiltigt eller utgånget token",
                401,
                {"WWW-Authenticate": 'Bearer realm="doc-harbor"'},
            )

        auth = request.authorization
        if auth:
            authenticated, role = check_auth(auth.username, auth.password)
            if authenticated:
                request.username = auth.username
                request.user_role = role
                return f(*args, **kwargs)
            if _is_api_request():
                return Response(
                    "Felaktigt användarnamn eller lösenord",
                    401,
                    {"WWW-Authenticate": 'Basic realm="doc-harbor"'},
                )

        if _is_api_request():
            return Response(
                "Logga in för att fortsätta",
                401,
                {"WWW-Authenticate": 'Basic realm="doc-harbor"'},
            )
        return redirect(url_for("auth.login_view", next=request.path))

    return decorated


def is_safe_username(username: str) -> bool:
    if not username:
        return False
    # Do not allow path traversal characters
    if ".." in username or "/" in username or "\\" in username:
        return False
    # Only allow safe characters for folder naming (alphanumeric, @, ., _, -, +)
    import re
    return bool(re.match(r"^[a-zA-Z0-9_@.+-]+$", username))

