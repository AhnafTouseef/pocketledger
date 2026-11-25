import json
import re

import pytest

from conftest import PASSWORD, csrf_from, login, make_user
from pocketledger.db import connect

UUID = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"


def api_post(client, token, payload, url="/api/transactions", method="post"):
    return getattr(client, method)(url, data=json.dumps(payload), content_type="application/json", headers={"X-CSRFToken": token})


# ---- access control ----------------------------------------------------------------------------------
def test_everything_requires_login(app):
    client = app.test_client()
    for path in ["/", "/accounts/", "/people/", "/transactions/", "/analytics/", "/settings/", "/reconcile/",
                 "/transactions/export.csv", "/settings/backup/download.db", "/settings/backup/download.json", "/settings/audit"]:
        r = client.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path
    r = client.get("/api/options")
    assert r.status_code == 401 and r.get_json()["code"] == "auth"


def test_every_registered_route_is_protected_except_the_public_list(app):
    from pocketledger.security import PUBLIC_ENDPOINTS
    client = app.test_client()
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC_ENDPOINTS or "GET" not in rule.methods:
            continue
        url = re.sub(r"<[^>]+>", "1", rule.rule)
        r = client.get(url)
        assert r.status_code in (302, 401), f"{rule.rule} is reachable without logging in ({r.status_code})"


def test_first_run_redirects_to_setup_then_creates_user(app):
    client = app.test_client()
    assert client.get("/login").headers["Location"].endswith("/setup")
    token = csrf_from(client.get("/setup").get_data(as_text=True))
    weak = client.post("/setup", data={"username": "owner", "password": "short", "confirm": "short", "csrf_token": token})
    assert b"at least 6" in weak.data
    mismatch = client.post("/setup", data={"username": "owner", "password": PASSWORD, "confirm": PASSWORD + "x", "csrf_token": token})
    assert b"don&#39;t match" in mismatch.data
    ok = client.post("/setup", data={"username": "owner", "password": PASSWORD, "confirm": PASSWORD, "csrf_token": token})
    assert ok.status_code == 302
    assert client.get("/setup").status_code == 302       # setup is closed once a user exists


def test_passwords_are_hashed(app, conn):
    make_user(conn)
    stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert PASSWORD not in stored and stored.startswith(("scrypt:", "pbkdf2:"))


def test_login_wrong_password_and_throttling(app, conn):
    make_user(conn)
    client = app.test_client()
    for _ in range(5):
        r = login(client, password="wrong")
        assert r.status_code == 200 and b"Wrong username or password" in r.data
    locked = login(client)                                 # even the right password is refused during lockout
    assert locked.status_code == 200 and b"Too many failed attempts" in locked.data


def test_login_does_not_reveal_whether_user_exists(app, conn):
    make_user(conn)
    a = login(app.test_client(), username="owner", password="nope-nope-nope").data
    b = login(app.test_client(), username="ghost", password="nope-nope-nope").data
    assert b"Wrong username or password" in a and b"Wrong username or password" in b


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)"])
def test_open_redirect_blocked(app, conn, target):
    make_user(conn)
    client = app.test_client()
    token = csrf_from(client.get("/login").get_data(as_text=True))
    r = client.post(f"/login?next={target}", data={"username": "owner", "password": PASSWORD, "csrf_token": token})
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_session_cookie_flags(authed):
    r = authed.get("/")
    # the cookie is set on login; check flags from a fresh login response
    from conftest import login as do_login
    c = authed.application.test_client()
    resp = do_login(c)
    cookie = resp.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


# ---- CSRF ------------------------------------------------------------------------------------------------
def test_state_changing_requests_need_csrf(authed):
    assert authed.post("/accounts/new", data={"name": "X", "category_id": 1}).status_code == 400
    assert authed.post("/accounts/new", data={"name": "X", "category_id": 1, "csrf_token": "forged"}).status_code == 400
    r = authed.post("/api/transactions", data="{}", content_type="application/json")
    assert r.status_code == 400 and r.get_json()["code"] == "csrf"
    assert authed.post("/logout").status_code == 400


def test_security_headers(authed):
    r = authed.get("/")
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["Cache-Control"] == "no-store"


