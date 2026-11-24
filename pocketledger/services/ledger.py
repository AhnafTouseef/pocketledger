"""Ledger engine: the only code that writes financial records.

Model
-----
Every transaction posts balanced double-entry lines (debit total == credit total):

    income              Dr account(dest)            Cr income
    expense             Dr expense                  Cr account(source)
    transfer            Dr account(dest)            Cr account(source)      <- one transaction, two lines
    lent                Dr receivable(person)       Cr account(source)
    borrowed            Dr account(dest)            Cr payable(person)
    repayment_received  Dr account(dest)            Cr receivable(person)
    repayment_made      Dr payable(person)          Cr account(source)
    adjustment (up)     Dr account(dest)            Cr equity
    adjustment (down)   Dr equity                  Cr account(source)

Assets (accounts, receivables) grow with debits; liabilities (payables) grow with credits.
Hence Net worth = assets - liabilities is untouched by transfers, lending, borrowing and
repayments, and changes only through income, expenses and adjustments.

Rows are immutable. "Editing" a financial field voids the original and posts a replacement
linked to it; both are recorded in the audit log. Nothing is ever deleted.
"""
from __future__ import annotations

import re
import uuid as uuidlib
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from ..db import transaction
from ..money import MAX_MINOR, MoneyError, format_money, parse_amount
from ..timeutil import UTC_FMT, is_valid_utc_iso, local_input_to_utc_iso, parse_date, utcnow, utcnow_iso
from . import audit, settings as settings_svc
from .errors import NotFoundError, ValidationError

TX_TYPES = (
    "income", "expense", "transfer", "lent", "borrowed",
    "repayment_received", "repayment_made", "adjustment",
)
TYPE_LABELS = {
    "income": "Income",
    "expense": "Expense",
    "transfer": "Transfer",
    "lent": "Money lent",
    "borrowed": "Money borrowed",
    "repayment_received": "Repayment received",
    "repayment_made": "Repayment made",
    "adjustment": "Balance adjustment",
}
NEEDS_SOURCE = {"expense", "transfer", "lent", "repayment_made"}
NEEDS_DEST = {"income", "transfer", "borrowed", "repayment_received"}
NEEDS_PERSON = {"lent", "borrowed", "repayment_received", "repayment_made"}
FINANCIAL_FIELDS = ("type", "amount_minor", "occurred_at", "source_account_id", "dest_account_id", "person_id")

PostResult = namedtuple("PostResult", "id uuid created warnings")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# --------------------------------------------------------------------------------------
# Entry construction (the accounting rules live here and only here)
# --------------------------------------------------------------------------------------
def build_entries(tx_type, amount, source_id, dest_id, person_id):
    def line(target, side, account_id=None, pid=None):
        return {
            "target_type": target,
            "account_id": account_id,
            "person_id": pid,
            "debit_minor": amount if side == "D" else 0,
            "credit_minor": amount if side == "C" else 0,
        }

    if tx_type == "income":
        return [line("account", "D", dest_id), line("income", "C")]
    if tx_type == "expense":
        return [line("expense", "D"), line("account", "C", source_id)]
    if tx_type == "transfer":
        return [line("account", "D", dest_id), line("account", "C", source_id)]
    if tx_type == "lent":
        return [line("receivable", "D", pid=person_id), line("account", "C", source_id)]
    if tx_type == "borrowed":
        return [line("account", "D", dest_id), line("payable", "C", pid=person_id)]
    if tx_type == "repayment_received":
        return [line("account", "D", dest_id), line("receivable", "C", pid=person_id)]
    if tx_type == "repayment_made":
        return [line("payable", "D", pid=person_id), line("account", "C", source_id)]
    if tx_type == "adjustment":
        if dest_id is not None:
            return [line("account", "D", dest_id), line("equity", "C")]
        return [line("equity", "D"), line("account", "C", source_id)]
    raise ValueError(f"unknown transaction type {tx_type!r}")


