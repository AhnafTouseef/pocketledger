import re

import pytest
from werkzeug.security import generate_password_hash

from pocketledger import create_app, security
from pocketledger.db import connect
from pocketledger.services import accounts as accounts_svc
from pocketledger.timeutil import utcnow_iso

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _reset_throttle():
    security.reset_throttle()
    yield
    security.reset_throttle()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_PATH", str(tmp_path / "inst"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    monkeypatch.setenv("POCKETLEDGER_ENV", "development")
    monkeypatch.setenv("REQUIRE_LOGIN", "true")
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    return create_app({"TESTING": True})


@pytest.fixture
def conn(app):
    c = connect(app.config["DATABASE"])
    yield c
    c.close()


@pytest.fixture
def ids(conn):
    """Two accounts and two people: Bank (৳10,000), Pocket (৳2,000), Rahim, Karim."""
    from pocketledger.services import people as people_svc
    return {
        "bank": accounts_svc.create_account(conn, name="Bank", category_id=2, currency="BDT", opening_balance="10,000"),
        "pocket": accounts_svc.create_account(conn, name="Pocket", category_id=1, currency="BDT", opening_balance="2000"),
        "rahim": people_svc.create_person(conn, name="Rahim"),
        "karim": people_svc.create_person(conn, name="Karim"),
    }


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def make_user(conn, username="owner", password=PASSWORD):
    conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                 (username, generate_password_hash(password), utcnow_iso()))


def login(client, username="owner", password=PASSWORD):
    page = client.get("/login")
    token = csrf_from(page.get_data(as_text=True))
    return client.post("/login", data={"username": username, "password": password, "csrf_token": token})


@pytest.fixture
def authed(app, conn):
    """A test client already signed in, plus a helper that returns the current CSRF token."""
    make_user(conn)
    client = app.test_client()
    assert login(client).status_code == 302
    client.token = lambda: csrf_from(client.get("/").get_data(as_text=True))
    return client