def test_xss_in_user_text_is_escaped(authed):
    tok = authed.token()
    authed.post("/people/new", data={"name": "<script>alert(1)</script>", "csrf_token": tok})
    html = authed.get("/people/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_sql_injection_attempts_are_inert(authed, conn):
    tok = authed.token()
    for term in ["' OR 1=1 --", "'; DROP TABLE transactions; --", "%", "_"]:
        assert authed.get("/transactions/", query_string={"q": term}).status_code == 200
    authed.post("/people/new", data={"name": "Robert'); DROP TABLE people;--", "csrf_token": tok})
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1


def test_password_change_signs_out_other_sessions(app, authed):
    other = app.test_client()
    assert login(other).status_code == 302
    assert other.get("/").status_code == 200
    tok = authed.token()
    r = authed.post("/settings/password", data={"current": PASSWORD, "new": "another long passphrase", "confirm": "another long passphrase", "csrf_token": tok})
    assert r.status_code == 302
    assert other.get("/").status_code == 302          # old session invalidated
    assert authed.get("/").status_code == 200         # this one stays signed in


# ---- end-to-end flows through the real routes -----------------------------------------------------------------
def test_full_money_flow_through_the_web_layer(authed, conn):
    tok = authed.token()
    for name, cat, bal in (("Bank", 2, "10000"), ("Pocket", 1, "2000")):
        r = authed.post("/accounts/new", data={"name": name, "category_id": cat, "currency": "BDT", "opening_balance": bal, "csrf_token": tok})
        assert r.status_code == 302
    bank, pocket = [r[0] for r in conn.execute("SELECT id FROM accounts ORDER BY id")]

    r = api_post(authed, tok, {"uuid": UUID, "type": "transfer", "source_account_id": str(bank), "dest_account_id": str(pocket), "amount": "2,000"})
    assert r.status_code == 201 and r.get_json()["ok"]
    again = api_post(authed, tok, {"uuid": UUID, "type": "transfer", "source_account_id": str(bank), "dest_account_id": str(pocket), "amount": "2,000"})
    assert again.status_code == 200 and again.get_json()["created"] is False
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1

    r = api_post(authed, tok, {"uuid": "11111111-2222-4333-8444-555555555555", "type": "lent", "source_account_id": str(pocket),
                               "new_person_name": "Tanvir", "amount": "700", "due_date": "2027-02-01"})
    assert r.status_code == 201
    dash = authed.get("/").get_data(as_text=True)
    assert "৳12,000.00" in dash                      # net worth unchanged by transfer and loan (Indian grouping)
    assert "৳11,300.00" in dash                      # liquid money excludes the ৳700 receivable
    people_page = authed.get("/people/").get_data(as_text=True)
    assert "Tanvir" in people_page and "৳700.00" in people_page

    bad = api_post(authed, tok, {"uuid": "22222222-2222-4333-8444-555555555555", "type": "repayment_received", "dest_account_id": str(pocket),
                                 "person_id": str(conn.execute("SELECT id FROM people").fetchone()[0]), "amount": "900"})
    assert bad.status_code == 422 and "owes you" in bad.get_json()["error"] and bad.get_json()["field"] == "amount"

    tx_id = conn.execute("SELECT id FROM transactions WHERE type='lent'").fetchone()[0]
    assert authed.get(f"/transactions/{tx_id}").status_code == 200
    v = authed.post(f"/transactions/{tx_id}/delete", data={"csrf_token": tok})
    assert v.status_code == 302
    assert authed.get(f"/transactions/{tx_id}").status_code == 404                # gone from the app
    from pocketledger.services import ledger
    assert ledger.verify_integrity(conn) == []


def test_api_edit_and_validation_shape(authed, conn, ids):
    tok = authed.token()
    r = api_post(authed, tok, {"uuid": UUID, "type": "expense", "source_account_id": str(ids["pocket"]), "amount": "100"})
    tx = r.get_json()["id"]
    r = api_post(authed, tok, {"type": "expense", "source_account_id": str(ids["pocket"]), "amount": "150", "reason": "typo"}, url=f"/api/transactions/{tx}", method="put")
    assert r.status_code == 200 and r.get_json()["kind"] == "replaced"
    r = api_post(authed, tok, {"type": "expense", "source_account_id": str(ids["pocket"]), "amount": "-3"}, url=f"/api/transactions/{tx + 1}", method="put")
    assert r.status_code == 422 and "field" in r.get_json()
    assert authed.get("/api/transactions/" + UUID).get_json()["saved"] is True
    assert authed.get("/api/transactions/ffffffff-ffff-4fff-8fff-ffffffffffff").status_code == 404


def test_csv_export_is_safe_and_exact(authed, conn, ids):
    tok = authed.token()
    api_post(authed, tok, {"uuid": UUID, "type": "expense", "source_account_id": str(ids["pocket"]), "amount": "1,234.50",
                           "description": "=HYPERLINK(\"http://evil\")"})
    body = authed.get("/transactions/export.csv").get_data(as_text=True)
    assert "1234.50" in body and "'=HYPERLINK" in body and ",=HYPERLINK" not in body


def test_filters_search_and_sorting(authed, conn, ids):
    tok = authed.token()
    for i, (amt, desc) in enumerate([("10", "Tea"), ("300", "Rickshaw"), ("20", "Tea again")]):
        api_post(authed, tok, {"uuid": f"00000000-0000-4000-8000-00000000000{i}", "type": "expense", "source_account_id": str(ids["pocket"]), "amount": amt, "description": desc})
    html = authed.get("/transactions/", query_string={"q": "tea"}).get_data(as_text=True)
    assert "Tea again" in html and "Rickshaw" not in html
    asc = authed.get("/transactions/", query_string={"sort": "amount", "dir": "asc"}).get_data(as_text=True)
    assert asc.index("Tea again") < asc.index("Rickshaw")
    assert "Rickshaw" in authed.get("/transactions/", query_string={"q": "300"}).get_data(as_text=True)   # search by amount
    assert "Rickshaw" not in authed.get("/transactions/", query_string={"type": "income"}).get_data(as_text=True)


def test_analytics_figures_come_from_transactions(authed, conn, ids):
    from pocketledger.services import analytics, ledger
    tok = authed.token()
    api_post(authed, tok, {"uuid": UUID, "type": "income", "dest_account_id": str(ids["bank"]), "amount": "5000"})
    api_post(authed, tok, {"uuid": "33333333-3333-4333-8333-333333333333", "type": "expense", "source_account_id": str(ids["pocket"]), "amount": "1250"})
    data = analytics.build(conn, "this_month")
    assert data["range_totals"]["income"] == 500000 and data["range_totals"]["expense"] == 125000
    assert data["balance_sheet"]["rows"][-1]["net_worth"] == ledger.totals(conn, "BDT")["net_worth_minor"]
    assert authed.get("/analytics/?range=custom&start=garbage&end=nonsense").status_code == 200


def test_restore_flow_requires_typed_confirmation(authed, conn, ids, tmp_path):
    import io
    tok = authed.token()
    api_post(authed, tok, {"uuid": UUID, "type": "expense", "source_account_id": str(ids["pocket"]), "amount": "10"})
    db_bytes = authed.get("/settings/backup/download.db").data
    api_post(authed, tok, {"uuid": "44444444-4444-4444-8444-444444444444", "type": "expense", "source_account_id": str(ids["pocket"]), "amount": "20"})
    r = authed.post("/settings/restore", data={"backup_file": (io.BytesIO(db_bytes), "b.db"), "csrf_token": tok}, content_type="multipart/form-data")
    assert r.status_code == 200 and b"Review before restoring" in r.data
    refused = authed.post("/settings/restore/confirm", data={"confirm_text": "yes", "csrf_token": tok})
    assert refused.status_code == 302
    c = connect(authed.application.config["DATABASE"])
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2          # nothing changed
    done = authed.post("/settings/restore/confirm", data={"confirm_text": "RESTORE", "csrf_token": tok})
    assert done.status_code == 302
    c.close()
    c = connect(authed.application.config["DATABASE"])        # a fresh handle: the file was swapped
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1          # rolled back to the backup
    c.close()
    assert authed.get("/").status_code == 200                                         # still signed in


def test_pwa_assets(app):
    client = app.test_client()
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.mimetype == "application/manifest+json"
    j = m.get_json()
    assert j["display"] == "standalone" and {i["sizes"] for i in j["icons"]} >= {"192x192", "512x512"}
    assert any(i.get("purpose") == "maskable" for i in j["icons"])
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["Service-Worker-Allowed"] == "/"
    assert client.get("/offline").status_code == 200
    for icon in j["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_production_config_requires_a_real_secret(monkeypatch, tmp_path):
    from pocketledger.config import load_config
    monkeypatch.setenv("INSTANCE_PATH", str(tmp_path))
    monkeypatch.setenv("POCKETLEDGER_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        load_config()
    monkeypatch.setenv("SECRET_KEY", "short")
    with pytest.raises(RuntimeError, match="too short"):
        load_config()
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    cfg = load_config()
    assert cfg["SESSION_COOKIE_SECURE"] is True and cfg["HSTS"] is True and cfg["ALLOW_WEB_SETUP"] is False


def test_minimum_password_length_is_configurable(tmp_path):
    from pocketledger.views.auth import validate_new_password
    from pocketledger import create_app
    base = {"DATABASE": str(tmp_path / "a.db"), "BACKUP_DIR": str(tmp_path / "b"), "TESTING": True,
            "SECRET_KEY": "x" * 40, "SKIP_ENV": True, "MIN_PASSWORD_LENGTH": 6}
    app = create_app(base)
    with app.app_context():
        assert validate_new_password("abcde", "abcde") == "Use at least 6 characters."
        assert validate_new_password("abcdef", "abcdef") is None      # 6 characters is accepted
    strict = create_app({**base, "DATABASE": str(tmp_path / "c.db"), "MIN_PASSWORD_LENGTH": 12})
    with strict.app_context():
        assert validate_new_password("abcdefghijk", "abcdefghijk") == "Use at least 12 characters."


def test_minimum_zero_allows_blank_password_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_PATH", str(tmp_path / "inst"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "z.db"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    monkeypatch.setenv("POCKETLEDGER_ENV", "development")
    monkeypatch.setenv("MIN_PASSWORD_LENGTH", "0")
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    from pocketledger import create_app
    app = create_app({"TESTING": True})
    assert app.config["MIN_PASSWORD_LENGTH"] == 0
    c = app.test_client()
    r = c.post("/setup", data={"username": "tester", "password": "", "confirm": "",
                               "csrf_token": csrf_from(c.get("/setup").get_data(as_text=True))})
    assert r.status_code == 302                                   # account created with a blank password
    page = c.get("/login").get_data(as_text=True)
    assert "required" not in page.split('name="password"')[1].split(">")[0]   # browser doesn't force a value
    r = c.post("/login", data={"username": "tester", "password": "", "csrf_token": csrf_from(page)})
    assert r.status_code == 302 and c.get("/").status_code == 200
    other = app.test_client()                                     # a wrong password is still rejected
    bad = other.post("/login", data={"username": "tester", "password": "nope",
                                     "csrf_token": csrf_from(other.get("/login").get_data(as_text=True))})
    assert b"Wrong username or password" in bad.data


def test_no_login_mode_opens_straight_to_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_PATH", str(tmp_path / "inst"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "n.db"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    monkeypatch.setenv("POCKETLEDGER_ENV", "development")
    monkeypatch.delenv("REQUIRE_LOGIN", raising=False)
    from pocketledger import create_app
    app = create_app({"TESTING": True})
    assert app.config["REQUIRE_LOGIN"] is False               # default in development
    c = app.test_client()
    home = c.get("/")
    assert home.status_code == 200                            # no sign-in, no setup, no redirect
    html = home.get_data(as_text=True)
    assert "Sign out" not in html and "/logout" not in html
    assert c.get("/login").status_code == 302 and c.get("/login").headers["Location"].endswith("/")
    assert c.get("/setup").status_code == 302
    assert c.get("/settings/").status_code == 200
    assert "Change password" not in c.get("/settings/").get_data(as_text=True)
    for path in ("/accounts/", "/people/", "/transactions/", "/analytics/", "/reconcile/", "/settings/audit"):
        assert c.get(path).status_code == 200, path
    # real work still goes through, CSRF still enforced, and the audit trail gets a user
    tok = csrf_from(html)
    assert c.post("/accounts/new", data={"name": "Cash", "category_id": "1", "opening_balance": "100", "currency": "BDT"}).status_code == 400
    r = c.post("/accounts/new", data={"name": "Cash", "category_id": "1", "opening_balance": "100", "currency": "BDT", "csrf_token": tok})
    assert r.status_code == 302
    import sqlite3
    row = sqlite3.connect(app.config["DATABASE"]).execute("SELECT user_id FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row and row[0] is not None


def test_production_still_requires_login_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_PATH", str(tmp_path / "inst"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    monkeypatch.setenv("POCKETLEDGER_ENV", "production")
    monkeypatch.delenv("REQUIRE_LOGIN", raising=False)
    from pocketledger import create_app
    app = create_app({"TESTING": True})
    assert app.config["REQUIRE_LOGIN"] is True
    assert app.test_client().get("/").status_code == 302
