"""Undo, and the final erasing of deleted records.

How it works (deliberately small)
---------------------------------
* Deleting a record is a *soft* delete: `deleted_at` is set and the record disappears from every
  screen and total. Nothing else about it changes, so bringing it back is exact.
* Every undoable action (create, edit, delete) writes ONE row to `undo_log` containing a short list
  of reversible steps. Two kinds of step exist:
      created  - "this row was inserted"            -> undo soft-deletes it
      set      - "these columns changed A -> B"     -> undo sets them back B -> A
* Undo is all-or-nothing and compare-and-swap: a `set` step is only reversed if the row still
  holds the value the action left behind. If anything touched it since (an edit, another undo,
  a newer delete), the whole undo is refused and nothing is changed, so later work is never
  overwritten.
* A log row flips `active -> undone` exactly once, in the same DB transaction as the reversal,
  so a second click (or a second device) cannot reverse the same action twice.
* Undo is offered for UNDO_WINDOW. Soft-deleted rows are erased for good after RETENTION.
"""
from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
import threading
from datetime import timedelta

from ..db import transaction
from ..timeutil import UTC_FMT, parse_utc, utcnow, utcnow_iso
from . import audit
from .errors import ConflictError, NotFoundError

UNDO_WINDOW = timedelta(minutes=15)
RETENTION = timedelta(hours=24)

# Columns an undo step may touch. Anything else in a (tampered) log row is refused.
_COLUMNS = {
    "accounts": {"name", "category_id", "description", "opening_balance_minor", "updated_at", "deleted_at"},
    "people": {"name", "contact", "notes", "updated_at", "deleted_at"},
    "transactions": {"status", "voided_at", "void_reason", "deleted_at", "replaced_by_id",
                     "description", "notes", "category_id", "due_date"},
}

_tls = threading.local()


# --------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------
class Group:
    """The steps of one user action. Services add steps while they work."""

    def __init__(self, label, restore=None):
        self.label = label
        self.restore = restore          # (endpoint, {url params}) shown after the action is undone
        self.ops = []
        self.token = None

    def created(self, table, row_id):
        self.ops.append({"k": "created", "t": table, "id": int(row_id)})

    def changed(self, table, row_id, before: dict, after: dict):
        cols = {c: [before[c], after[c]] for c in after if before.get(c) != after[c]}
        if cols:
            self.ops.append({"k": "set", "t": table, "id": int(row_id), "cols": cols})


@contextlib.contextmanager
def group(conn, label, restore=None):
    """Collect the steps of one action. Nested groups join the outer one, so a request that
    creates a person AND a transaction is a single Undo. Must be used INSIDE the DB transaction
    of the action: if that rolls back, no undo row exists either."""
    stack = getattr(_tls, "stack", None)
    if stack is None:
        stack = _tls.stack = []
    if stack:
        yield stack[-1]
        return
    grp = Group(label, restore)
    stack.append(grp)
    try:
        yield grp
        if grp.ops:
            _write(conn, grp)
    finally:
        stack.pop()


def _write(conn, grp):
    token = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO undo_log (token, label, ops_json, restore_url, created_at) VALUES (?,?,?,?,?)",
        (token, grp.label, json.dumps(grp.ops), json.dumps(grp.restore) if grp.restore else None, utcnow_iso()),
    )
    grp.token = token
    _tls.last = {"token": token, "label": grp.label}


def current():
    """The undo group of the action in progress, or None."""
    stack = getattr(_tls, "stack", None)
    return stack[-1] if stack else None


def take_last():
    """The undo offered by the action that just ran in this thread (once), or None."""
    last = getattr(_tls, "last", None)
    _tls.last = None
    return last


def clear_last():
    _tls.last = None


# --------------------------------------------------------------------------------------
# Soft-delete state of a transaction (shared by delete, undo of create, account/person delete)
# --------------------------------------------------------------------------------------
def tx_deleted_cols(now):
    return {"status": ["posted", "voided"], "voided_at": [None, now], "void_reason": [None, "Deleted"],
            "deleted_at": [None, now]}


# --------------------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------------------
def _refuse(message="Can't undo this any more because the record was changed afterwards."):
    raise ConflictError(message)


