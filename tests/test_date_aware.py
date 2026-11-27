"""Date-aware balances: anything dated before an account's opening date is history only.

Scenario that motivated this: you set up Wallet today with the money it holds now, then record
a loan you gave last month. Wallet must stay what you entered; the person simply owes you.
"""
import json
import random
from datetime import timedelta

from pocketledger import migrate
from pocketledger.db import connect
from pocketledger.services import accounts, analytics, backup, ledger, people, reconciliation, settings, transactions
from pocketledger.timeutil import day_start_utc, today_local


def _today(conn):
    return today_local(settings.tzname(conn))


def local(conn, days_ago, hh=12):
    return f"{(_today(conn) - timedelta(days=days_ago)).isoformat()}T{hh:02d}:00"


def post(conn, **kw):
    return ledger.post_transaction(conn, kw)


def make_account(conn, name, opening, opened_days_ago=0):
    d = (_today(conn) - timedelta(days=opened_days_ago)).isoformat()
    return accounts.create_account(conn, name=name, category_id=1, currency="BDT", opening_balance=opening, opened_on=d)


def net_worth(conn):
    return ledger.totals(conn, "BDT")["net_worth_minor"]


# ---- the reported bug --------------------------------------------------------------------------
def test_past_loan_does_not_reduce_a_wallet_opened_today(conn):
    wallet = make_account(conn, "Wallet", "3,000")            # holds ৳3,000 right now
    rahim = people.create_person(conn, name="Rahim")
    post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))

    assert ledger.account_balance(conn, wallet) == 300000          # unchanged: it was already gone
    assert accounts.get_account(conn, wallet)["balance_minor"] == 300000
    t = ledger.totals(conn, "BDT")
    assert (t["liquid_minor"], t["receivables_minor"], t["net_worth_minor"]) == (300000, 100000, 400000)
    assert ledger.verify_integrity(conn) == []

    # when Rahim pays back (today), the money lands in the wallet
    post(conn, type="repayment_received", dest_account_id=wallet, person_id=rahim, amount="400")
    assert ledger.account_balance(conn, wallet) == 340000
    t = ledger.totals(conn, "BDT")
    assert (t["receivables_minor"], t["net_worth_minor"]) == (60000, 400000)
    assert ledger.verify_integrity(conn) == []


def test_borrowed_in_the_past_does_not_add_to_current_balance(conn):
    wallet = make_account(conn, "Wallet", "3,000")
    mitu = people.create_person(conn, name="Mitu")
    post(conn, type="borrowed", dest_account_id=wallet, person_id=mitu, amount="2,000", occurred_local=local(conn, 20))
    assert ledger.account_balance(conn, wallet) == 300000
    t = ledger.totals(conn, "BDT")
    assert (t["payables_minor"], t["net_worth_minor"]) == (200000, 100000)
    post(conn, type="repayment_made", source_account_id=wallet, person_id=mitu, amount="500")
    assert ledger.account_balance(conn, wallet) == 250000          # repaying today does leave the wallet
    assert ledger.verify_integrity(conn) == []


def test_transactions_on_or_after_the_opening_day_still_count(conn):
    wallet = make_account(conn, "Wallet", "3,000")
    post(conn, type="expense", source_account_id=wallet, amount="100", occurred_local=local(conn, 0, 0))   # 00:00 today
    assert ledger.account_balance(conn, wallet) == 290000
    post(conn, type="expense", source_account_id=wallet, amount="100", occurred_local=local(conn, 1, 23))  # yesterday 23:00
    assert ledger.account_balance(conn, wallet) == 290000


def test_account_opened_earlier_counts_from_its_opening_day(conn):
    wallet = make_account(conn, "Wallet", "3,000", opened_days_ago=10)
    post(conn, type="expense", source_account_id=wallet, amount="200", occurred_local=local(conn, 20))   # before: ignored
    post(conn, type="expense", source_account_id=wallet, amount="300", occurred_local=local(conn, 5))    # after: counts
    assert ledger.account_balance(conn, wallet) == 270000
    assert ledger.verify_integrity(conn) == []


def test_transfer_between_accounts_opened_on_different_days(conn):
    old = make_account(conn, "Bank", "10,000", opened_days_ago=30)
    new = make_account(conn, "Wallet", "1,000", opened_days_ago=0)
    post(conn, type="transfer", source_account_id=old, dest_account_id=new, amount="500", occurred_local=local(conn, 7))
    assert ledger.account_balance(conn, old) == 950000         # leg after Bank opened: counts
    assert ledger.account_balance(conn, new) == 100000         # leg before Wallet opened: ignored
    assert ledger.verify_integrity(conn) == []


def test_expense_and_income_before_opening_are_history_only(conn):
    wallet = make_account(conn, "Wallet", "3,000")
    post(conn, type="expense", source_account_id=wallet, amount="400", occurred_local=local(conn, 9))
    post(conn, type="income", dest_account_id=wallet, amount="900", occurred_local=local(conn, 8))
    assert ledger.account_balance(conn, wallet) == 300000
    assert ledger.verify_integrity(conn) == []


