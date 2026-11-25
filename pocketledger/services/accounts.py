"""Accounts: where money physically or digitally sits."""
from __future__ import annotations

from ..db import transaction
from ..money import MoneyError, format_money, parse_amount
from ..timeutil import day_start_utc, parse_date, today_local, utcnow, utcnow_iso
from . import audit, ledger, settings as settings_svc, undo
from .errors import NotFoundError, ValidationError

_BASE_SQL = """
SELECT a.*, c.name AS category_name, c.kind AS category_kind, c.icon AS category_icon,
       v.balance_minor
FROM accounts a
JOIN account_categories c ON c.id = a.category_id
JOIN v_account_balances v ON v.account_id = a.id
"""


def list_accounts(conn, *, include_archived=False):
    where = "" if include_archived else "WHERE a.is_archived = 0"
    rows = conn.execute(f"{_BASE_SQL} {where} ORDER BY a.is_archived, c.sort_order, c.id, a.name").fetchall()
    return [_decorate(conn, dict(r)) for r in rows]


def get_account(conn, account_id):
    row = conn.execute(f"{_BASE_SQL} WHERE a.id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFoundError("Account not found.")
    return _decorate(conn, dict(row))


def _decorate(conn, acc):
    reminder_days = settings_svc.get_int(conn, "verify_reminder_days", 7)
    acc["needs_verification"] = False
    acc["verified_stale"] = False
    if acc["category_kind"] == "physical" and not acc["is_archived"]:
        if acc["last_verified_at"] is None:
            acc["needs_verification"] = acc["balance_minor"] != 0
        else:
            age = (utcnow() - _parse(acc["last_verified_at"])).days
            acc["verified_stale"] = age >= reminder_days
            acc["needs_verification"] = acc["verified_stale"] and acc["balance_minor"] != 0
    return acc


def _parse(iso):
    from ..timeutil import parse_utc
    return parse_utc(iso)


def _clean_text(value, limit, field, required=False, label="This"):
    value = (value or "").strip()
    if required and not value:
        raise ValidationError(f"{label} is required.", field)
    if len(value) > limit:
        raise ValidationError(f"{label} is limited to {limit} characters.", field)
    return value or None


def _check_name_free(conn, name, exclude_id=None):
    row = conn.execute(
        "SELECT id FROM accounts WHERE name = ? AND is_archived = 0 AND deleted_at IS NULL AND id IS NOT ?", (name, exclude_id)
    ).fetchone()
    if row is not None:
        raise ValidationError("You already have an active account with that name.", "name")


def _check_category(conn, category_id):
    try:
        cid = int(category_id)
    except (TypeError, ValueError):
        raise ValidationError("Choose an account type.", "category_id")
    row = conn.execute("SELECT * FROM account_categories WHERE id = ?", (cid,)).fetchone()
    if row is None or row["is_archived"]:
        raise ValidationError("Choose a valid account type.", "category_id")
    return cid


def create_account(conn, *, name, category_id, currency, opening_balance, opened_on=None, description=None):
    name = _clean_text(name, 60, "name", required=True, label="Account name")
    cid = _check_category(conn, category_id)
    cur = settings_svc.get_currency(conn, currency or settings_svc.base_currency_code(conn))
    if not cur["is_enabled"]:
        raise ValidationError("That currency isn't enabled.", "currency")
    try:
        opening_minor = parse_amount(opening_balance or "0", cur["minor_unit"])
    except MoneyError as exc:
        raise ValidationError(str(exc), "opening_balance")
    tz = settings_svc.tzname(conn)
    if opened_on:
        try:
            opened = parse_date(opened_on)
        except ValueError:
            raise ValidationError("Enter a valid date.", "opened_on")
        if opened > today_local(tz):
            raise ValidationError("The opening date can't be in the future.", "opened_on")
    else:
        opened = today_local(tz)
    description = _clean_text(description, 300, "description", label="Description")
    with transaction(conn), undo.group(conn, f"Created account “{name}”", ("accounts.index", {})) as grp:
        _check_name_free(conn, name)
        now = utcnow_iso()
        c = conn.execute(
            "INSERT INTO accounts (name, category_id, currency, opening_balance_minor, opened_on, description,"
            " created_at, updated_at, opening_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, cid, cur["code"], opening_minor, opened.isoformat(), description, now, now,
             day_start_utc(opened, tz)),
        )
        grp.created("accounts", c.lastrowid)
        audit.log(conn, "create", "account", c.lastrowid,
                  after={"name": name, "opening_balance_minor": opening_minor, "currency": cur["code"]})
    return c.lastrowid


