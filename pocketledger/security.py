"""Authentication guard, CSRF protection, login throttling and security headers.

Default-deny: every endpoint requires a signed-in user unless it is listed in PUBLIC_ENDPOINTS.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlparse

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for

from .db import get_db

PUBLIC_ENDPOINTS = {
    "auth.login", "auth.setup", "pwa.manifest", "pwa.service_worker", "pwa.offline", "pwa.health", "static",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_failures: dict[str, list[float]] = {}


# ---- CSRF -----------------------------------------------------------------------------
def csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def _csrf_ok() -> bool:
    sent = request.headers.get("X-CSRFToken") or request.form.get("csrf_token") or ""
    expected = session.get("_csrf") or ""
    return bool(sent and expected and hmac.compare_digest(sent, expected))


# ---- password fingerprint (invalidates old sessions after a password change) ----------
def password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:20]


def login_user(user) -> None:
    """Start a fresh session (prevents session fixation)."""
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["pf"] = password_fingerprint(user["password_hash"])
    session["seen"] = int(time.time())
    csrf_token()


def logout_user() -> None:
    session.clear()


def _load_user():
    uid = session.get("uid")
    if not uid:
        return None
    idle = current_app.config["IDLE_TIMEOUT_MINUTES"] * 60
    if time.time() - session.get("seen", 0) > idle:
        session.clear()
        return None
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None or session.get("pf") != password_fingerprint(row["password_hash"]):
        session.clear()
        return None
    session["seen"] = int(time.time())
    return row


def safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects."""
    if not target:
        return url_for("dashboard.index")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return url_for("dashboard.index")
    return target


# ---- login throttling (per client address; in-memory, fine for a single-process app) ---
def _throttle_key() -> str:
    return request.remote_addr or "unknown"


def login_blocked() -> bool:
    window = current_app.config["LOGIN_LOCKOUT_MINUTES"] * 60
    now = time.time()
    hits = [t for t in _failures.get(_throttle_key(), []) if now - t < window]
    _failures[_throttle_key()] = hits
    return len(hits) >= current_app.config["LOGIN_MAX_FAILURES"]


def record_login_failure() -> None:
    _failures.setdefault(_throttle_key(), []).append(time.time())


def clear_login_failures() -> None:
    _failures.pop(_throttle_key(), None)


def reset_throttle() -> None:  # used by tests
    _failures.clear()


# ---- wiring --------------------------------------------------------------------------
def _wants_json() -> bool:
    return request.path.startswith("/api/")


def local_user():
    """No-login mode: the single built-in owner row (needed so audit logs have a user). Created on first use."""
    from .db import transaction
    conn = get_db()
    row = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    if row is None:
        from werkzeug.security import generate_password_hash
        from .timeutil import utcnow_iso
        with transaction(conn):
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
                conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                             ("owner", generate_password_hash(secrets.token_hex(32)), utcnow_iso()))
        row = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    return row


def init_app(app) -> None:
    @app.before_request
    def guard():
        if current_app.config.get("FORCE_HTTPS") and not request.is_secure:
            return redirect(request.url.replace("http://", "https://", 1), code=301)
        endpoint = request.endpoint
        if endpoint is None:
            return None  # unknown URL -> 404 handler
        if request.method not in SAFE_METHODS and not _csrf_ok():
            if _wants_json():
                return jsonify(error="Your session expired. Reload the page and try again.", code="csrf"), 400
            abort(400, description="Security token missing or expired. Go back, reload the page and try again.")
        if not current_app.config.get("REQUIRE_LOGIN", True):
            if endpoint in ("auth.login", "auth.setup"):
                return redirect(url_for("dashboard.index"))
            if endpoint not in PUBLIC_ENDPOINTS:
                user = local_user()
                g.user, g.user_id = user, user["id"]
            return None
        if endpoint in PUBLIC_ENDPOINTS:
            return None
        user = _load_user()
        if user is None:
            if _wants_json():
                return jsonify(error="Please sign in.", code="auth"), 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        g.user = user
        g.user_id = user["id"]
        return None

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'",
        )
        if current_app.config.get("HSTS"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint != "static" and request.endpoint not in ("pwa.manifest", "pwa.service_worker"):
            resp.headers["Cache-Control"] = "no-store"
        return resp
