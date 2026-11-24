"""Command line tools: flask --app wsgi <command>"""
from __future__ import annotations

from pathlib import Path

import click
from flask import current_app
from flask.cli import with_appcontext
from werkzeug.security import generate_password_hash

from . import migrate
from .db import connect
from .services import backup, ledger
from .timeutil import utcnow, utcnow_iso


def _conn():
    return connect(current_app.config["DATABASE"])


def register(app):
    @app.cli.command("init-db")
    @with_appcontext
    def init_db():
        """Create or upgrade the database schema."""
        conn = _conn()
        try:
            click.echo(f"Database: {current_app.config['DATABASE']}")
            click.echo(f"Schema version: {migrate.current_version(conn)}")
        finally:
            conn.close()

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.password_option(confirmation_prompt=True)
    @with_appcontext
    def create_user(username, password):
        """Create the (single) login. Use this instead of web setup in production."""
        from .views.auth import validate_new_password
        problem = validate_new_password(password, password, username)
        if problem:
            raise click.ClickException(problem)
        conn = _conn()
        try:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise click.ClickException("A user already exists. Use reset-password to change the password.")
            conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                         (username.strip(), generate_password_hash(password), utcnow_iso()))
            click.echo("User created.")
        finally:
            conn.close()

    @app.cli.command("reset-password")
    @click.password_option(confirmation_prompt=True)
    @with_appcontext
    def reset_password(password):
        """Set a new password for the existing user (invalidates all sessions)."""
        from .views.auth import validate_new_password
        conn = _conn()
        try:
            user = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
            if user is None:
                raise click.ClickException("No user exists yet. Run create-user.")
            problem = validate_new_password(password, password, user["username"])
            if problem:
                raise click.ClickException(problem)
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user["id"]))
            click.echo(f"Password updated for {user['username']}.")
        finally:
            conn.close()

    @app.cli.command("backup")
    @with_appcontext
    def backup_cmd():
        """Write a consistent database backup into the backup folder."""
        cfg = current_app.config
        dest = Path(cfg["BACKUP_DIR"]) / f"pocketledger-{utcnow().strftime('%Y%m%d-%H%M%S')}.db"
        backup.create_db_backup(cfg["DATABASE"], str(dest))
        backup.rotate(cfg["BACKUP_DIR"])
        click.echo(f"Backup written: {dest}")

    @app.cli.command("check")
    @with_appcontext
    def check():
        """Audit the ledger: balanced entries, derived balances, net-worth identity."""
        conn = _conn()
        try:
            issues = ledger.verify_integrity(conn)
        finally:
            conn.close()
        if issues:
            for i in issues:
                click.echo(f"PROBLEM: {i}")
            raise SystemExit(1)
        click.echo("Ledger OK.")