def _entry_signature(entries):
    return sorted(
        (e["target_type"], e["account_id"], e["person_id"], e["debit_minor"], e["credit_minor"]) for e in entries
    )


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------
def _to_int(raw, field, message):
    if raw is None or raw == "" or isinstance(raw, bool):
        raise ValidationError(message, field)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError(message, field)


def _load_account(conn, raw, field, message, keep_ids):
    aid = _to_int(raw, field, message)
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (aid,)).fetchone()
    if row is None:
        raise ValidationError("That account doesn't exist.", field)
    if row["is_archived"] and aid not in keep_ids:
        raise ValidationError(f"“{row['name']}” is archived. Choose an active account.", field)
    return row


def _default_description(tx_type, src, dst, person, category, direction=None):
    if tx_type == "income":
        return f"Income: {category['name']}" if category else f"Income to {dst['name']}"
    if tx_type == "expense":
        return f"Expense: {category['name']}" if category else f"Expense from {src['name']}"
    if tx_type == "transfer":
        return f"Transfer: {src['name']} → {dst['name']}"
    if tx_type == "lent":
        return f"Lent to {person['name']}"
    if tx_type == "borrowed":
        return f"Borrowed from {person['name']}"
    if tx_type == "repayment_received":
        return f"Repayment from {person['name']}"
    if tx_type == "repayment_made":
        return f"Repayment to {person['name']}"
    acc = dst or src
    return f"Balance adjustment: {acc['name']} ({'increase' if dst else 'decrease'})"


