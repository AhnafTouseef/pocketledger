"""User-managed categories: transaction categories and account types."""
from __future__ import annotations

from ..db import transaction
from . import audit
from .errors import NotFoundError, ValidationError

ACCOUNT_KINDS = {"physical": "Physical cash", "bank": "Bank", "wallet": "Mobile wallet", "other": "Other"}
KIND_ICONS = {"physical": "banknote", "bank": "landmark", "wallet": "smartphone", "other": "wallet"}


def _name(value):
    value = (value or "").strip()
    if not value:
        raise ValidationError("Enter a name.", "name")
    if len(value) > 40:
        raise ValidationError("Names are limited to 40 characters.", "name")
    return value


# ---- transaction categories ---------------------------------------------------------
def list_transaction_categories(conn, kind=None, include_archived=False):
    sql = "SELECT * FROM transaction_categories WHERE 1=1"
    params = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if not include_archived:
        sql += " AND is_archived = 0"
    return [dict(r) for r in conn.execute(sql + " ORDER BY kind, sort_order, name", params)]


def add_transaction_category(conn, name, kind):
    name = _name(name)
    if kind not in ("income", "expense"):
        raise ValidationError("Choose income or expense.", "kind")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM transaction_categories WHERE name = ? AND kind = ?", (name, kind)).fetchone():
            raise ValidationError("That category already exists (it may be archived).", "name")
        cur = conn.execute("INSERT INTO transaction_categories (name, kind, sort_order) VALUES (?,?,50)", (name, kind))
        audit.log(conn, "create", "transaction_category", cur.lastrowid, after={"name": name, "kind": kind})


def rename_transaction_category(conn, cat_id, name):
    name = _name(name)
    with transaction(conn):
        row = conn.execute("SELECT * FROM transaction_categories WHERE id = ?", (cat_id,)).fetchone()
        if row is None:
            raise NotFoundError("Category not found.")
        if conn.execute("SELECT 1 FROM transaction_categories WHERE name = ? AND kind = ? AND id <> ?",
                        (name, row["kind"], cat_id)).fetchone():
            raise ValidationError("A category with that name already exists.", "name")
        conn.execute("UPDATE transaction_categories SET name = ? WHERE id = ?", (name, cat_id))
        audit.log(conn, "rename", "transaction_category", cat_id, before={"name": row["name"]}, after={"name": name})


def set_transaction_category_archived(conn, cat_id, archived: bool):
    with transaction(conn):
        row = conn.execute("SELECT * FROM transaction_categories WHERE id = ?", (cat_id,)).fetchone()
        if row is None:
            raise NotFoundError("Category not found.")
        conn.execute("UPDATE transaction_categories SET is_archived = ? WHERE id = ?", (1 if archived else 0, cat_id))
        audit.log(conn, "archive" if archived else "unarchive", "transaction_category", cat_id, before={"name": row["name"]})


# ---- account types ------------------------------------------------------------------
def list_account_categories(conn, include_archived=False):
    where = "" if include_archived else "WHERE is_archived = 0"
    return [dict(r) for r in conn.execute(f"SELECT * FROM account_categories {where} ORDER BY sort_order, name")]


def add_account_category(conn, name, kind):
    name = _name(name)
    if kind not in ACCOUNT_KINDS:
        raise ValidationError("Choose how this type behaves.", "kind")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM account_categories WHERE name = ?", (name,)).fetchone():
            raise ValidationError("That account type already exists.", "name")
        cur = conn.execute(
            "INSERT INTO account_categories (name, kind, icon, sort_order) VALUES (?,?,?,50)",
            (name, kind, KIND_ICONS[kind]),
        )
        audit.log(conn, "create", "account_category", cur.lastrowid, after={"name": name, "kind": kind})


def rename_account_category(conn, cat_id, name):
    name = _name(name)
    with transaction(conn):
        row = conn.execute("SELECT * FROM account_categories WHERE id = ?", (cat_id,)).fetchone()
        if row is None:
            raise NotFoundError("Account type not found.")
        if conn.execute("SELECT 1 FROM account_categories WHERE name = ? AND id <> ?", (name, cat_id)).fetchone():
            raise ValidationError("An account type with that name already exists.", "name")
        conn.execute("UPDATE account_categories SET name = ? WHERE id = ?", (name, cat_id))
        audit.log(conn, "rename", "account_category", cat_id, before={"name": row["name"]}, after={"name": name})


def set_account_category_archived(conn, cat_id, archived: bool):
    with transaction(conn):
        row = conn.execute("SELECT * FROM account_categories WHERE id = ?", (cat_id,)).fetchone()
        if row is None:
            raise NotFoundError("Account type not found.")
        if archived and conn.execute(
            "SELECT 1 FROM accounts WHERE category_id = ? AND is_archived = 0", (cat_id,)
        ).fetchone():
            raise ValidationError("Active accounts still use this type. Archive or change them first.")
        conn.execute("UPDATE account_categories SET is_archived = ? WHERE id = ?", (1 if archived else 0, cat_id))
        audit.log(conn, "archive" if archived else "unarchive", "account_category", cat_id, before={"name": row["name"]})
