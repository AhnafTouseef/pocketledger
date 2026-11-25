from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import get_db
from ..services import accounts as svc, categories, reconciliation, settings as settings_svc
from ..services import transactions as tx_svc
from ..services.errors import NotFoundError, ServiceError
from . import form_dict, form_errors, to_int

bp = Blueprint("accounts", __name__, url_prefix="/accounts")


@bp.route("/")
def index():
    conn = get_db()
    show_archived = request.args.get("archived") == "1"
    accounts = svc.list_accounts(conn, include_archived=show_archived)
    active = [a for a in accounts if not a["is_archived"]]
    archived = [a for a in accounts if a["is_archived"]]
    groups = {}
    for a in active:
        groups.setdefault(a["category_name"], {"kind": a["category_kind"], "icon": a["category_icon"], "items": []})
        groups[a["category_name"]]["items"].append(a)
    base = settings_svc.base_currency_code(conn)
    total = sum(a["balance_minor"] for a in active if a["currency"] == base)
    return render_template("accounts/index.html", groups=groups, archived=archived, total=total,
                           show_archived=show_archived, has_any=bool(active or archived))


@bp.route("/new", methods=["GET", "POST"])
def new():
    conn = get_db()
    values, errors = {}, {}
    if request.method == "POST":
        values = form_dict()
        try:
            account_id = svc.create_account(
                conn, name=values.get("name"), category_id=values.get("category_id"),
                currency=values.get("currency"), opening_balance=values.get("opening_balance"),
                opened_on=values.get("opened_on"), description=values.get("description"),
            )
            flash(f"Account “{values['name'].strip()}” created.", "success")
            return redirect(url_for("accounts.detail", account_id=account_id))
        except ServiceError as exc:
            errors = form_errors(exc)
    else:
        values = {"category_id": request.args.get("category_id", "")}
    return render_template(
        "accounts/form.html", values=values, errors=errors, editing=False,
        categories=categories.list_account_categories(conn), currencies=settings_svc.enabled_currencies(conn),
        base_currency=settings_svc.base_currency_code(conn),
    )


@bp.route("/<int:account_id>")
def detail(account_id):
    conn = get_db()
    try:
        acc = svc.get_account(conn, account_id)
    except NotFoundError:
        abort(404)
    page = to_int(request.args.get("page"), 1)
    history = tx_svc.search(conn, {"account_id": account_id}, page=page, per_page=25)
    recs = reconciliation.list_reconciliations(conn, account_id=account_id, limit=10)
    cur = settings_svc.get_currency(conn, acc["currency"])
    return render_template("accounts/detail.html", acc=acc, history=history, recs=recs, cur=cur,
                           summary=svc.deletion_summary(conn, account_id))


@bp.route("/<int:account_id>/edit", methods=["GET", "POST"])
def edit(account_id):
    conn = get_db()
    try:
        acc = svc.get_account(conn, account_id)
    except NotFoundError:
        abort(404)
    errors = {}
    locked = svc.has_history(conn, account_id)
    if request.method == "POST":
        values = form_dict()
        try:
            svc.update_account(
                conn, account_id, name=values.get("name"), category_id=values.get("category_id"),
                description=values.get("description"),
                opening_balance=None if locked else values.get("opening_balance"),
            )
            flash("Account updated.", "success")
            return redirect(url_for("accounts.detail", account_id=account_id))
        except ServiceError as exc:
            errors = form_errors(exc)
    else:
        from ..money import to_decimal_string
        cur = settings_svc.get_currency(conn, acc["currency"])
        values = {
            "name": acc["name"], "category_id": str(acc["category_id"]), "description": acc["description"] or "",
            "opening_balance": to_decimal_string(acc["opening_balance_minor"], cur["minor_unit"]),
        }
    return render_template(
        "accounts/form.html", values=values, errors=errors, editing=True, acc=acc, locked=locked,
        categories=categories.list_account_categories(conn), currencies=settings_svc.enabled_currencies(conn),
        base_currency=acc["currency"],
    )


@bp.route("/<int:account_id>/delete", methods=["POST"])
def delete(account_id):
    try:
        svc.delete_account(get_db(), account_id)
        flash("Account deleted.", "success")
    except NotFoundError:
        abort(404)
    except ServiceError as exc:
        flash(exc.message, "error")
        return redirect(url_for("accounts.detail", account_id=account_id))
    return redirect(url_for("accounts.index"))


@bp.route("/<int:account_id>/unarchive", methods=["POST"])
def unarchive(account_id):
    try:
        svc.unarchive_account(get_db(), account_id)
        flash("Account restored.", "success")
    except NotFoundError:
        abort(404)
    except ServiceError as exc:
        flash(exc.message, "error")
    return redirect(url_for("accounts.detail", account_id=account_id))