def prepare(conn, data, *, keep_account_ids=(), keep_person_ids=()):
    """Validate user/API input and return a normalised dict ready to insert."""
    tx_type = data.get("type")
    if tx_type not in TX_TYPES:
        raise ValidationError("Choose a transaction type.", "type")

    src = dst = person = category = None
    if tx_type == "adjustment":
        acc = _load_account(conn, data.get("account_id"), "account_id", "Choose an account to adjust.", keep_account_ids)
        direction = data.get("direction")
        if direction not in ("increase", "decrease"):
            raise ValidationError("Choose whether the balance goes up or down.", "direction")
        if direction == "increase":
            dst = acc
        else:
            src = acc
    else:
        if tx_type in NEEDS_SOURCE:
            label = "Choose the account the money comes from." if tx_type == "transfer" else "Choose an account to pay from."
            src = _load_account(conn, data.get("source_account_id"), "source_account_id", label, keep_account_ids)
        if tx_type in NEEDS_DEST:
            label = "Choose the account the money goes into." if tx_type == "transfer" else "Choose an account to receive into."
            dst = _load_account(conn, data.get("dest_account_id"), "dest_account_id", label, keep_account_ids)

    if src is not None and dst is not None and src["id"] == dst["id"]:
        raise ValidationError("Source and destination must be different accounts.", "dest_account_id")
    if src is not None and dst is not None and src["currency"] != dst["currency"]:
        raise ValidationError("Transfers between different currencies aren't supported yet.", "dest_account_id")

    account = src or dst
    currency_code = account["currency"]
    currency = settings_svc.get_currency(conn, currency_code)

    # amount
    if isinstance(data.get("amount_minor"), int) and not isinstance(data.get("amount_minor"), bool):
        amount = data["amount_minor"]
        if amount <= 0 or amount > MAX_MINOR:
            raise ValidationError("Enter an amount greater than zero.", "amount")
    else:
        try:
            amount = parse_amount(data.get("amount"), currency["minor_unit"])
        except MoneyError as exc:
            raise ValidationError(str(exc), "amount")
        if amount <= 0:
            raise ValidationError("Enter an amount greater than zero.", "amount")

    # person
    if tx_type in NEEDS_PERSON:
        pid = _to_int(data.get("person_id"), "person_id", "Choose a person.")
        person = conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
        if person is None:
            raise ValidationError("That person doesn't exist.", "person_id")
        if person["is_archived"] and pid not in keep_person_ids:
            raise ValidationError(f"“{person['name']}” is archived.", "person_id")

    # category (income / expense only)
    category_id = None
    if tx_type in ("income", "expense") and data.get("category_id") not in (None, "", 0, "0"):
        cid = _to_int(data["category_id"], "category_id", "Choose a valid category.")
        category = conn.execute("SELECT * FROM transaction_categories WHERE id = ?", (cid,)).fetchone()
        if category is None or category["kind"] != tx_type:
            raise ValidationError(f"Choose an {tx_type} category.", "category_id")
        category_id = cid

    # time
    tz = settings_svc.tzname(conn)
    now = utcnow()
    if data.get("occurred_local"):
        try:
            occurred_at = local_input_to_utc_iso(str(data["occurred_local"]), tz)
        except ValueError:
            raise ValidationError("Enter a valid date and time.", "occurred_local")
    elif data.get("occurred_at"):
        occurred_at = str(data["occurred_at"])
        if not is_valid_utc_iso(occurred_at):
            raise ValidationError("Enter a valid date and time.", "occurred_at")
    else:
        occurred_at = now.strftime(UTC_FMT)
    when = datetime.strptime(occurred_at, UTC_FMT).replace(tzinfo=timezone.utc)
    if when > now + timedelta(days=1):
        raise ValidationError("That date is in the future. Check the date and year.", "occurred_local")
    if when.year < 2000:
        raise ValidationError("That date is too far in the past. Check the year.", "occurred_local")

    # due date
    due_date = None
    if tx_type in ("lent", "borrowed") and data.get("due_date"):
        try:
            due_date = parse_date(str(data["due_date"])).isoformat()
        except ValueError:
            raise ValidationError("Enter a valid due date.", "due_date")

    # text
    description = (data.get("description") or "").strip()
    if len(description) > 200:
        raise ValidationError("Description is limited to 200 characters.", "description")
    notes = (data.get("notes") or "").strip() or None
    if notes and len(notes) > 2000:
        raise ValidationError("Notes are limited to 2000 characters.", "notes")
    if not description:
        description = _default_description(tx_type, src, dst, person, category)

    # reconciliation link
    recon_id = None
    if data.get("reconciliation_id") not in (None, "", 0, "0"):
        recon_id = _to_int(data["reconciliation_id"], "reconciliation_id", "Invalid reconciliation.")
        rec = conn.execute("SELECT account_id FROM reconciliations WHERE id = ?", (recon_id,)).fetchone()
        if rec is None:
            raise ValidationError("That reconciliation doesn't exist.", "reconciliation_id")
        if rec["account_id"] not in {a["id"] for a in (src, dst) if a is not None}:
            raise ValidationError("This transaction doesn't involve the account being reconciled.", "reconciliation_id")

    # identity / idempotency key
    tx_uuid = data.get("uuid")
    if tx_uuid in (None, ""):
        tx_uuid = str(uuidlib.uuid4())
    elif not isinstance(tx_uuid, str) or not _UUID_RE.match(tx_uuid):
        raise ValidationError("Invalid transaction id.", "uuid")

    return {
        "uuid": tx_uuid.lower(),
        "type": tx_type,
        "amount_minor": amount,
        "currency": currency_code,
        "currency_row": currency,
        "occurred_at": occurred_at,
        "source_account_id": src["id"] if src is not None else None,
        "dest_account_id": dst["id"] if dst is not None else None,
        "person_id": person["id"] if person is not None else None,
        "person_name": person["name"] if person is not None else None,
        "category_id": category_id,
        "description": description,
        "notes": notes,
        "due_date": due_date,
        "reconciliation_id": recon_id,
    }


