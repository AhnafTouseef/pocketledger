"""Forward-only SQL migrations: pocketledger/migrations/NNNN_name.sql, applied in order.

Each file runs inside one transaction together with its bookkeeping row, so a failed
migration leaves the database exactly as it was. Before upgrading a database that already
holds data, a copy is written next to the backups.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

from .db import connect
from .timeutil import day_start_utc, utcnow

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def discover() -> list[tuple[str, Path]]:
    found = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _NAME_RE.match(path.name)
        if m:
            found.append((m.group(1), path))
    return found


def latest_version() -> str:
    return discover()[-1][0]


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def applied_versions(conn: sqlite3.Connection) -> list[str]:
    _ensure_table(conn)
    return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def current_version(conn: sqlite3.Connection) -> str | None:
    versions = applied_versions(conn)
    return versions[-1] if versions else None


def pending(conn: sqlite3.Connection) -> list[tuple[str, Path]]:
    done = set(applied_versions(conn))
    return [(v, p) for v, p in discover() if v not in done]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply all pending migrations; returns the versions applied."""
    applied = []
    for version, path in pending(conn):
        sql = path.read_text(encoding="utf-8")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n"
            f"INSERT INTO schema_migrations (version, applied_at) VALUES ('{version}', '{utcnow().isoformat()}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    backfill_opening_at(conn)
    return applied


def backfill_opening_at(conn: sqlite3.Connection) -> int:
    """Give every account its opening moment (UTC start of its opening date, in the configured
    timezone). Idempotent; only touches accounts that don't have one yet."""
    from datetime import date
    if "opening_at" not in {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}:
        return 0
    rows = conn.execute("SELECT id, opened_on FROM accounts WHERE opening_at IS NULL").fetchall()
    if not rows:
        return 0
    tz_row = conn.execute("SELECT value FROM application_settings WHERE key = 'timezone'").fetchone()
    tz = tz_row[0] if tz_row else "Asia/Dhaka"
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            conn.execute("UPDATE accounts SET opening_at = ? WHERE id = ?",
                         (day_start_utc(date.fromisoformat(r[1]), tz), r[0]))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return len(rows)


def ensure_database(db_path: str, backup_dir: str | None = None) -> list[str]:
    """Create the database if needed and bring it to the latest schema."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed_with_data = path.exists() and path.stat().st_size > 0
    conn = connect(db_path)
    try:
        if existed_with_data and pending(conn) and backup_dir:
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
            stamp = utcnow().strftime("%Y%m%d-%H%M%S")
            conn.close()
            shutil.copy2(path, Path(backup_dir) / f"pre-migration-{stamp}.db")
            conn = connect(db_path)
        return migrate(conn)
    finally:
        conn.close()
