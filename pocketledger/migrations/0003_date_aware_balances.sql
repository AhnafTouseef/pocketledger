-- Date-aware account balances.
--
-- An account's opening balance is a snapshot "as of" its opening date. Anything dated BEFORE
-- that moment is history the snapshot already reflects, so it must not move the balance again.
-- (Example: you set up Wallet today with the ৳3,000 it holds now, then record a loan you gave
-- last month. The ৳3,000 is already after that loan, so Wallet stays ৳3,000; the person simply
-- owes you, and when they repay, Wallet goes up.)
--
-- opening_at is that moment in UTC (start of the opening date in the configured timezone).
-- It is filled in by the application (new accounts) and by the migration runner (existing
-- accounts); until then the view falls back to the plain opening date.

ALTER TABLE accounts ADD COLUMN opening_at TEXT;

DROP VIEW v_account_balances;
CREATE VIEW v_account_balances AS
SELECT a.id AS account_id,
       a.currency,
       a.opening_balance_minor + COALESCE((
           SELECT SUM(pe.debit_minor - pe.credit_minor)
           FROM v_posted_entries pe
           WHERE pe.target_type = 'account'
             AND pe.account_id = a.id
             AND pe.occurred_at >= COALESCE(a.opening_at, a.opened_on)), 0) AS balance_minor
FROM accounts a;

-- Changing the opening moment later would silently rewrite every balance.
CREATE TRIGGER trg_accounts_opening_at_locked BEFORE UPDATE OF opening_at ON accounts
WHEN OLD.opening_at IS NOT NULL AND NEW.opening_at IS NOT OLD.opening_at
BEGIN SELECT RAISE(ABORT, 'The opening date of an account cannot be changed'); END;