def update_account(conn, account_id, *, name, category_id, description, opening_balance=None):
    name = _clean_text(name, 60, "name", required=True, label="Account name")
    cid = _check_category(conn, category_id)
    description = _clean_text(description, 300, "description", label="Description")
    with transaction(conn), undo.group(conn, f"Edited account “{name}”", ("accounts.detail", {"account_id": account_id})) as grp:
        old = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if old is None or old["deleted_at"]:
            raise NotFoundError("Account not found.")
        if not old["is_archived"]:
            _check_name_free(conn, name, exclude_id=account_id)
        new_opening = old["opening_balance_minor"]
        if opening_balance is not None:
            cur = settings_svc.get_currency(conn, old["currency"])
            try:
                new_opening = parse_amount(opening_balance or "0", cur["minor_unit"])
            except MoneyError as exc:
                raise ValidationError(str(exc), "opening_balance")
            if new_opening != old["opening_balance_minor"] and has_history(conn, account_id):
                raise ValidationError(
                    "The opening balance is locked once an account has history. Use a balance adjustment instead.",
                    "opening_balance",
                )
        now = utcnow_iso()
        conn.execute(
            "UPDATE accounts SET name = ?, category_id = ?, description = ?, opening_balance_minor = ?, updated_at = ?"
            " WHERE id = ?", (name, cid, description, new_opening, now, account_id),
        )
        fields = ("name", "category_id", "description", "opening_balance_minor")
        grp.changed("accounts", account_id, {**{f: old[f] for f in fields}, "updated_at": old["updated_at"]},
                    {"name": name, "category_id": cid, "description": description,
                     "opening_balance_minor": new_opening, "updated_at": now})
        audit.log(conn, "update", "account", account_id,
                  before={k: old[k] for k in ("name", "category_id", "description", "opening_balance_minor")},
                  after={"name": name, "category_id": cid, "description": description, "opening_balance_minor": new_opening})


def has_history(conn, account_id) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM transaction_entries WHERE account_id = ? LIMIT 1", (account_id,)).fetchone()
        or conn.execute("SELECT 1 FROM reconciliations WHERE account_id = ? LIMIT 1", (account_id,)).fetchone()
    )


def deletion_summary(conn, account_id):
    """What deleting this account would do, for the confirmation dialog."""
    acc = get_account(conn, account_id)
    own = _own_transaction_ids(conn, account_id)
    touching = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE status = 'posted' AND deleted_at IS NULL "
        "AND (source_account_id = ? OR dest_account_id = ?)", (account_id, account_id)).fetchone()[0]
    return {"account": acc, "own_count": len(own), "kept_count": touching - len(own)}


def _own_transaction_ids(conn, account_id):
    """Income, expenses and balance adjustments belong to this account alone, so they go with it.
    Transfers and loans also describe another account or a person; those stay (they show this
    account as 'deleted') so nothing else changes."""
    return [r[0] for r in conn.execute(
        "SELECT id FROM transactions WHERE status = 'posted' AND deleted_at IS NULL "
        "AND type IN ('income', 'expense', 'adjustment') AND (source_account_id = ? OR dest_account_id = ?)",
        (account_id, account_id))]


def delete_account(conn, account_id):
    """Delete an account whatever it holds. No balance is moved and no other account is touched."""
    with transaction(conn), undo.group(conn, "Deleted account", ("accounts.detail", {"account_id": account_id})) as grp:
        acc = get_account(conn, account_id)
        now = utcnow_iso()
        for tx_id in _own_transaction_ids(conn, account_id):
            ledger.soft_delete_transaction(conn, tx_id, grp, now)
        conn.execute("UPDATE accounts SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                     (now, now, account_id))
        grp.changed("accounts", account_id, {"deleted_at": None, "updated_at": acc["updated_at"]},
                    {"deleted_at": now, "updated_at": now})
        grp.label = f"Deleted account “{acc['name']}”"
        audit.log(conn, "delete", "account", account_id)
    return acc["name"]


def unarchive_account(conn, account_id):
    with transaction(conn):
        acc = get_account(conn, account_id)
        _check_name_free(conn, acc["name"], exclude_id=account_id)
        conn.execute("UPDATE accounts SET is_archived = 0, archived_at = NULL, updated_at = ? WHERE id = ?",
                     (utcnow_iso(), account_id))
        audit.log(conn, "unarchive", "account", account_id, before={"name": acc["name"]})
