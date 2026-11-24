# PocketLedger

A personal finance ledger for one person: cash, bank and mobile-wallet accounts, money lent and borrowed, reconciliation of physical cash, and analytics. Built with Flask + SQLite, installable as a PWA, runs on Windows without Docker.

Priorities, in order: **financial correctness → data safety → convenience → looks.**

- Money is stored as **integer minor units** (paisa). No floats anywhere.
- Every transaction is posted as **balanced double-entry lines**. Balances are never stored — they are always derived from the ledger.
- Ledger rows, audit logs and reconciliations are **immutable** (enforced by SQLite triggers, not just application code). Corrections are made by voiding and re-posting, with an audit trail.
- BDT first (`৳`, Indian digit grouping). The schema has a `currencies` table, so adding currencies is data, not a rewrite (see Limitations).

---

## Quick start

### Windows (PowerShell)

```powershell
cd pocketledger
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env

flask --app wsgi init-db
flask --app wsgi create-user      # prompts for username + password (default minimum 6 characters, change with `MIN_PASSWORD_LENGTH`)
python run.py                     # http://127.0.0.1:5000
```

If PowerShell blocks activation: `Set-ExecutionPolicy -Scope Process RemoteSigned`, or use `cmd` and `.venv\Scripts\activate.bat`.

### macOS / Linux

```bash
cd pocketledger
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
flask --app wsgi init-db
flask --app wsgi create-user
python run.py
```

In development you can skip `create-user`: opening the site with no users shows a first-run setup page.

`run.py` options: `--host 0.0.0.0` (reachable from your LAN), `--port 8080`, `--debug`. It serves with **waitress** (pure Python, works on Windows).

### Try it with demo data

Demo data lives in its own database file and is never mixed with real data. The app shows a "demo" banner when it is open.

```powershell
python scripts\seed_demo.py                    # writes instance\demo.db
$env:DATABASE_PATH = "instance/demo.db"; python run.py
# sign in as: demo / demo-password-123
```

```bash
python scripts/seed_demo.py
DATABASE_PATH=instance/demo.db python run.py
```

`seed_demo.py` refuses to write into your real database or any database that contains non-demo data. Use `--reset` to recreate an existing demo file.

---

## What is in it

| Area | Features |
|---|---|
| **Dashboard** | Net worth (assets − liabilities), liquid money vs receivables, owed to you / you owe, accounts needing verification, account overview, quick actions, recent activity, alerts (overdue, due soon, unreconciled, stale verification) |
| **Accounts** | Any number of accounts; custom account categories; opening balance (locked once history exists); archive (never delete); account detail with full history |
| **Transfers** | One transaction, two ledger lines (debit destination, credit source). Does not change net worth. |
| **People** | Receivables and payables derived from transactions; partial repayments; due dates; per-loan remaining/overdue via FIFO allocation; repayment cannot exceed what is outstanding |
| **Transactions** | Income, expense, transfer, lent, borrowed, repayment received, repayment made, adjustment. Search, filter, sort, pagination, CSV export. Void and edit (edit = void + linked replacement) with audit log |
| **Reconciliation** | Record what you counted vs what the ledger expects. Never auto-classified as theft or loss. History is preserved; a difference can stay *unresolved* indefinitely. Resolve by adding the missing transaction (back-dated, linked) or an explicit, labelled adjustment |
| **Analytics** | Date presets/custom range; income vs expense and net-worth trend; expense by category; account distribution; outstanding receivables/payables; reconciliation history |
| **Backup / restore** | SQLite online backup, JSON export/import, CSV export, validation before restore, typed `RESTORE` confirmation, automatic safety backup before restore and before migrations |
| **PWA** | Installable, offline page, offline *entry* of transactions queued on-device and synced when online |
| **Security** | Password hashing (scrypt), CSRF on every state change, default-deny route guard, parameterised SQL, CSP, login throttling, session invalidation on password change |

---

## Accounting model

All amounts are positive integers; direction comes from which side (debit/credit) a line is posted to.

