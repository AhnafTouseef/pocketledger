"""Deletion without restrictions, and Undo: exact, one-shot, and never over later work."""
import random
import sqlite3
from datetime import timedelta

import pytest

from pocketledger.services import accounts as accounts_svc, ledger, people as people_svc, reconciliation, undo
from pocketledger.services.errors import ConflictError, NotFoundError
from pocketledger.timeutil import UTC_FMT, utcnow


def post(conn, **kw):
    return ledger.post_transaction(conn, kw)


def offer():
    """The Undo offered by the action that just ran."""
    o = undo.take_last()
    assert o and o["token"]
    return o["token"]


def snapshot(conn):
    """Everything an undo has to restore exactly: balances, people balances, and every row."""
    live = "deleted_at IS NULL"          # a record that was created and then undone is deleted, i.e. not part of the state
    return {
        "accounts": [tuple(r) for r in conn.execute(f"SELECT * FROM accounts WHERE {live} ORDER BY id")],
        "people": [tuple(r) for r in conn.execute(f"SELECT * FROM people WHERE {live} ORDER BY id")],
        "tx": [tuple(r) for r in conn.execute(f"SELECT * FROM transactions WHERE {live} ORDER BY id")],
        "entries": [tuple(r) for r in conn.execute(
            f"SELECT e.* FROM transaction_entries e JOIN transactions t ON t.id = e.transaction_id WHERE t.{live} ORDER BY e.id")],
        "bal": [tuple(r) for r in conn.execute("SELECT * FROM v_account_balances ORDER BY account_id")],
        "pbal": [tuple(r) for r in conn.execute("SELECT * FROM v_person_balances ORDER BY person_id, currency")],
    }


def clean(conn):
    assert ledger.verify_integrity(conn) == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ---- nothing blocks a delete ------------------------------------------------------------------------
def test_delete_account_with_balance_and_open_loans_needs_no_settling(conn, ids):
    post(conn, type="lent", source_account_id=ids["pocket"], person_id=ids["rahim"], amount="500")
    post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="700")
    bank_before = ledger.account_balance(conn, ids["bank"])
    accounts_svc.delete_account(conn, ids["pocket"])                     # holds money, has a loan and a transfer
    assert ledger.account_balance(conn, ids["bank"]) == bank_before      # nothing moved anywhere
    assert people_svc.get_person(conn, ids["rahim"])["receivable_minor"] == 50000   # the loan record survives
    clean(conn)


def test_delete_person_with_unsettled_loans_and_repayments(conn, ids):
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="1000")
    post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="250")
    post(conn, type="borrowed", dest_account_id=ids["pocket"], person_id=ids["rahim"], amount="100")
    people_svc.delete_person(conn, ids["rahim"])
    assert ledger.account_balance(conn, ids["bank"]) == 1000000
    assert ledger.account_balance(conn, ids["pocket"]) == 200000
    with pytest.raises(NotFoundError):
        people_svc.get_person(conn, ids["rahim"])
    clean(conn)


def test_delete_summary_for_the_confirmation_text(conn, ids):
    post(conn, type="expense", source_account_id=ids["pocket"], amount="10")
    post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="5")
    s = accounts_svc.deletion_summary(conn, ids["pocket"])
    assert s["account"]["name"] == "Pocket" and (s["own_count"], s["kept_count"]) == (1, 1)
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["karim"], amount="5")
    assert people_svc.deletion_summary(conn, ids["karim"])["record_count"] == 1


def test_a_deleted_account_stays_visible_by_name_on_surviving_transfers(conn, ids):
    post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="5", description="T")
    accounts_svc.delete_account(conn, ids["pocket"])
    from pocketledger.services import transactions as txs
    res = txs.search(conn, {})
    rows = res["items"]
    assert any("Pocket (deleted)" in (r["dest_name"] or "") for r in rows)


def test_account_name_is_free_again_after_deleting(conn, ids):
    accounts_svc.delete_account(conn, ids["pocket"])
    accounts_svc.create_account(conn, name="Pocket", category_id=1, currency="BDT", opening_balance="1")