def _revert_set(conn, op):
    table, cols = op["t"], op["cols"]
    allowed = _COLUMNS.get(table)
    if allowed is None or not set(cols) <= allowed:
        _refuse("This action can't be undone.")
    names = list(cols)
    sql = (f"UPDATE {table} SET " + ", ".join(f"{c} = ?" for c in names)
           + " WHERE id = ? AND " + " AND ".join(f"{c} IS ?" for c in names))
    params = [cols[c][0] for c in names] + [op["id"]] + [cols[c][1] for c in names]
    if conn.execute(sql, params).rowcount != 1:
        _refuse()


def _undo_created(conn, op):
    """Undo of 'created' = soft-delete the row, provided nothing was built on top of it since."""
    table, rid, now = op["t"], op["id"], utcnow_iso()
    if table == "transactions":
        _revert_set(conn, {"t": table, "id": rid, "cols": _flip(tx_deleted_cols(now))})
        return
    if table == "accounts":
        if conn.execute("SELECT 1 FROM transactions WHERE deleted_at IS NULL AND status = 'posted' "
                        "AND (source_account_id = ? OR dest_account_id = ?) LIMIT 1", (rid, rid)).fetchone():
            _refuse("This account has records now, so creating it can't be undone. Delete the account instead.")
    elif table == "people":
        if conn.execute("SELECT 1 FROM transactions WHERE deleted_at IS NULL AND status = 'posted' "
                        "AND person_id = ? LIMIT 1", (rid,)).fetchone():
            _refuse("This person has records now, so adding them can't be undone. Delete the person instead.")
    else:
        _refuse("This action can't be undone.")
    _revert_set(conn, {"t": table, "id": rid, "cols": {"deleted_at": [now, None]}})


def _flip(cols):
    """{col: [live, deleted]} -> what _revert_set needs to move a LIVE row to the deleted state:
    it writes cols[c][0] where the row currently holds cols[c][1]."""
    return {c: [v[1], v[0]] for c, v in cols.items()}


def undo(conn, token):
    """Reverse one action. Returns (label, restore) on success; raises if it can't be done."""
    try:
        with transaction(conn):
            row = conn.execute("SELECT * FROM undo_log WHERE token = ?", (token,)).fetchone()
            if row is None:
                raise NotFoundError("There is nothing to undo.")
            if row["status"] != "active":
                raise ConflictError("That has already been undone.")
            if utcnow() - parse_utc(row["created_at"]) > UNDO_WINDOW:
                raise ConflictError("It's too late to undo that.")
            for op in reversed(json.loads(row["ops_json"])):
                if op["k"] == "created":
                    _undo_created(conn, op)
                elif op["k"] == "set":
                    _revert_set(conn, op)
                else:
                    _refuse("This action can't be undone.")
            if conn.execute("UPDATE undo_log SET status = 'undone', undone_at = ? WHERE id = ? AND status = 'active'",
                            (utcnow_iso(), row["id"])).rowcount != 1:
                raise ConflictError("That has already been undone.")
            audit.log(conn, "undo", "undo", row["id"], reason=row["label"])
    except sqlite3.IntegrityError:
        # e.g. restoring "Wallet" while a different "Wallet" exists now
        raise ConflictError("Can't undo this: a record with the same name exists now.")
    restore = json.loads(row["restore_url"]) if row["restore_url"] else None
    return row["label"], restore


# --------------------------------------------------------------------------------------
# Erasing deleted records for good
# --------------------------------------------------------------------------------------
_last_purge = {"at": None}


def _chunks(ids, n=400):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _marks(ids):
    return ",".join("?" * len(ids))


