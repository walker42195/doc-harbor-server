import os
import hashlib
import hmac
import secrets
import functools
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from flask import request, redirect, url_for, Response, session
from db import get_db


MIN_STRONG_PASSWORD = 12


def is_weak_password(password: str) -> bool:
    return len(password or "") < MIN_STRONG_PASSWORD


def sync_admin_password_from_env():
    """Skapar admin från ADMIN_PASSWORD, eller återställer lösenordet när värdet ändras.

    Tidigare återställdes lösenordet vid varje nytt bygge, vilket skrev över ett
    lösenord som admin själv hade bytt till. Nu sparas en saltad hash av
    ADMIN_PASSWORD och lösenordet återställs bara om admin saknas eller om
    värdet i ADMIN_PASSWORD har ändrats sedan förra gången. Ändrat värde
    stänger också av kodkravet för admin (återställning om admin låst ute sig).
    """
    admin_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not admin_pw:
        return

    conn = get_db()
    admin_user = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key = 'admin_env_password'"
    ).fetchone()
    env_unchanged = False
    if row and ":" in row["value"]:
        salt, digest = row["value"].split(":", 1)
        env_unchanged = verify_password(admin_pw, digest, salt)

    if admin_user and env_unchanged:
        conn.close()
        return

    pw_hash, salt = hash_password(admin_pw)
    if admin_user:
        # Ett nytt ADMIN_PASSWORD är också vägen in om admin har låst ute sig
        # (t.ex. fel e-postadress med kodkrav), därför stängs kodkravet av här.
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, role = 'admin', pw_weak = 1, mfa_email = 0 WHERE username = 'admin'",
            (pw_hash, salt),
        )
    else:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role, pw_weak) VALUES ('admin', ?, ?, 'admin', 1)",
            (pw_hash, salt),
        )
    env_hash, env_salt = hash_password(admin_pw)
    conn.execute(
        "INSERT INTO system_settings (key, value) VALUES ('admin_env_password', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"{env_salt}:{env_hash}",),
    )
    conn.execute("DELETE FROM system_settings WHERE key = 'last_admin_reset_build'")
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


