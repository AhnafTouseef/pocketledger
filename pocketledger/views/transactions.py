"""Transaction pages. Entry forms are JS-built (static/js/txform.js) and talk to /api; the
server renders list, filters, detail, history and CSV export."""
from __future__ import annotations

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..db import get_db
from ..services import accounts as accounts_svc, categories, ledger, people as people_svc, settings as settings_svc
from ..services import transactions as svc
from ..services.errors import NotFoundError, ServiceError
from ..timeutil import utc_iso_to_local, utcnow
from . import to_int

bp = Blueprint("transactions", __name__, url_prefix="/transactions")

FILTER_KEYS = ("q", "type", "account_id", "person_id", "category_id", "date_from", "date_to", "status")


def _filters():
    return {k: (request.args.get(k) or "").strip() for k in FILTER_KEYS}


@bp.route("/")
def index():
    conn = get_db()
    filters = _filters()
    sort = request.args.get("sort", "date")
    direction = "asc" if request.args.get("dir") == "asc" else "desc"
    result = svc.search(conn, filters, sort=sort, direction=direction, page=to_int(request.args.get("page"), 1))
    sums = svc.totals_for_filter(conn, filters)
    active_filters = {k: v for k, v in filters.items() if v and not (k == "status" and v == "posted")}
    return render_template(
        "transactions/index.html", result=result, filters=filters, sort=sort, direction=direction, sums=sums,
        accounts=accounts_svc.list_accounts(conn, include_archived=True), people=people_svc.list_people(conn, include_archived=True),
        categories=categories.list_transaction_categories(conn, include_archived=True),
        types=ledger.TYPE_LABELS, active_filters=active_filters,
    )


@bp.route("/new")
def new():
    """Full-page entry form (the same form as the quick-entry sheet)."""
    prefill = {k: v for k, v in request.args.items() if k in (
        "type", "source_account_id", "dest_account_id", "account_id", "person_id", "direction", "amount",
        "reconciliation_id", "occurred_local", "description")}
    rec_id = to_int(prefill.get("reconciliation_id"))
    recon = None
    if rec_id:
        from ..services import reconciliation
        try:
            recon = reconciliation.get_reconciliation(get_db(), rec_id)
            if "occurred_local" not in prefill:
                tz = settings_svc.tzname(get_db())
                prefill["occurred_local"] = utc_iso_to_local(recon["verified_at"], tz).strftime("%Y-%m-%dT%H:%M")
        except NotFoundError:
            prefill.pop("reconciliation_id")
    return render_template("transactions/form.html", prefill=prefill, editing=False, recon=recon)


@bp.route("/<int:tx_id>")
def detail(tx_id):
    conn = get_db()
    try:
        t = svc.get_transaction(conn, tx_id)
    except NotFoundError:
        abort(404)
    replaces = replaced_by = None
    if t["replaces_id"]:
        replaces = conn.execute("SELECT id FROM transactions WHERE id = ?", (t["replaces_id"],)).fetchone()
    if t["replaced_by_id"]:
        replaced_by = conn.execute("SELECT id FROM transactions WHERE id = ?", (t["replaced_by_id"],)).fetchone()
    return render_template(
        "transactions/detail.html", t=t, entries=svc.get_entries(conn, tx_id), history=svc.get_history(conn, tx_id),
        replaces=replaces, replaced_by=replaced_by,
    )


@bp.route("/<int:tx_id>/edit")
def edit(tx_id):
    conn = get_db()
    try:
        t = svc.get_transaction(conn, tx_id)
    except NotFoundError:
        abort(404)
    if t["status"] == "voided":
        flash("Voided transactions can't be edited.", "error")
        return redirect(url_for("transactions.detail", tx_id=tx_id))
    tz = settings_svc.tzname(conn)
    from ..money import to_decimal_string
    cur = settings_svc.get_currency(conn, t["currency"])
    prefill = {
        "type": t["type"],
        "amount": to_decimal_string(t["amount_minor"], cur["minor_unit"]),
        "occurred_local": utc_iso_to_local(t["occurred_at"], tz).strftime("%Y-%m-%dT%H:%M"),
        "source_account_id": t["source_account_id"] or "", "dest_account_id": t["dest_account_id"] or "",
        "person_id": t["person_id"] or "", "category_id": t["category_id"] or "",
        "description": t["description"], "notes": t["notes"] or "", "due_date": t["due_date"] or "",
    }
    if t["type"] == "adjustment":
        prefill["account_id"] = t["dest_account_id"] or t["source_account_id"]
        prefill["direction"] = "increase" if t["dest_account_id"] else "decrease"
    return render_template("transactions/form.html", prefill=prefill, editing=True, t=t, recon=None)


@bp.route("/<int:tx_id>/void", methods=["POST"])
def void(tx_id):
    try:
        ledger.void_transaction(get_db(), tx_id, request.form.get("reason"))
        flash("Transaction voided. It stays in your history and all balances were updated.", "success")
    except NotFoundError:
        abort(404)
    except ServiceError as exc:
        flash(exc.message, "error")
    return redirect(url_for("transactions.detail", tx_id=tx_id))


@bp.route("/export.csv")
def export_csv():
    conn = get_db()
    filters = _filters()
    body = svc.export_csv(conn, filters)
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    return Response(
        "﻿" + body,  # BOM so Excel opens UTF-8 (৳, Bengali names) correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="pocketledger-transactions-{stamp}.csv"'},
    )
