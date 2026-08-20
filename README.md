# ShopNexa

A small web app for a shop owner to record sales, manage products and stock, track expenses,
and see net profit — with automatic SQLite/PostgreSQL database selection so data can persist across restarts and deployments.

## What's in it

- **Automatic database selection** — the app uses SQLite locally (`./data/shop.db`), SQLite on Render at
`DATA_DIR/shop.db` (on Render Free the included config uses writable `/tmp/shopnexa_data/shop.db`), or PostgreSQL automatically whenever `DATABASE_URL`
is present. You do not change application routes when switching databases.
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
  net profit, stock checks) happens server-side against the selected database — the frontend
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

Default accounts (change these before sharing the project — see `DEFAULT_USERS` in `db.py`):

| Username | Password | Role |
|---|---|---|
| `employee` | `staff123` | employee |
| `manager` | `sales123` | manager |
| `ahorklo` | `backstage123` | owner |

Sign in as `ahorklo` to reach **Backstage**, where you can add accounts of any role, promote
or demote existing accounts, remove accounts, and see the full sign-in and activity history.
Sign in as a manager to reach **Team**, a lighter-weight panel for onboarding employees
without full owner access. Any signed-in user can change their own password from the
**Password** link in the top nav.

## Using PostgreSQL

To use PostgreSQL, set `DATABASE_URL` to your PostgreSQL connection string before starting the app.
For example:

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DATABASE"
python app.py
```

If `DATABASE_URL` is not set, the app automatically uses local SQLite instead.

## Running it locally

```bash
python -m venv venv
# Windows PowerShell can skip activation and call the venv's Python directly:
# .\venv\Scripts\python.exe -m pip install -r requirements.txt
python -m pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000** in your browser.

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
3. Build command: `python -m pip install -r requirements.txt`
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
- **Change the default accounts.** The app seeds `manager` / `sales123` and
  `ahorklo` / `backstage123` on first run so there's something to log in with. Sign in as
  the owner and either change these passwords (Change Password) or add new accounts and
  remove the defaults from Backstage before giving this to a real shop.

## Notes for extending it

- Currency is labeled GHS by default; the owner can change this from Backstage.
- Expense categories (`Electricity`, `Transport`, `Rent`, `Staff payments`, `Supplies`,
  `Other`) are defined in `db.EXPENSE_CATEGORIES` — add more there if needed.



## Credential reset security

Administrative password resets in Backstage require both an authenticated Owner session and the `CREDENTIAL_RESET_TOKEN` environment secret. The supplied `render.yaml` generates this token automatically in Render. Keep it private and never commit a manually chosen token to source control.


### Automatic product artwork
Product artwork is generated by ShopNexa from each product name. No product-image folder needs to be copied to GitHub. The server automatically selects an appropriate illustrated category and generates the SVG at `/product-image?name=...`.

### Product images and mobile/desktop UI

ShopNexa generates a local product illustration from each product's name/category. No product image files need to be copied to GitHub. On the Products page, tap/click a product image to open its editor. The layout switches between a desktop sidebar and a phone-friendly drawer automatically.

The current visual system uses a charcoal/espresso background with burnt-orange accents and cream text; the previous white/blue dashboard styling is not used.

## ShopNexa Work AI

ShopNexa includes a role-aware Work AI at `/work-ai`.

- Uses the live ShopNexa database for sales, products, stock and permitted staff activity.
- Owner-only business analysis includes profit, expenses, staff activity, trends and reports.
- Managers/employees are restricted to the information allowed by their role.
- Questions that need current external information automatically trigger web search.
- Web search works through the built-in DuckDuckGo HTML fallback; set `TAVILY_API_KEY` in Render for a more reliable production search provider.
- The AI never receives passwords, CSRF tokens or session secrets.

### Optional Render environment variable

`TAVILY_API_KEY` — optional. If set, Work AI uses Tavily for web search and falls back safely if the provider is unavailable.

## Final sales/product synchronization fix
- The Sales page now server-renders the current active product list, so the native mobile product selector is populated even before JavaScript finishes loading.
- `/api/products` is explicitly no-cache and returns the same live database records used by the Products page.
- Sales refreshes the product list automatically every 4 seconds and when the page becomes visible, so products added or edited elsewhere become available without a manual reload.
- The selected product, price and stock limit are refreshed from the live product record.
- Existing checkout stock validation remains enforced server-side.

### Work AI Media Studio

Work AI now includes a built-in Media Studio for creating original business artwork and short videos. It uses the server-side OpenAI API: GPT Image 2 for images and Sora 2 for video jobs. Image generation returns a generated PNG to the authenticated browser; video generation is asynchronous and the UI polls the job until it is ready. The API key is stored only as the Render `OPENAI_API_KEY` environment secret and is never exposed to browser JavaScript.

Required Render variable:

`OPENAI_API_KEY` — required to enable image/video generation.

Optional variables:

`WORK_AI_IMAGE_MODEL` — defaults to `gpt-image-2`.
`WORK_AI_VIDEO_MODEL` — defaults to `sora-2`.
`MEDIA_DIR` — optional persistent directory for generated media; when using Render persistent disk, point this at a directory on that disk.

Generated video jobs use the Sora video API and can take time to render. The UI shows progress and embeds the completed MP4 in the Work AI page.
