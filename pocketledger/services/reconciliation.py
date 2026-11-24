"""Balance verification: compare what the books say with what you physically count.

Principles (from the spec): never call a discrepancy theft, never rewrite history, keep every
count, allow a discrepancy to stay unresolved. A reconciliation row is an immutable snapshot
(expected, counted, difference at the moment of counting). Whether it is *still* open is
derived live: counted - balance-as-of-that-moment, so recording a forgotten (back-dated)
transaction or an explicit adjustment resolves it without touching any old record.
"""
from __future__ import annotations

from ..db import transaction
from ..money import MoneyError, format_money, parse_amount
from ..timeutil import utcnow_iso
from . import audit, ledger, settings as settings_svc
from .errors import NotFoundError, ValidationError


def verify(conn, account_id, counted, note=None) -> int:
    """Record a physical count. `counted` is user text ('1,700') or int minor units."""
    note = (note or "").strip() or None
    if note and len(note) > 500:
        raise ValidationError("Notes are limited to 500 characters.", "note")
    with transaction(conn):
        acc = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if acc is None:
            raise NotFoundError("Account not found.")
        if acc["is_archived"]:
            raise ValidationError("Archived accounts can't be verified.", "account_id")
        cur = settings_svc.get_currency(conn, acc["currency"])
        if isinstance(counted, int) and not isinstance(counted, bool):
            counted_minor = counted
        else:
            try:
                counted_minor = parse_amount(counted, cur["minor_unit"])
            except MoneyError as exc:
                raise ValidationError(str(exc), "counted")
        now = utcnow_iso()
        expected = ledger.account_balance(conn, account_id, as_of_iso=now)
        diff = counted_minor - expected
        c = conn.execute(
            "INSERT INTO reconciliations (account_id, verified_at, expected_minor, counted_minor, difference_minor,"
            " note, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (account_id, now, expected, counted_minor, diff, note, audit.current_user_id(), now),
        )
        rid = c.lastrowid
        conn.execute(
            "INSERT INTO reconciliation_entries (reconciliation_id, entry_type, amount_minor, note, created_by, created_at)"
            " VALUES (?,?,?,?,?,?)", (rid, "verified", counted_minor, note, audit.current_user_id(), now),
        )
        conn.execute(
            "UPDATE accounts SET last_verified_balance_minor = ?, last_verified_at = ?, updated_at = ? WHERE id = ?",
            (counted_minor, now, now, account_id),
        )
        audit.log(conn, "verify", "reconciliation", rid,
                  after={"account_id": account_id, "expected_minor": expected, "counted_minor": counted_minor,
                         "difference_minor": diff})
    return rid


def _live_difference(conn, rec):
    return rec["counted_minor"] - ledger.account_balance(conn, rec["account_id"], as_of_iso=rec["verified_at"])


def _latest_ids(conn):
    return {
        r["account_id"]: r["id"]
        for r in conn.execute(
            "SELECT account_id, id FROM reconciliations r WHERE id = ("
            " SELECT id FROM reconciliations r2 WHERE r2.account_id = r.account_id ORDER BY verified_at DESC, id DESC LIMIT 1)"
        )
    }


def _describe(conn, rec, latest):
    d = dict(rec)
    d["live_difference_minor"] = _live_difference(conn, rec)
    if d["live_difference_minor"] == 0:
        d["status"] = "matched" if rec["difference_minor"] == 0 else "resolved"
    elif latest.get(rec["account_id"]) != rec["id"]:
        d["status"] = "superseded"
    elif rec["leave_unresolved"]:
        d["status"] = "unresolved"
    else:
        d["status"] = "open"
    return d


_JOIN = ("SELECT r.*, a.name AS account_name, a.currency AS currency, c.kind AS category_kind "
         "FROM reconciliations r JOIN accounts a ON a.id = r.account_id "
         "JOIN account_categories c ON c.id = a.category_id")


def get_reconciliation(conn, rec_id):
    rec = conn.execute(f"{_JOIN} WHERE r.id = ?", (rec_id,)).fetchone()
    if rec is None:
        raise NotFoundError("Reconciliation not found.")
    return _describe(conn, rec, _latest_ids(conn))


def list_reconciliations(conn, *, account_id=None, start_utc=None, end_utc=None, limit=200):
    where, params = [], []
    if account_id:
        where.append("r.account_id = ?")
        params.append(account_id)
    if start_utc:
        where.append("r.verified_at >= ?")
        params.append(start_utc)
    if end_utc:
        where.append("r.verified_at < ?")
        params.append(end_utc)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"{_JOIN} {clause} ORDER BY r.verified_at DESC, r.id DESC LIMIT ?", [*params, limit]).fetchall()
    latest = _latest_ids(conn)
    return [_describe(conn, r, latest) for r in rows]


