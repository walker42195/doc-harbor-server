import os
from pathlib import Path

APP_VERSION = "2.3.13"

ROOT = Path(os.environ.get("PROJECTS_ROOT", "/projects")).resolve()
DB_PATH = Path(os.environ.get("DB_PATH", "/projects/.doc-harbor.db")).resolve()

TEXT_EXTENSIONS = {
    ".md", ".txt", ".ts", ".js", ".mjs", ".json", ".dart", ".py",
    ".sh", ".bash", ".zsh", ".yaml", ".yml", ".html", ".css",
    ".kt", ".kts", ".java", ".gradle", ".toml", ".ini", ".cfg",
    ".xml", ".env", ".gitignore", ".lock", ".sql", ".swift",
    ".rs", ".go", ".rb", ".php", ".c", ".cpp", ".h", ".tsx", ".jsx",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}

MAX_VERSIONS = 20


def registration_enabled() -> bool:
    """Självregistrering via e-post (/register). Av som standard."""
    return os.environ.get("REGISTRATION_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
