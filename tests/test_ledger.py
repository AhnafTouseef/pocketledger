"""Financial integrity: the properties that must hold no matter what the user does."""
import random
import sqlite3

import pytest

from pocketledger.services import accounts as accounts_svc, audit, ledger, people as people_svc
from pocketledger.services.errors import NotFoundError, ValidationError


def post(conn, **kw):
    return ledger.post_transaction(conn, kw)


def nw(conn):
    return ledger.totals(conn, "BDT")["net_worth_minor"]


def tx_count(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


# ---- the spec's headline rules ----------------------------------------------------------------
def test_transfer_moves_money_without_changing_net_worth(conn, ids):
    before = ledger.totals(conn, "BDT")
    post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="2,000")
    after = ledger.totals(conn, "BDT")
    assert ledger.account_balance(conn, ids["bank"]) == 800000
    assert ledger.account_balance(conn, ids["pocket"]) == 400000
    assert after["assets_minor"] == before["assets_minor"]
    assert after["net_worth_minor"] == before["net_worth_minor"] == 1200000


def test_transfer_is_one_transaction_with_linked_debit_and_credit(conn, ids):
    r = post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="500")
    rows = conn.execute("SELECT account_id, debit_minor, credit_minor FROM transaction_entries WHERE transaction_id = ?", (r.id,)).fetchall()
    assert len(rows) == 2
    assert sum(x["debit_minor"] for x in rows) == sum(x["credit_minor"] for x in rows) == 50000
    assert {x["account_id"] for x in rows} == {ids["bank"], ids["pocket"]}


def test_receivables_are_assets_but_not_liquid(conn, ids):
    post(conn, type="lent", source_account_id=ids["pocket"], person_id=ids["rahim"], amount="500")
    t = ledger.totals(conn, "BDT")
    assert t["liquid_minor"] == 1150000          # 12,000 - 500
    assert t["receivables_minor"] == 50000
    assert t["assets_minor"] == 1200000
    assert t["net_worth_minor"] == 1200000       # lending doesn't change net worth


def test_borrowing_raises_assets_and_liabilities_equally(conn, ids):
    post(conn, type="borrowed", dest_account_id=ids["bank"], person_id=ids["karim"], amount="3000")
    t = ledger.totals(conn, "BDT")
    assert (t["liquid_minor"], t["payables_minor"], t["net_worth_minor"]) == (1500000, 300000, 1200000)


def test_income_and_expense_change_net_worth(conn, ids):
    post(conn, type="income", dest_account_id=ids["bank"], amount="5000")
    assert nw(conn) == 1700000
    post(conn, type="expense", source_account_id=ids["pocket"], amount="150.50")
    assert nw(conn) == 1700000 - 15050


def test_partial_multiple_repayments_and_full_settlement(conn, ids):
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="1000")
    post(conn, type="lent", source_account_id=ids["pocket"], person_id=ids["rahim"], amount="500")
    post(conn, type="repayment_received", dest_account_id=ids["pocket"], person_id=ids["rahim"], amount="400")
    post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="600")
    assert people_svc.get_person(conn, ids["rahim"])["receivable_minor"] == 50000
    post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="500")
    p = people_svc.get_person(conn, ids["rahim"])
    assert (p["receivable_minor"], p["lent_minor"], p["received_back_minor"]) == (0, 150000, 150000)
    assert nw(conn) == 1200000


def test_repayment_cannot_exceed_outstanding(conn, ids):
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="300")
    n = tx_count(conn)
    with pytest.raises(ValidationError, match="owes you"):
        post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="300.01")
    with pytest.raises(ValidationError, match="You owe"):
        post(conn, type="repayment_made", source_account_id=ids["bank"], person_id=ids["rahim"], amount="1")
    assert tx_count(conn) == n


