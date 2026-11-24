"""Everything the dashboard shows, assembled from the ledger."""
from __future__ import annotations

from datetime import timedelta

from ..timeutil import today_local
from . import accounts as accounts_svc, analytics, ledger, people, reconciliation, settings as settings_svc
from . import transactions as tx_svc


def build(conn):
    currency = settings_svc.base_currency_code(conn)
    cur_row = settings_svc.get_currency(conn, currency)
    tz = settings_svc.tzname(conn)
    today = today_local(tz)
    totals = ledger.totals(conn, currency)
    accts = accounts_svc.list_accounts(conn)
    base_accounts = [a for a in accts if a["currency"] == currency]
    other_currency_accounts = [a for a in accts if a["currency"] != currency]

    discrepancies = reconciliation.open_discrepancies(conn)
    disc_total = sum(d["live_difference_minor"] for d in discrepancies if d["currency"] == currency)
    due_days = settings_svc.get_int(conn, "due_soon_days", 7)

    alerts = {
        "discrepancies": discrepancies,
        "overdue_receivables": people.overdue_receivables(conn, today, currency),
        "upcoming_payables": people.upcoming_payables(conn, today, due_days, currency),
        "unverified_cash": [a for a in accts if a["needs_verification"]],
    }
    alert_count = sum(len(v) for v in alerts.values())

    start = today - timedelta(days=29)
    _, buckets = analytics.make_buckets(conn, start, today)
    series = analytics.balance_sheet_series(conn, buckets, currency)

    return {
        "currency": dict(cur_row),
        "totals": totals,
        "discrepancy_count": len(discrepancies),
        "discrepancy_total_minor": disc_total,
        "accounts": base_accounts,
        "other_currency_accounts": other_currency_accounts,
        "recent": tx_svc.recent(conn, 8),
        "alerts": alerts,
        "alert_count": alert_count,
        "net_worth_series": {"labels": [b[0] for b in buckets], "values": [s["net_worth"] for s in series]},
        "today": today.isoformat(),
        "due_soon_days": due_days,
    }