def test_direct_sql_deletes_are_still_refused(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="3")
    for sql in ("DELETE FROM transactions", "DELETE FROM transaction_entries", "DELETE FROM accounts", "DELETE FROM people"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    ledger.delete_transaction(conn, r.id)
    with pytest.raises(sqlite3.DatabaseError):                            # even a deleted row only goes through purge
        conn.execute("DELETE FROM transactions WHERE id = ?", (r.id,))


# ---- undo: every kind of action -----------------------------------------------------------------
def test_undo_delete_transaction_restores_balances_and_row(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="300", description="Lunch", notes="n")
    before = snapshot(conn)
    ledger.delete_transaction(conn, r.id)
    assert ledger.account_balance(conn, ids["pocket"]) == 200000
    undo.undo(conn, offer())
    assert snapshot(conn) == before
    clean(conn)


def test_undo_create_transaction_of_each_type(conn, ids):
    kinds = [
        dict(type="income", dest_account_id=ids["pocket"], amount="50"),
        dict(type="expense", source_account_id=ids["pocket"], amount="50"),
        dict(type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="50"),
        dict(type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="50"),
        dict(type="borrowed", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="50"),
        dict(type="adjustment", account_id=ids["pocket"], direction="increase", amount="50"),
        dict(type="adjustment", account_id=ids["pocket"], direction="decrease", amount="50"),
    ]
    for kw in kinds:
        before = snapshot(conn)
        post(conn, **kw)
        undo.undo(conn, offer())
        after = snapshot(conn)
        assert after["bal"] == before["bal"] and after["pbal"] == before["pbal"], kw
        clean(conn)


def test_undo_repayment(conn, ids):
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="1000")
    before = snapshot(conn)
    post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="400")
    assert people_svc.get_person(conn, ids["rahim"])["receivable_minor"] == 60000
    undo.undo(conn, offer())
    assert snapshot(conn)["pbal"] == before["pbal"] and snapshot(conn)["bal"] == before["bal"]
    clean(conn)


def test_creating_a_person_inline_is_one_undo_with_the_transaction(conn, ids):
    post(conn, type="lent", source_account_id=ids["bank"], new_person_name="Newcomer", amount="80")
    assert any(p["name"] == "Newcomer" for p in people_svc.list_people(conn))
    undo.undo(conn, offer())
    assert not any(p["name"] == "Newcomer" for p in people_svc.list_people(conn))
    assert ledger.account_balance(conn, ids["bank"]) == 1000000
    clean(conn)


def test_undo_delete_account_restores_its_records_and_links(conn, ids):
    post(conn, type="income", dest_account_id=ids["pocket"], amount="40")
    post(conn, type="lent", source_account_id=ids["pocket"], person_id=ids["rahim"], amount="100")
    post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="25")
    before = snapshot(conn)
    accounts_svc.delete_account(conn, ids["pocket"])
    undo.undo(conn, offer())
    assert snapshot(conn) == before
    clean(conn)


def test_undo_delete_person_restores_all_their_records(conn, ids):
    post(conn, type="lent", source_account_id=ids["bank"], person_id=ids["rahim"], amount="1000")
    post(conn, type="repayment_received", dest_account_id=ids["bank"], person_id=ids["rahim"], amount="250")
    before = snapshot(conn)
    people_svc.delete_person(conn, ids["rahim"])
    undo.undo(conn, offer())
    assert snapshot(conn) == before
    clean(conn)


def test_undo_create_account_and_person(conn, ids):
    a = accounts_svc.create_account(conn, name="Temp", category_id=1, currency="BDT", opening_balance="9")
    undo.undo(conn, offer())
    assert a not in {x["id"] for x in accounts_svc.list_accounts(conn)}
    p = people_svc.create_person(conn, name="Tmp")
    undo.undo(conn, offer())
    assert p not in {x["id"] for x in people_svc.list_people(conn)}
    clean(conn)


def test_undo_edit_in_place_and_by_replacement(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="100", description="Lunch")
    before = snapshot(conn)
    _, kind = ledger.edit_transaction(conn, r.id, {"type": "expense", "source_account_id": ids["pocket"], "amount": "100",
                                                   "description": "Dinner"}, reason="rename")
    assert kind == "updated"
    undo.undo(conn, offer())
    assert snapshot(conn) == before

    new_id, kind = ledger.edit_transaction(conn, r.id, {"type": "expense", "source_account_id": ids["pocket"], "amount": "250",
                                                        "description": "Lunch"}, reason="typo")
    assert kind == "replaced" and ledger.account_balance(conn, ids["pocket"]) == 200000 - 25000
    undo.undo(conn, offer())
    assert snapshot(conn) == before                                      # original is live again, replacement gone
    clean(conn)