def test_person_balance_is_derived_never_stored(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(people)")}
    assert not any("balance" in c or "owed" in c for c in cols)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    assert not any(c in ("balance", "balance_minor", "current_balance") for c in cols)


def test_fifo_loan_breakdown_and_overdue(conn, ids):
    from datetime import date
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="1000", due_date="2026-01-10",
         occurred_at="2026-01-01T06:00:00Z")
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="1000", due_date="2026-12-31",
         occurred_at="2026-02-01T06:00:00Z")
    post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="1200")
    loans = people_svc.loan_breakdown(conn, ids["rahim"], "receivable")
    assert [(l["repaid_minor"], l["remaining_minor"]) for l in loans] == [(100000, 0), (20000, 80000)]
    assert people_svc.overdue_receivables(conn, date(2026, 6, 1)) == []  # first loan is settled, second not due
    over = people_svc.overdue_receivables(conn, date(2027, 1, 5))
    assert [(o["remaining_minor"]) for o in over] == [80000]


# ---- validation ----------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["0", "-5", "abc", "", "1.234", "1e5"])
def test_bad_amounts_rejected(conn, ids, bad):
    n = tx_count(conn)
    with pytest.raises(ValidationError):
        post(conn, type="expense", source_account_id=ids["pocket"], amount=bad)
    assert tx_count(conn) == n


def test_transfer_requires_two_different_accounts(conn, ids):
    with pytest.raises(ValidationError):
        post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["bank"], amount="1")


def test_future_and_ancient_dates_rejected(conn, ids):
    with pytest.raises(ValidationError, match="future"):
        post(conn, type="expense", source_account_id=ids["pocket"], amount="1", occurred_at="2099-01-01T00:00:00Z")
    with pytest.raises(ValidationError, match="past"):
        post(conn, type="expense", source_account_id=ids["pocket"], amount="1", occurred_at="1999-01-01T00:00:00Z")


def test_archived_accounts_cannot_receive_new_transactions(conn, ids):
    empty = accounts_svc.create_account(conn, name="Empty", category_id=1, currency="BDT", opening_balance="0")
    accounts_svc.archive_account(conn, empty)
    with pytest.raises(ValidationError, match="archived"):
        post(conn, type="income", dest_account_id=empty, amount="5")


def test_cannot_archive_account_holding_money(conn, ids):
    with pytest.raises(ValidationError, match="still holds"):
        accounts_svc.archive_account(conn, ids["pocket"])


def test_category_must_match_type(conn, ids):
    income_cat = conn.execute("SELECT id FROM transaction_categories WHERE kind='income' LIMIT 1").fetchone()[0]
    with pytest.raises(ValidationError):
        post(conn, type="expense", source_account_id=ids["pocket"], amount="5", category_id=income_cat)


# ---- atomicity ------------------------------------------------------------------------------------
def test_failed_post_leaves_nothing_behind_even_for_new_person(conn, ids):
    n, people = tx_count(conn), conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    with pytest.raises(ValidationError):
        post(conn, type="lent", source_account_id=ids["pocket"], new_person_name="Zed", amount="not money")
    assert tx_count(conn) == n
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == people
    assert not conn.in_transaction


def test_crash_midway_rolls_everything_back(conn, ids, monkeypatch):
    before = (tx_count(conn), conn.execute("SELECT COUNT(*) FROM transaction_entries").fetchone()[0], nw(conn))

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(audit, "log", boom)
    with pytest.raises(RuntimeError):
        post(conn, type="expense", source_account_id=ids["pocket"], amount="100")
    monkeypatch.undo()
    assert (tx_count(conn), conn.execute("SELECT COUNT(*) FROM transaction_entries").fetchone()[0], nw(conn)) == before
    assert not conn.in_transaction
    assert ledger.verify_integrity(conn) == []


# ---- idempotency / duplicates ----------------------------------------------------------------------
def test_same_uuid_is_posted_once(conn, ids):
    u = "6f1c2b0e-9a3d-4c55-8d0e-0a1b2c3d4e5f"
    a = post(conn, uuid=u, type="expense", source_account_id=ids["pocket"], amount="75")
    b = post(conn, uuid=u, type="expense", source_account_id=ids["pocket"], amount="75")
    assert a.created and not b.created and a.id == b.id
    assert ledger.account_balance(conn, ids["pocket"]) == 200000 - 7500


def test_rapid_identical_transaction_warns_but_is_allowed(conn, ids):
    post(conn, type="expense", source_account_id=ids["pocket"], amount="40")
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="40")
    assert r.created and r.warnings


def test_bad_uuid_rejected(conn, ids):
    with pytest.raises(ValidationError):
        post(conn, uuid="not-a-uuid", type="expense", source_account_id=ids["pocket"], amount="1")


