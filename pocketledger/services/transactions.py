"""Read side of the transaction ledger: search, filters, sorting, details, CSV export."""
from __future__ import annotations

import csv
import io

from ..money import MoneyError, parse_amount, to_decimal_string
from ..timeutil import parse_date, range_bounds_utc, utc_iso_to_local
from . import audit, settings as settings_svc
from .errors import NotFoundError
from .ledger import TYPE_LABELS, TX_TYPES

SORTS = {"date": "t.occurred_at", "amount": "t.amount_minor", "created": "t.created_at"}

_SELECT = """
SELECT t.*, s.name AS source_name, d.name AS dest_name, p.name AS person_name,
       COALESCE(s.opening_at, s.opened_on) AS source_since, COALESCE(d.opening_at, d.opened_on) AS dest_since,
       c.name AS category_name, cur.symbol AS currency_symbol
FROM transactions t
LEFT JOIN accounts s ON s.id = t.source_account_id
LEFT JOIN accounts d ON d.id = t.dest_account_id
LEFT JOIN people p ON p.id = t.person_id
LEFT JOIN transaction_categories c ON c.id = t.category_id
JOIN currencies cur ON cur.code = t.currency
"""


def _like(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _intval(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _build_where(conn, f):
    tz = settings_svc.tzname(conn)
    where, params = [], []
    status = f.get("status") or "posted"
    if status in ("posted", "voided"):
        where.append("t.status = ?")
        params.append(status)
    q = (f.get("q") or "").strip()
    if q:
        clause = ("(t.description LIKE ? ESCAPE '\\' OR t.notes LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\' "
                  "OR s.name LIKE ? ESCAPE '\\' OR d.name LIKE ? ESCAPE '\\' OR c.name LIKE ? ESCAPE '\\'")
        params += [_like(q)] * 6
        try:
            minor = parse_amount(q, 2)
            clause += " OR t.amount_minor = ?"
            params.append(minor)
        except MoneyError:
            pass
        where.append(clause + ")")
    if f.get("type") in TX_TYPES:
        where.append("t.type = ?")
        params.append(f["type"])
    aid = _intval(f.get("account_id"))
    if aid:
        where.append("(t.source_account_id = ? OR t.dest_account_id = ?)")
        params += [aid, aid]
    pid = _intval(f.get("person_id"))
    if pid:
        where.append("t.person_id = ?")
        params.append(pid)
    cid = _intval(f.get("category_id"))
    if cid:
        where.append("t.category_id = ?")
        params.append(cid)
    for key, op in (("date_from", ">="), ("date_to", "<")):
        raw = (f.get(key) or "").strip()
        if raw:
            try:
                d = parse_date(raw)
            except ValueError:
                continue
            start, end = range_bounds_utc(d, d, tz)
            where.append(f"t.occurred_at {op} ?")
            params.append(start if key == "date_from" else end)
    return where, params


def search(conn, filters, *, sort="date", direction="desc", page=1, per_page=50):
    where, params = _build_where(conn, filters)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order_col = SORTS.get(sort, SORTS["date"])
    order_dir = "ASC" if direction == "asc" else "DESC"
    total = conn.execute(
        "SELECT COUNT(*) FROM transactions t LEFT JOIN accounts s ON s.id = t.source_account_id "
        "LEFT JOIN accounts d ON d.id = t.dest_account_id LEFT JOIN people p ON p.id = t.person_id "
        f"LEFT JOIN transaction_categories c ON c.id = t.category_id {clause}", params
    ).fetchone()[0]
    page = max(1, int(page))
    rows = conn.execute(
        f"{_SELECT} {clause} ORDER BY {order_col} {order_dir}, t.id {order_dir} LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    focus = _intval(filters.get("account_id"))
    items = [decorate(dict(r), focus_account_id=focus) for r in rows]
    return {
        "items": items, "total": total, "page": page, "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


def flow_direction(tx_type, has_source, has_dest):
    """+1 money comes in, -1 money goes out, 0 neutral (internal move)."""
    if tx_type in ("income", "borrowed", "repayment_received"):
        return 1
    if tx_type in ("expense", "lent", "repayment_made"):
        return -1
    if tx_type == "adjustment":
        return 1 if has_dest else -1
    return 0


def decorate(t, focus_account_id=None):
    t["type_label"] = TYPE_LABELS.get(t["type"], t["type"])
    t["direction"] = flow_direction(t["type"], t["source_account_id"] is not None, t["dest_account_id"] is not None)
    if focus_account_id and t["type"] == "transfer":
        t["direction"] = 1 if t["dest_account_id"] == focus_account_id else -1
    if t["type"] == "transfer":
        t["account_label"] = f"{t['source_name']} → {t['dest_name']}"
    else:
        t["account_label"] = t["source_name"] or t["dest_name"]
    # Accounts whose balance this transaction does NOT touch because it is dated before they were opened.
    t["pre_opening"] = [
        name for name, since in ((t.get("source_name"), t.get("source_since")), (t.get("dest_name"), t.get("dest_since")))
        if name and since and t["occurred_at"] < since
    ]
    return t


def get_transaction(conn, tx_id):
    row = conn.execute(f"{_SELECT} WHERE t.id = ?", (tx_id,)).fetchone()
    if row is None:
        raise NotFoundError("Transaction not found.")
    return decorate(dict(row))


def get_entries(conn, tx_id):
    rows = conn.execute(
        "SELECT e.*, a.name AS account_name, p.name AS person_name FROM transaction_entries e "
        "LEFT JOIN accounts a ON a.id = e.account_id LEFT JOIN people p ON p.id = e.person_id "
        "WHERE e.transaction_id = ? ORDER BY e.line_no", (tx_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_history(conn, tx_id):
    logs, _ = audit.list_logs(conn, entity_type="transaction", entity_id=tx_id, limit=50)
    return logs


def recent(conn, limit=8):
    rows = conn.execute(f"{_SELECT} WHERE t.status = 'posted' ORDER BY t.occurred_at DESC, t.id DESC LIMIT ?", (limit,)).fetchall()
    return [decorate(dict(r)) for r in rows]


def totals_for_filter(conn, filters):
    where, params = _build_where(conn, filters)
    where = [w for w in where if not w.startswith("t.status")] + ["t.status = 'posted'"]
    params = [p for p in params]
    # rebuild params without the status param (first element is the status when present)
    where_all, params_all = _build_where(conn, {**filters, "status": "posted"})
    clause = " AND ".join(where_all)
    rows = conn.execute(
        "SELECT t.type, SUM(t.amount_minor) AS total FROM transactions t "
        "LEFT JOIN accounts s ON s.id = t.source_account_id LEFT JOIN accounts d ON d.id = t.dest_account_id "
        "LEFT JOIN people p ON p.id = t.person_id LEFT JOIN transaction_categories c ON c.id = t.category_id "
        f"WHERE {clause} GROUP BY t.type", params_all
    ).fetchall()
    by = {r["type"]: r["total"] for r in rows}
    return {"income_minor": by.get("income", 0), "expense_minor": by.get("expense", 0)}


# ---- CSV ---------------------------------------------------------------------------
_CSV_COLUMNS = [
    "id", "uuid", "date_local", "type", "amount", "currency", "from_account", "to_account", "person",
    "category", "description", "notes", "due_date", "status", "void_reason", "replaces_id", "created_at_utc",
]


def _safe(value):
    """Neutralise spreadsheet formula injection in text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def export_csv(conn, filters) -> str:
    tz = settings_svc.tzname(conn)
    where, params = _build_where(conn, {**filters, "status": filters.get("status") or "all"})
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"{_SELECT} {clause} ORDER BY t.occurred_at, t.id", params).fetchall()
    units = {r["code"]: r["minor_unit"] for r in conn.execute("SELECT code, minor_unit FROM currencies")}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(_CSV_COLUMNS)
    for r in rows:
        w.writerow([
            r["id"], r["uuid"], utc_iso_to_local(r["occurred_at"], tz).strftime("%Y-%m-%d %H:%M"), r["type"],
            to_decimal_string(r["amount_minor"], units[r["currency"]]), r["currency"],
            _safe(r["source_name"] or ""), _safe(r["dest_name"] or ""), _safe(r["person_name"] or ""),
            _safe(r["category_name"] or ""), _safe(r["description"]), _safe(r["notes"] or ""), r["due_date"] or "",
            r["status"], _safe(r["void_reason"] or ""), r["replaces_id"] or "", r["created_at"],
        ])
    return out.getvalue()
