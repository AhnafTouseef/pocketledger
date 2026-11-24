"""Configuration from environment variables (optionally loaded from a .env file)."""
from __future__ import annotations

import os
import secrets
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _path(value: str) -> str:
    p = Path(value).expanduser()
    return str(p if p.is_absolute() else (BASE_DIR / p).resolve())


def _dev_secret(instance_dir: Path) -> str:
    """Development only: generate a random key once and keep it in instance/.secret_key."""
    key_file = instance_dir / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def load_config() -> dict:
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass

    env = os.environ.get("POCKETLEDGER_ENV", "development").lower()
    production = env == "production"
    instance_dir = Path(_path(os.environ.get("INSTANCE_PATH", "instance")))
    instance_dir.mkdir(parents=True, exist_ok=True)

    secret = os.environ.get("SECRET_KEY", "").strip()
    if not secret:
        if production:
            raise RuntimeError(
                "SECRET_KEY must be set in production. Generate one with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        secret = _dev_secret(instance_dir)
    elif production and len(secret) < 32:
        raise RuntimeError("SECRET_KEY is too short; use at least 32 random characters.")

    https_only = _bool("SESSION_COOKIE_SECURE", production)
    return {
        "ENV_NAME": env,
        "SECRET_KEY": secret,
        "INSTANCE_DIR": str(instance_dir),
        "DATABASE": _path(os.environ.get("DATABASE_PATH") or str(instance_dir / "pocketledger.db")),
        "BACKUP_DIR": _path(os.environ.get("BACKUP_DIR") or str(instance_dir / "backups")),
        "UPLOAD_DIR": str(instance_dir / "uploads"),
        "MAX_CONTENT_LENGTH": _int("MAX_UPLOAD_MB", 50) * 1024 * 1024,
        # sessions
        "SESSION_COOKIE_NAME": "pl_session",
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": https_only,
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=_int("SESSION_HOURS", 12)),
        "IDLE_TIMEOUT_MINUTES": _int("IDLE_TIMEOUT_MINUTES", 120),
        # transport
        "FORCE_HTTPS": _bool("FORCE_HTTPS", False),
        "HSTS": https_only,
        "TRUSTED_PROXY_COUNT": _int("TRUSTED_PROXY_COUNT", 0),
        # login protection
        # Login screen on/off. Off (no password at all) by default for local use; on by default in production.
        "REQUIRE_LOGIN": _bool("REQUIRE_LOGIN", production),
        "MIN_PASSWORD_LENGTH": max(0, _int("MIN_PASSWORD_LENGTH", 6)),
        "LOGIN_MAX_FAILURES": _int("LOGIN_MAX_FAILURES", 5),
        "LOGIN_LOCKOUT_MINUTES": _int("LOGIN_LOCKOUT_MINUTES", 15),
        # first-run web setup: enabled in development; in production only with SETUP_TOKEN
        "SETUP_TOKEN": os.environ.get("SETUP_TOKEN", "").strip(),
        "ALLOW_WEB_SETUP": _bool("ALLOW_WEB_SETUP", not production),
        "SEND_FILE_MAX_AGE_DEFAULT": timedelta(days=0 if not production else 1),
        "TEMPLATES_AUTO_RELOAD": not production,
        "JSON_SORT_KEYS": False,
    }
