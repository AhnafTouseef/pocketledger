from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import get_db
from ..services import people as svc, settings as settings_svc
from ..services import transactions as tx_svc
from ..services.errors import NotFoundError, ServiceError
from ..timeutil import today_local
from . import form_dict, form_errors, to_int

bp = Blueprint("people", __name__, url_prefix="/people")


@bp.route("/")
def index():
    conn = get_db()
    tab = request.args.get("tab", "receivables")
    if tab not in ("receivables", "payables"):
        tab = "receivables"
    people = svc.list_people(conn, include_archived=request.args.get("archived") == "1")
    today = today_local(settings_svc.tzname(conn)).isoformat()
    receivables = sorted((p for p in people if p["receivable_minor"] > 0), key=lambda p: -p["receivable_minor"])
    payables = sorted((p for p in people if p["payable_minor"] > 0), key=lambda p: -p["payable_minor"])
    settled = [p for p in people if p["receivable_minor"] == 0 and p["payable_minor"] == 0]
    return render_template(
        "people/index.html", tab=tab, receivables=receivables, payables=payables, settled=settled, today=today,
        total_recv=sum(p["receivable_minor"] for p in receivables), total_pay=sum(p["payable_minor"] for p in payables),
        show_archived=request.args.get("archived") == "1",
    )


@bp.route("/new", methods=["GET", "POST"])
def new():
    conn = get_db()
    values, errors = {}, {}
    if request.method == "POST":
        values = form_dict()
        try:
            pid = svc.create_person(conn, name=values.get("name"), contact=values.get("contact"), notes=values.get("notes"))
            flash(f"Added {values['name'].strip()}.", "success")
            return redirect(url_for("people.detail", person_id=pid))
        except ServiceError as exc:
            errors = form_errors(exc)
    return render_template("people/form.html", values=values, errors=errors, editing=False)


@bp.route("/<int:person_id>")
def detail(person_id):
    conn = get_db()
    try:
        person = svc.get_person(conn, person_id)
    except NotFoundError:
        abort(404)
    today = today_local(settings_svc.tzname(conn)).isoformat()
    loans_recv = svc.loan_breakdown(conn, person_id, "receivable")
    loans_pay = svc.loan_breakdown(conn, person_id, "payable")
    history = tx_svc.search(conn, {"person_id": person_id}, page=to_int(request.args.get("page"), 1), per_page=30)
    return render_template("people/detail.html", p=person, loans_recv=loans_recv, loans_pay=loans_pay,
                           history=history, today=today, summary=svc.deletion_summary(conn, person_id))


@bp.route("/<int:person_id>/edit", methods=["GET", "POST"])
def edit(person_id):
    conn = get_db()
    try:
        person = svc.get_person(conn, person_id)
    except NotFoundError:
        abort(404)
    errors = {}
    if request.method == "POST":
        values = form_dict()
        try:
            svc.update_person(conn, person_id, name=values.get("name"), contact=values.get("contact"), notes=values.get("notes"))
            flash("Saved.", "success")
            return redirect(url_for("people.detail", person_id=person_id))
        except ServiceError as exc:
            errors = form_errors(exc)
    else:
        values = {"name": person["name"], "contact": person["contact"] or "", "notes": person["notes"] or ""}
    return render_template("people/form.html", values=values, errors=errors, editing=True, p=person)


@bp.route("/<int:person_id>/delete", methods=["POST"])
def delete(person_id):
    try:
        svc.delete_person(get_db(), person_id)
        flash("Person deleted.", "success")
    except NotFoundError:
        abort(404)
    except ServiceError as exc:
        flash(exc.message, "error")
        return redirect(url_for("people.detail", person_id=person_id))
    return redirect(url_for("people.index"))


@bp.route("/<int:person_id>/unarchive", methods=["POST"])
def unarchive(person_id):
    try:
        svc.unarchive_person(get_db(), person_id)
        flash("Restored.", "success")
    except NotFoundError:
        abort(404)
    except ServiceError as exc:
        flash(exc.message, "error")
    return redirect(url_for("people.detail", person_id=person_id))
