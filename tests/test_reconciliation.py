import pytest

from pocketledger.services import accounts as accounts_svc, ledger, reconciliation as rec
from pocketledger.services.errors import NotFoundError, ValidationError


def post(conn, **kw):
    return ledger.post_transaction(conn, kw)


def test_spec_example_expected_2000_counted_1700(conn, ids):
    rid = rec.verify(conn, ids["pocket"], "1,700")
    r = rec.get_reconciliation(conn, rid)
    assert (r["expected_minor"], r["counted_minor"], r["difference_minor"]) == (200000, 170000, -30000)
    assert r["status"] == "open"
    assert [d["id"] for d in rec.open_discrepancies(conn)] == [rid]
    acc = accounts_svc.get_account(conn, ids["pocket"])
    assert (acc["last_verified_balance_minor"], acc["needs_verification"]) == (170000, False)
    assert acc["last_verified_at"] == r["verified_at"]


def test_verification_never_changes_the_books(conn, ids):
    before = (ledger.account_balance(conn, ids["pocket"]), conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
    rec.verify(conn, ids["pocket"], "1700")
    assert (ledger.account_balance(conn, ids["pocket"]), conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == before


def test_matching_count_is_not_a_discrepancy(conn, ids):
    rid = rec.verify(conn, ids["pocket"], "2000")
    assert rec.get_reconciliation(conn, rid)["status"] == "matched"
    assert rec.open_discrepancies(conn) == []


def test_explicit_adjustment_resolves_and_is_labelled(conn, ids):
    rid = rec.verify(conn, ids["pocket"], "1700")
    before = ledger.totals(conn, "BDT")["net_worth_minor"]
    rec.post_adjustment(conn, rid, "found short")
    assert ledger.account_balance(conn, ids["pocket"]) == 170000
    assert ledger.totals(conn, "BDT")["net_worth_minor"] == before - 30000
    r = rec.get_reconciliation(conn, rid)
    assert r["status"] == "resolved" and r["difference_minor"] == -30000   # original snapshot untouched
    tx = conn.execute("SELECT type, description, reconciliation_id FROM transactions WHERE type='adjustment'").fetchone()
    assert tx["reconciliation_id"] == rid and "Reconciliation adjustment" in tx["description"]
    assert "theft" not in tx["description"].lower()
    with pytest.raises(ValidationError):
        rec.post_adjustment(conn, rid)           # nothing left to adjust
    assert ledger.verify_integrity(conn) == []


def test_recording_a_forgotten_expense_resolves_without_adjustment(conn, ids):
    rid = rec.verify(conn, ids["pocket"], "1700")
    verified_at = rec.get_reconciliation(conn, rid)["verified_at"]
    post(conn, type="expense", source_account_id=ids["pocket"], amount="300", description="Forgot: auto fare",
         occurred_at=verified_at, reconciliation_id=rid)
    assert rec.get_reconciliation(conn, rid)["status"] == "resolved"
    assert rec.open_discrepancies(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE type='adjustment'").fetchone()[0] == 0
    kinds = [e["entry_type"] for e in rec.get_timeline(conn, rid)]
    assert kinds == ["verified", "transaction_linked"]


def test_unrelated_account_cannot_be_linked(conn, ids):
    rid = rec.verify(conn, ids["pocket"], "1700")
    with pytest.raises(ValidationError):
        post(conn, type="expense", source_account_id=ids["bank"], amount="10", reconciliation_id=rid)


def test_leave_unresolved_stays_visible_and_can_reopen(conn, ids):
    rid = rec.verify(conn, ids["pocket"], "1700")
    rec.leave_unresolved(conn, rid, "will check my bag")
    d = rec.open_discrepancies(conn)
    assert [x["status"] for x in d] == ["unresolved"]
    rec.reopen(conn, rid)
    assert rec.get_reconciliation(conn, rid)["status"] == "open"
    assert [e["entry_type"] for e in rec.get_timeline(conn, rid)] == ["verified", "left_unresolved", "reopened"]


def test_history_is_preserved_and_latest_count_wins(conn, ids):
    first = rec.verify(conn, ids["pocket"], "1700")
    second = rec.verify(conn, ids["pocket"], "2000")
    rows = rec.list_reconciliations(conn, account_id=ids["pocket"])
    assert {r["id"] for r in rows} == {first, second}
    assert rec.get_reconciliation(conn, first)["status"] == "superseded"
    assert rec.open_discrepancies(conn) == []
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE reconciliations SET counted_minor = 5 WHERE id = ?", (first,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM reconciliations WHERE id = ?", (first,))


def test_invalid_counts_rejected_and_deleted_accounts_refused(conn, ids):
    for bad in ("", "abc", "-5"):
        with pytest.raises(ValidationError):
            rec.verify(conn, ids["pocket"], bad)
    empty = accounts_svc.create_account(conn, name="Old", category_id=1, currency="BDT", opening_balance="0")
    accounts_svc.delete_account(conn, empty)
    with pytest.raises((ValidationError, NotFoundError)):
        rec.verify(conn, empty, "0")


def test_physical_cash_flagged_when_never_or_long_ago_verified(conn, ids):
    assert accounts_svc.get_account(conn, ids["pocket"])["needs_verification"] is True      # never verified, holds cash
    assert accounts_svc.get_account(conn, ids["bank"])["needs_verification"] is False       # bank accounts aren't nagged
    rec.verify(conn, ids["pocket"], "2000")
    assert accounts_svc.get_account(conn, ids["pocket"])["needs_verification"] is False
