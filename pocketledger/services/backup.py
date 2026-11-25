"""Backup, validation and restoration.

* Database backup: a consistent copy made with SQLite's online backup API.
* JSON export/import: portable, human-inspectable copy of every ledger table.
* Restoring never edits the live database in place. The candidate is built/validated in a
  temporary file, the current database is saved first, and only then is it swapped in.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .. import migrate
from ..db import connect
from ..timeutil import utcnow, utcnow_iso
from . import ledger
from .errors import ValidationError

REQUIRED_TABLES = {
    "users", "accounts", "account_categories", "people", "transactions", "transaction_entries",
    "reconciliations", "reconciliation_entries", "transaction_categories", "audit_logs",
    "application_settings", "currencies", "schema_migrations",
}
# dependency order for JSON import; users is intentionally excluded (the current login is kept)
JSON_TABLES = [
    "currencies", "account_categories", "accounts", "people", "transaction_categories",
    "reconciliations", "transactions", "transaction_entries", "reconciliation_entries",
    "audit_logs", "application_settings",
]
FORMAT = "pocketledger-export"
FORMAT_VERSION = 1


def _stamp():
    return utcnow().strftime("%Y%m%d-%H%M%S")


# ---- database backup ------------------------------------------------------------------
def create_db_backup(src_path: str, dest_path: str) -> str:
    """Consistent snapshot of the live DB into dest_path (works while the app is running)."""
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(src_path, timeout=15)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest_path


def rotate(backup_dir: str, keep: int = 15, prefix: str = "pocketledger-") -> None:
    files = sorted(Path(backup_dir).glob(f"{prefix}*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def list_backups(backup_dir: str):
    d = Path(backup_dir)
    if not d.exists():
        return []
    items = []
    for p in sorted(d.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime})
    return items


# ---- validation -------------------------------------------------------------------------
def validate_database_file(path: str) -> dict:
    """Open a candidate database read-only and check it is a sound PocketLedger ledger."""
    report = {"ok": False, "errors": [], "warnings": [], "stats": {}}
    try:
        with open(path, "rb") as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                report["errors"].append("This file is not a SQLite database.")
                return report
    except OSError as exc:
        report["errors"].append(f"Cannot read the file: {exc}")
        return report
    try:
        conn = connect(path, readonly=True)
    except sqlite3.Error as exc:
        report["errors"].append(f"Cannot open the database: {exc}")
        return report
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            report["errors"].append("Missing tables: " + ", ".join(sorted(missing)))
            return report
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        known = {v for v, _ in migrate.discover()}
        unknown = [v for v in versions if v not in known]
        if unknown:
            report["errors"].append(
                f"This backup was made by a newer PocketLedger (schema {unknown[-1]}). Update the app first."
            )
            return report
        if versions and versions[-1] != migrate.latest_version():
            report["warnings"].append(
                f"Older schema ({versions[-1]}); it will be upgraded to {migrate.latest_version()} on restore."
            )
        report["stats"] = {
            "accounts": conn.execute("SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL").fetchone()[0],
            "people": conn.execute("SELECT COUNT(*) FROM people WHERE deleted_at IS NULL").fetchone()[0],
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            "reconciliations": conn.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0],
            "schema_version": versions[-1] if versions else None,
        }
        report["errors"].extend(ledger.verify_integrity(conn))
    except sqlite3.Error as exc:
        report["errors"].append(f"Database error while validating: {exc}")
    finally:
        conn.close()
    report["ok"] = not report["errors"]
    return report


# ---- restore from a database file ---------------------------------------------------------
def restore_database(candidate_path: str, live_path: str, backup_dir: str) -> str:
    """Replace the live database with a validated candidate. Returns the safety-backup path.
    The caller must have closed its own connections to live_path first."""
    report = validate_database_file(candidate_path)
    if not report["ok"]:
        raise ValidationError("The backup failed validation: " + "; ".join(report["errors"][:3]))
    safety = create_db_backup(live_path, str(Path(backup_dir) / f"pre-restore-{_stamp()}.db"))
    staged = str(Path(live_path).with_suffix(".restore-staging"))
    shutil.copyfile(candidate_path, staged)
    # Upgrade older schemas in the staged copy (never in the uploaded original) and carry the
    # CURRENT login over, so restoring an old backup can never lock you out. The login is
    # upserted onto the same user id, which keeps every created_by / user_id reference valid.
    live = sqlite3.connect(live_path)
    live.row_factory = sqlite3.Row
    try:
        live_users = [dict(r) for r in live.execute("SELECT * FROM users")]
    finally:
        live.close()
    conn = connect(staged)
    try:
        migrate.migrate(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for u in live_users:
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET username = excluded.username, password_hash = excluded.password_hash",
                    (u["id"], u["username"], u["password_hash"], u["created_at"]),
                )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise ValidationError(f"Could not carry your login over to the restored data: {exc}")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.remove(live_path + suffix)
        except OSError:
            pass
    os.replace(staged, live_path)
    return safety


# ---- JSON export / import --------------------------------------------------------------------
def export_json(conn) -> dict:
    tables = {}
    for name in JSON_TABLES:
        tables[name] = [dict(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY 1")]
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "schema_version": migrate.current_version(conn),
        "exported_at": utcnow_iso(),
        "tables": tables,
    }


def _table_columns(conn, name):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({name})")]


def import_json_to_staging(data, staging_path: str, keep_users_from: str) -> dict:
    """Build a fresh database from a JSON export in staging_path and validate it."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValidationError("This isn't a PocketLedger JSON export.")
    if data.get("format_version") != FORMAT_VERSION:
        raise ValidationError(f"Unsupported export version {data.get('format_version')}.")
    tables = data.get("tables")
    if not isinstance(tables, dict) or not all(isinstance(tables.get(t, []), list) for t in JSON_TABLES):
        raise ValidationError("The export is missing ledger tables or is malformed.")
    if os.path.exists(staging_path):
        os.remove(staging_path)
    conn = connect(staging_path)
    try:
        migrate.migrate(conn)
        known = {v for v, _ in migrate.discover()}
        if data.get("schema_version") and data["schema_version"] not in known:
            raise ValidationError("The export was made by a newer PocketLedger. Update the app first.")
        src_users = sqlite3.connect(keep_users_from)
        src_users.row_factory = sqlite3.Row
        try:
            users = [dict(r) for r in src_users.execute("SELECT * FROM users")]
        finally:
            src_users.close()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for t in ("application_settings", "transaction_categories", "account_categories", "currencies"):
                conn.execute(f"DELETE FROM {t}")
            for u in users:
                conn.execute("INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
                             (u["id"], u["username"], u["password_hash"], u["created_at"]))
            owner_id = users[0]["id"] if users else None
            valid_users = {u["id"] for u in users}
            for name in JSON_TABLES:
                cols = _table_columns(conn, name)
                for row in tables.get(name, []):
                    if not isinstance(row, dict):
                        raise ValidationError(f"Malformed row in {name}.")
                    unknown = set(row) - set(cols)
                    if unknown:
                        raise ValidationError(f"Unknown column(s) in {name}: {', '.join(sorted(unknown))}.")
                    row = dict(row)
                    for ucol in ("user_id", "created_by"):
                        if ucol in row and row[ucol] is not None and row[ucol] not in valid_users:
                            row[ucol] = owner_id
                    keys = list(row)
                    conn.execute(
                        f"INSERT INTO {name} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                        [row[k] for k in keys],
                    )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        migrate.backfill_opening_at(conn)
    except sqlite3.Error as exc:
        raise ValidationError(f"The export could not be loaded: {exc}")
    finally:
        conn.close()
    return validate_database_file(staging_path)


def prune_old_files(directory: str, max_age_seconds: int = 3600) -> None:
    d = Path(directory)
    if not d.exists():
        return
    now = utcnow().timestamp()
    for p in d.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > max_age_seconds:
                p.unlink()
        except OSError:
            pass