# --------------------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------------------
def _resolve_new_person(conn, data):
    """Allow 'new_person_name' so a loan to a new contact is one step. Runs inside the
    caller's transaction: if posting fails, the new person is rolled back with it."""
    name = (data.get("new_person_name") or "").strip()
    if data.get("person_id") not in (None, "", 0, "0") or not name or data.get("type") not in NEEDS_PERSON:
        return
    if len(name) > 80:
        raise ValidationError("Names are limited to 80 characters.", "new_person_name")
    row = conn.execute("SELECT id FROM people WHERE name = ? AND is_archived = 0", (name,)).fetchone()
    if row is not None:
        data["person_id"] = row["id"]
        return
    now = utcnow_iso()
    cur = conn.execute(
        "INSERT INTO people (name, created_at, updated_at) VALUES (?, ?, ?)", (name, now, now)
    )
    data["person_id"] = cur.lastrowid
    audit.log(conn, "create", "person", cur.lastrowid, after={"name": name, "via": "quick transaction"})


def person_outstanding(conn, person_id, currency, side):
    """Outstanding receivable ('receivable') or payable ('payable') for a person."""
    col = "receivable_minor" if side == "receivable" else "payable_minor"
    row = conn.execute(
        f"SELECT {col} FROM v_person_balances WHERE person_id = ? AND currency = ?", (person_id, currency)
    ).fetchone()
    return row[0] if row else 0


def _check_repayment_limit(conn, c):
    if c["type"] not in ("repayment_received", "repayment_made"):
        return
    side = "receivable" if c["type"] == "repayment_received" else "payable"
    outstanding = person_outstanding(conn, c["person_id"], c["currency"], side)
    if c["amount_minor"] > outstanding:
        fmt = format_money(outstanding, c["currency_row"])
        who = c["person_name"]
        if c["type"] == "repayment_received":
            msg = f"{who} owes you {fmt}, so a larger repayment can't be recorded. Record the extra as a new loan from them if that's what happened."
        else:
            msg = f"You owe {who} {fmt}, so a larger repayment can't be recorded. Record the extra as a new loan to them if that's what happened."
        raise ValidationError(msg, "amount")


def _assert_people_consistent(conn, person_ids):
    """After a void/replace, nobody's outstanding balance may go negative."""
    for pid in {p for p in person_ids if p is not None}:
        for r in conn.execute(
            "SELECT b.currency, b.receivable_minor, b.payable_minor, p.name FROM v_person_balances b "
            "JOIN people p ON p.id = b.person_id WHERE b.person_id = ?", (pid,)
        ):
            if r["receivable_minor"] < 0 or r["payable_minor"] < 0:
                raise ValidationError(
                    f"That change would leave {r['name']}'s balance negative, because later repayments "
                    "depend on it. Void or correct those repayments first.",
                )


def _insert_transaction(conn, c, *, replaces_id=None):
    cur = conn.execute(
        "INSERT INTO transactions (uuid, type, amount_minor, currency, occurred_at, source_account_id,"
        " dest_account_id, person_id, category_id, description, notes, due_date, reconciliation_id,"
        " replaces_id, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            c["uuid"], c["type"], c["amount_minor"], c["currency"], c["occurred_at"], c["source_account_id"],
            c["dest_account_id"], c["person_id"], c["category_id"], c["description"], c["notes"], c["due_date"],
            c["reconciliation_id"], replaces_id, audit.current_user_id(), utcnow_iso(),
        ),
    )
    tx_id = cur.lastrowid
    entries = build_entries(c["type"], c["amount_minor"], c["source_account_id"], c["dest_account_id"], c["person_id"])
    if sum(e["debit_minor"] for e in entries) != sum(e["credit_minor"] for e in entries):
        raise AssertionError("unbalanced entries")  # programming error; rolls back the transaction
    for i, e in enumerate(entries, 1):
        conn.execute(
            "INSERT INTO transaction_entries (transaction_id, line_no, target_type, account_id, person_id,"
            " debit_minor, credit_minor) VALUES (?,?,?,?,?,?,?)",
            (tx_id, i, e["target_type"], e["account_id"], e["person_id"], e["debit_minor"], e["credit_minor"]),
        )
    if c["reconciliation_id"]:
        conn.execute(
            "INSERT INTO reconciliation_entries (reconciliation_id, entry_type, transaction_id, amount_minor, note,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                c["reconciliation_id"],
                "adjustment_posted" if c["type"] == "adjustment" else "transaction_linked",
                tx_id, c["amount_minor"], c["description"], audit.current_user_id(), utcnow_iso(),
            ),
        )
    return tx_id


