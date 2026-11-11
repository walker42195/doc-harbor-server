import os
import secrets
from datetime import timedelta
from flask import Flask, request as flask_request
from config import APP_VERSION, registration_enabled
from db import init_db


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    if not os.environ.get("SECRET_KEY"):
        app.logger.warning(
            "SECRET_KEY saknas – en slumpad nyckel används, så alla loggas ut vid omstart. "
            "Sätt SECRET_KEY i .env (t.ex. openssl rand -hex 32)."
        )
    # SameSite=Lax: webbläsaren skickar inte inloggningskakan med formulär från andra sajter (CSRF).
    # Inloggningen i webben gäller i 90 dagar (session.permanent), så att man inte
    # behöver ange lösenord och kod varje gång.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=90),
        # Sätt SESSION_COOKIE_SECURE=true när tjänsten nås via HTTPS (rekommenderas).
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").strip().lower()
        in ("1", "true", "yes", "on"),
    )

    from helpers import fmt_size, fmt_time, _markdown_render
    app.jinja_env.globals["fmt_size"] = fmt_size
    app.jinja_env.globals["fmt_time"] = fmt_time
    app.jinja_env.filters["markdown_render"] = _markdown_render

    @app.context_processor
    def inject_globals():
        import update_check
        security_warnings = []
        if getattr(flask_request, "user_role", None) == "admin" and flask_request.username:
            from auth import admin_security_warnings
            security_warnings = admin_security_warnings(flask_request.username)
        return {
            "app_version": APP_VERSION,
            "registration_enabled": registration_enabled(),
            "security_warnings": security_warnings,
            **update_check.status(),
        }

    @app.before_request
    def before_request():
        flask_request.user_role = None
        flask_request.username = None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # Användarnas egna HTML- och SVG-filer visas utan att skript körs (sandbox utan
        # allow-scripts), så att en fil i en delad mapp inte kan agera som den som öppnar den.
        if response.mimetype in ("text/html", "image/svg+xml") and flask_request.path.startswith(
            ("/raw/", "/image/", "/api/file/")
        ):
            response.headers["Content-Security-Policy"] = (
                "sandbox allow-same-origin allow-popups allow-popups-to-escape-sandbox; "
                "script-src 'none'; object-src 'none'"
            )
        return response

    @app.after_request
    def add_no_cache_headers(response):
        # Prevent Cloudflare and browser caching for API endpoints, JSON responses, APK downloads, and dynamic pages
        path = flask_request.path
        if path.startswith('/api/') or path in ['/version.json', '/doc-harbor.apk'] or not path.startswith('/static/'):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0, s-maxage=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            response.headers["CDN-Cache-Control"] = "no-store"
            response.headers["Cloudflare-CDN-Cache-Control"] = "no-store"
        return response

    from routes.setup_routes import bp as setup_bp
    from routes.auth_routes import bp as auth_bp
    from routes.register_routes import bp as register_bp
    from routes.file_routes import bp as file_bp
    from routes.user_routes import bp as user_bp
    from routes.group_routes import bp as group_bp
    from routes.api_routes import bp as api_bp
    from routes.todo_routes import bp as todo_bp
    from routes.kanban_routes import bp as kanban_bp
    from routes.backup_routes import bp as backup_bp

    app.register_blueprint(setup_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(register_bp)
    app.register_blueprint(file_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(group_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(todo_bp)
    app.register_blueprint(kanban_bp)
    app.register_blueprint(backup_bp)

    from config import ROOT, DB_PATH
    from db import check_storage_writable
    for problem in check_storage_writable(ROOT, DB_PATH):
        app.logger.error(problem)

    init_db()
    from auth import sync_admin_password_from_env
    sync_admin_password_from_env()

    import update_check
    update_check.start()
    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