def test_reconciliation_uses_the_same_rule(conn):
    wallet = make_account(conn, "Wallet", "3,000")
    rahim = people.create_person(conn, name="Rahim")
    post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))
    rid = reconciliation.verify(conn, wallet, "3000")                       # you count ৳3,000: it matches
    rec = [r for r in reconciliation.list_reconciliations(conn, limit=5) if r["id"] == rid][0]
    assert rec["difference_minor"] == 0 and rec["status"] == "matched"


def test_deleting_a_pre_opening_transaction_keeps_everything_consistent(conn):
    wallet = make_account(conn, "Wallet", "3,000")
    rahim = people.create_person(conn, name="Rahim")
    tx = post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))
    ledger.delete_transaction(conn, tx.id)
    assert ledger.account_balance(conn, wallet) == 300000
    assert ledger.totals(conn, "BDT")["net_worth_minor"] == 300000
    assert ledger.verify_integrity(conn) == []


# ---- independent model: random accounts, random dates --------------------------------------------
def test_randomised_model_matches_balances_and_integrity(conn):
    rnd = random.Random(7)
    accs = {make_account(conn, f"A{i}", str(rnd.randint(0, 5000)), opened_days_ago=rnd.randint(0, 40)): None for i in range(4)}
    ppl = [people.create_person(conn, name=f"P{i}") for i in range(3)]
    ids = list(accs)
    # (account -> opening, since-date) for the model; kept separate from the code under test
    info = {}
    for a in ids:
        row = conn.execute("SELECT opening_balance_minor, opened_on FROM accounts WHERE id = ?", (a,)).fetchone()
        info[a] = (row[0], row[1])
    model = {a: info[a][0] for a in ids}
    recv = {p: 0 for p in ppl}; pay = {p: 0 for p in ppl}

    def effect(a, days_ago, delta):
        day = (_today(conn) - timedelta(days=days_ago)).isoformat()
        if day >= info[a][1]:                      # on/after the opening day -> counts
            model[a] += delta

    for _ in range(120):
        t = rnd.choice(["income", "expense", "transfer", "lent", "borrowed", "repayment_received", "repayment_made"])
        days = rnd.randint(0, 60)
        amt = rnd.randint(1, 900)
        when = local(conn, days, rnd.randint(0, 23))
        a, b, p = rnd.choice(ids), rnd.choice(ids), rnd.choice(ppl)
        try:
            if t == "income":
                post(conn, type=t, dest_account_id=a, amount=str(amt), occurred_local=when); effect(a, days, amt * 100)
            elif t == "expense":
                post(conn, type=t, source_account_id=a, amount=str(amt), occurred_local=when); effect(a, days, -amt * 100)
            elif t == "transfer":
                if a == b:
                    continue
                post(conn, type=t, source_account_id=a, dest_account_id=b, amount=str(amt), occurred_local=when)
                effect(a, days, -amt * 100); effect(b, days, amt * 100)
            elif t == "lent":
                post(conn, type=t, source_account_id=a, person_id=p, amount=str(amt), occurred_local=when)
                effect(a, days, -amt * 100); recv[p] += amt * 100
            elif t == "borrowed":
                post(conn, type=t, dest_account_id=a, person_id=p, amount=str(amt), occurred_local=when)
                effect(a, days, amt * 100); pay[p] += amt * 100
            elif t == "repayment_received":
                if recv[p] < amt * 100:
                    continue
                post(conn, type=t, dest_account_id=a, person_id=p, amount=str(amt), occurred_local=when)
                effect(a, days, amt * 100); recv[p] -= amt * 100
            else:
                if pay[p] < amt * 100:
                    continue
                post(conn, type=t, source_account_id=a, person_id=p, amount=str(amt), occurred_local=when)
                effect(a, days, -amt * 100); pay[p] -= amt * 100
        except Exception:
            continue    # the service may legitimately refuse (e.g. repayment before the loan date); model skips too
    # (a refused post never reaches the model's effect(), because effect() runs after post())
    for a in ids:
        assert ledger.account_balance(conn, a) == model[a], a
        assert accounts.get_account(conn, a)["balance_minor"] == model[a], a
    t = ledger.totals(conn, "BDT")
    assert t["liquid_minor"] == sum(model.values())
    assert t["receivables_minor"] == sum(recv.values()) and t["payables_minor"] == sum(pay.values())
    assert ledger.verify_integrity(conn) == []


