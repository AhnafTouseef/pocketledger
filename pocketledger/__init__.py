"""PocketLedger application factory."""
from __future__ import annotations

import logging

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db, migrate, security
from .config import load_config
from .money import format_amount, format_money
from .services import settings as settings_svc
from .timeutil import humanize_ago, utc_iso_to_local

__version__ = "1.0.0"

NAV_ITEMS = [
    ("dashboard.index", "Dashboard", "layout-dashboard"),
    ("accounts.index", "Accounts", "wallet"),
    ("people.index", "People", "users"),
    ("transactions.index", "Transactions", "arrow-left-right"),
    ("analytics.index", "Analytics", "chart-column"),
    ("settings.index", "Settings", "settings"),
]


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)
    config = load_config() if not (overrides or {}).get("SKIP_ENV") else {}
    config.update(overrides or {})
    app.config.from_mapping(config)

    if app.config.get("TRUSTED_PROXY_COUNT"):
        n = app.config["TRUSTED_PROXY_COUNT"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)

    migrate.ensure_database(app.config["DATABASE"], app.config["BACKUP_DIR"])
    db.init_app(app)
    security.init_app(app)
    _register_template_helpers(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    from . import cli
    cli.register(app)
    if not app.debug and not app.testing:
        logging.basicConfig(level=logging.INFO)
    return app


def _register_blueprints(app: Flask) -> None:
    from .views import accounts, analytics, api, auth, dashboard, people, pwa, reconcile, settings, transactions

    for module in (auth, dashboard, accounts, people, transactions, reconcile, analytics, settings, api, pwa):
        app.register_blueprint(module.bp)


def _currency_row(code: str | None = None):
    """Per-request cache of currency rows."""
    cache = g.setdefault("_currencies", {})
    if not cache:
        for r in db.get_db().execute("SELECT * FROM currencies"):
            cache[r["code"]] = dict(r)
    if code is None:
        code = settings_svc.base_currency_code(db.get_db())
    return cache.get(code) or next(iter(cache.values()))


def _register_template_helpers(app: Flask) -> None:
    # A global (not a context-processor value) so macros imported from _macros.html can use it too.
    app.jinja_env.globals["csrf_token"] = security.csrf_token

    @app.template_filter("money")
    def money_filter(minor, currency=None):
        if minor is None:
            return "–"
        return format_money(int(minor), _currency_row(currency))

    @app.template_filter("amount")
    def amount_filter(minor, currency=None):
        c = _currency_row(currency)
        return format_amount(int(minor), c["minor_unit"], c["grouping"])

    @app.template_filter("amount_plain")
    def amount_plain(minor, currency=None):
        """Machine-readable decimal ('300.00') for prefilling form inputs."""
        from .money import to_decimal_string
        return to_decimal_string(int(minor), _currency_row(currency)["minor_unit"])

    @app.template_filter("localdt")
    def localdt(iso, fmt="%d %b %Y, %I:%M %p"):
        if not iso:
            return ""
        return utc_iso_to_local(iso, settings_svc.tzname(db.get_db())).strftime(fmt)

    @app.template_filter("localdate")
    def localdate(iso):
        return localdt(iso, "%d %b %Y")

    @app.template_filter("localinput")
    def localinput(iso):
        return localdt(iso, "%Y-%m-%dT%H:%M")

    @app.template_filter("ago")
    def ago(iso):
        return humanize_ago(iso)

    @app.template_filter("datefmt")
    def datefmt(value):
        """'2026-11-23' -> '23 Nov 2026'"""
        from datetime import date as _date
        try:
            return _date.fromisoformat(value).strftime("%d %b %Y")
        except (TypeError, ValueError):
            return value or ""

    @app.context_processor
    def inject():
        ctx = {"nav_items": NAV_ITEMS, "app_version": __version__}
        if g.get("user") is not None:
            conn = db.get_db()
            ctx["is_demo"] = settings_svc.is_demo(conn)
            ctx["current_user"] = g.user
        return ctx


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(HTTPException)
    def http_error(err: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(error=err.description or err.name, code=err.code), err.code
        return render_template("errors/error.html", code=err.code, title=err.name, message=err.description), err.code

    @app.errorhandler(Exception)
    def unexpected(err: Exception):
        app.logger.exception("Unhandled error")
        if request.path.startswith("/api/"):
            return jsonify(error="Something went wrong on the server. Nothing was saved unless you see a confirmation."), 500
        return render_template(
            "errors/error.html", code=500, title="Something went wrong",
            message="An unexpected error occurred. Your data was not changed by this request.",
        ), 500