def snapshot(conn, tx_id):
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return dict(row) if row else None


def _duplicate_warnings(conn, c, new_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE id <> ? AND status = 'posted' AND type = ? AND amount_minor = ?"
        " AND source_account_id IS ? AND dest_account_id IS ? AND person_id IS ?"
        " AND created_at >= ?",
        (
            new_id, c["type"], c["amount_minor"], c["source_account_id"], c["dest_account_id"], c["person_id"],
            (utcnow() - timedelta(minutes=2)).strftime(UTC_FMT),
        ),
    ).fetchone()
    if row[0]:
        return ["An identical transaction was recorded in the last two minutes. If this was a double tap, void one of them."]
    return []


def post_transaction(conn, data, *, replaces_id=None) -> PostResult:
    """Validate and post one transaction atomically. Idempotent on `uuid`."""
    data = dict(data)
    supplied = data.get("uuid")
    if supplied not in (None, "") and not (isinstance(supplied, str) and _UUID_RE.match(supplied)):
        raise ValidationError("Invalid transaction id.", "uuid")
    with transaction(conn):
        if supplied:
            existing = conn.execute("SELECT id, uuid FROM transactions WHERE uuid = ?", (supplied.lower(),)).fetchone()
            if existing is not None:
                return PostResult(existing["id"], existing["uuid"], False, [])
        _resolve_new_person(conn, data)
        c = prepare(conn, data)
        _check_repayment_limit(conn, c)
        tx_id = _insert_transaction(conn, c, replaces_id=replaces_id)
        audit.log(conn, "create", "transaction", tx_id, after=snapshot(conn, tx_id))
        warnings = _duplicate_warnings(conn, c, tx_id)
    return PostResult(tx_id, c["uuid"], True, warnings)


# --------------------------------------------------------------------------------------
# Corrections: void / edit (never destructive)
# --------------------------------------------------------------------------------------
def _get_posted(conn, tx_id):
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if row is None:
        raise NotFoundError("Transaction not found.")
    if row["status"] == "voided":
        raise ValidationError("This transaction has already been voided.")
    return row


def _void_row(conn, row, reason):
    conn.execute(
        "UPDATE transactions SET status = 'voided', voided_at = ?, void_reason = ? WHERE id = ?",
        (utcnow_iso(), reason, row["id"]),
    )


def void_transaction(conn, tx_id, reason):
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValidationError("Give a short reason for voiding this transaction.", "reason")
    with transaction(conn):
        row = _get_posted(conn, tx_id)
        before = dict(row)
        _void_row(conn, row, reason)
        _assert_people_consistent(conn, [row["person_id"]])
        audit.log(conn, "void", "transaction", tx_id, before=before, after=snapshot(conn, tx_id), reason=reason)