# ---- immutability / corrections / audit --------------------------------------------------------------
def test_ledger_rows_cannot_be_rewritten_or_deleted(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="10")
    for sql in (
        f"UPDATE transactions SET amount_minor = 1 WHERE id = {r.id}",
        f"UPDATE transactions SET source_account_id = {ids['bank']} WHERE id = {r.id}",
        f"DELETE FROM transactions WHERE id = {r.id}",
        f"UPDATE transaction_entries SET debit_minor = 5 WHERE transaction_id = {r.id}",
        f"DELETE FROM transaction_entries WHERE transaction_id = {r.id}",
        "UPDATE audit_logs SET action = 'x'", "DELETE FROM audit_logs",
        f"DELETE FROM accounts WHERE id = {ids['bank']}",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    assert ledger.verify_integrity(conn) == []


def test_database_rejects_unbalanced_or_wrong_shape_rows(conn, ids):
    with pytest.raises(sqlite3.IntegrityError):  # an 'income' that names a source account
        conn.execute("INSERT INTO transactions (uuid,type,amount_minor,currency,occurred_at,source_account_id,dest_account_id,description,created_at)"
                     " VALUES ('u1','income',5,'BDT','2026-01-01T00:00:00Z',1,2,'x','2026-01-01T00:00:00Z')")
    with pytest.raises(sqlite3.IntegrityError):  # zero amount
        conn.execute("INSERT INTO transactions (uuid,type,amount_minor,currency,occurred_at,dest_account_id,description,created_at)"
                     " VALUES ('u2','income',0,'BDT','2026-01-01T00:00:00Z',2,'x','2026-01-01T00:00:00Z')")


def test_void_reverts_balances_keeps_record_and_audits(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="300")
    ledger.void_transaction(conn, r.id, "entered twice")
    assert ledger.account_balance(conn, ids["pocket"]) == 200000
    row = conn.execute("SELECT status, void_reason FROM transactions WHERE id = ?", (r.id,)).fetchone()
    assert (row["status"], row["void_reason"]) == ("voided", "entered twice")
    logs, _ = audit.list_logs(conn, entity_type="transaction", entity_id=r.id)
    assert {l["action"] for l in logs} == {"create", "void"}
    with pytest.raises(ValidationError, match="already"):
        ledger.void_transaction(conn, r.id, "again")
    with pytest.raises(ValidationError):
        ledger.void_transaction(conn, post(conn, type="expense", source_account_id=ids["pocket"], amount="1").id, " ")


def test_cannot_void_a_loan_that_later_repayments_depend_on(conn, ids):
    loan = post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="1000")
    post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="400")
    with pytest.raises(ValidationError, match="negative"):
        ledger.void_transaction(conn, loan.id, "mistake")
    assert conn.execute("SELECT status FROM transactions WHERE id = ?", (loan.id,)).fetchone()[0] == "posted"
    assert ledger.verify_integrity(conn) == []


def test_edit_amount_voids_original_and_links_replacement(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="100", description="Lunch")
    new_id, kind = ledger.edit_transaction(conn, r.id, {"type": "expense", "source_account_id": ids["pocket"], "amount": "120", "description": "Lunch"}, reason="typo")
    assert kind == "replaced" and new_id != r.id
    old = conn.execute("SELECT status, replaced_by_id FROM transactions WHERE id = ?", (r.id,)).fetchone()
    new = conn.execute("SELECT replaces_id, amount_minor FROM transactions WHERE id = ?", (new_id,)).fetchone()
    assert (old["status"], old["replaced_by_id"]) == ("voided", new_id)
    assert (new["replaces_id"], new["amount_minor"]) == (r.id, 12000)
    assert ledger.account_balance(conn, ids["pocket"]) == 200000 - 12000
    assert ledger.verify_integrity(conn) == []


def test_edit_description_only_updates_in_place_with_audit(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="100", description="Lunch")
    same, kind = ledger.edit_transaction(conn, r.id, {"type": "expense", "source_account_id": ids["pocket"], "amount": "100", "description": "Team lunch"})
    assert (same, kind) == (r.id, "updated")
    assert conn.execute("SELECT description FROM transactions WHERE id = ?", (r.id,)).fetchone()[0] == "Team lunch"
    logs, _ = audit.list_logs(conn, entity_type="transaction", entity_id=r.id)
    assert "update" in {l["action"] for l in logs}


