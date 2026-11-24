"""Application settings and currency lookups."""
from __future__ import annotations

from ..timeutil import utcnow_iso
from .errors import ValidationError

DEFAULTS = {
    "base_currency": "BDT",
    "timezone": "Asia/Dhaka",
    "week_start": "sunday",
    "verify_reminder_days": "7",
    "due_soon_days": "7",
    "is_demo": "0",
}
WEEK_STARTS = ("saturday", "sunday", "monday")


def get(conn, key, default=None):
    row = conn.execute("SELECT value FROM application_settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row[0]
    return DEFAULTS.get(key) if default is None else default


def get_all(conn) -> dict:
    out = dict(DEFAULTS)
    for r in conn.execute("SELECT key, value FROM application_settings"):
        out[r["key"]] = r["value"]
    return out


def set_value(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO application_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), utcnow_iso()),
    )


def tzname(conn) -> str:
    return get(conn, "timezone")


def get_int(conn, key, default) -> int:
    try:
        return int(get(conn, key, str(default)))
    except (TypeError, ValueError):
        return default


def is_demo(conn) -> bool:
    return get(conn, "is_demo") == "1"


def base_currency_code(conn) -> str:
    return get(conn, "base_currency")


def get_currency(conn, code):
    row = conn.execute("SELECT * FROM currencies WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise ValidationError(f"Unknown currency {code}.", "currency")
    return row


def base_currency(conn):
    return get_currency(conn, base_currency_code(conn))


def enabled_currencies(conn):
    return conn.execute("SELECT * FROM currencies WHERE is_enabled = 1 ORDER BY code").fetchall()


def update_preferences(conn, *, timezone_name, week_start, verify_reminder_days, due_soon_days):
    from ..timeutil import get_tz
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(timezone_name)
    except Exception:
        if timezone_name != "Asia/Dhaka":
            raise ValidationError("Unknown timezone. Use a name like Asia/Dhaka.", "timezone")
    if week_start not in WEEK_STARTS:
        raise ValidationError("Choose a valid first day of the week.", "week_start")
    for label, val in (("Verification reminder", verify_reminder_days), ("Due-soon window", due_soon_days)):
        if not str(val).isdigit() or not (1 <= int(val) <= 365):
            raise ValidationError(f"{label} must be a number of days between 1 and 365.")
    set_value(conn, "timezone", timezone_name)
    set_value(conn, "week_start", week_start)
    set_value(conn, "verify_reminder_days", int(verify_reminder_days))
    set_value(conn, "due_soon_days", int(due_soon_days))
