# Ahorklo Daily Sales Desk

A small web app for a shop owner to record sales, manage products and stock, track expenses,
and see net profit — all backed by SQLite locally/with a Render disk, with optional PostgreSQL support.

## What's in it

- **Automatic database selection** — locally the app uses SQLite at `data/shop.db`.
  On Render it uses SQLite at `/var/data/shop.db`, which is on the attached persistent disk.
  If the environment provides `DATABASE_URL`, the same code automatically uses PostgreSQL instead.
  You never select a database in the application.
- **Products** (`/products`, owner) — add/edit/delete products with selling price, cost price,
  stock quantity, and a minimum stock level.
- **Automatic inventory** — selling a product decrements its stock automatically; a low-stock
  banner appears on the dashboard once an item hits its minimum.
- **Sales history** (`/history`) — filter by Today / Yesterday / This week / This month /
  Custom range, with CSV / Excel / PDF export (owner).
- **Expenses** (`/expenses`, owner) — record Electricity, Transport, Rent, Staff payments,
  Supplies, or Other, filterable by date range with the same export options.
- **Receipts** — the dashboard checkout uses a basket, so one sale can ring up several items
  at once. Completing a sale shows a receipt banner linking to a printable, itemized receipt
  (`/receipt/<id>`) — a real "SHOP NAME / item × qty — total / TOTAL" slip you can print or
  save as PDF from the browser.
- **Staff performance** (`/staff`, owner) — sales total and transaction count per staff
  member, for any date range.
- **Reports** (`/reports`, owner) — a Profit report (Revenue − Cost of Goods − Expenses = Net
  Profit, with an expense-by-category breakdown) and an Inventory report (current stock and
  stock value at cost), both exportable to CSV / Excel / PDF, plus quick links to the daily /
  weekly / monthly and staff reports.
- **Dashboard** — today's sales, products sold, transactions, and net profit up top; gross
  profit, best seller, top products, today's expenses, a low-stock banner, and a running
  profit chart below.

## How it works

- **Backend (`app.py`)** — a Flask app with a small JSON API. All arithmetic (totals, profit,
  net profit, stock checks) happens server-side against the SQLite database — the frontend
  only renders what the API returns.
- **Frontend** — plain HTML/CSS/JS per page (no build step). The dashboard polls
  `/api/summary` every 8 seconds so totals stay live, with a status banner if the server can't
  be reached.
- **Checkout & receipts** — `POST /api/checkout` takes a basket of `{product_id, quantity,
  unit_price}` lines, validates stock, records each line under a shared `receipt_id`, and
  decrements stock. `GET /api/receipt/<id>` returns that receipt for printing.
- **Exports** — `/api/export/<report>.<csv|xlsx|pdf>` (report is `sales`, `inventory`,
  `staff`, `expenses`, or `profit`) builds the file on the fly with the `csv` standard
  library, `openpyxl`, and `reportlab`.
