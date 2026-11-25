"""People you lend to or borrow from.

Receivables/payables are *derived* from ledger entries (see v_person_balances); there is no
stored balance to overwrite. Repayments are matched to loans oldest-first (FIFO) so each
loan has a remaining amount and an overdue state.
"""
from __future__ import annotations

from ..db import transaction
from ..money import format_money
from ..timeutil import parse_date, utcnow_iso
from . import audit, ledger, settings as settings_svc, undo
from .errors import NotFoundError, ValidationError


def _clean(value, limit, field, label):
    value = (value or "").strip()
    if len(value) > limit:
        raise ValidationError(f"{label} is limited to {limit} characters.", field)
    return value or None


def create_person(conn, *, name, contact=None, notes=None):
    name = _clean(name, 80, "name", "Name")
    if not name:
        raise ValidationError("Enter the person's name.", "name")
    contact = _clean(contact, 120, "contact", "Contact")
    notes = _clean(notes, 1000, "notes", "Notes")
    with transaction(conn), undo.group(conn, f"Added {name}", ("people.index", {})) as grp:
        if conn.execute("SELECT 1 FROM people WHERE name = ? AND is_archived = 0 AND deleted_at IS NULL", (name,)).fetchone():
            raise ValidationError("Someone with that name already exists. Add a detail to tell them apart, e.g. “Rahim (cousin)”.", "name")
        now = utcnow_iso()
        cur = conn.execute(
            "INSERT INTO people (name, contact, notes, created_at, updated_at) VALUES (?,?,?,?,?)",
            (name, contact, notes, now, now),
        )
        grp.created("people", cur.lastrowid)
        audit.log(conn, "create", "person", cur.lastrowid, after={"name": name})
    return cur.lastrowid


def update_person(conn, person_id, *, name, contact, notes):
    name = _clean(name, 80, "name", "Name")
    if not name:
        raise ValidationError("Enter the person's name.", "name")
    contact = _clean(contact, 120, "contact", "Contact")
    notes = _clean(notes, 1000, "notes", "Notes")
    with transaction(conn), undo.group(conn, f"Edited {name}", ("people.detail", {"person_id": person_id})) as grp:
        old = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if old is None or old["deleted_at"]:
            raise NotFoundError("Person not found.")
        if not old["is_archived"] and conn.execute(
            "SELECT 1 FROM people WHERE name = ? AND is_archived = 0 AND deleted_at IS NULL AND id <> ?", (name, person_id)
        ).fetchone():
            raise ValidationError("Someone with that name already exists.", "name")
        now = utcnow_iso()
        conn.execute("UPDATE people SET name = ?, contact = ?, notes = ?, updated_at = ? WHERE id = ?",
                     (name, contact, notes, now, person_id))
        grp.changed("people", person_id, {"name": old["name"], "contact": old["contact"], "notes": old["notes"],
                                         "updated_at": old["updated_at"]},
                    {"name": name, "contact": contact, "notes": notes, "updated_at": now})
        audit.log(conn, "update", "person", person_id,
                  before={k: old[k] for k in ("name", "contact", "notes")},
                  after={"name": name, "contact": contact, "notes": notes})


def deletion_summary(conn, person_id):
    """What deleting this person would do, for the confirmation dialog."""
    p = get_person(conn, person_id)
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE person_id = ? AND status = 'posted' AND deleted_at IS NULL",
                     (person_id,)).fetchone()[0]
    return {"person": p, "record_count": n}


