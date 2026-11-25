import json
import shutil
import sqlite3

import pytest

from pocketledger.db import connect
from pocketledger.services import backup, ledger, reconciliation
from pocketledger.services.errors import ValidationError


def populate(conn, ids):
    ledger.post_transaction(conn, {"type": "transfer", "source_account_id": ids["bank"], "dest_account_id": ids["pocket"], "amount": "500"})
    ledger.post_transaction(conn, {"type": "lent", "source_account_id": ids["pocket"], "person_id": ids["rahim"], "amount": "300", "due_date": "2027-01-01"})
    r = ledger.post_transaction(conn, {"type": "expense", "source_account_id": ids["bank"], "amount": "99"})
    ledger.delete_transaction(conn, r.id)
    reconciliation.verify(conn, ids["pocket"], "2000")


def snapshot(conn):
    return {
        "balances": [tuple(r) for r in conn.execute("SELECT account_id, balance_minor FROM v_account_balances ORDER BY 1")],
        "people": [tuple(r) for r in conn.execute("SELECT person_id, receivable_minor, payable_minor FROM v_person_balances ORDER BY 1")],
        "tx": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "voided": conn.execute("SELECT COUNT(*) FROM transactions WHERE status='voided'").fetchone()[0],
        "recs": conn.execute("SELECT COUNT(*) FROM reconciliations").fetchone()[0],
        "audit": conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0],
    }


def test_db_backup_validates_and_restores_exactly(app, conn, ids, tmp_path):
    populate(conn, ids)
    want = snapshot(conn)
    path = backup.create_db_backup(app.config["DATABASE"], str(tmp_path / "b.db"))
    report = backup.validate_database_file(path)
    assert report["ok"], report
    # change the live data, then restore the backup over it
    ledger.post_transaction(conn, {"type": "income", "dest_account_id": ids["bank"], "amount": "7777"})
    assert snapshot(conn) != want
    conn.close()
    safety = backup.restore_database(path, app.config["DATABASE"], str(tmp_path / "safety"))
    fresh = connect(app.config["DATABASE"])
    try:
        assert snapshot(fresh) == want
        assert ledger.verify_integrity(fresh) == []
    finally:
        fresh.close()
    # the data that was live before the restore is still recoverable
    before_restore = connect(safety)
    try:
        assert before_restore.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == want["tx"] + 1
    finally:
        before_restore.close()


def test_tampered_backup_is_refused(app, conn, ids, tmp_path):
    populate(conn, ids)
    path = backup.create_db_backup(app.config["DATABASE"], str(tmp_path / "bad.db"))
    c = sqlite3.connect(path)
    c.execute("DROP TRIGGER trg_entries_no_update")
    c.execute("UPDATE transaction_entries SET credit_minor = credit_minor + 1 WHERE id = 2")
    c.commit(); c.close()
    report = backup.validate_database_file(path)
    assert not report["ok"] and any("unbalanced" in e or "match" in e for e in report["errors"])
    conn.close()
    with pytest.raises(ValidationError):
        backup.restore_database(path, app.config["DATABASE"], str(tmp_path / "safety"))


def test_non_database_and_foreign_files_refused(tmp_path):
    junk = tmp_path / "junk.db"; junk.write_bytes(b"this is not sqlite at all")
    assert not backup.validate_database_file(str(junk))["ok"]
    other = tmp_path / "other.db"; c = sqlite3.connect(other); c.execute("create table x(a)"); c.commit(); c.close()
    assert "Missing tables" in backup.validate_database_file(str(other))["errors"][0]


def test_json_export_import_roundtrip_preserves_everything(app, conn, ids, tmp_path):
    populate(conn, ids)
    want = snapshot(conn)
    data = json.loads(json.dumps(backup.export_json(conn)))     # through real JSON, like a file
    staged = str(tmp_path / "staged.db")
    report = backup.import_json_to_staging(data, staged, app.config["DATABASE"])
    assert report["ok"], report
    c = connect(staged)
    try:
        assert snapshot(c) == want
        # transaction relationships survive: replacement links, entries, reconciliation links
        assert c.execute("SELECT COUNT(*) FROM transaction_entries").fetchone()[0] == conn.execute("SELECT COUNT(*) FROM transaction_entries").fetchone()[0]
    finally:
        c.close()


def test_json_import_rejects_garbage_and_wrong_format(app, tmp_path):
    for bad in ({}, {"format": "something-else"}, {"format": "pocketledger-export", "format_version": 99, "tables": {}},
                {"format": "pocketledger-export", "format_version": 1, "tables": {"accounts": "nope"}}):
        with pytest.raises(ValidationError):
            backup.import_json_to_staging(bad, str(tmp_path / "s.db"), app.config["DATABASE"])


def test_json_import_with_unbalanced_data_fails_validation(app, conn, ids, tmp_path):
    populate(conn, ids)
    data = json.loads(json.dumps(backup.export_json(conn)))
    data["tables"]["transaction_entries"][0]["debit_minor"] += 5
    report = backup.import_json_to_staging(data, str(tmp_path / "s.db"), app.config["DATABASE"])
    assert not report["ok"]


def test_restore_keeps_the_current_login(app, conn, ids, tmp_path):
    from conftest import make_user
    make_user(conn, "owner", "current password!")
    path = backup.create_db_backup(app.config["DATABASE"], str(tmp_path / "old.db"))
    conn.execute("UPDATE users SET password_hash = 'changed-after-backup'")
    conn.close()
    backup.restore_database(path, app.config["DATABASE"], str(tmp_path / "safety"))
    c = connect(app.config["DATABASE"])
    try:
        assert c.execute("SELECT password_hash FROM users").fetchone()[0] == "changed-after-backup"
    finally:
        c.close()


def test_rotate_keeps_newest(tmp_path):
    import os, time
    for i in range(5):
        p = tmp_path / f"pocketledger-{i}.db"; p.write_bytes(b"x"); os.utime(p, (time.time() + i, time.time() + i))
    backup.rotate(str(tmp_path), keep=2)
    assert sorted(p.name for p in tmp_path.glob("*.db")) == ["pocketledger-3.db", "pocketledger-4.db"]
