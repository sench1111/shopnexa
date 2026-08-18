"""
Ahorklo Daily Sales Desk
--------------------
A small Flask app for a shop owner to record sales, manage products and
stock, and see running totals and profit — all backed by SQLite so nothing
is lost on a restart.

Run locally with:
    pip install -r requirements.txt
    python app.py

Then open http://127.0.0.1:5000 in a browser.
"""

import os
import io
import csv
import uuid
import secrets
import hmac
from functools import wraps
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for, g
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from markupsafe import escape
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

import db

app = Flask(__name__)

# Debug mode must never be on in a real deployment — Flask's debugger lets
# anyone who can trigger an unhandled exception run arbitrary Python on the
# server. It is opt-in via an explicit env var, off by default everywhere.
app.config["DEBUG"] = os.environ.get("FLASK_DEBUG", "0") == "1"

# The secret key MUST come from the environment in production — a hardcoded
# key means anyone who can read the source code can forge login sessions.
# We only fall back to a random one so local dev still works out of the box;
# that fallback changes every restart, which is fine for dev (it just signs
# everyone out) but is exactly why it must never be used for a real deployment.
_env_secret = os.environ.get("SECRET_KEY")
IS_PRODUCTION = bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID") or os.environ.get("FLASK_ENV") == "production")
if IS_PRODUCTION and not _env_secret:
    raise RuntimeError("SECRET_KEY must be set in production. Generate a long random value and add it to the hosting environment.")
if not _env_secret:
    print("WARNING: SECRET_KEY is not set; a temporary development key will be used.")
app.secret_key = _env_secret or secrets.token_hex(32)

# --- Session cookie hardening -----------------------------------------------
# HTTPONLY: JavaScript can't read the session cookie (limits XSS impact).
# SAMESITE=Lax: the cookie is not sent on cross-site POSTs, which is the
#   backbone of CSRF defense here, on top of the explicit CSRF tokens below.
# SECURE: only sent over HTTPS. Forced on when the app is reached over TLS
#   or an env var says the deployment is HTTPS-terminated (e.g. behind
#   Render's proxy); left off for plain local http:// dev so the cookie
#   still works there.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (IS_PRODUCTION or os.environ.get("FORCE_HTTPS", "0") == "1") and not app.config["DEBUG"]
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

# Reject absurdly large request bodies outright (basic DoS/abuse guard).
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB

# Trust exactly one layer of reverse proxy (Render/Railway/Fly all sit in
# front of the app) so request.remote_addr — used for login rate limiting
# and the login/activity log — reflects the real client IP instead of the
# proxy's, without blindly trusting arbitrary X-Forwarded-For headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# CSRF protection for every state-changing request. HTML forms carry a
# hidden csrf_token field; JSON fetch() calls carry it via the X-CSRFToken
# header (see static/csrf.js), which Flask-WTF also accepts.
app.config["WTF_CSRF_TIME_LIMIT"] = None  # tied to the session, not a fixed clock
csrf = CSRFProtect(app)

# Rate limiting — primarily to slow down brute-force login attempts.
_rate_store = os.environ.get("RATELIMIT_STORAGE_URI", "").strip()
if not _rate_store:
    _rate_store = os.environ.get("REDIS_URL", "").strip() or "memory://"
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri=_rate_store,
)

db.init_db(app)
print(f"Database backend: {db.DB_KIND}" + (f" ({db.DB_PATH})" if db.DB_PATH else " (DATABASE_URL)"))

DATE_FMT = "%Y-%m-%d %H:%M:%S"
LOGIN_LOCKOUT_THRESHOLD = 8       # failed attempts...
LOGIN_LOCKOUT_WINDOW_MIN = 15     # ...within this many minutes triggers a lockout


@app.before_request
def create_csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(24)


