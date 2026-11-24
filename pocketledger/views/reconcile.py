from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import get_db
from ..services import accounts as accounts_svc, reconciliation as svc, settings as settings_svc
from ..services.errors import NotFoundError, ServiceError
from . import form_errors

bp = Blueprint("reconcile", __name__, url_prefix="/reconcile")


@bp.route("/")
def index():
    conn = get_db()
    accounts = accounts_svc.list_accounts(conn)
    history = svc.list_reconciliations(conn, limit=60)
    return render_template(
        "reconcile/index.html", accounts=accounts, discrepancies=svc.open_discrepancies(conn), history=history,
        reminder_days=settings_svc.get_int(conn, "verify_reminder_days", 7),
    )


@bp.route("/account/<int:account_id>", methods=["GET", "POST"])
def verify(account_id):
    conn = get_db()
    try:
        acc = accounts_svc.get_account(conn, account_id)
    except NotFoundError:
        abort(404)
    errors, values = {}, {}
    if request.method == "POST":
        values = {"counted": request.form.get("counted", ""), "note": request.form.get("note", "")}
        try:
            rid = svc.verify(conn, account_id, values["counted"], values["note"])
            rec = svc.get_reconciliation(conn, rid)
            if rec["difference_minor"] == 0:
                flash("Verified: the physical balance matches your records.", "success")
            else:
                flash("Verified, and the count doesn't match your records. Decide below how to handle the difference.", "warning")
            return redirect(url_for("reconcile.detail", rec_id=rid))
        except ServiceError as exc:
            errors = form_errors(exc)
    cur = settings_svc.get_currency(conn, acc["currency"])
    return render_template("reconcile/verify.html", acc=acc, cur=cur, values=values, errors=errors)


@bp.route("/<int:rec_id>")
def detail(rec_id):
    conn = get_db()
    try:
        rec = svc.get_reconciliation(conn, rec_id)
    except NotFoundError:
        abort(404)
    cur = settings_svc.get_currency(conn, rec["currency"])
    return render_template(
        "reconcile/detail.html", rec=rec, cur=cur, timeline=svc.get_timeline(conn, rec_id),
        nearby=svc.nearby_transactions(conn, rec),
    )


def _act(rec_id, fn, ok_message):
    try:
        fn()
        flash(ok_message, "success")
    except NotFoundError:
        abort(404)
    except ServiceError as exc:
        flash(exc.message, "error")
    return redirect(url_for("reconcile.detail", rec_id=rec_id))


@bp.route("/<int:rec_id>/adjust", methods=["POST"])
def adjust(rec_id):
    if request.form.get("confirm") != "yes":
        flash("Tick the box to confirm the adjustment.", "error")
        return redirect(url_for("reconcile.detail", rec_id=rec_id))
    return _act(rec_id, lambda: svc.post_adjustment(get_db(), rec_id, request.form.get("note")),
                "Adjustment posted. It's recorded as an explicit, labelled entry; nothing earlier was changed.")


@bp.route("/<int:rec_id>/unresolved", methods=["POST"])
def unresolved(rec_id):
    return _act(rec_id, lambda: svc.leave_unresolved(get_db(), rec_id, request.form.get("note")),
                "Left unresolved. It stays visible on the dashboard until the books match or you adjust.")


@bp.route("/<int:rec_id>/reopen", methods=["POST"])
def reopen(rec_id):
    return _act(rec_id, lambda: svc.reopen(get_db(), rec_id), "Marked as open for investigation again.")


@bp.route("/<int:rec_id>/note", methods=["POST"])
def note(rec_id):
    return _act(rec_id, lambda: svc.add_note(get_db(), rec_id, request.form.get("note")), "Note added.")