def test_edit_cannot_make_repayment_exceed_balance(conn, ids):
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="500")
    rep = post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="100")
    with pytest.raises(ValidationError):
        ledger.edit_transaction(conn, rep.id, {"type": "repayment_received", "dest_account_id": ids["bank"], "person_id": ids["rahim"], "amount": "900"})
    assert conn.execute("SELECT status FROM transactions WHERE id = ?", (rep.id,)).fetchone()[0] == "posted"


def test_opening_balance_locked_once_history_exists(conn, ids):
    post(conn, type="expense", source_account_id=ids["pocket"], amount="1")
    with pytest.raises(ValidationError, match="locked"):
        accounts_svc.update_account(conn, ids["pocket"], name="Pocket", category_id=1, description="", opening_balance="9999")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE accounts SET opening_balance_minor = 5 WHERE id = ?", (ids["pocket"],))


def test_integrity_check_detects_tampering(conn, ids):
    post(conn, type="expense", source_account_id=ids["pocket"], amount="10")
    assert ledger.verify_integrity(conn) == []
    conn.execute("DROP TRIGGER trg_entries_no_update")
    conn.execute("UPDATE transaction_entries SET debit_minor = debit_minor + 1 WHERE id = 1")
    problems = ledger.verify_integrity(conn)
    assert any("unbalanced" in p or "don't match" in p for p in problems)


# ---- property test: random activity vs an independent model ------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 3, 7, 42])
def test_random_activity_matches_independent_model(conn, ids, seed):
    rnd = random.Random(seed)
    acc = {ids["bank"]: 1000000, ids["pocket"]: 200000}
    recv = {ids["rahim"]: 0, ids["karim"]: 0}
    pay = {ids["rahim"]: 0, ids["karim"]: 0}
    income = expense = adjust = 0
    A, P = list(acc), list(recv)
    for _ in range(250):
        t = rnd.choice(["income", "expense", "transfer", "lent", "borrowed", "repayment_received", "repayment_made", "adjustment"])
        amt = rnd.randint(1, 500000)
        kw = {"type": t, "amount_minor": amt}
        s, d, p = rnd.choice(A), rnd.choice(A), rnd.choice(P)
        if t == "income": kw["dest_account_id"] = d
        elif t == "expense": kw["source_account_id"] = s
        elif t == "transfer": kw.update(source_account_id=s, dest_account_id=d)
        elif t == "lent": kw.update(source_account_id=s, person_id=p)
        elif t == "borrowed": kw.update(dest_account_id=d, person_id=p)
        elif t == "repayment_received": kw.update(dest_account_id=d, person_id=p)
        elif t == "repayment_made": kw.update(source_account_id=s, person_id=p)
        else: kw.update(account_id=s, direction=rnd.choice(["increase", "decrease"]))
        try:
            post(conn, **kw)
        except ValidationError:
            continue
        if t == "income": acc[d] += amt; income += amt
        elif t == "expense": acc[s] -= amt; expense += amt
        elif t == "transfer": acc[s] -= amt; acc[d] += amt
        elif t == "lent": acc[s] -= amt; recv[p] += amt
        elif t == "borrowed": acc[d] += amt; pay[p] += amt
        elif t == "repayment_received": acc[d] += amt; recv[p] -= amt
        elif t == "repayment_made": acc[s] -= amt; pay[p] -= amt
        else:
            delta = amt if kw["direction"] == "increase" else -amt
            acc[s] += delta; adjust += delta
    for a, expected in acc.items():
        assert ledger.account_balance(conn, a) == expected
    t = ledger.totals(conn, "BDT")
    assert t["liquid_minor"] == sum(acc.values())
    assert t["receivables_minor"] == sum(recv.values()) and t["payables_minor"] == sum(pay.values())
    assert t["net_worth_minor"] == 1200000 + income - expense + adjust
    assert all(v >= 0 for v in list(recv.values()) + list(pay.values()))
    assert ledger.verify_integrity(conn) == []
