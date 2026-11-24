-- PocketLedger schema, migration 0001
--
-- Accounting model (double entry, integer minor units):
--   * Every transaction has >= 2 rows in transaction_entries; sum(debit) == sum(credit).
--   * Entry targets: account (asset), receivable (asset), payable (liability),
--     income, expense, equity (balance adjustments).
--   * Balances are NEVER stored. They are derived from entries by the v_* views.
--   * Ledger rows are immutable (enforced by triggers). Corrections are made by voiding
--     a transaction and posting a replacement; every correction is written to audit_logs.

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE application_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE currencies (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    minor_unit INTEGER NOT NULL DEFAULT 2 CHECK (minor_unit BETWEEN 0 AND 4),
    grouping   TEXT NOT NULL DEFAULT 'western' CHECK (grouping IN ('western', 'indian')),
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1))
);

CREATE TABLE account_categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL COLLATE NOCASE,
    kind        TEXT NOT NULL CHECK (kind IN ('physical', 'bank', 'wallet', 'other')),
    icon        TEXT NOT NULL DEFAULT 'wallet',
    is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ux_account_categories_name ON account_categories (name);

CREATE TABLE accounts (
    id                          INTEGER PRIMARY KEY,
    name                        TEXT NOT NULL COLLATE NOCASE,
    category_id                 INTEGER NOT NULL REFERENCES account_categories (id),
    currency                    TEXT NOT NULL REFERENCES currencies (code),
    opening_balance_minor       INTEGER NOT NULL DEFAULT 0,
    opened_on                   TEXT NOT NULL,            -- local calendar date, YYYY-MM-DD
    description                 TEXT,
    is_archived                 INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    archived_at                 TEXT,
    last_verified_balance_minor INTEGER,
    last_verified_at            TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_accounts_active_name ON accounts (name) WHERE is_archived = 0;

CREATE TABLE people (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL COLLATE NOCASE,
    contact     TEXT,
    notes       TEXT,
    is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_people_active_name ON people (name) WHERE is_archived = 0;

CREATE TABLE transaction_categories (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL COLLATE NOCASE,
    kind        TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
    is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (name, kind)
);

-- A reconciliation is one physical count of one account. Rows are never rewritten;
-- the only mutable column is leave_unresolved.
CREATE TABLE reconciliations (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts (id),
    verified_at      TEXT NOT NULL,
    expected_minor   INTEGER NOT NULL,
    counted_minor    INTEGER NOT NULL CHECK (counted_minor >= 0),
    difference_minor INTEGER NOT NULL,
    note             TEXT,
    leave_unresolved INTEGER NOT NULL DEFAULT 0 CHECK (leave_unresolved IN (0, 1)),
    created_by       INTEGER REFERENCES users (id),
    created_at       TEXT NOT NULL,
    CHECK (difference_minor = counted_minor - expected_minor)
);
CREATE INDEX ix_recon_account ON reconciliations (account_id, verified_at);

CREATE TABLE transactions (
    id                INTEGER PRIMARY KEY,
    uuid              TEXT NOT NULL UNIQUE,                -- also the idempotency key
    type              TEXT NOT NULL CHECK (type IN (
                          'income', 'expense', 'transfer', 'lent', 'borrowed',
                          'repayment_received', 'repayment_made', 'adjustment')),
    amount_minor      INTEGER NOT NULL CHECK (amount_minor > 0),
    currency          TEXT NOT NULL REFERENCES currencies (code),
    occurred_at       TEXT NOT NULL,                       -- UTC, YYYY-MM-DDTHH:MM:SSZ
    source_account_id INTEGER REFERENCES accounts (id),
    dest_account_id   INTEGER REFERENCES accounts (id),
    person_id         INTEGER REFERENCES people (id),
    category_id       INTEGER REFERENCES transaction_categories (id),
    description       TEXT NOT NULL,
    notes             TEXT,
    due_date          TEXT,                                -- lent / borrowed only
    reconciliation_id INTEGER REFERENCES reconciliations (id),
    status            TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'voided')),
    voided_at         TEXT,
    void_reason       TEXT,
    replaces_id       INTEGER REFERENCES transactions (id),
    replaced_by_id    INTEGER REFERENCES transactions (id),
    created_by        INTEGER REFERENCES users (id),
    created_at        TEXT NOT NULL,
    CHECK (source_account_id IS NULL OR dest_account_id IS NULL OR source_account_id <> dest_account_id),
    CHECK (
        (type = 'income'             AND dest_account_id IS NOT NULL AND source_account_id IS NULL     AND person_id IS NULL) OR
        (type = 'expense'            AND source_account_id IS NOT NULL AND dest_account_id IS NULL     AND person_id IS NULL) OR
        (type = 'transfer'           AND source_account_id IS NOT NULL AND dest_account_id IS NOT NULL AND person_id IS NULL) OR
        (type = 'lent'               AND source_account_id IS NOT NULL AND dest_account_id IS NULL     AND person_id IS NOT NULL) OR
        (type = 'borrowed'           AND dest_account_id IS NOT NULL   AND source_account_id IS NULL   AND person_id IS NOT NULL) OR
        (type = 'repayment_received' AND dest_account_id IS NOT NULL   AND source_account_id IS NULL   AND person_id IS NOT NULL) OR
        (type = 'repayment_made'     AND source_account_id IS NOT NULL AND dest_account_id IS NULL     AND person_id IS NOT NULL) OR
        (type = 'adjustment'         AND ((source_account_id IS NULL) <> (dest_account_id IS NULL)) AND person_id IS NULL)
    ),
    CHECK ((status = 'voided') = (voided_at IS NOT NULL))
);
CREATE INDEX ix_tx_occurred ON transactions (occurred_at);
CREATE INDEX ix_tx_type ON transactions (type, status);
CREATE INDEX ix_tx_person ON transactions (person_id);
CREATE INDEX ix_tx_source ON transactions (source_account_id);
CREATE INDEX ix_tx_dest ON transactions (dest_account_id);
CREATE INDEX ix_tx_recon ON transactions (reconciliation_id);

