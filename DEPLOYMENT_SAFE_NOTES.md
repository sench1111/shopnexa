# ShopNexa — safe deployment notes

## Important
- Do **not** delete the existing `shopnexa-db` PostgreSQL database.
- Keep the existing `DATABASE_URL` on the Render web service. The app automatically
  selects PostgreSQL when `DATABASE_URL` is present.
- Set `SECRET_KEY` in Render. The Blueprint can generate one for a new environment,
  but an existing production service should retain its current secret.
- Set `OWNER_PASSWORD`, `MANAGER_PASSWORD`, and `EMPLOYEE_PASSWORD` only when
  initializing a new/empty production database. Existing users are not overwritten
  just because those environment variables change.
- `CREDENTIAL_RESET_TOKEN` must be at least 32 characters. It is submitted through
  the reset form and is invalidated after a successful reset; rotate it in Render
  to perform another emergency reset.

## Owner login
The Owner account username is `admin`. The reset page labels it `Owner (admin)`.
