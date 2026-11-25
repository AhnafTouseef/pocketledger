"""JSON endpoints used by the quick-entry sheet, the offline queue and entry forms.

Responses never claim a save unless the database transaction committed. Every create is
idempotent on the client-generated `uuid`, so a retried or replayed submission can never
produce a duplicate.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request, url_for

from ..db import get_db
from ..money import format_money
from ..services import accounts as accounts_svc, categories, ledger, people as people_svc, settings as settings_svc, undo as undo_svc
from ..services.errors import ConflictError, NotFoundError, ServiceError
from ..timeutil import local_date, today_local, utc_iso_to_local, utcnow_iso

bp = Blueprint("api", __name__, url_prefix="/api")

ALLOWED_FIELDS = {
    "uuid", "type", "amount", "occurred_local", "source_account_id", "dest_account_id", "account_id", "direction",
    "person_id", "new_person_name", "category_id", "description", "notes", "due_date", "reconciliation_id",
}


def _payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ServiceError("Send the transaction as JSON.")
    return {k: v for k, v in data.items() if k in ALLOWED_FIELDS}


def _error(exc: ServiceError, status=422):
    return jsonify(error=exc.message, field=exc.field), status


@bp.route("/options")
def options():
    """Everything the entry form needs; cached client-side for offline use."""
    conn = get_db()
    cur = settings_svc.base_currency(conn)
    tz = settings_svc.tzname(conn)
    return jsonify(
        generated_at=utcnow_iso(),
        currency=dict(cur),
        timezone=tz,
        today=today_local(tz).isoformat(),
        accounts=[
            {"id": a["id"], "name": a["name"], "kind": a["category_kind"], "category": a["category_name"],
             "currency": a["currency"], "balance_minor": a["balance_minor"], "opened_on": a["opened_on"]}
            for a in accounts_svc.list_accounts(conn)
        ],
        people=[
            {"id": p["id"], "name": p["name"], "receivable_minor": p["receivable_minor"], "payable_minor": p["payable_minor"]}
            for p in people_svc.list_people(conn)
        ],
        categories={
            "income": [{"id": c["id"], "name": c["name"]} for c in categories.list_transaction_categories(conn, "income")],
            "expense": [{"id": c["id"], "name": c["name"]} for c in categories.list_transaction_categories(conn, "expense")],
        },
        types=ledger.TYPE_LABELS,
    )


@bp.route("/transactions", methods=["POST"])
def create_transaction():
    conn = get_db()
    try:
        data = _payload()
        if not data.get("uuid"):
            raise ServiceError("Missing transaction id.", "uuid")
        res = ledger.post_transaction(conn, data)
    except ServiceError as exc:
        return _error(exc)
    t = conn.execute("SELECT type, amount_minor, currency FROM transactions WHERE id = ?", (res.id,)).fetchone()
    cur = dict(settings_svc.get_currency(conn, t["currency"]))
    return jsonify(
        ok=True, id=res.id, uuid=res.uuid, created=res.created, warnings=res.warnings,
        undo=undo_svc.take_last() if res.created else None,
        message=f"{ledger.TYPE_LABELS[t['type']]} of {format_money(t['amount_minor'], cur)} saved."
        if res.created else "Already saved earlier; nothing was duplicated.",
    ), (201 if res.created else 200)


@bp.route("/transactions/<uuid>")
def transaction_status(uuid):
    """Lets the offline queue confirm whether a transaction reached the server."""
    row = get_db().execute("SELECT id, status FROM transactions WHERE uuid = ?", (uuid.lower(),)).fetchone()
    if row is None:
        return jsonify(saved=False), 404
    return jsonify(saved=True, id=row["id"], status=row["status"])


@bp.route("/transactions/<int:tx_id>", methods=["PUT"])
def update_transaction(tx_id):
    conn = get_db()
    try:
        data = _payload()
        new_id, kind = ledger.edit_transaction(conn, tx_id, data, reason=(request.get_json(silent=True) or {}).get("reason"))
    except NotFoundError:
        return jsonify(error="Transaction not found."), 404
    except ServiceError as exc:
        return _error(exc)
    messages = {
        "updated": "Details updated.",
        "replaced": "Changes saved.",
        "unchanged": "Nothing changed.",
    }
    return jsonify(ok=True, id=new_id, kind=kind, message=messages[kind], undo=undo_svc.take_last())


@bp.route("/undo/<token>", methods=["POST"])
def undo_action(token):
    """Reverse one earlier action completely, once. Refused (and nothing changed) if the records
    involved were changed afterwards."""
    try:
        label, restore = undo_svc.undo(get_db(), token)
    except NotFoundError as exc:
        return jsonify(error=exc.message), 404
    except ConflictError as exc:
        return jsonify(error=exc.message), 409
    target = url_for(restore[0], **restore[1]) if restore else url_for("dashboard.index")
    return jsonify(ok=True, message=f"Undone: {label}", redirect=target)


@bp.route("/ping")
def ping():
    return jsonify(ok=True, at=utcnow_iso())