CREATE TABLE transaction_entries (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions (id),
    line_no        INTEGER NOT NULL,
    target_type    TEXT NOT NULL CHECK (target_type IN (
                       'account', 'receivable', 'payable', 'income', 'expense', 'equity')),
    account_id     INTEGER REFERENCES accounts (id),
    person_id      INTEGER REFERENCES people (id),
    debit_minor    INTEGER NOT NULL DEFAULT 0 CHECK (debit_minor >= 0),
    credit_minor   INTEGER NOT NULL DEFAULT 0 CHECK (credit_minor >= 0),
    UNIQUE (transaction_id, line_no),
    CHECK ((debit_minor > 0 AND credit_minor = 0) OR (debit_minor = 0 AND credit_minor > 0)),
    CHECK (
        (target_type = 'account'                    AND account_id IS NOT NULL AND person_id IS NULL) OR
        (target_type IN ('receivable', 'payable')   AND person_id IS NOT NULL  AND account_id IS NULL) OR
        (target_type IN ('income', 'expense', 'equity') AND account_id IS NULL AND person_id IS NULL)
    )
);
CREATE INDEX ix_entries_tx ON transaction_entries (transaction_id);
CREATE INDEX ix_entries_account ON transaction_entries (account_id);
CREATE INDEX ix_entries_person ON transaction_entries (person_id);

CREATE TABLE reconciliation_entries (
    id                INTEGER PRIMARY KEY,
    reconciliation_id INTEGER NOT NULL REFERENCES reconciliations (id),
    entry_type        TEXT NOT NULL CHECK (entry_type IN (
                          'verified', 'note', 'transaction_linked', 'adjustment_posted',
                          'left_unresolved', 'reopened')),
    transaction_id    INTEGER REFERENCES transactions (id),
    amount_minor      INTEGER,
    note              TEXT,
    created_by        INTEGER REFERENCES users (id),
    created_at        TEXT NOT NULL
);
CREATE INDEX ix_recon_entries ON reconciliation_entries (reconciliation_id);

CREATE TABLE audit_logs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users (id),
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER,
    before_json TEXT,
    after_json  TEXT,
    reason      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX ix_audit_entity ON audit_logs (entity_type, entity_id);
CREATE INDEX ix_audit_created ON audit_logs (created_at);

-- ---------------------------------------------------------------------------------
-- Immutability guards. Financial history cannot be rewritten, not even by a bug.
-- ---------------------------------------------------------------------------------

CREATE TRIGGER trg_tx_no_delete BEFORE DELETE ON transactions
BEGIN SELECT RAISE(ABORT, 'Transactions cannot be deleted; void them instead'); END;

CREATE TRIGGER trg_tx_immutable BEFORE UPDATE ON transactions
WHEN NEW.id <> OLD.id
  OR NEW.uuid <> OLD.uuid
  OR NEW.type <> OLD.type
  OR NEW.amount_minor <> OLD.amount_minor
  OR NEW.currency <> OLD.currency
  OR NEW.occurred_at <> OLD.occurred_at
  OR NEW.source_account_id IS NOT OLD.source_account_id
  OR NEW.dest_account_id IS NOT OLD.dest_account_id
  OR NEW.person_id IS NOT OLD.person_id
  OR NEW.reconciliation_id IS NOT OLD.reconciliation_id
  OR NEW.replaces_id IS NOT OLD.replaces_id
  OR NEW.created_at <> OLD.created_at
  OR (OLD.replaced_by_id IS NOT NULL AND NEW.replaced_by_id IS NOT OLD.replaced_by_id)
  OR (OLD.status = 'voided' AND (NEW.status <> 'voided'
                                 OR NEW.voided_at IS NOT OLD.voided_at
                                 OR NEW.void_reason IS NOT OLD.void_reason
                                 OR NEW.description <> OLD.description
                                 OR NEW.category_id IS NOT OLD.category_id))