def test_undo_edit_of_account_and_person_details(conn, ids):
    before = snapshot(conn)
    accounts_svc.update_account(conn, ids["pocket"], name="Wallet", category_id=1, description="x")
    undo.undo(conn, offer())
    people_svc.update_person(conn, ids["rahim"], name="Rahim K", contact="01", notes="n")
    undo.undo(conn, offer())
    assert snapshot(conn) == before


# ---- undo safety ----------------------------------------------------------------------------------
def test_undo_works_once_only(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="30")
    ledger.delete_transaction(conn, r.id)
    tok = offer()
    undo.undo(conn, tok)
    before = snapshot(conn)
    with pytest.raises(ConflictError, match="already"):
        undo.undo(conn, tok)
    assert snapshot(conn) == before                                      # no double restore, nothing changed
    with pytest.raises(NotFoundError):
        undo.undo(conn, "no-such-token")


def test_undo_does_not_overwrite_later_work(conn, ids):
    a = post(conn, type="expense", source_account_id=ids["pocket"], amount="10", description="A")
    ledger.delete_transaction(conn, a.id)
    tok = offer()
    post(conn, type="expense", source_account_id=ids["pocket"], amount="20", description="B")   # unrelated, later
    undo.undo(conn, tok)
    assert ledger.account_balance(conn, ids["pocket"]) == 200000 - 1000 - 2000    # both effects present
    clean(conn)


def test_undo_refused_when_the_record_changed_afterwards(conn, ids):
    accounts_svc.update_account(conn, ids["pocket"], name="Wallet", category_id=1, description="")
    first = offer()
    accounts_svc.update_account(conn, ids["pocket"], name="Purse", category_id=1, description="")
    before = snapshot(conn)
    with pytest.raises(ConflictError):
        undo.undo(conn, first)                                           # would clobber the newer rename
    assert snapshot(conn) == before                                      # all-or-nothing
    assert conn.execute("SELECT status FROM undo_log WHERE token = ?", (first,)).fetchone()[0] == "active"


def test_undo_delete_refused_if_same_name_was_taken_meanwhile(conn, ids):
    accounts_svc.delete_account(conn, ids["pocket"])
    tok = offer()
    accounts_svc.create_account(conn, name="Pocket", category_id=1, currency="BDT", opening_balance="1")
    before = snapshot(conn)
    with pytest.raises(ConflictError, match="same name"):
        undo.undo(conn, tok)
    assert snapshot(conn) == before


def test_undo_of_creation_refused_once_records_depend_on_it(conn, ids):
    a = accounts_svc.create_account(conn, name="Temp", category_id=1, currency="BDT", opening_balance="9")
    tok = offer()
    post(conn, type="expense", source_account_id=a, amount="1")
    with pytest.raises(ConflictError):
        undo.undo(conn, tok)
    clean(conn)


def test_undo_offer_expires(conn, ids):
    r = post(conn, type="expense", source_account_id=ids["pocket"], amount="30")
    tok = offer()
    old = (utcnow() - timedelta(minutes=16)).strftime(UTC_FMT)
    conn.execute("UPDATE undo_log SET created_at = ? WHERE token = ?", (old, tok))
    with pytest.raises(ConflictError, match="too late"):
        undo.undo(conn, tok)
    assert ledger.account_balance(conn, ids["pocket"]) == 200000 - 3000


def test_failed_action_leaves_no_undo_offer_and_no_changes(conn, ids):
    undo.clear_last()
    before = snapshot(conn)
    logs = conn.execute("SELECT COUNT(*) FROM undo_log").fetchone()[0]
    with pytest.raises(Exception):
        post(conn, type="expense", source_account_id=ids["pocket"], amount="-1")
    assert undo.take_last() is None and snapshot(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM undo_log").fetchone()[0] == logs


def test_reconciliation_verify_is_not_an_undoable_action(conn, ids):
    undo.clear_last()
    reconciliation.verify(conn, ids["pocket"], "2000")
    assert undo.take_last() is None


# ---- erasing deleted records for good ----------------------------------------------------------------
def test_purge_erases_old_deleted_records_and_leaves_a_consistent_database(conn, ids):
    post(conn, type="lent", source_account_id=ids["pocket"], person_id=ids["rahim"], amount="100")
    post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="25")
    keep = post(conn, type="expense", source_account_id=ids["bank"], amount="7")
    reconciliation.verify(conn, ids["pocket"], "2000")
    accounts_svc.delete_account(conn, ids["pocket"])
    people_svc.delete_person(conn, ids["rahim"])
    r = post(conn, type="expense", source_account_id=ids["bank"], amount="3")
    ledger.delete_transaction(conn, r.id)

    assert undo.purge(conn, now=utcnow() + timedelta(hours=1), force=True) == 0        # too recent: nothing goes
    erased = undo.purge(conn, now=utcnow() + timedelta(hours=25), force=True)
    assert erased == 2                      # the loan (went with Rahim) and the single expense; the transfer survives
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE id = ?", (r.id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM people WHERE id = ?", (ids["rahim"],)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE id = ?", (ids["pocket"],)).fetchone()[0] == 1   # the transfer still names it
    assert conn.execute("SELECT COUNT(*) FROM undo_log").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE entity_type='transaction' AND entity_id = ?", (r.id,)).fetchone()[0] == 0
    assert conn.execute("SELECT status FROM transactions WHERE id = ?", (keep.id,)).fetchone()[0] == "posted"
    assert conn.execute("SELECT value FROM application_settings WHERE key = 'purge_mode'").fetchone() is None
    clean(conn)


