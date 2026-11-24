"""Analytics derived purely from ledger rows. Nothing here is stored or estimated."""
from __future__ import annotations

from bisect import bisect_left
from datetime import date, timedelta

from ..timeutil import day_start_utc, local_date, parse_date, range_bounds_utc, today_local, utc_iso_to_local
from . import reconciliation, settings as settings_svc

PRESETS = [
    ("this_week", "This week"),
    ("this_month", "This month"),
    ("last_month", "Last month"),
    ("this_year", "This year"),
    ("custom", "Custom"),
]
_WEEKDAY = {"monday": 0, "sunday": 6, "saturday": 5}


def resolve_range(conn, preset, start=None, end=None):
    """-> (start_date, end_date_inclusive, preset, label). End is capped at today."""
    tz = settings_svc.tzname(conn)
    today = today_local(tz)
    preset = preset if preset in dict(PRESETS) else "this_month"
    if preset == "this_week":
        first = _WEEKDAY.get(settings_svc.get(conn, "week_start"), 6)
        s = today - timedelta(days=(today.weekday() - first) % 7)
        e = s + timedelta(days=6)
    elif preset == "this_month":
        s = today.replace(day=1)
        e = _month_end(s)
    elif preset == "last_month":
        e = today.replace(day=1) - timedelta(days=1)
        s = e.replace(day=1)
    elif preset == "this_year":
        s, e = date(today.year, 1, 1), date(today.year, 12, 31)
    else:
        try:
            s, e = parse_date(start), parse_date(end)
        except (ValueError, AttributeError, TypeError):
            preset = "this_month"
            s = today.replace(day=1)
            e = _month_end(s)
        if e < s:
            s, e = e, s
        if (e - s).days > 3660:
            s = e - timedelta(days=3660)
    e = min(e, today)
    s = min(s, e)
    label = f"{s.strftime('%d %b %Y')} – {e.strftime('%d %b %Y')}"
    return s, e, preset, label


def _month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def make_buckets(conn, start: date, end: date):
    """Choose day/week/month buckets by range length. Each bucket is (label, start, end)."""
    span = (end - start).days + 1
    first = _WEEKDAY.get(settings_svc.get(conn, "week_start"), 6)
    buckets = []
    if span <= 35:
        d = start
        while d <= end:
            buckets.append((d.strftime("%d %b"), d, d))
            d += timedelta(days=1)
        return "day", buckets
    if span <= 120:
        d = start - timedelta(days=(start.weekday() - first) % 7)
        while d <= end:
            b_start, b_end = max(d, start), min(d + timedelta(days=6), end)
            buckets.append((b_start.strftime("%d %b"), b_start, b_end))
            d += timedelta(days=7)
        return "week", buckets
    d = start.replace(day=1)
    while d <= end:
        b_start, b_end = max(d, start), min(_month_end(d), end)
        buckets.append((d.strftime("%b %Y"), b_start, b_end))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return "month", buckets


def balance_sheet_series(conn, buckets, currency):
    """Accounts, receivables, payables, assets, liabilities, net worth at the end of each bucket."""
    tz = settings_svc.tzname(conn)
    events = []  # (utc_iso, accounts_delta, receivable_delta, payable_delta)
    since = {}  # account id -> opening moment; legs dated before it are already inside the opening balance
    for a in conn.execute("SELECT id, opening_balance_minor, opened_on, opening_at FROM accounts WHERE currency = ?", (currency,)):
        since[a["id"]] = a["opening_at"] or day_start_utc(parse_date(a["opened_on"]), tz)
        events.append((since[a["id"]], a["opening_balance_minor"], 0, 0))
    for e in conn.execute(
        "SELECT occurred_at, target_type, account_id, debit_minor, credit_minor FROM v_posted_entries "
        "WHERE currency = ? AND target_type IN ('account', 'receivable', 'payable')", (currency,)
    ):
        d = e["debit_minor"] - e["credit_minor"]
        if e["target_type"] == "account":
            if e["occurred_at"] >= since.get(e["account_id"], ""):
                events.append((e["occurred_at"], d, 0, 0))
        elif e["target_type"] == "receivable":
            events.append((e["occurred_at"], 0, d, 0))
        else:
            events.append((e["occurred_at"], 0, 0, -d))
    events.sort(key=lambda ev: ev[0])
    times = [ev[0] for ev in events]
    series, ptr, acc_t, rec_t, pay_t = [], 0, 0, 0, 0
    for label, b_start, b_end in buckets:
        _, end_excl = range_bounds_utc(b_start, b_end, tz)
        stop = bisect_left(times, end_excl)
        while ptr < stop:
            _, a, r, p = events[ptr]
            acc_t, rec_t, pay_t = acc_t + a, rec_t + r, pay_t + p
            ptr += 1
        assets = acc_t + rec_t
        series.append({
            "label": label, "accounts": acc_t, "receivables": rec_t, "payables": pay_t,
            "assets": assets, "liabilities": pay_t, "net_worth": assets - pay_t,
        })
    return series