BEGIN SELECT RAISE(ABORT, 'Ledger transactions are immutable; void and re-post instead'); END;

CREATE TRIGGER trg_entries_no_update BEFORE UPDATE ON transaction_entries
BEGIN SELECT RAISE(ABORT, 'Ledger entries are immutable'); END;
CREATE TRIGGER trg_entries_no_delete BEFORE DELETE ON transaction_entries
BEGIN SELECT RAISE(ABORT, 'Ledger entries cannot be deleted'); END;

CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'Audit log is append-only'); END;
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_logs
BEGIN SELECT RAISE(ABORT, 'Audit log is append-only'); END;

CREATE TRIGGER trg_recon_no_delete BEFORE DELETE ON reconciliations
BEGIN SELECT RAISE(ABORT, 'Reconciliation history cannot be deleted'); END;
CREATE TRIGGER trg_recon_immutable BEFORE UPDATE ON reconciliations
WHEN NEW.id <> OLD.id OR NEW.account_id <> OLD.account_id OR NEW.verified_at <> OLD.verified_at
  OR NEW.expected_minor <> OLD.expected_minor OR NEW.counted_minor <> OLD.counted_minor
  OR NEW.difference_minor <> OLD.difference_minor OR NEW.created_at <> OLD.created_at
  OR NEW.note IS NOT OLD.note
BEGIN SELECT RAISE(ABORT, 'Reconciliation records are immutable'); END;

CREATE TRIGGER trg_recon_entries_no_update BEFORE UPDATE ON reconciliation_entries
BEGIN SELECT RAISE(ABORT, 'Reconciliation history is append-only'); END;
CREATE TRIGGER trg_recon_entries_no_delete BEFORE DELETE ON reconciliation_entries
BEGIN SELECT RAISE(ABORT, 'Reconciliation history is append-only'); END;

-- An opening balance is part of the ledger baseline: once an account has any history,
-- changing it would silently rewrite the past. Use an explicit adjustment instead.
CREATE TRIGGER trg_accounts_opening_locked BEFORE UPDATE OF opening_balance_minor ON accounts
WHEN NEW.opening_balance_minor <> OLD.opening_balance_minor
 AND (EXISTS (SELECT 1 FROM transaction_entries WHERE account_id = OLD.id)
      OR EXISTS (SELECT 1 FROM reconciliations WHERE account_id = OLD.id))
BEGIN SELECT RAISE(ABORT, 'Opening balance is locked once an account has history; post an adjustment'); END;

CREATE TRIGGER trg_accounts_no_delete BEFORE DELETE ON accounts
BEGIN SELECT RAISE(ABORT, 'Accounts cannot be deleted; archive them'); END;
CREATE TRIGGER trg_people_no_delete BEFORE DELETE ON people
BEGIN SELECT RAISE(ABORT, 'People cannot be deleted; archive them'); END;

-- ---------------------------------------------------------------------------------
-- Derived views. Balances come from here and only from here.
-- ---------------------------------------------------------------------------------

CREATE VIEW v_posted_entries AS
SELECT e.id           AS entry_id,
       e.transaction_id,
       e.target_type,
       e.account_id,
       e.person_id,
       e.debit_minor,
       e.credit_minor,
       t.type         AS tx_type,
       t.occurred_at,
       t.currency,
       t.category_id
FROM transaction_entries e
JOIN transactions t ON t.id = e.transaction_id
WHERE t.status = 'posted';

CREATE VIEW v_account_balances AS
SELECT a.id AS account_id,
       a.currency,
       a.opening_balance_minor + COALESCE((
           SELECT SUM(pe.debit_minor - pe.credit_minor)
           FROM v_posted_entries pe
           WHERE pe.target_type = 'account' AND pe.account_id = a.id), 0) AS balance_minor
FROM accounts a;

CREATE VIEW v_person_balances AS
SELECT person_id,
       currency,
       SUM(CASE WHEN target_type = 'receivable' THEN debit_minor  ELSE 0 END) AS lent_minor,
       SUM(CASE WHEN target_type = 'receivable' THEN credit_minor ELSE 0 END) AS received_back_minor,
       SUM(CASE WHEN target_type = 'payable'    THEN credit_minor ELSE 0 END) AS borrowed_minor,
       SUM(CASE WHEN target_type = 'payable'    THEN debit_minor  ELSE 0 END) AS repaid_minor,
       SUM(CASE WHEN target_type = 'receivable' THEN debit_minor - credit_minor ELSE 0 END) AS receivable_minor,
       SUM(CASE WHEN target_type = 'payable'    THEN credit_minor - debit_minor ELSE 0 END) AS payable_minor
FROM v_posted_entries
WHERE target_type IN ('receivable', 'payable')
GROUP BY person_id, currency;
