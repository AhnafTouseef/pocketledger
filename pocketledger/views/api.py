"""JSON endpoints used by the quick-entry sheet, the offline queue and entry forms.

Responses never claim a save unless the database transaction committed. Every create is
idempotent on the client-generated `uuid`, so a retried or replayed submission can never
produce a duplicate.
"""
from __future__ import annotations

import re

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


_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_REF_FIELDS = (("source_account_id", "acct:", accounts_svc), ("dest_account_id", "acct:", accounts_svc),
               ("account_id", "acct:", accounts_svc), ("person_id", "person:", people_svc))


def _payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ServiceError("Send the transaction as JSON.")
    return {k: v for k, v in data.items() if k in ALLOWED_FIELDS}


def _resolve_refs(conn, data):
    """A transaction queued offline may point at an account or person that was also created offline
    ("acct:<client id>", "person:<client id>"). By the time it arrives the record exists (the device
    syncs those first); swap the reference for the real id. An unknown reference is a clear rejection."""
    for field, prefix, svc in _REF_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.startswith(prefix):
            row = svc.find_by_client_uuid(conn, value[len(prefix):].lower())
            if row is None:
                what = "account" if prefix == "acct:" else "person"
                raise ServiceError(f"The {what} this entry uses was never created on the server (it may have been "
                                   f"rejected or discarded). Choose an existing {what}.", field)
            data[field] = row["id"]


def _client_uuid(data):
    value = str(data.get("uuid") or "").strip().lower()
    if not _UUID_RE.match(value):
        raise ServiceError("Missing or invalid id for this request.", "uuid")
    return value


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
        account_categories=[{"id": c["id"], "name": c["name"], "kind": c["kind"]} for c in categories.list_account_categories(conn)],
        currencies=[{"code": c["code"], "symbol": c["symbol"], "minor_unit": c["minor_unit"]} for c in settings_svc.enabled_currencies(conn)],
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
        _resolve_refs(conn, data)
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


ACCOUNT_FIELDS = {"uuid", "name", "category_id", "currency", "opening_balance", "opened_on", "description"}
PERSON_FIELDS = {"uuid", "name", "contact", "notes"}


@bp.route("/accounts", methods=["POST"])
def create_account():
    """Create an account. Idempotent on the client's uuid, so a queued request that is sent twice makes one account."""
    conn = get_db()
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ServiceError("Send the account as JSON.")
        data = {k: v for k, v in body.items() if k in ACCOUNT_FIELDS}
        cu = _client_uuid(data)
        earlier = accounts_svc.find_by_client_uuid(conn, cu)
        if earlier:
            return jsonify(ok=True, id=earlier["id"], created=False, undo=None,
                           message="Already saved earlier; nothing was duplicated."), 200
        new_id = accounts_svc.create_account(
            conn, name=data.get("name"), category_id=data.get("category_id"), currency=data.get("currency"),
            opening_balance=data.get("opening_balance"), opened_on=data.get("opened_on"),
            description=data.get("description"), client_uuid=cu)
    except ServiceError as exc:
        return _error(exc)
    return jsonify(ok=True, id=new_id, created=True, undo=undo_svc.take_last(),
                   message=f"Account “{str(data['name']).strip()}” created."), 201


@bp.route("/people", methods=["POST"])
def create_person():
    conn = get_db()
    try:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ServiceError("Send the person as JSON.")
        data = {k: v for k, v in body.items() if k in PERSON_FIELDS}
        cu = _client_uuid(data)
        earlier = people_svc.find_by_client_uuid(conn, cu)
        if earlier:
            return jsonify(ok=True, id=earlier["id"], created=False, undo=None,
                           message="Already saved earlier; nothing was duplicated."), 200
        new_id = people_svc.create_person(conn, name=data.get("name"), contact=data.get("contact"),
                                          notes=data.get("notes"), client_uuid=cu)
    except ServiceError as exc:
        return _error(exc)
    return jsonify(ok=True, id=new_id, created=True, undo=undo_svc.take_last(),
                   message=f"{str(data['name']).strip()} added."), 201


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
        _resolve_refs(conn, data)
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