def open_discrepancies(conn):
    """Latest count of each active account that does not (any longer) match the books."""
    latest = _latest_ids(conn)
    if not latest:
        return []
    out = []
    for rid in latest.values():
        rec = conn.execute(f"{_JOIN} WHERE r.id = ?", (rid,)).fetchone()
        if rec is None:
            continue
        acc_archived = conn.execute("SELECT is_archived FROM accounts WHERE id = ?", (rec["account_id"],)).fetchone()[0]
        if acc_archived:
            continue
        d = _describe(conn, rec, latest)
        if d["status"] in ("open", "unresolved"):
            out.append(d)
    return sorted(out, key=lambda d: d["verified_at"], reverse=True)


def get_timeline(conn, rec_id):
    rows = conn.execute(
        "SELECT e.*, t.description AS tx_description, t.type AS tx_type, t.status AS tx_status "
        "FROM reconciliation_entries e LEFT JOIN transactions t ON t.id = e.transaction_id "
        "WHERE e.reconciliation_id = ? ORDER BY e.id", (rec_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def nearby_transactions(conn, rec, limit=15):
    """Recent activity on the account around the count, to help jog memory."""
    rows = conn.execute(
        "SELECT t.id, t.occurred_at, t.type, t.amount_minor, t.description, t.source_account_id, t.dest_account_id "
        "FROM transactions t WHERE t.status = 'posted' AND (t.source_account_id = ? OR t.dest_account_id = ?) "
        "AND t.occurred_at <= ? ORDER BY t.occurred_at DESC, t.id DESC LIMIT ?",
        (rec["account_id"], rec["account_id"], rec["verified_at"], limit),
    ).fetchall()
    return [dict(r) for r in rows]


def add_note(conn, rec_id, note):
    note = (note or "").strip()
    if not note:
        raise ValidationError("Write a note first.", "note")
    if len(note) > 1000:
        raise ValidationError("Notes are limited to 1000 characters.", "note")
    with transaction(conn):
        _require(conn, rec_id)
        conn.execute(
            "INSERT INTO reconciliation_entries (reconciliation_id, entry_type, note, created_by, created_at)"
            " VALUES (?,?,?,?,?)", (rec_id, "note", note, audit.current_user_id(), utcnow_iso()),
        )


def _require(conn, rec_id):
    rec = conn.execute("SELECT * FROM reconciliations WHERE id = ?", (rec_id,)).fetchone()
    if rec is None:
        raise NotFoundError("Reconciliation not found.")
    return rec


def leave_unresolved(conn, rec_id, note=None):
    with transaction(conn):
        rec = _require(conn, rec_id)
        if _live_difference(conn, rec) == 0:
            raise ValidationError("This count already matches the books; there is nothing to leave open.")
        conn.execute("UPDATE reconciliations SET leave_unresolved = 1 WHERE id = ?", (rec_id,))
        conn.execute(
            "INSERT INTO reconciliation_entries (reconciliation_id, entry_type, note, created_by, created_at)"
            " VALUES (?,?,?,?,?)", (rec_id, "left_unresolved", (note or "").strip() or None, audit.current_user_id(), utcnow_iso()),
        )
        audit.log(conn, "leave_unresolved", "reconciliation", rec_id)


def reopen(conn, rec_id):
    with transaction(conn):
        rec = _require(conn, rec_id)
        if not rec["leave_unresolved"]:
            return
        conn.execute("UPDATE reconciliations SET leave_unresolved = 0 WHERE id = ?", (rec_id,))
        conn.execute(
            "INSERT INTO reconciliation_entries (reconciliation_id, entry_type, created_by, created_at) VALUES (?,?,?,?)",
            (rec_id, "reopened", audit.current_user_id(), utcnow_iso()),
        )
        audit.log(conn, "reopen", "reconciliation", rec_id)


def post_adjustment(conn, rec_id, note=None):
    """Post an explicit, labelled balance adjustment that makes the books match the count."""
    note = (note or "").strip() or None
    with transaction(conn):
        rec = _require(conn, rec_id)
        live = _live_difference(conn, rec)
        if live == 0:
            raise ValidationError("Nothing to adjust: the books already match the counted amount.")
        acc = conn.execute("SELECT * FROM accounts WHERE id = ?", (rec["account_id"],)).fetchone()
        if acc["is_archived"]:
            raise ValidationError("This account is archived.")
        cur = settings_svc.get_currency(conn, acc["currency"])
        description = (
            f"Reconciliation adjustment: {acc['name']} counted {format_money(rec['counted_minor'], cur)}, "
            f"books showed {format_money(rec['counted_minor'] - live, cur)}"
        )
        ledger.post_transaction(conn, {
            "type": "adjustment",
            "account_id": acc["id"],
            "direction": "increase" if live > 0 else "decrease",
            "amount_minor": abs(live),
            "occurred_at": rec["verified_at"],
            "description": description,
            "notes": note,
            "reconciliation_id": rec_id,
        })