| Type | Debit | Credit |
|---|---|---|
| Income | account | income category |
| Expense | expense category | account |
| Transfer | destination account | source account |
| Lent | receivable (person) | account |
| Borrowed | account | payable (person) |
| Repayment received | account | receivable (person) |
| Repayment made | payable (person) | account |
| Adjustment | account ↔ equity (either direction) | |

- **Opening date (date-aware balances).** An account's opening balance is a snapshot at the start of its opening date. A transaction dated *before* that is history the snapshot already includes, so it is recorded (and the person's receivable/payable still appears) but does **not** change that account's balance. Example: you set up Wallet today with the ৳3,000 it holds now, then record a ৳1,000 loan from last month. Wallet stays ৳3,000, Rahim owes you ৳1,000 (net worth ৳4,000), and when he repays, Wallet goes up. The entry form warns you when you pick such a date, and these rows carry a "before opening" badge. If you want older transactions to count, create the account with an earlier opening date and the balance it held then.
- **Net worth** = accounts + receivables − payables.
- **Integrity identity** (checked by `flask --app wsgi check` and in Settings → Integrity):
  `net worth = Σ opening balances + income − expenses + adjustments − (account legs dated before their account's opening date)`.
  Transfers, lending, borrowing and repayments cancel out of it by construction.
- Every transaction's debits equal its credits. Violations are rejected before commit.

### Why these design choices

| Decision | Reason | Trade-off |
|---|---|---|
| Derive balances from entries (SQL views) instead of storing them | A stored balance can silently drift from history; a derived one cannot | Slightly more work per read; fine at personal scale |
| Immutable ledger + void/replace instead of in-place edits | Full history, auditability, no "where did this number go" | Editing an amount creates a new transaction id (linked via `replaces_id`/`replaced_by_id`) |
| Triggers enforce immutability | Protects against bugs and manual SQL, not just the web UI | Maintenance/repair needs a deliberate migration |
| Idempotency key (client UUID) per transaction | Offline queue and double-taps can't create duplicates | Needs a client that generates UUIDs (the bundled JS does) |
| SQLite, `BEGIN IMMEDIATE`, savepoints | Zero-setup, atomic, runs on Windows | One writer at a time, needs a persistent disk on hosts |
| Forward-only SQL migrations | Simple, reviewable, no ORM | No automatic downgrade; a backup is taken before each upgrade |
| Vanilla JS, vendored Chart.js and Lucide | No build step, works offline, CSP `script-src 'self'` | Less ergonomic than a framework |
| Service worker caches only the static shell | Financial pages are never served stale from cache | Offline = entry only, not browsing |

---

## Project layout

```
pocketledger/
├── run.py                 # waitress launcher
├── wsgi.py                # WSGI entry point (app = create_app())
├── Procfile               # for Heroku-style hosts
├── requirements.txt       # runtime deps
├── requirements-dev.txt   # + pytest, Pillow
├── .env.example
├── pocketledger/
│   ├── __init__.py        # app factory, template filters, error handlers
│   ├── config.py          # environment configuration
│   ├── db.py, migrate.py  # connections, transaction(), migration runner
│   ├── money.py           # parsing / formatting integer minor units
│   ├── timeutil.py        # UTC storage, local display
│   ├── security.py        # CSRF, auth guard, headers, login throttle
│   ├── cli.py             # flask commands
│   ├── migrations/        # 0001_initial.sql, 0002_reference_data.sql (forward-only)
│   ├── services/          # ledger, accounts, people, transactions, reconciliation,
│   │                      #   analytics, dashboard, backup, audit, settings
│   ├── views/             # Flask blueprints (one per area) + JSON API + PWA routes
│   ├── templates/
│   └── static/            # css, js, vendor (Chart.js, Lucide), icons, sw.js
├── scripts/seed_demo.py
├── tools/make_icons.py    # regenerates PWA icons (needs Pillow)
└── tests/
```

