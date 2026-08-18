# ShopNexa — Complete Render Deployment

This package contains the complete application, including the login form, owner Backstage editor, database layer, templates, static files, reports, products, expenses, staff, receipts and security hardening.

## Render settings

- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Branch: `main`

## Required environment variables

Set these in Render before deploying:

- `SECRET_KEY` — a long random secret
- `OWNER_PASSWORD` — owner bootstrap password (10+ characters)
- `MANAGER_PASSWORD` — manager bootstrap password (10+ characters)
- `EMPLOYEE_PASSWORD` — employee bootstrap password (10+ characters)
- `CREDENTIAL_RESET_TOKEN` — optional emergency reset token, 32+ random characters

The application automatically uses PostgreSQL when `DATABASE_URL` is supplied. Without it, it uses SQLite: `./data/shop.db` locally and `/var/data/shop.db` on Render.

Important: `/var/data` is only persistent if the Render service has persistent storage attached. For PostgreSQL, add the PostgreSQL connection string as `DATABASE_URL`.

## Login

The login page contains the username and password fields. The owner bootstrap account is `admin` with the password supplied through `OWNER_PASSWORD`.

## Backstage

Only the owner account can open `/backstage`. From Backstage the owner can:

- change shop name and currency
- add staff accounts
- assign employee/manager/owner roles
- remove staff accounts (with last-owner protection)
- view recent sign-ins
- view the activity log

## Safe replacement

Upload the contents of this ZIP to the GitHub repository as the complete repository. Do not upload only `app.py` and `requirements.txt`; the `templates/`, `static/`, and `db.py` files are required.

If the Render service is already connected to the GitHub repository, pushing/replacing the repository contents triggers a new deploy.