@app.after_request
def set_security_headers(response):
    """Defense-in-depth HTTP headers, applied to every response."""
    # The application has inline scripts for its self-contained pages. Give
    # every script tag a per-response nonce so the CSP can reject injected
    # <script> elements without relying on unsafe-inline.
    if response.mimetype == "text/html":
        html = response.get_data(as_text=True)
        nonce = g.get("csp_nonce", "")
        if nonce:
            import re as _re
            html = _re.sub(r"<script(?=\s|>)", f'<script nonce="{nonce}"', html)
            response.set_data(html)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # The UI uses inline <script>/<style> blocks throughout, so a strict CSP
    # would break every page; this still blocks framing, plugins, and any
    # externally-hosted script/object from being pulled in.
    nonce = g.get("csp_nonce", "")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'; connect-src 'self'"
    )
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Sales, staff, receipts and reports contain business-sensitive data.
    if session.get("username"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Small data-access helpers
# ---------------------------------------------------------------------------
def get_settings():
    rows = db.get_db().execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def get_all_users():
    rows = db.get_db().execute("SELECT * FROM users ORDER BY username").fetchall()
    return {row["username"]: dict(row) for row in rows}


def get_user_row(username):
    row = db.get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def current_user():
    username = session.get("username")
    if username:
        user = get_user_row(username)
        if user:
            return username, user
    return None, None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not logged in"}), 401
            return redirect(url_for("login"))
        _, user = current_user()
        if user and user.get("must_change_password") and request.endpoint not in {"change_password", "logout"}:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Password change required", "code": "PASSWORD_CHANGE_REQUIRED"}), 403
            return redirect(url_for("change_password"))
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        username, user = current_user()
        if not username:
            return redirect(url_for("login"))
        if user.get("must_change_password") and request.endpoint != "change_password":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Password change required", "code": "PASSWORD_CHANGE_REQUIRED"}), 403
            return redirect(url_for("change_password"))
        if user["role"] != "owner":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Owner access required"}), 403
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def manager_required(view):
    """Manager or owner — used for the Team dashboard, which sits a level
    below Backstage: it can onboard front-line employees without handing
    out full owner access."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        username, user = current_user()
        if not username:
            return redirect(url_for("login"))
        if user.get("must_change_password") and request.endpoint != "change_password":
            if request.path.startswith("/api/"):
                return jsonify({"error": "Password change required", "code": "PASSWORD_CHANGE_REQUIRED"}), 403
            return redirect(url_for("change_password"))
        if db.ROLE_RANK.get(user["role"], 0) < db.ROLE_RANK["manager"]:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Manager access required"}), 403
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def log_activity(action):
    """Persist a meaningful signed-in action for Backstage (owner, sees
    everyone) and Team (manager, sees employee-level activity only)."""
    username, user = current_user()
    db.get_db().execute(
        "INSERT INTO activity_log (username, display_name, role, action, ts) VALUES (?, ?, ?, ?, ?)",
        (username or "(unknown)", user["display_name"] if user else "(unknown)",
         user["role"] if user else None, action, datetime.now().strftime(DATE_FMT)),
    )
    db.get_db().commit()


# ---------------------------------------------------------------------------
# Date-range resolution for sales history / dashboard
# ---------------------------------------------------------------------------
def resolve_range(range_key, start_str=None, end_str=None):
    """Return (start_dt, end_dt_exclusive) for a named range or custom dates."""
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if range_key == "yesterday":
        start = today_start - timedelta(days=1)
        end = today_start
    elif range_key == "week":
        start = today_start - timedelta(days=today_start.weekday())  # Monday
        end = today_start + timedelta(days=1)
    elif range_key == "month":
        start = today_start.replace(day=1)
        end = today_start + timedelta(days=1)
    elif range_key == "custom" and start_str and end_str:
        start = datetime.strptime(start_str, "%Y-%m-%d")
        end = datetime.strptime(end_str, "%Y-%m-%d") + timedelta(days=1)
    elif range_key == "all":
        start = datetime(2000, 1, 1)
        end = now + timedelta(days=1)
    else:  # "today" and any unrecognized value
        range_key = "today"
        start = today_start
        end = today_start + timedelta(days=1)

    return range_key, start, end


def fetch_sales(start, end):
    rows = db.get_db().execute(
        "SELECT * FROM sales WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
        (start.strftime(DATE_FMT), end.strftime(DATE_FMT)),
    ).fetchall()
    return [dict(r) for r in rows]


def compute_summary(sales):
    total_sales = 0.0
    total_units = 0
    total_profit = 0.0
    item_units = {}

    for t in sales:
        total_sales += t["unit_price"] * t["quantity"]
        total_units += t["quantity"]
        total_profit += t["profit"]
        item_units[t["item"]] = item_units.get(t["item"], 0) + t["quantity"]

    best_selling_item = max(item_units, key=item_units.get) if item_units else None
    total_profit = round(total_profit, 2)
    profit_status = "profit" if total_profit > 0 else ("loss" if total_profit < 0 else "even")

    return {
        "total_sales": round(total_sales, 2),
        "num_transactions": len(sales),
        "total_units": total_units,
        "best_selling_item": best_selling_item,
        "total_profit": total_profit,
        "profit_status": profit_status,
    }


def get_low_stock():
    rows = db.get_db().execute(
        "SELECT * FROM products WHERE active = 1 AND stock_qty <= min_stock ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_top_products(sales, limit=5):
    totals = {}
    for t in sales:
        key = t["item"]
        totals.setdefault(key, {"item": key, "units": 0, "revenue": 0.0})
        totals[key]["units"] += t["quantity"]
        totals[key]["revenue"] += t["line_total"]
    ranked = sorted(totals.values(), key=lambda x: x["units"], reverse=True)
    for r in ranked:
        r["revenue"] = round(r["revenue"], 2)
    return ranked[:limit]


def fetch_expenses(start, end):
    rows = db.get_db().execute(
        "SELECT * FROM expenses WHERE ts >= ? AND ts < ? ORDER BY ts DESC",
        (start.strftime(DATE_FMT), end.strftime(DATE_FMT)),
    ).fetchall()
    return [dict(r) for r in rows]


def compute_financials(sales, expenses):
    """Revenue, cost of goods, expenses, and net profit for a set of sales/expenses."""
    revenue = round(sum(t["line_total"] for t in sales), 2)
    cost_of_goods = round(sum(t["cost_price"] * t["quantity"] for t in sales), 2)
    gross_profit = round(revenue - cost_of_goods, 2)
    total_expenses = round(sum(e["amount"] for e in expenses), 2)
    net_profit = round(gross_profit - total_expenses, 2)
    return {
        "revenue": revenue,
        "cost_of_goods": cost_of_goods,
        "gross_profit": gross_profit,
        "total_expenses": total_expenses,
        "net_profit": net_profit,
    }


def get_staff_performance(sales):
    totals = {}
    for t in sales:
        uname = t.get("username") or "(unknown)"
        totals.setdefault(uname, {"username": uname, "sales": 0.0, "transactions": 0})
        totals[uname]["sales"] += t["line_total"]
        totals[uname]["transactions"] += 1
    ranked = sorted(totals.values(), key=lambda x: x["sales"], reverse=True)
    for r in ranked:
        r["sales"] = round(r["sales"], 2)
    return ranked


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per minute")
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = db.get_db()

        # Per-account lockout: too many recent failures blocks further
        # attempts on that username for a while, regardless of which IP
        # they come from. This is on top of the per-IP rate limit above,
        # so a distributed guessing attempt doesn't just work around it.
        window_start = (datetime.now() - timedelta(minutes=LOGIN_LOCKOUT_WINDOW_MIN)).strftime(DATE_FMT)
        locked_out = False
        if username:
            recent_failures = conn.execute(
                "SELECT COUNT(*) AS c FROM login_log WHERE username = ? AND success = 0 AND ts > ?",
                (username, window_start),
            ).fetchone()["c"]
            locked_out = recent_failures >= LOGIN_LOCKOUT_THRESHOLD

        if locked_out:
            success = False
        else:
            user = get_user_row(username)
            success = bool(user and check_password_hash(user["password_hash"], password))

        conn.execute(
            "INSERT INTO login_log (username, ts, success, ip) VALUES (?, ?, ?, ?)",
            (username or "(blank)", datetime.now().strftime(DATE_FMT), int(success), request.remote_addr),
        )
        conn.commit()

        if success:
            # Rotate the session on privilege change (login) to prevent
            # session fixation, and make it a proper expiring session.
            session.clear()
            session.permanent = True
            session["username"] = username
            log_activity("Signed in")
            if user.get("must_change_password"):
                return redirect(url_for("change_password"))
            return redirect(url_for("index"))

        error = (
            f"Too many failed attempts for '{username}'. Try again in a few minutes."
            if locked_out
            else "Incorrect username or password."
        )

    return render_template("login.html", error=error, settings=get_settings())


# ---------------------------------------------------------------------------
# Emergency credential reset
# ---------------------------------------------------------------------------
# This is intentionally separate from the normal "change password" flow.
# It is disabled unless the administrator explicitly supplies a strong
# CREDENTIAL_RESET_TOKEN environment variable. The token is never placed in
# the URL; it is submitted in a POST form protected by Flask-WTF CSRF.
#
# After one successful reset, the same token is invalidated in the database.
# To use the emergency reset again, the administrator must rotate the
# CREDENTIAL_RESET_TOKEN in the hosting environment.
CREDENTIAL_RESET_USERS = {
    "admin": "owner",
    "manager": "manager",
    "employee": "employee",
}
CREDENTIAL_RESET_MIN_LENGTH = 12


def _credential_reset_available():
    token = os.environ.get("CREDENTIAL_RESET_TOKEN", "").strip()
    if len(token) < 32:
        return False
    try:
        used = db.get_db().execute(
            "SELECT value FROM settings WHERE key = ?",
            ("credential_reset_used_at",),
        ).fetchone()
        return not used or not used["value"]
    except Exception:
        return False


@app.route("/reset-credentials", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def reset_credentials():
    configured_token = os.environ.get("CREDENTIAL_RESET_TOKEN", "").strip()
    error = None
    success = None

    if request.method == "POST":
        if len(configured_token) < 32:
            error = "Credential reset is not enabled. An administrator must configure CREDENTIAL_RESET_TOKEN."
        elif not _credential_reset_available():
            error = "This reset token has already been used. Rotate CREDENTIAL_RESET_TOKEN to enable another reset."
        else:
            supplied_token = request.form.get("reset_token", "")
            username = request.form.get("username", "").strip().lower()
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            # Compare encoded bytes so reset tokens containing Unicode characters
            # cannot trigger hmac.compare_digest's str/ASCII TypeError.
            token_ok = hmac.compare_digest(
                supplied_token.encode("utf-8"),
                configured_token.encode("utf-8"),
            )
            if not token_ok:
                error = "Invalid reset token."
            elif username not in CREDENTIAL_RESET_USERS:
                error = "Invalid account."
            elif len(new_password) < CREDENTIAL_RESET_MIN_LENGTH:
                error = f"New password must be at least {CREDENTIAL_RESET_MIN_LENGTH} characters."
            elif new_password != confirm_password:
                error = "New password and confirmation do not match."
            elif not get_user_row(username):
                error = "That account does not exist."
            else:
                conn = db.get_db()
                conn.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = ?",
                    (generate_password_hash(new_password), username),
                )
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    ("credential_reset_used_at", datetime.now().strftime(DATE_FMT)),
                )
                conn.execute(
                    "INSERT INTO activity_log (username, display_name, role, action, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("system", "Security", None,
                     f"Emergency credential reset completed for '{username}'",
                     datetime.now().strftime(DATE_FMT)),
                )
                conn.commit()
                success = f"Password reset for '{username}'. The reset token has been invalidated. You can now sign in with the new password."

    return render_template(
        "reset_credentials.html",
        error=error,
        success=success,
        reset_available=_credential_reset_available(),
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    username, user = current_user()
    error = None
    success = None

    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        if not check_password_hash(user["password_hash"], current_pw):
            error = "Your current password is incorrect."
        elif len(new_pw) < 10:
            error = "New password must be at least 8 characters."
        elif new_pw != confirm_pw:
            error = "New password and confirmation don't match."
        else:
            conn = db.get_db()
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = ?",
                (generate_password_hash(new_pw), username),
            )
            conn.commit()
            user = get_user_row(username)
            log_activity("Changed their own password")
            success = "Password updated. Use it next time you sign in."

    return render_template("change_password.html", error=error, success=success, user=user, settings=get_settings())


# ---------------------------------------------------------------------------
# Backstage — owner-only control panel
# ---------------------------------------------------------------------------
@app.route("/backstage", methods=["GET"])
@owner_required
def backstage():
    rows = db.get_db().execute("SELECT * FROM login_log ORDER BY id DESC LIMIT 50").fetchall()
    recent_logins = [{"time": r["ts"], "username": r["username"], "success": r["success"], "ip": r["ip"]} for r in rows]
    activity_rows = db.get_db().execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 50").fetchall()
    recent_activity = [dict(r) for r in activity_rows]
    return render_template(
        "backstage.html",
        users=get_all_users(),
        recent_logins=recent_logins,
        recent_activity=recent_activity,
        settings=get_settings(),
        current_username=session.get("username"),
        error=request.args.get("error"),
        success=request.args.get("success"),
    )


@app.route("/backstage/add-user", methods=["POST"])
@owner_required
def backstage_add_user():
    username = request.form.get("new_username", "").strip()
    password = request.form.get("new_password", "")
    role = request.form.get("new_role", "manager")
    display_name = request.form.get("new_display_name", "").strip() or username

    if not username or not password:
        return redirect(url_for("backstage", error="Username and password are required."))
    if get_user_row(username):
        return redirect(url_for("backstage", error=f"'{username}' already exists."))
    if len(password) < 10:
        return redirect(url_for("backstage", error="Password must be at least 8 characters."))
    if role not in ("employee", "manager", "owner"):
        role = "employee"

    conn = db.get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
        (username, generate_password_hash(password), role, display_name),
    )
    conn.commit()
    log_activity(f"Added staff account '{username}' as {role}")
    return redirect(url_for("backstage", success=f"Added '{username}' as {role}."))


@app.route("/backstage/update-role", methods=["POST"])
@owner_required
def backstage_update_role():
    username = request.form.get("username", "")
    new_role = request.form.get("role", "")
    current = session.get("username")
    user = get_user_row(username)

    if not user:
        return redirect(url_for("backstage", error="That user doesn't exist."))
    if new_role not in ("employee", "manager", "owner"):
        return redirect(url_for("backstage", error="Not a valid role."))
    if username == current and new_role != "owner":
        return redirect(url_for("backstage", error="You can't demote the account you're signed in with."))

    old_role = user["role"]
    if old_role == "owner" and new_role != "owner":
        owners_left = db.get_db().execute(
            "SELECT COUNT(*) c FROM users WHERE role = 'owner' AND username != ?", (username,)
        ).fetchone()["c"]
        if not owners_left:
            return redirect(url_for("backstage", error="Can't demote the last owner account."))

    conn = db.get_db()
    conn.execute("UPDATE users SET role = ? WHERE username = ?", (new_role, username))
    conn.commit()
    log_activity(f"Changed '{username}' role from {old_role} to {new_role}")
    return redirect(url_for("backstage", success=f"'{username}' is now {new_role}."))


@app.route("/backstage/remove-user", methods=["POST"])
@owner_required
def backstage_remove_user():
    username = request.form.get("username", "")
    current = session.get("username")

    if username == current:
        return redirect(url_for("backstage", error="You can't remove the account you're signed in with."))
    if not get_user_row(username):
        return redirect(url_for("backstage", error="That user doesn't exist."))

    conn = db.get_db()
    owners_left = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE role = 'owner' AND username != ?", (username,)
    ).fetchone()["c"]
    target_role = get_user_row(username)["role"]
    if target_role == "owner" and owners_left == 0:
        return redirect(url_for("backstage", error="Can't remove the last owner account."))

    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    log_activity(f"Removed staff account '{username}'")
    return redirect(url_for("backstage", success=f"Removed '{username}'."))


@app.route("/backstage/settings", methods=["POST"])
@owner_required
def backstage_settings():
    shop_name = request.form.get("shop_name", "").strip()
    currency = request.form.get("currency", "").strip()

    conn = db.get_db()
    if shop_name:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'shop_name'", (shop_name,))
    if currency:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'currency'", (currency,))
    conn.commit()

    log_activity("Updated shop settings")
    return redirect(url_for("backstage", success="Settings updated."))


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------
@app.route("/welcome")
def welcome():
    """Public product page suitable for demos, buyer previews and sales links."""
    return render_template("welcome.html")

@app.route("/")
@login_required
def index():
    username, user = current_user()
    return render_template("index.html", user=user, settings=get_settings())


@app.route("/history")
@login_required
def history():
    username, user = current_user()
    return render_template("history.html", user=user, settings=get_settings())


@app.route("/products")
@owner_required
def products_page():
    username, user = current_user()
    rows = db.get_db().execute("SELECT * FROM products WHERE active = 1 ORDER BY name").fetchall()
    return render_template("products.html", user=user, settings=get_settings(), products=[dict(r) for r in rows])


@app.route("/expenses")
@owner_required
def expenses_page():
    username, user = current_user()
    return render_template("expenses.html", user=user, settings=get_settings(), categories=db.EXPENSE_CATEGORIES)


@app.route("/staff")
@owner_required
def staff_page():
    username, user = current_user()
    return render_template("staff.html", user=user, settings=get_settings())


@app.route("/reports")
@owner_required
def reports_page():
    username, user = current_user()
    return render_template("reports.html", user=user, settings=get_settings())


@app.route("/receipt/<receipt_id>")
@login_required
def receipt_page(receipt_id):
    username, user = current_user()
    return render_template("receipt.html", user=user, settings=get_settings(), receipt_id=receipt_id)


# ---------------------------------------------------------------------------
# Team — manager-level staff dashboard (onboard employees without owner access)
# ---------------------------------------------------------------------------
@app.route("/team", methods=["GET"])
@manager_required
def team_dashboard():
    username, user = current_user()
    users = get_all_users()
    employees = {u: info for u, info in users.items() if info["role"] == "employee"}
    conn = db.get_db()
    team_logins = [
        {"time": r["ts"], "username": r["username"], "success": r["success"], "ip": r["ip"]}
        for r in conn.execute(
            "SELECT l.* FROM login_log l LEFT JOIN users u ON u.username = l.username "
            "WHERE u.role IS NULL OR u.role != 'owner' ORDER BY l.id DESC LIMIT 50"
        ).fetchall()
    ]
    team_activity = [dict(r) for r in conn.execute(
        "SELECT * FROM activity_log WHERE role IS NULL OR role != 'owner' ORDER BY id DESC LIMIT 50"
    ).fetchall()]
    return render_template(
        "team.html",
        user=user,
        employees=employees,
        recent_logins=team_logins,
        recent_activity=team_activity,
        settings=get_settings(),
        current_username=session.get("username"),
        error=request.args.get("error"),
        success=request.args.get("success"),
    )


@app.route("/team/add-employee", methods=["POST"])
@manager_required
def team_add_employee():
    username = request.form.get("new_username", "").strip()
    password = request.form.get("new_password", "")
    display_name = request.form.get("new_display_name", "").strip() or username

    if not username or not password:
        return redirect(url_for("team_dashboard", error="Username and password are required."))
    if get_user_row(username):
        return redirect(url_for("team_dashboard", error=f"'{username}' already exists."))
    if len(password) < 10:
        return redirect(url_for("team_dashboard", error="Password must be at least 8 characters."))

    conn = db.get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, 'employee', ?)",
        (username, generate_password_hash(password), display_name),
    )
    conn.commit()
    log_activity(f"Added employee account '{username}'")
    return redirect(url_for("team_dashboard", success=f"Added '{username}' to the team."))


@app.route("/team/remove-employee", methods=["POST"])
@manager_required
def team_remove_employee():
    username = request.form.get("username", "")
    user = get_user_row(username)

    if not user:
        return redirect(url_for("team_dashboard", error="That user doesn't exist."))
    if user["role"] != "employee":
        return redirect(url_for("team_dashboard", error="You can only remove employee accounts from here."))

    conn = db.get_db()
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    log_activity(f"Removed employee account '{username}'")
    return redirect(url_for("team_dashboard", success=f"Removed '{username}'."))


# ---------------------------------------------------------------------------
# Product management API
# ---------------------------------------------------------------------------
def _parse_product_form(data):
    name = str(data.get("name", "")).strip()
    try:
        selling_price = float(data.get("selling_price"))
        cost_price = float(data.get("cost_price") or 0)
        stock_qty = int(data.get("stock_qty") or 0)
        min_stock = int(data.get("min_stock") or 0)
    except (TypeError, ValueError):
        return None, "Prices and quantities must be numbers."

    if not name:
        return None, "Product name is required."
    if selling_price <= 0:
        return None, "Selling price must be greater than 0."
    if cost_price < 0 or stock_qty < 0 or min_stock < 0:
        return None, "Prices and quantities can't be negative."

    return {
        "name": name,
        "selling_price": selling_price,
        "cost_price": cost_price,
        "stock_qty": stock_qty,
        "min_stock": min_stock,
    }, None


@app.route("/api/products", methods=["GET"])
@login_required
def api_products():
    rows = db.get_db().execute("SELECT * FROM products WHERE active = 1 ORDER BY name").fetchall()
    return jsonify({"products": [dict(r) for r in rows]})


@app.route("/api/products", methods=["POST"])
@owner_required
def api_add_product():
    product, error = _parse_product_form(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400

    conn = db.get_db()
    existing = conn.execute("SELECT id FROM products WHERE name = ? AND active = 1", (product["name"],)).fetchone()
    if existing:
        return jsonify({"error": f"'{product['name']}' already exists."}), 400

    conn.execute(
        "INSERT INTO products (name, selling_price, cost_price, stock_qty, min_stock, active, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (product["name"], product["selling_price"], product["cost_price"], product["stock_qty"],
         product["min_stock"], datetime.now().strftime(DATE_FMT)),
    )
    conn.commit()
    log_activity(f"Added product '{product['name']}'")
    return jsonify({"ok": True}), 201


@app.route("/api/products/<int:product_id>", methods=["PUT"])
@owner_required
def api_edit_product(product_id):
    product, error = _parse_product_form(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400

    conn = db.get_db()
    row = conn.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        return jsonify({"error": "Product not found."}), 404

    duplicate = conn.execute(
        "SELECT id FROM products WHERE name = ? AND active = 1 AND id != ?", (product["name"], product_id)
    ).fetchone()
    if duplicate:
        return jsonify({"error": f"'{product['name']}' already exists."}), 400

    conn.execute(
        "UPDATE products SET name = ?, selling_price = ?, cost_price = ?, stock_qty = ?, min_stock = ? WHERE id = ?",
        (product["name"], product["selling_price"], product["cost_price"], product["stock_qty"],
         product["min_stock"], product_id),
    )
    conn.commit()
    log_activity(f"Edited product '{product['name']}'")
    return jsonify({"ok": True})


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
@owner_required
def api_delete_product(product_id):
    conn = db.get_db()
    row = conn.execute("SELECT id, name FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        return jsonify({"error": "Product not found."}), 404
    # Soft delete so past sales still show the correct product name/history.
    conn.execute("UPDATE products SET active = 0 WHERE id = ?", (product_id,))
    conn.commit()
    log_activity(f"Removed product '{row['name']}'")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Sales API — checkout supports multiple items in one sale so a single
# receipt can show "Coca-Cola x2, Bread x1, TOTAL ..." like a real till.
# ---------------------------------------------------------------------------
@app.route("/api/checkout", methods=["POST"])
@login_required
def checkout():
    data = request.get_json(silent=True) or {}
    lines_in = data.get("lines")
    username, _ = current_user()

    if not isinstance(lines_in, list) or not lines_in or len(lines_in) > 100:
        return jsonify({"error": "Add between 1 and 100 items to the basket."}), 400

    conn = db.get_db()
    prepared = []
    combined = {}

    # Validate the request before opening the write transaction.
    for line in lines_in:
        if not isinstance(line, dict):
            return jsonify({"error": "Invalid basket line."}), 400
        try:
            product_id = int(line.get("product_id"))
            quantity = int(line.get("quantity"))
        except (TypeError, ValueError):
            return jsonify({"error": "Each basket line needs a product and a whole-number quantity."}), 400

        if product_id <= 0 or quantity <= 0 or quantity > 100000:
            return jsonify({"error": "Product and quantity values are invalid."}), 400

        try:
            unit_price = float(line.get("unit_price"))
        except (TypeError, ValueError):
            return jsonify({"error": "Selling price must be a number."}), 400
        if not (0 < unit_price <= 100000000):
            return jsonify({"error": "Selling price is outside the allowed range."}), 400

        combined[product_id] = combined.get(product_id, 0) + quantity
        if combined[product_id] > 100000:
            return jsonify({"error": "The quantity for one product is too large."}), 400

        prepared.append({"product_id": product_id, "quantity": quantity, "unit_price": unit_price})

    # Make the stock deduction atomic. SQLite gets an IMMEDIATE write lock;
    # PostgreSQL uses its normal row-level locking through the UPDATE.
    try:
        if db.DB_KIND == "sqlite":
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")

        # Conditional UPDATE prevents two concurrent checkouts from selling
        # the same final units of stock.
        for product_id, qty in combined.items():
            cur = conn.execute(
                "UPDATE products SET stock_qty = stock_qty - ? "
                "WHERE id = ? AND active = 1 AND stock_qty >= ?",
                (qty, product_id, qty),
            )
            if getattr(cur, "rowcount", 0) != 1:
                conn.rollback()
                return jsonify({"error": "Stock changed while checking out. Please refresh and try again."}), 409

        # Fetch the locked/updated products after the atomic stock reservation.
        products = {}
        for product_id in combined:
            row = conn.execute(
                "SELECT * FROM products WHERE id = ? AND active = 1", (product_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                return jsonify({"error": "One of the basket items no longer exists."}), 409
            products[product_id] = row

        receipt_id = uuid.uuid4().hex[:16]
        now = datetime.now().strftime(DATE_FMT)
        receipt_lines = []

        for line in prepared:
            product = products[line["product_id"]]
            quantity = line["quantity"]
            unit_price = line["unit_price"]
            cost_price = float(product["cost_price"])
            line_total = round(unit_price * quantity, 2)
            profit = round((unit_price - cost_price) * quantity, 2)

            conn.execute(
                "INSERT INTO sales (product_id, item, unit_price, cost_price, quantity, line_total, profit, "
                "username, receipt_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (product["id"], product["name"], unit_price, cost_price, quantity,
                 line_total, profit, username, receipt_id, now),
            )
            receipt_lines.append({
                "item": product["name"],
                "quantity": quantity,
                "unit_price": unit_price,
                "line_total": line_total,
            })

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_activity(f"Recorded a sale (receipt #{receipt_id}, {len(receipt_lines)} item(s))")

    settings = get_settings()
    receipt = {
        "receipt_id": receipt_id,
        "shop_name": settings["shop_name"],
        "currency": settings["currency"],
        "ts": now,
        "cashier": username,
        "lines": receipt_lines,
        "total": round(sum(l["line_total"] for l in receipt_lines), 2),
    }

    _, start, end = resolve_range("today")
    sales = fetch_sales(start, end)
    expenses = fetch_expenses(start, end)
    return jsonify({
        "summary": compute_summary(sales),
        "financials": compute_financials(sales, expenses),
        "transactions": sales,
        "low_stock": get_low_stock(),
        "top_products": get_top_products(sales),
        "receipt": receipt,
    }), 201


@app.route("/api/receipt/<receipt_id>", methods=["GET"])
@login_required
def api_receipt(receipt_id):
    rows = db.get_db().execute("SELECT * FROM sales WHERE receipt_id = ? ORDER BY id", (receipt_id,)).fetchall()
    if not rows:
        return jsonify({"error": "Receipt not found."}), 404
    settings = get_settings()
    lines = [{"item": r["item"], "quantity": r["quantity"], "unit_price": r["unit_price"], "line_total": r["line_total"]} for r in rows]
    return jsonify({
        "receipt_id": receipt_id,
        "shop_name": settings["shop_name"],
        "currency": settings["currency"],
        "ts": rows[0]["ts"],
        "cashier": rows[0]["username"],
        "lines": lines,
        "total": round(sum(l["line_total"] for l in lines), 2),
    })


@app.route("/api/summary", methods=["GET"])
@login_required
def get_summary():
    _, start, end = resolve_range("today")
    sales = fetch_sales(start, end)
    expenses = fetch_expenses(start, end)
    return jsonify({
        "summary": compute_summary(sales),
        "financials": compute_financials(sales, expenses),
        "transactions": sales,
        "low_stock": get_low_stock(),
        "top_products": get_top_products(sales),
    })


@app.route("/api/sales", methods=["GET"])
@login_required
def api_sales():
    range_key, start, end = resolve_range(
        request.args.get("range", "today"), request.args.get("start"), request.args.get("end")
    )
    sales = fetch_sales(start, end)
    expenses = fetch_expenses(start, end)
    return jsonify({
        "range": range_key,
        "summary": compute_summary(sales),
        "financials": compute_financials(sales, expenses),
        "transactions": list(reversed(sales)),
    })


@app.route("/api/reset", methods=["POST"])
@owner_required
def reset():
    """Clear today's sales and restore the stock that was sold — for
    correcting mistakes, not a normal end-of-day action (the database keeps
    every day's history automatically now)."""
    _, start, end = resolve_range("today")
    conn = db.get_db()
    todays = conn.execute(
        "SELECT * FROM sales WHERE ts >= ? AND ts < ?", (start.strftime(DATE_FMT), end.strftime(DATE_FMT))
    ).fetchall()
    for sale in todays:
        if sale["product_id"] is not None:
            conn.execute(
                "UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                (sale["quantity"], sale["product_id"]),
            )
    conn.execute("DELETE FROM sales WHERE ts >= ? AND ts < ?", (start.strftime(DATE_FMT), end.strftime(DATE_FMT)))
    conn.commit()
    log_activity(f"Cleared today's sales ({len(todays)} transaction(s))")

    sales = fetch_sales(start, end)
    expenses = fetch_expenses(start, end)
    return jsonify({
        "summary": compute_summary(sales),
        "financials": compute_financials(sales, expenses),
        "transactions": sales,
        "low_stock": get_low_stock(),
        "top_products": get_top_products(sales),
    })


# ---------------------------------------------------------------------------
# Expenses API
# ---------------------------------------------------------------------------
@app.route("/api/expenses", methods=["GET"])
@owner_required
def api_expenses():
    range_key, start, end = resolve_range(
        request.args.get("range", "today"), request.args.get("start"), request.args.get("end")
    )
    expenses = fetch_expenses(start, end)
    return jsonify({
        "range": range_key,
        "expenses": expenses,
        "total": round(sum(e["amount"] for e in expenses), 2),
        "categories": db.EXPENSE_CATEGORIES,
    })


@app.route("/api/expenses", methods=["POST"])
@owner_required
def api_add_expense():
    data = request.get_json(silent=True) or {}
    username, _ = current_user()
    category = str(data.get("category", "")).strip()
    note = str(data.get("note", "")).strip()

    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Amount must be a number."}), 400

    if category not in db.EXPENSE_CATEGORIES:
        return jsonify({"error": "Choose a valid category."}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be greater than 0."}), 400

    conn = db.get_db()
    conn.execute(
        "INSERT INTO expenses (category, amount, note, username, ts) VALUES (?, ?, ?, ?, ?)",
        (category, amount, note, username, datetime.now().strftime(DATE_FMT)),
    )
    conn.commit()
    log_activity(f"Recorded {category} expense of {amount:.2f}")
    return jsonify({"ok": True}), 201


@app.route("/api/expenses/<int:expense_id>", methods=["DELETE"])
@owner_required
def api_delete_expense(expense_id):
    conn = db.get_db()
    row = conn.execute("SELECT id, category, amount FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if not row:
        return jsonify({"error": "Expense not found."}), 404
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    log_activity(f"Removed {row['category']} expense of {row['amount']:.2f}")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Staff performance API
# ---------------------------------------------------------------------------
@app.route("/api/staff", methods=["GET"])
@owner_required
def api_staff():
    range_key, start, end = resolve_range(
        request.args.get("range", "today"), request.args.get("start"), request.args.get("end")
    )
    sales = fetch_sales(start, end)
    return jsonify({"range": range_key, "staff": get_staff_performance(sales)})


@app.route("/api/reports/profit", methods=["GET"])
@owner_required
def api_reports_profit():
    range_key, start, end = resolve_range(
        request.args.get("range", "today"), request.args.get("start"), request.args.get("end")
    )
    sales = fetch_sales(start, end)
    expenses = fetch_expenses(start, end)
    financials = compute_financials(sales, expenses)

    by_category = {}
    for e in expenses:
        by_category[e["category"]] = by_category.get(e["category"], 0) + e["amount"]
    expense_breakdown = [{"category": c, "amount": round(a, 2)} for c, a in
                          sorted(by_category.items(), key=lambda x: -x[1])]

    return jsonify({"range": range_key, "financials": financials, "expense_breakdown": expense_breakdown})


@app.route("/api/report", methods=["GET"])
@login_required
def report():
    range_key, start, end = resolve_range(
        request.args.get("range", "today"), request.args.get("start"), request.args.get("end")
    )
    log_activity(f"Downloaded the {range_key} sales report")
    sales = fetch_sales(start, end)
    summary = compute_summary(sales)
    generated_at = datetime.now().strftime(DATE_FMT)
    settings = get_settings()

    # This report is assembled as raw HTML (not rendered through Jinja), so
    # every value that ultimately comes from user input — product names,
    # the shop name/currency an owner can set in Backstage, etc. — must be
    # escaped by hand here or it becomes a stored-XSS vector.
    rows_html = ""
    for t in sales:
        profit = t.get("profit", 0.0)
        profit_color = "#34d399" if profit > 0 else ("#f87171" if profit < 0 else "#8a8478")
        rows_html += f"""
        <tr>
            <td>{escape(t['ts'])}</td>
            <td>{escape(t['item'])}</td>
            <td>{t['unit_price']:.2f}</td>
            <td>{t['quantity']}</td>
            <td>{t['line_total']:.2f}</td>
            <td style="color:{profit_color}; font-weight:600;">{profit:+.2f}</td>
        </tr>"""

    if not sales:
        rows_html = "<tr><td colspan='6' style='text-align:center;'>No transactions recorded</td></tr>"

    profit_color = "#34d399" if summary["total_profit"] > 0 else ("#f87171" if summary["total_profit"] < 0 else "#e8a855")
    profit_word = {"profit": "In profit", "loss": "Operating at a loss", "even": "Break even"}[summary["profit_status"]]
    shop_name = escape(settings["shop_name"])
    currency = escape(settings["currency"])
    best_selling_item = escape(summary["best_selling_item"]) if summary["best_selling_item"] else "N/A"
    range_title = escape(range_key.title())

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{shop_name} — Sales Report</title>
<style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 0; padding: 2.5rem; color: #e8e4da; background: #05070a; }}
    .eyebrow {{ text-transform: uppercase; letter-spacing: 0.12em; font-size: 0.75rem; color: #f0a63e; font-weight: 700; margin: 0 0 0.4rem; }}
    h1 {{ font-family: Georgia, 'Times New Roman', serif; margin: 0 0 0.25rem; font-size: 2rem; color: #f5efe3; }}
    .meta {{ color: #8a8478; margin-bottom: 1.75rem; font-size: 0.9rem; }}
    .summary {{ display: flex; flex-wrap: wrap; gap: 1rem; margin-bottom: 2rem; }}
    .summary .box {{ background: #12181a; border: 1px solid rgba(245,239,227,0.08); color: #f5efe3; border-radius: 10px; padding: 1rem 1.25rem; min-width: 150px; }}
    .summary .box .label {{ text-transform: uppercase; letter-spacing: 0.1em; font-size: 0.68rem; color: #a89f8c; margin-bottom: 0.3rem; }}
    .summary .box .value {{ font-size: 1.3rem; font-weight: 700; color: #f0a63e; }}
    table {{ border-collapse: collapse; width: 100%; background: #0d1214; border-radius: 10px; overflow: hidden; }}
    th, td {{ padding: 10px 14px; text-align: left; font-size: 0.9rem; border-bottom: 1px solid rgba(245,239,227,0.06); }}
    th {{ background: #12181a; text-transform: uppercase; letter-spacing: 0.06em; font-size: 0.72rem; color: #a89f8c; }}
</style>
</head>
<body>
    <p class="eyebrow">{range_title} Snapshot</p>
    <h1>{shop_name} — Sales Report</h1>
    <p class="meta">Generated {generated_at}</p>

    <div class="summary">
        <div class="box"><div class="label">Total sales</div><div class="value">{currency} {summary['total_sales']:.2f}</div></div>
        <div class="box"><div class="label">Transactions</div><div class="value">{summary['num_transactions']}</div></div>
        <div class="box"><div class="label">Units sold</div><div class="value">{summary['total_units']}</div></div>
        <div class="box"><div class="label">Best seller</div><div class="value">{best_selling_item}</div></div>
        <div class="box"><div class="label">{profit_word}</div><div class="value" style="color:{profit_color};">{currency} {summary['total_profit']:+.2f}</div></div>
    </div>
    <table>
        <thead>
            <tr><th>Time</th><th>Item</th><th>Unit Price</th><th>Qty</th><th>Line Total</th><th>Profit</th></tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
</body>
</html>"""

    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f"attachment; filename=sales_report_{range_key}.html"},
    )


# ---------------------------------------------------------------------------
# Exports — CSV / Excel / PDF for sales, inventory, staff, and expenses
# ---------------------------------------------------------------------------
def _range_from_request():
    return resolve_range(
        request.args.get("range", "today"), request.args.get("start"), request.args.get("end")
    )


def _csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _xlsx_response(filename, sheets):
    """sheets: list of (sheet_name, header, rows)"""
    wb = Workbook()
    wb.remove(wb.active)
    for name, header, rows in sheets:
        ws = wb.create_sheet(title=name[:31])
        ws.append(header)
        for row in rows:
            ws.append(row)
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(width + 2, 40)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _pdf_response(filename, title, subtitle, sections):
    """sections: list of (section_title, header, rows)"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles["Title"]), Paragraph(subtitle, styles["Normal"]), Spacer(1, 0.5 * cm)]

    for section_title, header, rows in sections:
        story.append(Paragraph(section_title, styles["Heading2"]))
        data = [header] + [[str(c) for c in row] for row in rows] if rows else [header, ["No data"] + [""] * (len(header) - 1)]
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17322f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f0")]),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.6 * cm))

    doc.build(story)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/export/sales.<fmt>")
@owner_required
def export_sales(fmt):
    range_key, start, end = _range_from_request()
    sales = fetch_sales(start, end)
    settings = get_settings()
    header = ["Date & time", "Item", "Unit price", "Qty", "Line total", "Profit", "Cashier"]
    rows = [[t["ts"], t["item"], t["unit_price"], t["quantity"], t["line_total"], t["profit"], t["username"] or ""] for t in sales]

    if fmt == "csv":
        return _csv_response(f"sales_{range_key}.csv", header, rows)
    if fmt == "xlsx":
        return _xlsx_response(f"sales_{range_key}.xlsx", [("Sales", header, rows)])
    if fmt == "pdf":
        summary = compute_summary(sales)
        subtitle = f"{settings['shop_name']} · {range_key.title()} · Total {settings['currency']} {summary['total_sales']:.2f}"
        return _pdf_response(f"sales_{range_key}.pdf", "Sales Report", subtitle, [("Transactions", header, rows)])
    return jsonify({"error": "Unsupported format. Use csv, xlsx, or pdf."}), 400


@app.route("/api/export/inventory.<fmt>")
@owner_required
def export_inventory(fmt):
    rows_raw = db.get_db().execute("SELECT * FROM products WHERE active = 1 ORDER BY name").fetchall()
    header = ["Product", "Selling price", "Cost price", "Stock", "Min stock", "Status"]
    rows = [[r["name"], r["selling_price"], r["cost_price"], r["stock_qty"], r["min_stock"],
             "LOW" if r["stock_qty"] <= r["min_stock"] else "OK"] for r in rows_raw]

    if fmt == "csv":
        return _csv_response("inventory.csv", header, rows)
    if fmt == "xlsx":
        return _xlsx_response("inventory.xlsx", [("Inventory", header, rows)])
    if fmt == "pdf":
        settings = get_settings()
        return _pdf_response("inventory.pdf", "Inventory Report", settings["shop_name"], [("Products", header, rows)])
    return jsonify({"error": "Unsupported format. Use csv, xlsx, or pdf."}), 400


@app.route("/api/export/staff.<fmt>")
@owner_required
def export_staff(fmt):
    range_key, start, end = _range_from_request()
    sales = fetch_sales(start, end)
    staff = get_staff_performance(sales)
    settings = get_settings()
    header = ["Staff", "Sales", "Transactions"]
    rows = [[s["username"], s["sales"], s["transactions"]] for s in staff]

    if fmt == "csv":
        return _csv_response(f"staff_{range_key}.csv", header, rows)
    if fmt == "xlsx":
        return _xlsx_response(f"staff_{range_key}.xlsx", [("Staff", header, rows)])
    if fmt == "pdf":
        subtitle = f"{settings['shop_name']} · {range_key.title()}"
        return _pdf_response(f"staff_{range_key}.pdf", "Staff Performance", subtitle, [("Staff", header, rows)])
    return jsonify({"error": "Unsupported format. Use csv, xlsx, or pdf."}), 400


@app.route("/api/export/expenses.<fmt>")
@owner_required
def export_expenses(fmt):
    range_key, start, end = _range_from_request()
    expenses = fetch_expenses(start, end)
    settings = get_settings()
    header = ["Date & time", "Category", "Amount", "Note", "Recorded by"]
    rows = [[e["ts"], e["category"], e["amount"], e["note"] or "", e["username"] or ""] for e in expenses]

    if fmt == "csv":
        return _csv_response(f"expenses_{range_key}.csv", header, rows)
    if fmt == "xlsx":
        return _xlsx_response(f"expenses_{range_key}.xlsx", [("Expenses", header, rows)])
    if fmt == "pdf":
        subtitle = f"{settings['shop_name']} · {range_key.title()}"
        return _pdf_response(f"expenses_{range_key}.pdf", "Expenses Report", subtitle, [("Expenses", header, rows)])
    return jsonify({"error": "Unsupported format. Use csv, xlsx, or pdf."}), 400


@app.route("/api/export/profit.<fmt>")
@owner_required
def export_profit(fmt):
    range_key, start, end = _range_from_request()
    sales = fetch_sales(start, end)
    expenses = fetch_expenses(start, end)
    financials = compute_financials(sales, expenses)
    settings = get_settings()
    header = ["Metric", "Amount"]
    rows = [
        ["Revenue", financials["revenue"]],
        ["Cost of goods sold", financials["cost_of_goods"]],
        ["Gross profit", financials["gross_profit"]],
        ["Expenses", financials["total_expenses"]],
        ["Net profit", financials["net_profit"]],
    ]
    expense_header = ["Category", "Amount"]
    by_category = {}
    for e in expenses:
        by_category[e["category"]] = by_category.get(e["category"], 0) + e["amount"]
    expense_rows = [[cat, round(amt, 2)] for cat, amt in sorted(by_category.items(), key=lambda x: -x[1])]

    if fmt == "csv":
        return _csv_response(f"profit_{range_key}.csv", header, rows)
    if fmt == "xlsx":
        return _xlsx_response(f"profit_{range_key}.xlsx", [("Profit", header, rows), ("Expenses by category", expense_header, expense_rows)])
    if fmt == "pdf":
        subtitle = f"{settings['shop_name']} · {range_key.title()}"
        return _pdf_response(f"profit_{range_key}.pdf", "Profit Report", subtitle,
                              [("Summary", header, rows), ("Expenses by category", expense_header, expense_rows)])
    return jsonify({"error": "Unsupported format. Use csv, xlsx, or pdf."}), 400


if __name__ == "__main__":
    # DEBUG is env-controlled (see app.config["DEBUG"] above) and defaults
    # to off — the Werkzeug debugger allows remote code execution if it's
    # ever left on in a reachable deployment.
    app.run(debug=app.config["DEBUG"])
