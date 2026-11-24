"""Single-user authentication: first-run setup, login, logout, password change."""
from __future__ import annotations

import hmac

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import security
from ..db import get_db, transaction
from ..services import audit
from ..timeutil import utcnow_iso

bp = Blueprint("auth", __name__)

_DUMMY_HASH = generate_password_hash("not-a-real-password")


def _has_user(conn) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def validate_new_password(password: str, confirm: str, username: str = "") -> str | None:
    minimum = current_app.config["MIN_PASSWORD_LENGTH"]
    if len(password) < minimum:
        return f"Use at least {minimum} characters."
    if password != confirm:
        return "The two passwords don't match."
    if minimum == 0:          # you asked for no password rules: nothing else is checked
        return None
    if username and username.lower() in password.lower():
        return "The password shouldn't contain your username."
    if password.lower() in {"password123", "1234567890", "qwertyuiop", "pocketledger"}:
        return "That password is too common."
    return None


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    conn = get_db()
    if _has_user(conn):
        return redirect(url_for("auth.login"))
    if not current_app.config["ALLOW_WEB_SETUP"]:
        return render_template(
            "errors/error.html", code=403, title="Setup is disabled",
            message="Create the account from the server with: flask --app wsgi create-user",
        ), 403
    token_required = bool(current_app.config["SETUP_TOKEN"])
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        supplied = request.form.get("setup_token", "")
        if token_required and not hmac.compare_digest(supplied, current_app.config["SETUP_TOKEN"]):
            error = "The setup token is wrong."
        elif not (3 <= len(username) <= 40):
            error = "Choose a username between 3 and 40 characters."
        else:
            error = validate_new_password(password, confirm, username)
        if error is None:
            with transaction(conn):
                if _has_user(conn):
                    return redirect(url_for("auth.login"))
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), utcnow_iso()),
                )
                audit.log(conn, "create", "user", cur.lastrowid, after={"username": username}, user_id=cur.lastrowid)
            flash("Account created. Sign in to continue.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/setup.html", error=error, token_required=token_required)


@bp.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    if not _has_user(conn):
        return redirect(url_for("auth.setup"))
    error = None
    if request.method == "POST":
        if security.login_blocked():
            error = f"Too many failed attempts. Try again in {current_app.config['LOGIN_LOCKOUT_MINUTES']} minutes."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            # always run one hash check so timing doesn't reveal whether the username exists
            ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
            if user is not None and ok:
                security.clear_login_failures()
                security.login_user(user)
                return redirect(security.safe_next(request.args.get("next")))
            security.record_login_failure()
            error = "Wrong username or password."
    return render_template("auth/login.html", error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    security.logout_user()
    flash("You've been signed out.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/settings/password", methods=["POST"])
def change_password():
    conn = get_db()
    current = request.form.get("current", "")
    new = request.form.get("new", "")
    confirm = request.form.get("confirm", "")
    user = g.user
    if not check_password_hash(user["password_hash"], current):
        flash("Your current password is wrong.", "error")
    else:
        problem = validate_new_password(new, confirm, user["username"])
        if problem:
            flash(problem, "error")
        else:
            new_hash = generate_password_hash(new)
            with transaction(conn):
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
                audit.log(conn, "change_password", "user", user["id"])
            security.login_user({"id": user["id"], "password_hash": new_hash})
            flash("Password changed. Other sessions were signed out.", "success")
    return redirect(url_for("settings.index") + "#security")