- **Activity log** — every meaningful action (sign-ins, sales, resets, product/expense
  changes, account and role changes, report downloads) is written to `activity_log`.
  Owners see everything in Backstage; managers see team-level activity (excluding the
  owner's own) in Team.

## Logging in

Three roles, per-account passwords (hashed, never stored in plain text):

- **Employee** — front-line access: ring up sales, view sales history, download reports,
  change their own password.
- **Manager** — everything above, plus the **Team** dashboard: onboard and remove employee
  accounts, and see team-level sign-ins and activity (the owner's own activity stays private
  to the owner).
- **Owner** — everything above, plus Products, Expenses, Staff, Reports, exports, and the
  **Backstage** panel — manage every account (including changing anyone's role), see the
  full login history and a company-wide activity log, and edit shop name/currency.

Default accounts (seeded once, on first run, if the `users` table is empty — see
`DEFAULT_USERS` in `db.py`). **Set these via environment variables before the first run**;
if you don't, the app falls back to the placeholder passwords shown below, which anyone who
has read this file can guess:

| Username | Env var to set its password | Fallback if unset | Role |
|---|---|---|---|
| `employee` | `EMPLOYEE_PASSWORD` | Randomly generated locally | employee |
| `manager` | `MANAGER_PASSWORD` | Randomly generated locally | manager |
| `admin` | `OWNER_PASSWORD` | Randomly generated locally | owner |

Sign in as `admin` to reach **Backstage**, where you can add accounts of any role, promote
or demote existing accounts, remove accounts, and see the full sign-in and activity history.
Sign in as a manager to reach **Team**, a lighter-weight panel for onboarding employees
without full owner access. Any signed-in user can change their own password from the
**Password** link in the top nav.

## Running it locally

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000** in your browser. On the first local run, if you did not set the
three password environment variables, the app generates random bootstrap passwords and prints
them once in the terminal. Save them and change them after signing in.

## Bonus: Git & GitHub

```bash
git init
git add .
git commit -m "Shop sales desk: products, inventory, expenses, receipts, reports"
```

Create an empty repo on GitHub, then:

```bash
git remote add origin https://github.com/<your-username>/<repo-name>.git
git branch -M main
git push -u origin main
```

## Bonus: Deploying (Render example)

1. Push the project to GitHub (above).
2. On [render.com](https://render.com), create a **New Web Service** and connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app` (already set in the included `Procfile`)
5. Deploy — Render gives you a live URL to share.

The same `Procfile` also works on Railway and Fly.io with minimal extra config. Whichever
host you pick, make sure its disk persists across deploys/restarts (or mount a persistent
volume at `data/`) — otherwise the SQLite file resets each time.

## Data storage

Everything (users, login history, activity log, settings, products, sales, expenses) lives
in one SQLite file, resolved in this order: the `DATABASE_PATH` env var if set, otherwise
`DATA_DIR` (point this at a Render/Railway/Fly persistent disk) + `shop.db`, otherwise
`./data/shop.db` for local dev. Created automatically on first run. Back it up by copying
that one file. The database runs in WAL mode for better concurrency under multiple gunicorn
workers.

## Before deploying for real

- **Set `SECRET_KEY`** in your environment. Without it, the app falls back to a random key
  that changes every restart (signing everyone out) — that's fine for local dev, but a real
  deployment needs a fixed, secret value only you know, e.g.:
  `export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")`
- **Set `EMPLOYEE_PASSWORD`, `MANAGER_PASSWORD`, and `OWNER_PASSWORD`** before the first
  production run. The application contains **no hardcoded production passwords**. If an
  existing production database is upgraded without those variables, all existing accounts
  are forced through the Change Password screen before normal use.
- Passwords must be at least 10 characters. Use unique, randomly generated passwords for
  production accounts.
- **Serve it over HTTPS.** The session cookie is marked `Secure` by default (see
  `FORCE_HTTPS` in `app.py`), so sign-in won't work over plain HTTP in production — this is
  intentional. Render/Railway/Fly all terminate TLS for you automatically.
- **Leave `FLASK_DEBUG` unset.** The app only enables Flask's interactive debugger — which
  allows remote code execution — when `FLASK_DEBUG=1` is explicitly set. Never set it on a
  deployment reachable by anyone but you.

## Notes for extending it

- Currency is labeled GHS by default; the owner can change this from Backstage.
- Expense categories (`Electricity`, `Transport`, `Rent`, `Staff payments`, `Supplies`,
  `Other`) are defined in `db.EXPENSE_CATEGORIES` — add more there if needed.



## Security hardening in this release

- Production startup requires `SECRET_KEY` and, when the database is empty, all three bootstrap passwords.
- No static demo passwords are embedded in the source code; local bootstrap passwords are randomly generated.
- Existing production accounts are forced to change passwords when bootstrap credentials are not configured.
- Login protection combines per-IP rate limiting with persistent per-account failed-login lockout.
- Session cookies use `HttpOnly`, `SameSite=Lax`, `Secure` in production, and a 12-hour lifetime with refresh.
- CSRF protection covers HTML forms and state-changing JSON requests.
- A per-response CSP nonce protects inline scripts without `unsafe-inline` for JavaScript.
- Database checkout uses atomic stock reservation to prevent overselling under concurrent requests.
- Dynamic database/API values rendered into `innerHTML` are HTML-escaped.
- Request bodies are capped at 2 MB and checkout quantities/basket size are bounded.
- PostgreSQL and persistent Render SQLite selection remain automatic.


## Emergency credential reset

If an existing Owner, Manager, or Employee account's password is forgotten,
use `/reset-credentials` instead of deleting the database.

Before using it in production, set a strong random environment variable:

`CREDENTIAL_RESET_TOKEN`

Use at least 32 random characters. The reset token is submitted only over
HTTPS and is invalidated after one successful password reset. Rotate the
environment variable to enable another emergency reset.

The reset page can reset only the fixed bootstrap accounts:
`admin` (Owner), `manager`, and `employee`. It does not delete sales,
products, expenses, staff records, or other database data.

The existing `OWNER_PASSWORD`, `MANAGER_PASSWORD`, and `EMPLOYEE_PASSWORD`
variables remain bootstrap credentials for a brand-new empty database; they
do not overwrite passwords in an already-populated database.