Business rules live in `services/`; views only parse requests and render. Nothing outside `services/ledger.py` writes ledger entries.

---

## Configuration

Set in `.env` or the environment.

| Variable | Default | Meaning |
|---|---|---|
| `POCKETLEDGER_ENV` | `development` | `production` turns on strict checks (see below) |
| `SECRET_KEY` | generated in dev | **Required in production**, 32+ chars. `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_PATH` | `instance/pocketledger.db` | SQLite file |
| `BACKUP_DIR` | `instance/backups` | Where backups are written |
| `INSTANCE_PATH` | `instance` | Instance folder |
| `SESSION_COOKIE_SECURE` | true in production | Send cookies only over HTTPS |
| `FORCE_HTTPS` | false | Redirect http → https (301) |
| `TRUSTED_PROXY_COUNT` | 0 | Set to `1` behind one reverse proxy so scheme/host/client IP are read correctly |
| `ALLOW_WEB_SETUP` | on in dev, off in prod | Browser-based first-user creation |
| `SETUP_TOKEN` | empty | If set, the setup page requires this token |
| `REQUIRE_LOGIN` | off in dev, on in prod | `false` = no login screen, no account: the app opens straight to the dashboard. `true` = sign-in required. **Never expose a no-login instance to a network you don't trust** |
| `MIN_PASSWORD_LENGTH` | 6 | Minimum password length; `0` disables all password rules (blank password allowed). Raise it (10+) if the app is reachable from the internet |
| `SESSION_HOURS` / `IDLE_TIMEOUT_MINUTES` | 12 / 120 | Session limits |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES` | 5 / 15 | Login throttle |
| `MAX_UPLOAD_MB` | 50 | Restore/import upload limit |

In production the app refuses to start without a strong `SECRET_KEY`, sends `Strict-Transport-Security`, marks cookies `Secure`, and returns 403 on `/setup` unless you enabled it.

Timezone and display preferences (default `Asia/Dhaka`) are in Settings → Preferences. Timestamps are stored in UTC.

---

## Backup and restore

- **Settings → Backup**: create a database backup (SQLite online backup — safe while the app runs), download JSON or CSV exports.
- **CLI**: `flask --app wsgi backup`
- **Restore**: upload a `.db` backup or JSON export → the file is validated (SQLite header, required tables, schema version, ledger integrity) → a review page shows what it contains → you must type `RESTORE` → a safety backup of the current data is taken first → the file replaces the database. Your current login is carried over so you are not locked out.
- Backups are plain SQLite files containing your financial data. Store them somewhere you trust.

Back up before you upgrade, and keep at least one copy off the machine that runs the app.

---

## Offline / PWA

- Installing the app and the service worker require **HTTPS** (or `localhost`). Over plain HTTP on a LAN address the site works normally but the browser will not offer install or offline support.
- Offline you can **enter transactions only**. They are stored in IndexedDB with a client UUID, shown as *not saved yet*, and sent when you are online and an app page is open. Duplicates are impossible (idempotency key).
- Anything the server rejects (e.g. a repayment larger than the outstanding balance) is **not altered** — it stays in the queue flagged "needs attention" for you to fix or discard.
- If your session has expired, the queue waits and asks you to sign in.
- The on-device cache holds account and people **names** (not balances) so the offline form can work.
- Queued entries use the device's clock converted to the configured timezone; if your phone and the configured timezone differ, check the time on queued entries.

---

## Deployment

SQLite needs a **persistent disk**. A host with an ephemeral filesystem will lose your data on every deploy or restart.

Always set in production:

```
POCKETLEDGER_ENV=production
SECRET_KEY=<64 hex chars>
DATABASE_PATH=/path/on/persistent/disk/pocketledger.db
BACKUP_DIR=/path/on/persistent/disk/backups
TRUSTED_PROXY_COUNT=1        # when behind a proxy / platform router
FORCE_HTTPS=true             # unless the proxy already redirects
```

Create the first user from a shell (`flask --app wsgi create-user`), or set `ALLOW_WEB_SETUP=true` and `SETUP_TOKEN=<secret>` for the first visit and remove them afterwards.

### Option A — VPS (Ubuntu) + reverse proxy

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
export POCKETLEDGER_ENV=production SECRET_KEY=... DATABASE_PATH=/var/lib/pocketledger/pocketledger.db BACKUP_DIR=/var/lib/pocketledger/backups
flask --app wsgi init-db && flask --app wsgi create-user
waitress-serve --listen=127.0.0.1:8000 wsgi:app
```