def edit_transaction(conn, tx_id, data, reason=None):
    """Apply an edit. Returns (transaction_id, kind) where kind is 'updated' (non-financial
    fields changed in place) or 'replaced' (original voided, new transaction posted)."""
    data = dict(data)
    with transaction(conn):
        old = _get_posted(conn, tx_id)
        _resolve_new_person(conn, data)
        data["uuid"] = None  # a replacement gets its own identity
        keep_acc = {a for a in (old["source_account_id"], old["dest_account_id"]) if a is not None}
        keep_ppl = {old["person_id"]} if old["person_id"] is not None else set()
        c = prepare(conn, data, keep_account_ids=keep_acc, keep_person_ids=keep_ppl)
        changed = [f for f in FINANCIAL_FIELDS if old[f] != c[f]]
        before = dict(old)
        if not changed:
            if c["description"] == old["description"] and c["notes"] == old["notes"] \
                    and c["category_id"] == old["category_id"] and c["due_date"] == old["due_date"]:
                return tx_id, "unchanged"
            conn.execute(
                "UPDATE transactions SET description = ?, notes = ?, category_id = ?, due_date = ? WHERE id = ?",
                (c["description"], c["notes"], c["category_id"], c["due_date"], tx_id),
            )
            audit.log(conn, "update", "transaction", tx_id, before=before, after=snapshot(conn, tx_id),
                      reason=(reason or "").strip() or None)
            return tx_id, "updated"

        reason = (reason or "").strip() or "Corrected"
        _void_row(conn, old, f"Replaced by correction: {reason}")
        c["reconciliation_id"] = None
        if old["reconciliation_id"]:
            rec = conn.execute("SELECT account_id FROM reconciliations WHERE id = ?", (old["reconciliation_id"],)).fetchone()
            if rec and rec["account_id"] in (c["source_account_id"], c["dest_account_id"]):
                c["reconciliation_id"] = old["reconciliation_id"]
        _check_repayment_limit(conn, c)
        new_id = _insert_transaction(conn, c, replaces_id=tx_id)
        conn.execute("UPDATE transactions SET replaced_by_id = ? WHERE id = ?", (new_id, tx_id))
        _assert_people_consistent(conn, [old["person_id"], c["person_id"]])
        audit.log(conn, "correct", "transaction", tx_id, before=before, after=snapshot(conn, new_id),
                  reason=reason)
        audit.log(conn, "create", "transaction", new_id, after=snapshot(conn, new_id),
                  reason=f"Replaces #{tx_id}: {reason}")
        return new_id, "replaced"


