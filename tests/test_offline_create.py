"""Offline creation of accounts and people: idempotent create APIs and references to not-yet-synced records."""
import json

from pocketledger.services import ledger

A_UUID = "11111111-1111-4111-8111-111111111111"
P_UUID = "22222222-2222-4222-8222-222222222222"
T1, T2, T3 = ("33333333-3333-4333-8333-33333333333%d" % i for i in (1, 2, 3))


def post(authed, url, payload):
    return authed.post(url, data=json.dumps(payload), content_type="application/json", headers={"X-CSRFToken": authed.token()})


def test_options_carry_what_the_offline_forms_need(authed, ids):
    o = authed.get("/api/options").get_json()
    assert o["account_categories"] and {"id", "name", "kind"} <= set(o["account_categories"][0])
    assert o["currencies"] and o["currencies"][0]["code"]


def test_create_account_is_idempotent(authed, conn, ids):
    body = {"uuid": A_UUID, "name": "bKash", "category_id": 1, "opening_balance": "250", "currency": "BDT", "opened_on": "", "description": ""}
    r1 = post(authed, "/api/accounts", body)
    assert r1.status_code == 201 and r1.get_json()["created"] and r1.get_json()["undo"]
    r2 = post(authed, "/api/accounts", body)                                 # the queue replays it
    assert r2.status_code == 200 and r2.get_json()["created"] is False and r2.get_json()["id"] == r1.get_json()["id"]
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE name = 'bKash'").fetchone()[0] == 1
    assert ledger.account_balance(conn, r1.get_json()["id"]) == 25000
    assert ledger.verify_integrity(conn) == []


def test_create_person_is_idempotent(authed, conn, ids):
    body = {"uuid": P_UUID, "name": "Nila", "contact": "01700", "notes": ""}
    r1 = post(authed, "/api/people", body); r2 = post(authed, "/api/people", body)
    assert (r1.status_code, r2.status_code) == (201, 200) and r1.get_json()["id"] == r2.get_json()["id"]
    assert conn.execute("SELECT COUNT(*) FROM people WHERE name = 'Nila'").fetchone()[0] == 1


def test_rejections_are_clear_and_create_nothing(authed, conn, ids):
    before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    dup = post(authed, "/api/accounts", {"uuid": A_UUID, "name": "Pocket", "category_id": 1, "opening_balance": "0"})
    assert dup.status_code == 422 and dup.get_json()["field"] == "name"
    assert post(authed, "/api/accounts", {"name": "NoId", "category_id": 1}).status_code == 422
    assert post(authed, "/api/accounts", {"uuid": "zz", "name": "Bad", "category_id": 1}).status_code == 422
    assert post(authed, "/api/people", {"uuid": P_UUID, "name": "Rahim"}).status_code == 422        # name taken
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before


def test_transactions_can_use_accounts_and_people_that_were_created_offline(authed, conn, ids):
    acc = post(authed, "/api/accounts", {"uuid": A_UUID, "name": "bKash", "category_id": 1, "opening_balance": "1000"}).get_json()["id"]
    per = post(authed, "/api/people", {"uuid": P_UUID, "name": "Nila"}).get_json()["id"]
    r1 = post(authed, "/api/transactions", {"uuid": T1, "type": "expense", "amount": "100", "source_account_id": f"acct:{A_UUID}"})
    r2 = post(authed, "/api/transactions", {"uuid": T2, "type": "lent", "amount": "300", "source_account_id": f"acct:{A_UUID}", "person_id": f"person:{P_UUID}"})
    r3 = post(authed, "/api/transactions", {"uuid": T3, "type": "transfer", "amount": "50", "source_account_id": f"acct:{A_UUID}", "dest_account_id": str(ids["bank"])})
    assert [r.status_code for r in (r1, r2, r3)] == [201, 201, 201]
    assert ledger.account_balance(conn, acc) == 100000 - 10000 - 30000 - 5000
    assert conn.execute("SELECT person_id FROM transactions WHERE uuid = ?", (T2,)).fetchone()[0] == per
    # replay of a transaction that used a reference: still one transaction
    again = post(authed, "/api/transactions", {"uuid": T2, "type": "lent", "amount": "300", "source_account_id": f"acct:{A_UUID}", "person_id": f"person:{P_UUID}"})
    assert again.status_code == 200 and again.get_json()["created"] is False
    assert ledger.verify_integrity(conn) == []


def test_reference_to_a_record_that_was_never_created_is_rejected_not_guessed(authed, conn, ids):
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    r = post(authed, "/api/transactions", {"uuid": T1, "type": "expense", "amount": "10", "source_account_id": "acct:99999999-9999-4999-8999-999999999999"})
    assert r.status_code == 422 and "never created" in r.get_json()["error"]
    r = post(authed, "/api/transactions", {"uuid": T2, "type": "lent", "amount": "10", "source_account_id": str(ids["bank"]), "person_id": "person:" + P_UUID})
    assert r.status_code == 422
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before


def test_offline_created_account_can_be_undone_deleted_and_exported(authed, conn, ids):
    r = post(authed, "/api/accounts", {"uuid": A_UUID, "name": "Temp", "category_id": 1, "opening_balance": "5"}).get_json()
    u = authed.post(f"/api/undo/{r['undo']['token']}", headers={"X-CSRFToken": authed.token()})
    assert u.status_code == 200
    assert conn.execute("SELECT deleted_at FROM accounts WHERE client_uuid = ?", (A_UUID,)).fetchone()[0]


def test_offline_page_offers_the_add_buttons_and_loads_the_script(authed):
    html = authed.get("/offline").get_data(as_text=True)
    assert "data-add-account" in html and "data-add-person" in html and "/static/js/offadd.js" in html
    assert "/static/js/offadd.js" in authed.get("/").get_data(as_text=True)
