import sqlite3
from config import DB_PATH


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_used REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_by TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_members (
            group_id TEXT NOT NULL REFERENCES groups(id),
            username TEXT NOT NULL REFERENCES users(username),
            role TEXT NOT NULL DEFAULT 'member',
            joined_at REAL NOT NULL,
            PRIMARY KEY (group_id, username)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # Inloggningskoder via e-post. Varje inloggningsförsök får en "challenge"
    # (slumpad, skickas till klienten) och en 6-siffrig kod (skickas per mejl).
    # Båda lagras bara som hash.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenge_hash TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            last_sent REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
        )
    """)
    _add_column(conn, "users", "email", "TEXT")
    # 1 = inloggning kräver kod via e-post (styrs per konto av admin)
    _add_column(conn, "users", "mfa_email", "INTEGER NOT NULL DEFAULT 0")
    # 1 = lösenordet är för svagt eller satt från ADMIN_PASSWORD → rekommendation visas
    _add_column(conn, "users", "pw_weak", "INTEGER NOT NULL DEFAULT 0")
    # 1 = token utfärdat efter kodverifiering (krävs för konton med kodkrav)
    _add_column(conn, "tokens", "mfa", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def _add_column(conn, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def check_storage_writable(root, db_path) -> list[str]:
    """Returnerar felmeddelanden om data- eller databasmappen inte är skrivbar.

    Containern kör som en vanlig användare (uid 10001). En datamapp som skapats
    av en äldre version (som körde som root) måste därför byta ägare.
    """
    import os
    problems = []
    for d in (root, db_path.parent):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not os.access(d, os.W_OK):
            problems.append(
                f"Mappen {d} är inte skrivbar för uid {os.getuid()}. "
                f"Kör på värden: sudo chown -R {os.getuid()}:{os.getgid()} <datamappen>"
            )
    return problems