# --------------------------------------------------------------------------------------
# Derived figures
# --------------------------------------------------------------------------------------
def account_balance(conn, account_id, as_of_iso=None):
    row = conn.execute("SELECT opening_balance_minor, COALESCE(opening_at, opened_on) AS since "
                       "FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFoundError("Account not found.")
    # Entries dated before the account's opening moment are already inside the opening balance.
    sql = ("SELECT COALESCE(SUM(debit_minor - credit_minor), 0) FROM v_posted_entries "
           "WHERE target_type = 'account' AND account_id = ? AND occurred_at >= ?")
    params = [account_id, row["since"]]
    if as_of_iso:
        sql += " AND occurred_at <= ?"
        params.append(as_of_iso)
    return row[0] + conn.execute(sql, params).fetchone()[0]


def account_balances(conn):
    return {r["account_id"]: r["balance_minor"] for r in conn.execute("SELECT account_id, balance_minor FROM v_account_balances")}


def totals(conn, currency):
    """Financial overview for one currency. Net worth = assets - liabilities, where
    assets = account balances + receivables and liabilities = payables."""
    accounts_total = conn.execute(
        "SELECT COALESCE(SUM(v.balance_minor), 0) FROM v_account_balances v "
        "JOIN accounts a ON a.id = v.account_id WHERE a.currency = ?", (currency,)
    ).fetchone()[0]
    r = conn.execute(
        "SELECT COALESCE(SUM(receivable_minor), 0), COALESCE(SUM(payable_minor), 0) FROM v_person_balances WHERE currency = ?",
        (currency,),
    ).fetchone()
    receivables, payables = r[0], r[1]
    assets = accounts_total + receivables
    return {
        "currency": currency,
        "liquid_minor": accounts_total,
        "receivables_minor": receivables,
        "payables_minor": payables,
        "assets_minor": assets,
        "liabilities_minor": payables,
        "net_worth_minor": assets - payables,
    }


def verify_integrity(conn):
    """Independent audit of the whole ledger. Returns a list of problems (empty == healthy).

    Checks: SQLite integrity + foreign keys; every transaction's entries balance and match
    the accounting rule for its type exactly; no negative person balances; and the
    net-worth identity  assets - liabilities == opening balances + income - expenses + adjustments
    (minus any account legs dated before that account's opening moment, which its balance ignores).
    """
    issues = []
    ic = conn.execute("PRAGMA integrity_check").fetchall()
    if [r[0] for r in ic] != ["ok"]:
        issues.append("SQLite integrity check failed: " + "; ".join(str(r[0]) for r in ic[:3]))
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        issues.append(f"{len(fk)} foreign key violation(s).")

    entries_by_tx = {}
    for e in conn.execute("SELECT * FROM transaction_entries ORDER BY transaction_id, line_no"):
        entries_by_tx.setdefault(e["transaction_id"], []).append(dict(e))
    for t in conn.execute("SELECT * FROM transactions"):
        entries = entries_by_tx.get(t["id"], [])
        if not entries:
            issues.append(f"Transaction #{t['id']} has no ledger entries.")
            continue
        if sum(e["debit_minor"] for e in entries) != sum(e["credit_minor"] for e in entries):
            issues.append(f"Transaction #{t['id']} is unbalanced (debits != credits).")
        expected = build_entries(t["type"], t["amount_minor"], t["source_account_id"], t["dest_account_id"], t["person_id"])
        if _entry_signature(expected) != _entry_signature(entries):
            issues.append(f"Transaction #{t['id']} ({t['type']}) has ledger entries that don't match its type.")
        if t["status"] == "voided" and t["replaced_by_id"]:
            if conn.execute("SELECT 1 FROM transactions WHERE id = ?", (t["replaced_by_id"],)).fetchone() is None:
                issues.append(f"Transaction #{t['id']} points to a missing replacement.")

    for r in conn.execute(
        "SELECT b.person_id, b.currency, b.receivable_minor, b.payable_minor, p.name FROM v_person_balances b "
        "JOIN people p ON p.id = b.person_id"
    ):
        if r["receivable_minor"] < 0 or r["payable_minor"] < 0:
            issues.append(f"{r['name']} has a negative outstanding balance.")

    for cur in [r[0] for r in conn.execute("SELECT code FROM currencies")]:
        t = totals(conn, cur)
        opening = conn.execute(
            "SELECT COALESCE(SUM(opening_balance_minor), 0) FROM accounts WHERE currency = ?", (cur,)
        ).fetchone()[0]
        flows = conn.execute(
            "SELECT COALESCE(SUM(CASE target_type WHEN 'income' THEN credit_minor WHEN 'expense' THEN -debit_minor"
            " WHEN 'equity' THEN credit_minor - debit_minor ELSE 0 END), 0) FROM v_posted_entries WHERE currency = ?",
            (cur,),
        ).fetchone()[0]
        # Account legs dated before the account's opening moment are not counted in its balance
        # (the opening balance already reflects them), so they are removed from the identity too.
        skipped = conn.execute(
            "SELECT COALESCE(SUM(pe.debit_minor - pe.credit_minor), 0) FROM v_posted_entries pe "
            "JOIN accounts a ON a.id = pe.account_id "
            "WHERE pe.target_type = 'account' AND a.currency = ? AND pe.occurred_at < COALESCE(a.opening_at, a.opened_on)",
            (cur,),
        ).fetchone()[0]
        expected = opening + flows - skipped
        if t["net_worth_minor"] != expected:
            issues.append(
                f"Net worth identity broken for {cur}: assets - liabilities = {t['net_worth_minor']}, "
                f"but opening balances + income - expenses + adjustments (+ pre-opening history) = {expected}."
            )
    return issues
