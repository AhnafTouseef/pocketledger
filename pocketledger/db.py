"""SQLite access. One connection per request, explicit transactions, foreign keys always on."""
from __future__ import annotations

import contextlib
import itertools
import sqlite3

from flask import current_app, g

_savepoint_ids = itertools.count(1)


def connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection in autocommit mode (we issue BEGIN/COMMIT ourselves)."""
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=15)
    else:
        conn = sqlite3.connect(path, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection):
    """Atomic block. Outermost use is BEGIN IMMEDIATE (serialises writers, so balance
    checks and inserts see a consistent state); nested use becomes a SAVEPOINT."""
    if conn.in_transaction:
        name = f"sp_{next(_savepoint_ids)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
