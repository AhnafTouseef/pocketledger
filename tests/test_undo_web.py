"""Delete + Undo through the web layer: confirmation text, the Undo offer, and the undo endpoint."""
import json
import re

from conftest import csrf_from
from pocketledger.services import ledger

UUID = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def api(client, tok, method, url, payload=None):
    return getattr(client, method)(url, data=json.dumps(payload) if payload is not None else None,
                                   content_type="application/json", headers={"X-CSRFToken": tok})


def offer_from(html):
    m = re.search(r'id="undo-offer" hidden data-token="([^"]+)" data-label="([^"]*)" data-message="([^"]*)"', html)
    return m and m.groups()


def test_account_page_confirmation_names_the_account_and_balance_and_the_real_world_note(authed, conn, ids):
    html = authed.get(f"/accounts/{ids['pocket']}").get_data(as_text=True)
    assert "Delete the account" in html and "Pocket" in html
    assert "৳2,000.00" in html                                          # the recorded balance, in the dialog text
    assert "does not change any real money" in html
    assert "archive" not in html.lower().replace("unarchive", "")      # the old Archive action is gone


def test_account_delete_flow_shows_undo_and_undo_restores(authed, conn, ids):
    tok = authed.token()
    r = authed.post(f"/accounts/{ids['pocket']}/delete", data={"csrf_token": tok})
    assert r.status_code == 302
    page = authed.get(r.headers["Location"]).get_data(as_text=True)
    found = offer_from(page)
    assert found, "an Undo offer must follow a delete"
    assert authed.get(f"/accounts/{ids['pocket']}").status_code == 404

    u = api(authed, authed.token(), "post", f"/api/undo/{found[0]}")
    assert u.status_code == 200 and u.get_json()["ok"] and u.get_json()["redirect"].endswith(f"/accounts/{ids['pocket']}")
    assert authed.get(f"/accounts/{ids['pocket']}").status_code == 200
    assert ledger.account_balance(conn, ids["pocket"]) == 200000
    assert ledger.verify_integrity(conn) == []

    again = api(authed, authed.token(), "post", f"/api/undo/{found[0]}")      # a second click / second device
    assert again.status_code == 409 and "already" in again.get_json()["error"]
    assert api(authed, authed.token(), "post", "/api/undo/not-a-token").status_code == 404


def test_the_offer_is_shown_once_and_only_after_a_successful_action(authed, conn, ids):
    tok = authed.token()
    authed.post(f"/people/{ids['rahim']}/delete", data={"csrf_token": tok})
    first = authed.get("/people/").get_data(as_text=True)
    assert offer_from(first)
    assert not offer_from(authed.get("/people/").get_data(as_text=True))       # gone on the next page view


def test_new_transaction_api_returns_an_undo_that_works(authed, conn, ids):
    tok = authed.token()
    r = api(authed, tok, "post", "/api/transactions",
            {"uuid": UUID, "type": "expense", "source_account_id": str(ids["pocket"]), "amount": "100"})
    assert r.status_code == 201
    offer = r.get_json()["undo"]
    assert offer and offer["token"]
    assert ledger.account_balance(conn, ids["pocket"]) == 190000
    assert api(authed, tok, "post", f"/api/undo/{offer['token']}").status_code == 200
    assert ledger.account_balance(conn, ids["pocket"]) == 200000
    # a replayed (idempotent) submission offers no second undo and creates nothing
    r2 = api(authed, tok, "post", "/api/transactions",
             {"uuid": UUID, "type": "expense", "source_account_id": str(ids["pocket"]), "amount": "100"})
    assert r2.status_code == 200 and r2.get_json()["undo"] is None


def test_transaction_delete_page_and_flow(authed, conn, ids):
    r = ledger.post_transaction(conn, {"type": "lent", "source_account_id": ids["bank"], "person_id": ids["rahim"], "amount": "1000"})
    html = authed.get(f"/transactions/{r.id}").get_data(as_text=True)
    assert "Delete this record" in html and "Void" not in html
    assert "does not change any real money" in html
    tok = csrf_from(html)
    resp = authed.post(f"/transactions/{r.id}/delete", data={"csrf_token": tok})
    assert resp.status_code == 302
    found = offer_from(authed.get(resp.headers["Location"]).get_data(as_text=True))
    assert found
    assert api(authed, authed.token(), "post", f"/api/undo/{found[0]}").status_code == 200
    assert authed.get(f"/transactions/{r.id}").status_code == 200
    assert authed.post(f"/transactions/{r.id}/delete", data={}).status_code in (400, 403)   # CSRF still enforced


def test_deleting_something_that_is_gone_is_a_clean_404(authed, conn, ids):
    tok = authed.token()
    assert authed.post("/accounts/999/delete", data={"csrf_token": tok}).status_code == 404
    assert authed.post("/people/999/delete", data={"csrf_token": tok}).status_code == 404
    assert authed.post("/transactions/999/delete", data={"csrf_token": tok}).status_code == 404


def test_login_and_setup_forms_give_instant_feedback(app, conn):
    from werkzeug.security import generate_password_hash
    c = app.test_client()
    setup = c.get("/setup").get_data(as_text=True)
    assert 'data-busy="Creating account…"' in setup and "/static/js/busy.js" in setup
    conn.execute("INSERT INTO users (username, password_hash) VALUES ('u1', ?)", (generate_password_hash("secret1"),))
    conn.commit()
    login = c.get("/login").get_data(as_text=True)
    assert 'data-busy="Signing in…"' in login and "/static/js/busy.js" in login
