"""Append-only audit trail. Every correction, void, archive and restore lands here."""
from __future__ import annotations

import json

from flask import g, has_request_context

from ..timeutil import utcnow_iso


def current_user_id():
    if has_request_context():
        return g.get("user_id")
    return None


def _dump(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def log(conn, action, entity_type, entity_id=None, before=None, after=None, reason=None, user_id=None):
    conn.execute(
        "INSERT INTO audit_logs (user_id, action, entity_type, entity_id, before_json, after_json, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id or current_user_id(), action, entity_type, entity_id, _dump(before), _dump(after), reason, utcnow_iso()),
    )


def row_dict(row):
    return dict(row) if row is not None else None


def list_logs(conn, *, limit=50, offset=0, entity_type=None, entity_id=None):
    where, params = [], []
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        where.append("entity_id = ?")
        params.append(entity_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM audit_logs {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
    ).fetchall()
    return [dict(r) for r in rows], total
