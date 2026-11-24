"""Accounts: where money physically or digitally sits."""
from __future__ import annotations

from ..db import transaction
from ..money import MoneyError, format_money, parse_amount
from ..timeutil import day_start_utc, parse_date, today_local, utcnow, utcnow_iso
from . import audit, ledger, settings as settings_svc
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
        "SELECT id FROM accounts WHERE name = ? AND is_archived = 0 AND id IS NOT ?", (name, exclude_id)
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
    with transaction(conn):
        _check_name_free(conn, name)
        now = utcnow_iso()
        c = conn.execute(
            "INSERT INTO accounts (name, category_id, currency, opening_balance_minor, opened_on, description,"
            " created_at, updated_at, opening_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, cid, cur["code"], opening_minor, opened.isoformat(), description, now, now,
             day_start_utc(opened, tz)),
        )
        audit.log(conn, "create", "account", c.lastrowid,
                  after={"name": name, "opening_balance_minor": opening_minor, "currency": cur["code"]})
    return c.lastrowid


def update_account(conn, account_id, *, name, category_id, description, opening_balance=None):
    name = _clean_text(name, 60, "name", required=True, label="Account name")
    cid = _check_category(conn, category_id)
    description = _clean_text(description, 300, "description", label="Description")
    with transaction(conn):
        old = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if old is None:
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
        conn.execute(
            "UPDATE accounts SET name = ?, category_id = ?, description = ?, opening_balance_minor = ?, updated_at = ?"
            " WHERE id = ?", (name, cid, description, new_opening, utcnow_iso(), account_id),
        )
        audit.log(conn, "update", "account", account_id,
                  before={k: old[k] for k in ("name", "category_id", "description", "opening_balance_minor")},
                  after={"name": name, "category_id": cid, "description": description, "opening_balance_minor": new_opening})


def has_history(conn, account_id) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM transaction_entries WHERE account_id = ? LIMIT 1", (account_id,)).fetchone()
        or conn.execute("SELECT 1 FROM reconciliations WHERE account_id = ? LIMIT 1", (account_id,)).fetchone()
    )


def archive_account(conn, account_id):
    with transaction(conn):
        acc = get_account(conn, account_id)
        if acc["is_archived"]:
            return
        if acc["balance_minor"] != 0:
            cur = settings_svc.get_currency(conn, acc["currency"])
            raise ValidationError(
                f"“{acc['name']}” still holds {format_money(acc['balance_minor'], cur)}. Move it to another "
                "account (or post an adjustment) before archiving, so no money disappears from view.",
            )
        conn.execute("UPDATE accounts SET is_archived = 1, archived_at = ?, updated_at = ? WHERE id = ?",
                     (utcnow_iso(), utcnow_iso(), account_id))
        audit.log(conn, "archive", "account", account_id, before={"name": acc["name"]})


def unarchive_account(conn, account_id):
    with transaction(conn):
        acc = get_account(conn, account_id)
        _check_name_free(conn, acc["name"], exclude_id=account_id)
        conn.execute("UPDATE accounts SET is_archived = 0, archived_at = NULL, updated_at = ? WHERE id = ?",
                     (utcnow_iso(), account_id))
        audit.log(conn, "unarchive", "account", account_id, before={"name": acc["name"]})
