# ShopNexa Security Notes

- Passwords are hashed; plaintext passwords are not stored.
- Owner credential resets require authentication plus `CREDENTIAL_RESET_TOKEN`.
- `CREDENTIAL_RESET_TOKEN` must be configured only as a Render secret/environment variable.
- Session cookies use HttpOnly, Secure (when deployed over HTTPS), and SameSite=Lax.
- Login attempts are rate-limited.
- CSRF protection is enabled for Flask forms.
- Security response headers are added globally.
- Password policy requires at least 10 characters including uppercase, lowercase, a number, and a special character.
- Do not commit secrets, database URLs, reset tokens, or production credentials to GitHub.

## Deployment security notes (current build)

- CSRF protection is enabled for normal forms and JSON API requests. The browser automatically sends the Flask-WTF token on unsafe same-origin `fetch` requests.
- `SECRET_KEY` must be supplied by the deployment environment. Render generates it from `render.yaml`.
- Owner administrative password resets require the authenticated Owner role **and** `CREDENTIAL_RESET_TOKEN`.
- An Owner can reset another Owner's password, but cannot use the administrative reset control to reset their own password.
- New, changed, and reset passwords must be at least 10 characters and include uppercase, lowercase, a number, and a special character.
- Product images are generated locally from the product name/category, so adding products does not require uploading or copying an images folder to GitHub.