def test_purge_keeps_a_deleted_account_that_a_surviving_record_still_names(conn, ids):
    post(conn, type="transfer", source_account_id=ids["bank"], dest_account_id=ids["pocket"], amount="25")
    # delete the person-free account: the transfer survives, so the account row must too
    accounts_svc.delete_account(conn, ids["pocket"])
    undo.purge(conn, now=utcnow() + timedelta(hours=25), force=True)
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE id = ?", (ids["pocket"],)).fetchone()[0] == 1
    clean(conn)


# ---- randomised: any sequence of actions, undone newest-first, returns to the exact start -----------------
def test_random_actions_each_undo_to_the_exact_prior_state(conn, ids):
    rnd = random.Random(11)
    a_ids = [ids["bank"], ids["pocket"]]
    p_ids = [ids["rahim"], ids["karim"]]
    for step in range(120):
        before = snapshot(conn)
        undo.clear_last()
        op = rnd.choice(["income", "expense", "transfer", "lent", "borrowed", "delete_tx", "delete_acc", "delete_person", "edit"])
        try:
            if op == "income":
                post(conn, type="income", dest_account_id=rnd.choice(a_ids), amount=str(rnd.randint(1, 500)))
            elif op == "expense":
                post(conn, type="expense", source_account_id=rnd.choice(a_ids), amount=str(rnd.randint(1, 500)))
            elif op == "transfer":
                s, d = rnd.sample(a_ids, 2)
                post(conn, type="transfer", source_account_id=s, dest_account_id=d, amount=str(rnd.randint(1, 300)))
            elif op == "lent":
                post(conn, type="lent", source_account_id=rnd.choice(a_ids), person_id=rnd.choice(p_ids), amount=str(rnd.randint(1, 300)))
            elif op == "borrowed":
                post(conn, type="borrowed", dest_account_id=rnd.choice(a_ids), person_id=rnd.choice(p_ids), amount=str(rnd.randint(1, 300)))
            elif op == "delete_tx":
                row = conn.execute("SELECT id FROM transactions WHERE status='posted' AND deleted_at IS NULL ORDER BY RANDOM() LIMIT 1").fetchone()
                if row:
                    ledger.delete_transaction(conn, row[0])
            elif op == "delete_acc" and len(a_ids) > 1 and step % 9 == 0:
                accounts_svc.delete_account(conn, a_ids[-1])
            elif op == "delete_person" and step % 7 == 0:
                people_svc.delete_person(conn, p_ids[-1])
            elif op == "edit":
                row = conn.execute("SELECT id, type, source_account_id, dest_account_id, person_id, amount_minor FROM transactions "
                                   "WHERE status='posted' AND deleted_at IS NULL AND type IN ('income','expense') "
                                   "ORDER BY RANDOM() LIMIT 1").fetchone()
                if row:
                    data = {"type": row["type"], "amount": str(rnd.randint(1, 400))}
                    if row["source_account_id"]:
                        data["source_account_id"] = row["source_account_id"]
                    if row["dest_account_id"]:
                        data["dest_account_id"] = row["dest_account_id"]
                    ledger.edit_transaction(conn, row["id"], data, reason="x")
        except Exception as exc:                        # a rejected action must change nothing
            assert snapshot(conn) == before, (op, exc)
            continue
        clean(conn)
        o = undo.take_last()
        if o is None or rnd.random() < 0.45:            # leave some actions in place so the data keeps evolving
            continue
        undo.undo(conn, o["token"])
        assert snapshot(conn) == before, (step, op)
        clean(conn)
