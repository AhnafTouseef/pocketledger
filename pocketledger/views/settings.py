"""Settings: preferences, categories, backup & restore, audit trail, integrity check."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session,
                   url_for)

from .. import db as db_module
from ..db import get_db
from ..services import audit, backup, categories, ledger, settings as svc
from ..services.errors import NotFoundError, ServiceError, ValidationError
from ..timeutil import utcnow
from . import to_int

bp = Blueprint("settings", __name__, url_prefix="/settings")

_BACKUP_NAME = re.compile(r"^[A-Za-z0-9._-]+\.db$")


def _stamp():
    return utcnow().strftime("%Y%m%d-%H%M%S")


def _upload_dir() -> Path:
    p = Path(current_app.config["UPLOAD_DIR"])
    p.mkdir(parents=True, exist_ok=True)
    return p


@bp.route("/")
def index():
    conn = get_db()
    recent, _ = audit.list_logs(conn, limit=6)
    return render_template(
        "settings/index.html", prefs=svc.get_all(conn),
        tx_categories=categories.list_transaction_categories(conn, include_archived=True),
        account_categories=categories.list_account_categories(conn, include_archived=True),
        backups=backup.list_backups(current_app.config["BACKUP_DIR"]), recent_audit=recent,
        week_starts=svc.WEEK_STARTS,
    )


@bp.route("/preferences", methods=["POST"])
def preferences():
    try:
        from ..db import transaction
        with transaction(get_db()) as conn:
            svc.update_preferences(
                conn, timezone_name=request.form.get("timezone", "").strip(), week_start=request.form.get("week_start", ""),
                verify_reminder_days=request.form.get("verify_reminder_days", ""), due_soon_days=request.form.get("due_soon_days", ""),
            )
            audit.log(conn, "update", "settings", None, after=dict(request.form.items()))
        flash("Preferences saved.", "success")
    except ServiceError as exc:
        flash(exc.message, "error")
    return redirect(url_for("settings.index") + "#preferences")


# ---- categories ---------------------------------------------------------------------------
@bp.route("/categories/<scope>/add", methods=["POST"])
def category_add(scope):
    conn = get_db()
    try:
        if scope == "tx":
            categories.add_transaction_category(conn, request.form.get("name"), request.form.get("kind"))
        elif scope == "account":
            categories.add_account_category(conn, request.form.get("name"), request.form.get("kind"))
        else:
            abort(404)
        flash("Added.", "success")
    except ServiceError as exc:
        flash(exc.message, "error")
    return redirect(url_for("settings.index") + "#categories")


@bp.route("/categories/<scope>/<int:cat_id>/<action>", methods=["POST"])
def category_action(scope, cat_id, action):
    conn = get_db()
    try:
        if scope == "tx":
            if action == "rename":
                categories.rename_transaction_category(conn, cat_id, request.form.get("name"))
            elif action in ("archive", "unarchive"):
                categories.set_transaction_category_archived(conn, cat_id, action == "archive")
            else:
                abort(404)
        elif scope == "account":
            if action == "rename":
                categories.rename_account_category(conn, cat_id, request.form.get("name"))
            elif action in ("archive", "unarchive"):
                categories.set_account_category_archived(conn, cat_id, action == "archive")
            else:
                abort(404)
        else:
            abort(404)
        flash("Saved.", "success")
    except NotFoundError:
        abort(404)
    except ServiceError as exc:
        flash(exc.message, "error")
    return redirect(url_for("settings.index") + "#categories")


# ---- backup ---------------------------------------------------------------------------------
@bp.route("/backup/save", methods=["POST"])
def backup_save():
    cfg = current_app.config
    dest = Path(cfg["BACKUP_DIR"]) / f"pocketledger-{_stamp()}.db"
    backup.create_db_backup(cfg["DATABASE"], str(dest))
    backup.rotate(cfg["BACKUP_DIR"])
    audit.log(get_db(), "backup", "database", None, after={"file": dest.name})
    flash(f"Backup saved on the server as {dest.name}. Download a copy too, so it lives somewhere else.", "success")
    return redirect(url_for("settings.index") + "#backup")


@bp.route("/backup/download.db")
def backup_download_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", dir=_upload_dir())
    os.close(fd)
    try:
        backup.create_db_backup(current_app.config["DATABASE"], tmp)
        data = Path(tmp).read_bytes()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    audit.log(get_db(), "export", "database", None, after={"format": "sqlite"})
    return Response(data, mimetype="application/vnd.sqlite3",
                    headers={"Content-Disposition": f'attachment; filename="pocketledger-{_stamp()}.db"'})


@bp.route("/backup/download.json")
def backup_download_json():
    conn = get_db()
    payload = backup.export_json(conn)
    audit.log(conn, "export", "database", None, after={"format": "json"})
    return Response(json.dumps(payload, ensure_ascii=False, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="pocketledger-{_stamp()}.json"'})


@bp.route("/backup/saved/<name>")
def backup_download_saved(name):
    if not _BACKUP_NAME.match(name):
        abort(404)
    path = Path(current_app.config["BACKUP_DIR"]) / name
    if not path.is_file():
        abort(404)
    return Response(path.read_bytes(), mimetype="application/vnd.sqlite3",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- restore: stage -> review -> typed confirmation -> swap -----------------------------------
def _discard_staged():
    info = session.pop("staged_restore", None)
    if info:
        try:
            os.remove(_upload_dir() / info["file"])
        except OSError:
            pass


def _live_stats(conn):
    return {
        "accounts": conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
        "people": conn.execute("SELECT COUNT(*) FROM people").fetchone()[0],
        "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "reconciliations": conn.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0],
    }


def _stage_and_review(file_bytes: bytes, source_name: str):
    conn = get_db()
    backup.prune_old_files(str(_upload_dir()))
    _discard_staged()
    staged_name = f"staged-{secrets.token_hex(8)}.db"
    staged_path = _upload_dir() / staged_name
    try:
        if file_bytes[:16] == b"SQLite format 3\x00":
            staged_path.write_bytes(file_bytes)
            report = backup.validate_database_file(str(staged_path))
            kind = "database file"
        else:
            try:
                data = json.loads(file_bytes.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                raise ValidationError("That file is neither a PocketLedger database backup nor a JSON export.")
            report = backup.import_json_to_staging(data, str(staged_path), current_app.config["DATABASE"])
            kind = "JSON export"
    except ServiceError as exc:
        _safe_remove(staged_path)
        flash(exc.message, "error")
        return redirect(url_for("settings.index") + "#backup")
    if not staged_path.exists():
        flash("The file could not be read.", "error")
        return redirect(url_for("settings.index") + "#backup")
    session["staged_restore"] = {"file": staged_name, "name": source_name, "kind": kind}
    return render_template("settings/restore_review.html", report=report, source=source_name, kind=kind,
                           live=_live_stats(conn))


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


@bp.route("/restore", methods=["POST"])
def restore_upload():
    f = request.files.get("backup_file")
    if f is None or not f.filename:
        flash("Choose a backup file first.", "error")
        return redirect(url_for("settings.index") + "#backup")
    return _stage_and_review(f.read(), os.path.basename(f.filename)[:120])


@bp.route("/restore/saved", methods=["POST"])
def restore_saved():
    name = request.form.get("name", "")
    path = Path(current_app.config["BACKUP_DIR"]) / name
    if not _BACKUP_NAME.match(name) or not path.is_file():
        abort(404)
    return _stage_and_review(path.read_bytes(), name)


@bp.route("/restore/cancel", methods=["POST"])
def restore_cancel():
    _discard_staged()
    flash("Restore cancelled. Nothing was changed.", "success")
    return redirect(url_for("settings.index") + "#backup")


@bp.route("/restore/confirm", methods=["POST"])
def restore_confirm():
    info = session.get("staged_restore")
    if not info:
        flash("There's nothing staged to restore. Upload the file again.", "error")
        return redirect(url_for("settings.index") + "#backup")
    if request.form.get("confirm_text", "").strip() != "RESTORE":
        flash("Type RESTORE exactly to confirm. Nothing was changed.", "error")
        return redirect(url_for("settings.index") + "#backup")
    staged = _upload_dir() / info["file"]
    if not staged.is_file():
        session.pop("staged_restore", None)
        flash("The staged file expired. Upload it again.", "error")
        return redirect(url_for("settings.index") + "#backup")
    cfg = current_app.config
    db_module.close_db()  # release this request's connection so the file can be replaced
    try:
        safety = backup.restore_database(str(staged), cfg["DATABASE"], cfg["BACKUP_DIR"])
    except ServiceError as exc:
        flash(exc.message, "error")
        return redirect(url_for("settings.index") + "#backup")
    finally:
        _safe_remove(staged)
        session.pop("staged_restore", None)
    audit.log(get_db(), "restore", "database", None, after={"source": info["name"], "safety_backup": Path(safety).name})
    flash(f"Restored from {info['name']}. Your previous data was saved first as {Path(safety).name}.", "success")
    return redirect(url_for("dashboard.index"))


# ---- audit trail & integrity ----------------------------------------------------------------
@bp.route("/audit")
def audit_log():
    conn = get_db()
    page = max(1, to_int(request.args.get("page"), 1))
    entity = request.args.get("entity") or None
    per = 50
    logs, total = audit.list_logs(conn, limit=per, offset=(page - 1) * per, entity_type=entity)
    entities = [r[0] for r in conn.execute("SELECT DISTINCT entity_type FROM audit_logs ORDER BY 1")]
    return render_template("settings/audit.html", logs=logs, total=total, page=page, pages=max(1, -(-total // per)),
                           entity=entity, entities=entities)


@bp.route("/integrity", methods=["POST"])
def integrity():
    issues = ledger.verify_integrity(get_db())
    if issues:
        for i in issues[:8]:
            flash(i, "error")
    else:
        flash("Ledger check passed: every transaction balances, every balance derives from its entries, "
              "and net worth reconciles to income, expenses and adjustments.", "success")
    return redirect(url_for("settings.index") + "#integrity")
