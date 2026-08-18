# ShopNexa — Assignment II + Role-Based Sales App

A browser-based Flask shop sales application built to satisfy the Assignment II requirements while adding secure role-based access for Owner, Manager and Employee users.

## Assignment requirements covered

- Browser-based web application
- Flask/Python backend using variables, lists, dictionaries and control flow
- Sale form: item name, unit price and quantity
- Browser calls the API; sale totals are calculated on the server
- Live summary: total sales, transaction count, units sold and best-selling item
- Transaction table refreshed after every sale
- HTML report download through `/api/report`
- CSV report download through `/api/report/csv`
- Reset-day button with confirmation through `/api/reset`
- Visible server connection status
- GitHub/Render deployment support through Procfile and Gunicorn

## Added commercial features

- Login page at `/login`
- Owner, Manager and Employee roles
- Session authentication
- Role-based API permissions (server-side enforcement)
- Backstage/control center at `/backstage`
- Each transaction records who sold it
- Sign out

## Default local demo credentials

These credentials are fixed for local testing. On Render, set environment variables to production passwords.

- Owner: `owner` / `Owner@12345`
- Manager: `manager` / `Manager@12345`
- Employee: `employee` / `Employee@12345`

## Production environment variables

Set: `OWNER_PASSWORD`, `MANAGER_PASSWORD`, `EMPLOYEE_PASSWORD`, and a strong random `SESSION_SECRET`.

Optionally set `COOKIE_SECURE=1` when serving over HTTPS.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000/login`.

## Important data note

This Assignment II build intentionally uses the simple in-memory transaction list from the assignment. Restarting the server clears the day's transactions. For the persistent Render version, merge these role/login features into the PostgreSQL/SQLite persistent build rather than replacing that database-backed version with this ZIP.