def purge(conn, *, now=None, force=False):
    """Erase records that have been deleted for longer than RETENTION, together with the activity-log
    lines about them. Accounts and people that surviving records still point to (the other side of a
    transfer or loan) are kept as hidden placeholders until nothing refers to them. Returns the number
    of transactions erased. Cheap when there is nothing to do; throttled to once an hour per process."""
    now = now or utcnow()
    if not force and _last_purge["at"] and now - _last_purge["at"] < timedelta(hours=1):
        return 0
    _last_purge["at"] = now
    cutoff = (now - RETENTION).strftime(UTC_FMT)
    if not (conn.execute("SELECT 1 FROM transactions WHERE deleted_at IS NOT NULL AND deleted_at < ? LIMIT 1", (cutoff,)).fetchone()
            or conn.execute("SELECT 1 FROM accounts WHERE deleted_at IS NOT NULL AND deleted_at < ? LIMIT 1", (cutoff,)).fetchone()
            or conn.execute("SELECT 1 FROM people WHERE deleted_at IS NOT NULL AND deleted_at < ? LIMIT 1", (cutoff,)).fetchone()
            or conn.execute("SELECT 1 FROM undo_log WHERE created_at < ? LIMIT 1", (cutoff,)).fetchone()):
        return 0
    erased = 0
    with transaction(conn):
        conn.execute("DELETE FROM undo_log WHERE created_at < ?", (cutoff,))
        conn.execute("INSERT INTO application_settings (key, value) VALUES ('purge_mode', '1') "
                     "ON CONFLICT(key) DO UPDATE SET value = '1'")
        try:
            tx_ids = [r[0] for r in conn.execute(
                "SELECT t.id FROM transactions t WHERE t.deleted_at IS NOT NULL AND t.deleted_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM transactions x WHERE x.replaces_id = t.id)", (cutoff,))]
            for part in _chunks(tx_ids):
                m = _marks(part)
                conn.execute(f"DELETE FROM reconciliation_entries WHERE transaction_id IN ({m})", part)
                conn.execute(f"DELETE FROM transaction_entries WHERE transaction_id IN ({m})", part)
                conn.execute(f"UPDATE transactions SET replaced_by_id = NULL WHERE replaced_by_id IN ({m})", part)
                conn.execute(f"DELETE FROM audit_logs WHERE entity_type = 'transaction' AND entity_id IN ({m})", part)
                conn.execute(f"DELETE FROM transactions WHERE id IN ({m})", part)
            erased = len(tx_ids)

            # reconciliations of deleted accounts, unless a surviving record still links to them
            rec_ids = [r[0] for r in conn.execute(
                "SELECT r.id FROM reconciliations r JOIN accounts a ON a.id = r.account_id "
                "WHERE a.deleted_at IS NOT NULL AND a.deleted_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.reconciliation_id = r.id)", (cutoff,))]
            for part in _chunks(rec_ids):
                m = _marks(part)
                conn.execute(f"DELETE FROM reconciliation_entries WHERE reconciliation_id IN ({m})", part)
                conn.execute(f"DELETE FROM audit_logs WHERE entity_type = 'reconciliation' AND entity_id IN ({m})", part)
                conn.execute(f"DELETE FROM reconciliations WHERE id IN ({m})", part)

            acc_ids = [r[0] for r in conn.execute(
                "SELECT a.id FROM accounts a WHERE a.deleted_at IS NOT NULL AND a.deleted_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.source_account_id = a.id OR t.dest_account_id = a.id) "
                "AND NOT EXISTS (SELECT 1 FROM transaction_entries e WHERE e.account_id = a.id) "
                "AND NOT EXISTS (SELECT 1 FROM reconciliations r WHERE r.account_id = a.id)", (cutoff,))]
            for part in _chunks(acc_ids):
                m = _marks(part)
                conn.execute(f"DELETE FROM audit_logs WHERE entity_type = 'account' AND entity_id IN ({m})", part)
                conn.execute(f"DELETE FROM accounts WHERE id IN ({m})", part)

            ppl_ids = [r[0] for r in conn.execute(
                "SELECT p.id FROM people p WHERE p.deleted_at IS NOT NULL AND p.deleted_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.person_id = p.id) "
                "AND NOT EXISTS (SELECT 1 FROM transaction_entries e WHERE e.person_id = p.id)", (cutoff,))]
            for part in _chunks(ppl_ids):
                m = _marks(part)
                conn.execute(f"DELETE FROM audit_logs WHERE entity_type = 'person' AND entity_id IN ({m})", part)
                conn.execute(f"DELETE FROM people WHERE id IN ({m})", part)
        finally:
            conn.execute("DELETE FROM application_settings WHERE key = 'purge_mode'")
    return erased


def init_app(app):
    """Hooks: one undo offer per request; show it after a redirect; tidy up now and then."""
    from flask import request, session

    from ..db import get_db

    @app.before_request
    def _reset():
        clear_last()
        if request.method == "POST":           # housekeeping rides along with writes; throttled and cheap
            try:
                purge(get_db())
            except Exception:
                app.logger.exception("Could not erase old deleted records")

    @app.after_request
    def _offer(response):
        last = take_last()
        if last and response.status_code in (301, 302, 303):
            session["undo"] = last
        return response

    @app.context_processor
    def _toast():
        return {"undo_offer": session.pop("undo", None)} if "undo" in session else {}