def flow_series(conn, buckets, currency):
    """Per-bucket totals of every transaction type (posted only)."""
    tz = settings_svc.tzname(conn)
    start_utc, _ = range_bounds_utc(buckets[0][1], buckets[0][1], tz)
    _, end_utc = range_bounds_utc(buckets[-1][2], buckets[-1][2], tz)
    index = {}
    for i, (_, s, e) in enumerate(buckets):
        d = s
        while d <= e:
            index[d] = i
            d += timedelta(days=1)
    keys = ("income", "expense", "lent", "borrowed", "repayment_received", "repayment_made", "adjust_up", "adjust_down")
    out = [dict.fromkeys(keys, 0) for _ in buckets]
    for t in conn.execute(
        "SELECT type, amount_minor, occurred_at, dest_account_id FROM transactions "
        "WHERE status = 'posted' AND currency = ? AND occurred_at >= ? AND occurred_at < ?",
        (currency, start_utc, end_utc),
    ):
        i = index.get(local_date(t["occurred_at"], tz))
        if i is None:
            continue
        key = t["type"]
        if key == "adjustment":
            key = "adjust_up" if t["dest_account_id"] is not None else "adjust_down"
        if key in out[i]:
            out[i][key] += t["amount_minor"]
    return out


def expense_by_category(conn, start, end, currency, top=8):
    tz = settings_svc.tzname(conn)
    s, e = range_bounds_utc(start, end, tz)
    rows = conn.execute(
        "SELECT COALESCE(c.name, 'Uncategorised') AS name, SUM(t.amount_minor) AS total FROM transactions t "
        "LEFT JOIN transaction_categories c ON c.id = t.category_id WHERE t.status = 'posted' AND t.type = 'expense' "
        "AND t.currency = ? AND t.occurred_at >= ? AND t.occurred_at < ? GROUP BY 1 ORDER BY total DESC",
        (currency, s, e),
    ).fetchall()
    items = [{"name": r["name"], "total": r["total"]} for r in rows]
    if len(items) > top:
        rest = sum(i["total"] for i in items[top:])
        items = items[:top] + [{"name": "Everything else", "total": rest}]
    return items


def account_distribution(conn, currency):
    rows = conn.execute(
        "SELECT a.name, v.balance_minor FROM accounts a JOIN v_account_balances v ON v.account_id = a.id "
        "WHERE a.currency = ? AND a.is_archived = 0 ORDER BY v.balance_minor DESC", (currency,)
    ).fetchall()
    return [{"name": r["name"], "balance": r["balance_minor"]} for r in rows]


def outstanding_people(conn, currency):
    rows = conn.execute(
        "SELECT p.name, b.receivable_minor, b.payable_minor FROM v_person_balances b JOIN people p ON p.id = b.person_id "
        "WHERE b.currency = ? AND (b.receivable_minor > 0 OR b.payable_minor > 0)", (currency,)
    ).fetchall()
    recv = sorted(({"name": r["name"], "amount": r["receivable_minor"]} for r in rows if r["receivable_minor"] > 0),
                  key=lambda x: -x["amount"])
    pay = sorted(({"name": r["name"], "amount": r["payable_minor"]} for r in rows if r["payable_minor"] > 0),
                 key=lambda x: -x["amount"])
    return recv, pay


def build(conn, preset, start=None, end=None):
    currency = settings_svc.base_currency_code(conn)
    cur_row = settings_svc.get_currency(conn, currency)
    s, e, preset, label = resolve_range(conn, preset, start, end)
    granularity, buckets = make_buckets(conn, s, e)
    bs = balance_sheet_series(conn, buckets, currency)
    flows = flow_series(conn, buckets, currency)

    # fixed 12-month view for "monthly income and spending", independent of the filter
    tz = settings_svc.tzname(conn)
    today = today_local(tz)
    m = today.replace(day=1)
    months = []
    for _ in range(12):
        months.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    months.reverse()
    mbuckets = [(d.strftime("%b %y"), d, min(_month_end(d), today)) for d in months]
    monthly = flow_series(conn, mbuckets, currency)

    tz_s, tz_e = range_bounds_utc(s, e, tz)
    recs = reconciliation.list_reconciliations(conn, start_utc=tz_s, end_utc=tz_e, limit=100)
    recv, pay = outstanding_people(conn, currency)
    totals_range = {k: sum(f[k] for f in flows) for k in flows[0]}
    return {
        "currency": dict(cur_row),
        "preset": preset, "presets": PRESETS, "start": s.isoformat(), "end": e.isoformat(), "label": label,
        "granularity": granularity,
        "range_totals": totals_range,
        "balance_sheet": {"labels": [b[0] for b in buckets], "rows": bs},
        "flows": {"labels": [b[0] for b in buckets], "rows": flows},
        "monthly": {"labels": [b[0] for b in mbuckets], "rows": monthly},
        "expense_categories": expense_by_category(conn, s, e, currency),
        "account_distribution": account_distribution(conn, currency),
        "receivables": recv, "payables": pay,
        "reconciliations": [
            {
                "id": r["id"], "account": r["account_name"], "verified_at": r["verified_at"],
                "date_label": utc_iso_to_local(r["verified_at"], tz).strftime("%d %b"),
                "expected": r["expected_minor"], "counted": r["counted_minor"],
                "difference": r["difference_minor"], "live_difference": r["live_difference_minor"],
                "status": r["status"],
            } for r in recs
        ],
    }
