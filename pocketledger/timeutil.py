"""Timestamps are stored in UTC as 'YYYY-MM-DDTHH:MM:SSZ' and shown in the user's timezone."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utcnow_iso() -> str:
    return utcnow().strftime(UTC_FMT)


def get_tz(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        # Windows without the tzdata package: Bangladesh has no DST, so a fixed offset is exact.
        if name == "Asia/Dhaka":
            return timezone(timedelta(hours=6), "BDT")
        return timezone.utc


def parse_utc(iso: str) -> datetime:
    return datetime.strptime(iso, UTC_FMT).replace(tzinfo=timezone.utc)


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(UTC_FMT)


def is_valid_utc_iso(value: str) -> bool:
    try:
        parse_utc(value)
        return True
    except (ValueError, TypeError):
        return False


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def local_input_to_utc_iso(value: str, tzname: str) -> str:
    """Convert '2026-11-23T14:30' (an <input type=datetime-local> value) in `tzname` to UTC."""
    value = value.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(value, fmt)
            break
        except ValueError:
            naive = None
    if naive is None:
        # a bare date means "that day, at the current time of day" is ambiguous: use noon
        naive = datetime.combine(parse_date(value), time(12, 0))
    return to_utc_iso(naive.replace(tzinfo=get_tz(tzname)))


def utc_iso_to_local(iso: str, tzname: str) -> datetime:
    return parse_utc(iso).astimezone(get_tz(tzname))


def utc_iso_to_local_input(iso: str, tzname: str) -> str:
    return utc_iso_to_local(iso, tzname).strftime("%Y-%m-%dT%H:%M")


def local_date(iso: str, tzname: str) -> date:
    return utc_iso_to_local(iso, tzname).date()


def today_local(tzname: str) -> date:
    return datetime.now(get_tz(tzname)).date()


def day_start_utc(d: date, tzname: str) -> str:
    return to_utc_iso(datetime.combine(d, time(0, 0)).replace(tzinfo=get_tz(tzname)))


def range_bounds_utc(start: date, end: date, tzname: str) -> tuple[str, str]:
    """[start 00:00 local, end+1 00:00 local) expressed in UTC. End date is inclusive."""
    return day_start_utc(start, tzname), day_start_utc(end + timedelta(days=1), tzname)


def humanize_ago(iso: str | None, now: datetime | None = None) -> str:
    if not iso:
        return "never"
    now = now or utcnow()
    delta = now - parse_utc(iso)
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    days = secs // 86400
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    return f"{days // 30} months ago"