def delete_person(conn, person_id):
    """Delete a person and the lending / borrowing / repayment records with them, settled or not.
    Those records are the person's own, so they go together (one Undo brings everything back)."""
    with transaction(conn), undo.group(conn, "Deleted person", ("people.detail", {"person_id": person_id})) as grp:
        p = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if p is None or p["deleted_at"]:
            raise NotFoundError("Person not found.")
        now = utcnow_iso()
        for (tx_id,) in conn.execute("SELECT id FROM transactions WHERE person_id = ? AND status = 'posted' "
                                     "AND deleted_at IS NULL", (person_id,)).fetchall():
            ledger.soft_delete_transaction(conn, tx_id, grp, now)
        conn.execute("UPDATE people SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                     (now, now, person_id))
        grp.changed("people", person_id, {"deleted_at": None, "updated_at": p["updated_at"]},
                    {"deleted_at": now, "updated_at": now})
        grp.label = f"Deleted {p['name']}"
        audit.log(conn, "delete", "person", person_id)
    return p["name"]


def unarchive_person(conn, person_id):
    with transaction(conn):
        p = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if p is None:
            raise NotFoundError("Person not found.")
        if conn.execute("SELECT 1 FROM people WHERE name = ? AND is_archived = 0 AND deleted_at IS NULL", (p["name"],)).fetchone():
            raise ValidationError("An active person with that name already exists.")
        conn.execute("UPDATE people SET is_archived = 0, updated_at = ? WHERE id = ?", (utcnow_iso(), person_id))
        audit.log(conn, "unarchive", "person", person_id, before={"name": p["name"]})


def balances(conn, person_id):
    """{currency: row} of derived totals for one person."""
    return {
        r["currency"]: dict(r)
        for r in conn.execute("SELECT * FROM v_person_balances WHERE person_id = ?", (person_id,))
    }


def get_person(conn, person_id, currency=None):
    p = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if p is None or p["deleted_at"]:
        raise NotFoundError("Person not found.")
    currency = currency or settings_svc.base_currency_code(conn)
    out = dict(p)
    b = balances(conn, person_id).get(currency, {})
    for key in ("lent_minor", "received_back_minor", "borrowed_minor", "repaid_minor", "receivable_minor", "payable_minor"):
        out[key] = b.get(key, 0) or 0
    out["currency"] = currency
    return out


# --- FIFO loan breakdown -------------------------------------------------------------
def _allocate(loans, repaid_total):
    pool = repaid_total
    out = []
    for loan in loans:
        applied = min(loan["amount_minor"], pool)
        pool -= applied
        item = dict(loan)
        item["repaid_minor"] = applied
        item["remaining_minor"] = loan["amount_minor"] - applied
        out.append(item)
    return out


def loan_breakdown(conn, person_id, side, currency=None):
    """Per-loan view. side='receivable' (you lent) or 'payable' (you borrowed)."""
    currency = currency or settings_svc.base_currency_code(conn)
    loan_type, repay_type = ("lent", "repayment_received") if side == "receivable" else ("borrowed", "repayment_made")
    loans = conn.execute(
        "SELECT id, occurred_at, amount_minor, due_date, description FROM transactions "
        "WHERE person_id = ? AND currency = ? AND type = ? AND status = 'posted' ORDER BY occurred_at, id",
        (person_id, currency, loan_type),
    ).fetchall()
    repaid = conn.execute(
        "SELECT COALESCE(SUM(amount_minor), 0) FROM transactions WHERE person_id = ? AND currency = ? "
        "AND type = ? AND status = 'posted'", (person_id, currency, repay_type),
    ).fetchone()[0]
    return _allocate(loans, repaid)


def _all_open_loans(conn, side, currency):
    loan_type, repay_type = ("lent", "repayment_received") if side == "receivable" else ("borrowed", "repayment_made")
    loans, repaid = {}, {}
    for r in conn.execute(
        "SELECT t.id, t.person_id, t.occurred_at, t.amount_minor, t.due_date, t.description, p.name AS person_name "
        "FROM transactions t JOIN people p ON p.id = t.person_id "
        "WHERE t.currency = ? AND t.type = ? AND t.status = 'posted' ORDER BY t.occurred_at, t.id",
        (currency, loan_type),
    ):
        loans.setdefault(r["person_id"], []).append(dict(r))
    for r in conn.execute(
        "SELECT person_id, SUM(amount_minor) AS total FROM transactions WHERE currency = ? AND type = ? "
        "AND status = 'posted' GROUP BY person_id", (currency, repay_type),
    ):
        repaid[r["person_id"]] = r["total"]
    result = []
    for pid, plist in loans.items():
        for loan in _allocate(plist, repaid.get(pid, 0)):
            if loan["remaining_minor"] > 0:
                result.append(loan)
    return result


def list_people(conn, currency=None, include_archived=False):
    currency = currency or settings_svc.base_currency_code(conn)
    where = "WHERE p.deleted_at IS NULL" + ("" if include_archived else " AND p.is_archived = 0")
    rows = conn.execute(
        "SELECT p.*, COALESCE(b.lent_minor, 0) AS lent_minor, COALESCE(b.received_back_minor, 0) AS received_back_minor,"
        " COALESCE(b.borrowed_minor, 0) AS borrowed_minor, COALESCE(b.repaid_minor, 0) AS repaid_minor,"
        " COALESCE(b.receivable_minor, 0) AS receivable_minor, COALESCE(b.payable_minor, 0) AS payable_minor "
        "FROM people p LEFT JOIN v_person_balances b ON b.person_id = p.id AND b.currency = ? "
        f"{where} ORDER BY p.name", (currency,),
    ).fetchall()
    people = [dict(r) for r in rows]
    next_due = {"receivable": {}, "payable": {}}
    for side in ("receivable", "payable"):
        for loan in _all_open_loans(conn, side, currency):
            if loan["due_date"]:
                cur = next_due[side].get(loan["person_id"])
                if cur is None or loan["due_date"] < cur:
                    next_due[side][loan["person_id"]] = loan["due_date"]
    for p in people:
        p["next_due_receivable"] = next_due["receivable"].get(p["id"])
        p["next_due_payable"] = next_due["payable"].get(p["id"])
    return people


def overdue_receivables(conn, today, currency=None):
    currency = currency or settings_svc.base_currency_code(conn)
    out = []
    for loan in _all_open_loans(conn, "receivable", currency):
        if loan["due_date"] and loan["due_date"] < today.isoformat():
            loan["days_overdue"] = (today - parse_date(loan["due_date"])).days
            out.append(loan)
    return sorted(out, key=lambda l: l["due_date"])


def upcoming_payables(conn, today, within_days, currency=None):
    currency = currency or settings_svc.base_currency_code(conn)
    out = []
    for loan in _all_open_loans(conn, "payable", currency):
        if not loan["due_date"]:
            continue
        days_left = (parse_date(loan["due_date"]) - today).days
        if days_left <= within_days:
            loan["days_left"] = days_left
            out.append(loan)
    return sorted(out, key=lambda l: l["due_date"])