Put nginx or Caddy in front for HTTPS (Caddy: `reverse_proxy 127.0.0.1:8000` gets you automatic certificates) and run waitress under systemd. Use **one** worker process.

### Option B — Platform hosts (Render, Fly.io, Railway, PythonAnywhere, …)

The `Procfile` runs `waitress-serve --host=0.0.0.0 --port=$PORT wsgi:app`. Attach a persistent volume/disk, point `DATABASE_PATH` and `BACKUP_DIR` at it, and set the variables above. Check the current plan limits of your host: free tiers commonly lack persistent disks or sleep when idle, which is a poor fit for a ledger.

### Option C — Always-on home PC

Run `python run.py --host 0.0.0.0` and reach it over a VPN such as Tailscale, which also gives you HTTPS-capable hostnames for PWA install. Do not expose it directly to the internet without HTTPS.

### Single-process note

The login throttle is in memory and SQLite has one writer. Run one process (waitress threads are fine).

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest                          # 107 tests
flask --app wsgi check          # integrity check of your actual database
```

The suite covers money parsing/formatting, every transaction type's ledger lines, a randomized model test (random operations checked against an independent calculation of every balance), void/edit, FIFO loan allocation, immutability triggers, idempotency, reconciliation lifecycle, backup/restore validation, auth/CSRF/route-guard, and every route.

### Verify the financial calculations by hand

On a fresh (or demo) database:

1. Create accounts **Pocket** ৳5,000 and **bKash** ৳1,000 → net worth ৳6,000.
2. Transfer ৳2,000 Pocket → bKash → Pocket ৳3,000, bKash ৳3,000, **net worth still ৳6,000**.
3. Lend ৳1,000 to a person from Pocket → Pocket ৳2,000, receivable ৳1,000, net worth ৳6,000.
4. Receive ৳400 back into bKash → receivable ৳600, bKash ৳3,400, net worth ৳6,000.
5. Expense ৳500 from Pocket → net worth ৳5,500.
6. Verify Pocket: count ৳1,500 vs expected ৳1,500 → matched. Count ৳1,200 → ৳300 short, status *open*; it stays on the dashboard until you resolve or mark it unresolved. Net worth does not change unless you post a missing transaction or an explicit adjustment.
7. Settings → Integrity (or `flask --app wsgi check`) reports **Ledger OK**.

---

## Known limitations

- **Single user, single base currency in the UI.** The schema supports multiple currencies; cross-currency transfers are rejected and there is no FX. Adding a currency means inserting a `currencies` row and surfacing it in forms.
- **Notifications are in-app only** (dashboard alerts). No push, email or SMS.
- **Offline is entry-only.** The queue syncs while an app page is open (no Background Sync API). Queued entries show device-local time.
- **Login throttle is per process and per IP, in memory** — it resets on restart.
- **SQLite = one writer**, needs a persistent disk, and is meant for one process.
- **An account's opening date can't be changed after creation** (it would rewrite every balance). To change it, create a new account with the right date and balance.
- **Editing money fields creates a new transaction** (old one voided and linked), so ids change.
- **Not implemented:** recurring transactions, budgets, attachments/receipts, split transactions, CSV import, multi-user sharing.
- Front-end behaviour (offline queue, install prompt, charts) was verified manually in headless Chromium; it is not part of the automated pytest suite.

---

## License

Your project; add a license if you publish it. Vendored libraries (Chart.js, Lucide) keep their own licenses in `pocketledger/static/vendor/`.