# ---- existing databases are upgraded correctly --------------------------------------------------
def test_migration_adds_and_backfills_opening_moment_for_old_accounts(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    real = migrate.discover()
    monkeypatch.setattr(migrate, "discover", lambda: real[:2])         # a database from before date-awareness
    migrate.ensure_database(path)
    c = connect(path)
    assert "opening_at" not in {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
    c.execute("INSERT INTO accounts (name, category_id, currency, opening_balance_minor, opened_on, created_at, updated_at) "
              "VALUES ('Old wallet', 1, 'BDT', 300000, '2026-11-20', '2026-11-20T00:00:00Z', '2026-11-20T00:00:00Z')")
    c.close()
    monkeypatch.undo()
    assert migrate.ensure_database(path) == ["0003", "0004", "0005"]
    c = connect(path)
    row = c.execute("SELECT opening_at FROM accounts").fetchone()
    assert row["opening_at"] == day_start_utc(__import__("datetime").date(2026, 11, 20), "Asia/Dhaka")
    assert c.execute("SELECT balance_minor FROM v_account_balances").fetchone()[0] == 300000
    # the opening moment cannot be rewritten afterwards
    import sqlite3
    try:
        c.execute("UPDATE accounts SET opening_at = '2020-01-01T00:00:00Z'")
        raise AssertionError("opening_at should be locked")
    except sqlite3.DatabaseError as exc:
        assert "cannot be changed" in str(exc)
    c.close()


def test_view_and_python_agree_when_opening_at_is_missing(conn):
    wallet = make_account(conn, "Wallet", "3,000")
    rahim = people.create_person(conn, name="Rahim")
    post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))
    conn.execute("DROP TRIGGER trg_accounts_opening_at_locked")
    conn.execute("UPDATE accounts SET opening_at = NULL")          # simulate an import that hasn't been backfilled
    assert ledger.account_balance(conn, wallet) == conn.execute("SELECT balance_minor FROM v_account_balances").fetchone()[0] == 300000
    assert migrate.backfill_opening_at(conn) == 1


def test_json_round_trip_keeps_date_aware_balances(app, conn, tmp_path):
    wallet = make_account(conn, "Wallet", "3,000")
    rahim = people.create_person(conn, name="Rahim")
    post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))
    post(conn, type="expense", source_account_id=wallet, amount="50")
    want = [tuple(r) for r in conn.execute("SELECT account_id, balance_minor FROM v_account_balances ORDER BY 1")]
    data = json.loads(json.dumps(backup.export_json(conn)))
    report = backup.import_json_to_staging(data, str(tmp_path / "s.db"), app.config["DATABASE"])
    assert report["ok"], report
    s = connect(str(tmp_path / "s.db"))
    assert [tuple(r) for r in s.execute("SELECT account_id, balance_minor FROM v_account_balances ORDER BY 1")] == want
    assert s.execute("SELECT COUNT(*) FROM accounts WHERE opening_at IS NULL").fetchone()[0] == 0
    s.close()


def test_export_from_before_this_feature_imports_and_is_backfilled(app, conn, tmp_path):
    wallet = make_account(conn, "Wallet", "3,000")
    rahim = people.create_person(conn, name="Rahim")
    post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))
    data = json.loads(json.dumps(backup.export_json(conn)))
    for row in data["tables"]["accounts"]:
        row.pop("opening_at", None)                                       # an export made by the previous version
    report = backup.import_json_to_staging(data, str(tmp_path / "s2.db"), app.config["DATABASE"])
    assert report["ok"], report
    s = connect(str(tmp_path / "s2.db"))
    assert s.execute("SELECT balance_minor FROM v_account_balances").fetchone()[0] == 300000
    assert s.execute("SELECT COUNT(*) FROM accounts WHERE opening_at IS NULL").fetchone()[0] == 0
    s.close()


# ---- read side / UI -----------------------------------------------------------------------------
def test_charts_history_agrees_with_current_net_worth(conn):
    wallet = make_account(conn, "Wallet", "3,000")
    rahim = people.create_person(conn, name="Rahim")
    post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))
    today = _today(conn)
    buckets = [("all", today - timedelta(days=60), today)]
    last = analytics.balance_sheet_series(conn, buckets, "BDT")[-1]
    assert last["accounts"] == 300000 and last["receivables"] == 100000 and last["net_worth"] == net_worth(conn) == 400000


def test_transactions_are_flagged_when_dated_before_the_account(conn):
    wallet = make_account(conn, "Wallet", "3,000")
    rahim = people.create_person(conn, name="Rahim")
    early = post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))
    normal = post(conn, type="expense", source_account_id=wallet, amount="50")
    assert transactions.get_transaction(conn, early.id)["pre_opening"] == ["Wallet"]
    assert transactions.get_transaction(conn, normal.id)["pre_opening"] == []


def test_pages_and_api_expose_the_rule(app, conn):
    from tests.conftest import csrf_from, login, make_user
    make_user(conn)
    client = app.test_client()
    login(client)
    wallet = make_account(conn, "Wallet", "3,000")
    rahim = people.create_person(conn, name="Rahim")
    tx = post(conn, type="lent", source_account_id=wallet, person_id=rahim, amount="1,000", occurred_local=local(conn, 30))
    opts = client.get("/api/options").get_json()
    assert opts["accounts"][0]["opened_on"] == _today(conn).isoformat()
    detail = client.get(f"/transactions/{tx.id}").get_data(as_text=True)
    assert "does <strong>not</strong> change" in detail
    assert "before opening" in client.get("/transactions/").get_data(as_text=True)
    assert client.get(f"/accounts/{wallet}").status_code == 200
