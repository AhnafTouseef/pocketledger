"""Create a DEMO database with realistic sample data.

    python scripts/seed_demo.py                 # writes instance/demo.db
    python scripts/seed_demo.py --reset         # recreate an existing demo database
    # then run the app against it:
    #   Windows (PowerShell):  $env:DATABASE_PATH="instance/demo.db"; python run.py
    #   macOS/Linux:           DATABASE_PATH=instance/demo.db python run.py
    # Sign in as  demo / demo-password-123

Sample data is never mixed with real records: it goes into its own file, is flagged
is_demo=1 (the app shows a banner), and this script refuses to write into any database that
holds real data or into the configured real database.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from werkzeug.security import generate_password_hash  # noqa: E402

from pocketledger import migrate  # noqa: E402
from pocketledger.config import BASE_DIR  # noqa: E402
from pocketledger.db import connect  # noqa: E402
from pocketledger.services import accounts, ledger, people, reconciliation, settings  # noqa: E402
from pocketledger.timeutil import local_input_to_utc_iso, today_local, utcnow_iso  # noqa: E402

DEMO_USER, DEMO_PASSWORD = "demo", "demo-password-123"


def real_db_path() -> Path:
    raw = os.environ.get("DATABASE_PATH")
    inst = Path(os.environ.get("INSTANCE_PATH", BASE_DIR / "instance"))
    p = Path(raw) if raw else inst / "pocketledger.db"
    return (p if p.is_absolute() else BASE_DIR / p).resolve()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "instance" / "demo.db"))
    ap.add_argument("--reset", action="store_true", help="delete and recreate an existing DEMO database")
    args = ap.parse_args()
    target = Path(args.db).resolve()

    if target == real_db_path():
        sys.exit("Refusing: that is your real database. Demo data goes in its own file (default instance/demo.db).")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 0:
        conn = connect(str(target))
        try:
            try:
                is_demo = conn.execute("SELECT value FROM application_settings WHERE key='is_demo'").fetchone()
                has_data = conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()
            except Exception:
                sys.exit("Refusing: that file exists and isn't a PocketLedger database.")
        finally:
            conn.close()
        if has_data and not (is_demo and is_demo[0] == "1"):
            sys.exit("Refusing: that database contains real data (it is not flagged as demo).")
        if not args.reset:
            sys.exit(f"{target} already exists. Use --reset to recreate it.")
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(str(target) + suffix)
            except OSError:
                pass

    migrate.ensure_database(str(target))
    conn = connect(str(target))
    rnd = random.Random(42)
    tz = settings.tzname(conn)
    today = today_local(tz)

    def at(days_ago, hh=12, mm=0):
        d = today - timedelta(days=days_ago)
        return local_input_to_utc_iso(f"{d.isoformat()}T{hh:02d}:{mm:02d}", tz)

    def acct_id(name):
        return conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()[0]

    def cat_id(name, kind):
        return conn.execute("SELECT id FROM transaction_categories WHERE name = ? AND kind = ?", (name, kind)).fetchone()[0]

    def post(**kw):
        return ledger.post_transaction(conn, kw)

    try:
        settings.set_value(conn, "is_demo", "1")
        conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                     (DEMO_USER, generate_password_hash(DEMO_PASSWORD), utcnow_iso()))

        start = 100
        for name, cat, opening in [
            ("Pocket", 1, "3,500"), ("Wallet", 1, "1,200"), ("Home cash", 1, "15,000"),
            ("DBBL Savings", 2, "85,000"), ("bKash", 3, "4,200"), ("Nagad", 3, "800"),
        ]:
            accounts.create_account(conn, name=name, category_id=cat, currency="BDT", opening_balance=opening,
                                    opened_on=(today - timedelta(days=start)).isoformat())
        bank, pocket, bkash = acct_id("DBBL Savings"), acct_id("Pocket"), acct_id("bKash")
        wallet, home, nagad = acct_id("Wallet"), acct_id("Home cash"), acct_id("Nagad")

        def balance(a):
            return ledger.account_balance(conn, a)

        def spend(day, account, amount_taka, category, desc):
            minor = amount_taka * 100
            if balance(account) < minor:  # top up from the bank first, like real life
                top = ((minor - balance(account)) // 100000 + 1) * 1000
                post(type="transfer", source_account_id=bank, dest_account_id=account, amount=str(top),
                     occurred_at=at(day, 8, 30), description=f"Top-up for {conn.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]}")
            post(type="expense", source_account_id=account, amount=str(amount_taka), category_id=cat_id(category, "expense"),
                 occurred_at=at(day, rnd.randint(8, 21), rnd.randint(0, 59)), description=desc)

        # monthly rhythm over ~3 months
        for day in range(start - 1, 0, -1):
            d = today - timedelta(days=day)
            if d.day == 1:
                post(type="income", dest_account_id=bank, amount="62,000", category_id=cat_id("Salary", "income"),
                     occurred_at=at(day, 10), description="Monthly salary")
            if d.day == 3:
                spend(day, bank, 18000, "Rent", "House rent")
            if d.day == 5:
                spend(day, bank, 2400, "Bills & utilities", "Electricity & gas")
            if d.day == 7:
                spend(day, bkash, 750, "Mobile & internet", "Internet bill")
            if d.day == 12 and rnd.random() < 0.8:
                post(type="income", dest_account_id=bkash, amount=str(rnd.choice([6500, 8000, 12000])),
                     category_id=cat_id("Freelance", "income"), occurred_at=at(day, 16), description="Freelance project payment")
            if d.weekday() == 4:  # Friday: restock pocket cash from the bank
                post(type="transfer", source_account_id=bank, dest_account_id=pocket, amount="3,000",
                     occurred_at=at(day, 9), description="Weekly pocket cash")
            if rnd.random() < 0.85:
                spend(day, pocket, rnd.choice([120, 180, 250, 90, 320, 150]), "Food", rnd.choice(["Lunch", "Tea & snacks", "Dinner", "Groceries"]))
            if rnd.random() < 0.5:
                spend(day, pocket if rnd.random() < .6 else bkash, rnd.choice([40, 60, 120, 200]), "Transport", rnd.choice(["Rickshaw", "Bus fare", "Uber"]))
            if rnd.random() < 0.08:
                spend(day, bkash, rnd.choice([800, 1500, 2200]), "Shopping", "Shopping")

        # people: an overdue receivable, a recent one, a payable due soon, and a settled loan
        rahim = people.create_person(conn, name="Rahim", contact="01711-000000", notes="Cousin. Promised to return after Eid bonus.")
        karim = people.create_person(conn, name="Karim", contact="01811-000000")
        mitu = people.create_person(conn, name="Mitu Apa")
        sadia = people.create_person(conn, name="Sadia")
        post(type="lent", source_account_id=bank, person_id=rahim, amount="5,000", occurred_at=at(45, 14),
             due_date=(today - timedelta(days=10)).isoformat(), description="Lent to Rahim")
        post(type="repayment_received", dest_account_id=bkash, person_id=rahim, amount="1,500", occurred_at=at(20, 18), description="Rahim paid back part")
        post(type="lent", source_account_id=home, person_id=karim, amount="2,000", occurred_at=at(6, 11),
             due_date=(today + timedelta(days=20)).isoformat(), description="Lent to Karim")
        post(type="borrowed", dest_account_id=bank, person_id=mitu, amount="8,000", occurred_at=at(30, 13),
             due_date=(today + timedelta(days=5)).isoformat(), description="Borrowed from Mitu Apa")
        post(type="repayment_made", source_account_id=bank, person_id=mitu, amount="2,000", occurred_at=at(10, 12), description="Paid Mitu Apa part")
        post(type="lent", source_account_id=wallet, person_id=sadia, amount="1,000", occurred_at=at(60, 15), description="Lent to Sadia")
        post(type="repayment_received", dest_account_id=wallet, person_id=sadia, amount="1,000", occurred_at=at(40, 17), description="Sadia settled up")

        # reconciliation: Wallet matches, Pocket is short by 300 and left for investigation
        reconciliation.verify(conn, wallet, ledger.account_balance(conn, wallet), "Counted after dinner")
        rid = reconciliation.verify(conn, pocket, ledger.account_balance(conn, pocket) - 30000, "Felt lighter than expected")
        problems = ledger.verify_integrity(conn)
        if problems:
            raise SystemExit("Seed produced an inconsistent ledger: " + "; ".join(problems))
    finally:
        conn.close()
    print(f"Demo database created: {target}")
    print(f"Sign in as {DEMO_USER} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