def requires_mfa(username: str) -> bool:
    """True om kontot kräver kod via e-post vid inloggning."""
    conn = get_db()
    row = conn.execute(
        "SELECT mfa_email, email FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    return bool(row and row["mfa_email"] and row["email"])


def auth_epoch(username: str) -> int:
    conn = get_db()
    row = conn.execute("SELECT auth_epoch FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row["auth_epoch"] if row else 0


def revoke_logins(username: str) -> None:
    """Loggar ut kontot överallt: appar (tokens), väntande koder och webbsessioner."""
    conn = get_db()
    conn.execute("DELETE FROM tokens WHERE username = ?", (username,))
    conn.execute("DELETE FROM login_codes WHERE username = ?", (username,))
    conn.execute("UPDATE users SET auth_epoch = auth_epoch + 1 WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def create_token(username: str, mfa: bool = False) -> str:
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now().timestamp()
    conn = get_db()
    conn.execute(
        "INSERT INTO tokens (token_hash, username, created_at, last_used, mfa) VALUES (?, ?, ?, ?, ?)",
        (token_hash, username, now, now, 1 if mfa else 0),
    )
    conn.commit()
    conn.close()
    return token


def verify_token(token: str):
    """Returnerar (användarnamn, roll) för ett giltigt token, annars (None, None).

    Ett token som utfärdades utan kodverifiering gäller inte för ett konto som
    kräver kod (t.ex. om kodkravet slogs på efteråt).
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn = get_db()
    row = conn.execute(
        "SELECT t.username, t.mfa, u.role, u.mfa_email, u.email FROM tokens t "
        "JOIN users u ON t.username = u.username WHERE t.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row and row["mfa_email"] and row["email"] and not row["mfa"]:
        row = None
    if row:
        conn.execute(
            "UPDATE tokens SET last_used = ? WHERE token_hash = ?",
            (datetime.now().timestamp(), token_hash),
        )
        conn.commit()
    conn.close()
    return (row["username"], row["role"]) if row else (None, None)


# ── Inloggningskod via e-post ────────────────────────────────────────────────
LOGIN_CODE_TTL = 10 * 60      # koden gäller i 10 minuter
LOGIN_CODE_RESEND = 60        # ny kod tidigast efter 60 s
LOGIN_CODE_MAX_ATTEMPTS = 5   # sedan måste inloggningen börja om


class LoginCodeError(Exception):
    """Fel i kodsteget. restart=True betyder att inloggningen måste börja om med lösenordet."""

    def __init__(self, message: str, restart: bool = False):
        super().__init__(message)
        self.restart = restart


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _send_login_code(email: str, code: str) -> None:
    from mailer import send_mail
    send_mail(
        email,
        f"Din inloggningskod: {code} — doc-harbor",
        f"Din inloggningskod för doc-harbor är: {code}\n\n"
        f"Koden gäller i {LOGIN_CODE_TTL // 60} minuter.\n\n"
        f"Om det inte var du som loggade in: byt lösenord direkt, "
        f"eftersom någon känner till ditt lösenord.",
    )


def start_login_challenge(username: str) -> tuple[str, str]:
    """Skapar en inloggningskod, mejlar den och returnerar (challenge, maskad e-post).

    Anropas först när lösenordet är kontrollerat. Kastar LoginCodeError om
    mejlet inte kan skickas.
    """
    from mailer import mask_email
    conn = get_db()
    row = conn.execute("SELECT email FROM users WHERE username = ?", (username,)).fetchone()
    if not row or not row["email"]:
        conn.close()
        raise LoginCodeError("Kontot saknar e-postadress.")
    email = row["email"]
    now = time.time()
    challenge = secrets.token_urlsafe(32)
    code = str(secrets.randbelow(10 ** 6)).zfill(6)
    conn.execute("DELETE FROM login_codes WHERE expires_at < ?", (now,))
    conn.execute(
        "INSERT INTO login_codes (challenge_hash, username, code_hash, created_at, expires_at, last_sent) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_sha(challenge), username, _sha(challenge + code), now, now + LOGIN_CODE_TTL, now),
    )
    conn.commit()
    conn.close()
    try:
        _send_login_code(email, code)
    except Exception as e:
        logging.error("Kunde inte skicka inloggningskod till %s: %s", username, e)
        raise LoginCodeError("Kunde inte skicka koden via e-post. Försök igen senare.")
    return challenge, mask_email(email)


def resend_login_code(challenge: str) -> str:
    """Skickar en ny kod för samma inloggningsförsök. Returnerar maskad e-post."""
    from mailer import mask_email
    now = time.time()
    conn = get_db()
    row = conn.execute(
        "SELECT lc.id, lc.last_sent, lc.expires_at, u.email FROM login_codes lc "
        "JOIN users u ON u.username = lc.username WHERE lc.challenge_hash = ?",
        (_sha(challenge),),
    ).fetchone()
    if not row or row["expires_at"] < now:
        conn.close()
        raise LoginCodeError("Inloggningen har gått ut. Logga in igen.", restart=True)
    if now - row["last_sent"] < LOGIN_CODE_RESEND:
        conn.close()
        raise LoginCodeError("Vänta en minut innan du begär en ny kod.")
    code = str(secrets.randbelow(10 ** 6)).zfill(6)
    conn.execute(
        "UPDATE login_codes SET code_hash = ?, last_sent = ?, expires_at = ?, attempts = 0 WHERE id = ?",
        (_sha(challenge + code), now, now + LOGIN_CODE_TTL, row["id"]),
    )
    conn.commit()
    conn.close()
    try:
        _send_login_code(row["email"], code)
    except Exception as e:
        logging.error("Kunde inte skicka ny inloggningskod: %s", e)
        raise LoginCodeError("Kunde inte skicka koden via e-post. Försök igen senare.")
    return mask_email(row["email"])


def verify_login_code(challenge: str, code: str) -> tuple[str, str]:
    """Kontrollerar koden. Returnerar (användarnamn, roll) eller kastar LoginCodeError."""
    now = time.time()
    code = (code or "").strip().replace(" ", "")
    conn = get_db()
    row = conn.execute(
        "SELECT lc.id, lc.username, lc.code_hash, lc.expires_at, lc.attempts, u.role FROM login_codes lc "
        "JOIN users u ON u.username = lc.username WHERE lc.challenge_hash = ?",
        (_sha(challenge or ""),),
    ).fetchone()
    if not row:
        conn.close()
        raise LoginCodeError("Inloggningen har gått ut. Logga in igen.", restart=True)
    if login_blocked(row["username"]):
        conn.close()
        raise LoginCodeError("För många misslyckade försök. Vänta en stund och försök igen.")
    if row["expires_at"] < now:
        conn.execute("DELETE FROM login_codes WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        raise LoginCodeError("Koden har gått ut. Logga in igen.", restart=True)
    if not hmac.compare_digest(_sha(challenge + code), row["code_hash"]):
        _record_failure(row["username"])
        attempts = row["attempts"] + 1
        if attempts >= LOGIN_CODE_MAX_ATTEMPTS:
            conn.execute("DELETE FROM login_codes WHERE id = ?", (row["id"],))
            conn.commit()
            conn.close()
            raise LoginCodeError("För många felaktiga koder. Logga in igen.", restart=True)
        conn.execute("UPDATE login_codes SET attempts = ? WHERE id = ?", (attempts, row["id"]))
        conn.commit()
        conn.close()
        raise LoginCodeError("Felaktig kod.")
    conn.execute("DELETE FROM login_codes WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return row["username"], row["role"]


def admin_security_warnings(username: str) -> list[str]:
    """Rekommendationer som visas för en inloggad admin om det egna kontot."""
    conn = get_db()
    row = conn.execute(
        "SELECT email, mfa_email, pw_weak FROM users WHERE username = ? AND role = 'admin'",
        (username,),
    ).fetchone()
    conn.close()
    if not row:
        return []
    warnings = []
    if not row["email"]:
        warnings.append(
            "Lägg till en e-postadress på ditt admin-konto, så att du kan slå på kodverifiering via e-post."
        )
    elif not row["mfa_email"]:
        warnings.append("Slå på kodverifiering via e-post för ditt admin-konto.")
    if row["pw_weak"]:
        warnings.append(
            f"Byt admin-lösenordet till ett säkert lösenord (minst {MIN_STRONG_PASSWORD} tecken)."
        )
    return warnings


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
                "SELECT role, mfa_email, email, auth_epoch FROM users WHERE username = ?", (session["username"],)
            ).fetchone()
            conn.close()
            # En session utan kodverifiering gäller inte för ett konto som kräver kod
            # (t.ex. om kodkravet slogs på efter inloggningen).
            if row and row["mfa_email"] and row["email"] and not session.get("mfa"):
                row = None
            # Lösenordet har återställts av admin efter inloggningen.
            if row and row["auth_epoch"] != session.get("epoch", 0):
                row = None
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
                # verify_token har redan kontrollerat kodkravet: för ett konto med kodkrav
                # är tokenet kodverifierat. Utan kodkrav sätts False, så att sessionen
                # slutar gälla om kodkravet slås på senare.
                session["mfa"] = requires_mfa(username)
                session["epoch"] = auth_epoch(username)
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
            if authenticated and requires_mfa(auth.username):
                # Basic Auth skickar bara lösenordet – det skulle gå förbi kodkravet.
                return Response(
                    "Kontot kräver kodverifiering via e-post. Logga in i appen eller webben.",
                    401,
                )
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

