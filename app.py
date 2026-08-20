import re
import threading
import subprocess
"""ShopNexa
---------
A Flask retail sales, inventory and reporting app with automatic SQLite or PostgreSQL storage.

Run locally with:
    python -m pip install -r requirements.txt
    python app.py

Then open http://127.0.0.1:5000 in a browser.
"""

import os
import hmac
import io
import csv
import uuid
import secrets
import base64
import json
from pathlib import Path

import requests
from functools import wraps
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

import db
from web_search import search_web

app = Flask(__name__)

# Work AI is intentionally in Owner Power Mode for this build while the
# sales workflow is being repaired. It grants the AI the same BUSINESS-DATA
# visibility as the owner (sales, expenses, stock, reports and staff activity)
# without ever exposing credentials, session tokens, CSRF secrets or password
# hashes. Set WORK_AI_OWNER_MODE=false to restore normal role filtering.
app.config["WORK_AI_OWNER_MODE"] = os.getenv("WORK_AI_OWNER_MODE", "true").lower() in ("1", "true", "yes", "on")

def validate_password_strength(password):
    """Require a stronger password for new/reset credentials."""
    if not isinstance(password, str) or len(password) < 10:
        return False, "Password must be at least 10 characters."
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter."
    if not re.search(r"\d", password):
        return False, "Password must contain at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return False, "Password must contain at least one special character."
    return True, None

# Security hardening
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)

csrf = CSRFProtect(app)
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://"
)


# The secret key MUST come from the environment in production — a hardcoded
# key means anyone who can read the source code can forge login sessions.
# We only fall back to a random one so local dev still works out of the box;
# that fallback changes every restart, which is fine for dev (it just signs
# everyone out) but is exactly why it must never be used for a real deployment.
_env_secret = os.environ.get("SECRET_KEY")
if not _env_secret:
    print("WARNING: SECRET_KEY is not set. Set a permanent SECRET_KEY in production.")
app.secret_key = _env_secret or secrets.token_hex(32)

db.init_db(app)

DATE_FMT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Work AI media generation (GPT Image + Sora)
# ---------------------------------------------------------------------------
_default_media_dir = os.path.join(getattr(db, "DATA_DIR", os.path.join(os.getcwd(), "data")), "work_ai_media")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", _default_media_dir))
# Render Free does not make /var/data writable. Fall back automatically so a
# stale MEDIA_DIR environment variable cannot prevent the app from booting.
if os.getenv("RENDER") and str(MEDIA_DIR).startswith("/var/data"):
    MEDIA_DIR = Path("/tmp/shopnexa_data/work_ai_media")
try:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    MEDIA_DIR = Path("/tmp/shopnexa_data/work_ai_media")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
WORK_AI_MODEL = os.getenv("WORK_AI_MODEL", "gpt-5.6")
WORK_AI_IMAGE_MODEL = os.getenv("WORK_AI_IMAGE_MODEL", "gpt-image-2")
WORK_AI_VIDEO_MODEL = os.getenv("WORK_AI_VIDEO_MODEL", "sora-2")
LONG_VIDEO_JOBS = {}
LONG_VIDEO_LOCK = threading.Lock()

def _openai_headers():
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")
    return {"Authorization": f"Bearer {OPENAI_API_KEY}"}

def _safe_media_name(prefix, ext):
    return f"{prefix}_{uuid.uuid4().hex}.{ext}"

def _media_public_url(filename):
    return url_for("work_ai_media_file", filename=filename)



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
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        username, user = current_user()
        if not username:
            return redirect(url_for("login"))
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
# ShopNexa Work AI
# ---------------------------------------------------------------------------
WEB_TRIGGER_WORDS = (
    "web", "internet", "online", "latest", "current", "news", "price", "prices",
    "market", "trend", "recent", "update", "research", "competitor", "weather",
    "exchange rate", "currency rate", "external"
)


def _money(value):
    return f"{get_settings().get('currency', 'GHS')} {float(value or 0):,.2f}"


def _ai_range_from_text(text):
    t = text.lower()
    if any(x in t for x in ("this month", "monthly", "month")):
        return resolve_range("month")
    if any(x in t for x in ("this week", "weekly", "week")):
        return resolve_range("week")
    if "yesterday" in t:
        return resolve_range("yesterday")
    if "all time" in t or "ever" in t:
        return resolve_range("all")
    return resolve_range("today")


def _ai_activity(text, user):
    t = text.lower()
    asks_activity = any(k in t for k in (
        "manager", "employee", "staff", "activity", "worked", "actions",
        "what work", "what was done", "who changed", "who edited", "who added"
    ))
    if not asks_activity:
        return None
    conn = db.get_db()
    # In Work AI Owner Power Mode, activity visibility is owner-level for the
    # AI conversation only. It does not change the user's actual app role.
    if app.config.get("WORK_AI_OWNER_MODE"):
        where = "1=1"
        params = []
    elif user["role"] == "owner":
        where = "1=1"
        params = []
    else:
        where = "role = ?"
        params = ["employee"]

    range_key, start, end = _ai_range_from_text(text)
    where += " AND ts >= ? AND ts < ?"
    params.extend([start.strftime(DATE_FMT), end.strftime(DATE_FMT)])
    rows = conn.execute(
        f"SELECT display_name, username, role, action, ts FROM activity_log WHERE {where} ORDER BY id DESC LIMIT 40",
        tuple(params),
    ).fetchall()
    # Sales are also part of the work record. This lets an owner ask what a
    # manager actually did, not just what buttons they clicked.
    sales_where = "ts >= ? AND ts < ?"
    sales_params = [start.strftime(DATE_FMT), end.strftime(DATE_FMT)]
    if user["role"] == "manager" and not app.config.get("WORK_AI_OWNER_MODE"):
        sales_where += " AND username IN (SELECT username FROM users WHERE role = 'employee')"
    sale_rows = conn.execute(
        f"SELECT username, COUNT(*) AS transactions, SUM(line_total) AS revenue, SUM(quantity) AS units FROM sales WHERE {sales_where} GROUP BY username ORDER BY revenue DESC",
        tuple(sales_params),
    ).fetchall()
    if not rows and not sale_rows:
        return {"answer": f"I found no recorded staff activity for {range_key}."}
    lines = [f"Activity for {range_key}:"]
    for r in rows[:20]:
        lines.append(f"• {r['display_name']} ({r['role']}): {r['action']} — {r['ts']}")
    if sale_rows:
        lines.append("\nSales recorded by staff:")
        for r in sale_rows:
            staff = get_user_row(r['username'])
            label = staff['display_name'] if staff else r['username']
            lines.append(f"• {label}: {r['transactions']} transaction(s), {r['units']} unit(s), {_money(r['revenue'])}")
    return {"answer": "\n".join(lines), "activity": [dict(r) for r in rows], "staff_sales": [dict(r) for r in sale_rows]}


def _ai_business(text, user):
    t = text.lower()
    sensitive = any(k in t for k in (
        "profit", "expense", "expenses", "cost", "financial", "staff performance",
        "sales trend", "business report", "stock value", "net profit", "gross profit"
    ))
    if sensitive and not app.config.get("WORK_AI_OWNER_MODE") and user["role"] != "owner":
        return {"answer": "That business information is restricted to the Business Owner. I can still help you with products, stock availability and permitted sales information."}

    range_key, start, end = _ai_range_from_text(text)
    sales = fetch_sales(start, end)
    expenses = fetch_expenses(start, end)
    financials = compute_financials(sales, expenses)
    summary = compute_summary(sales)
    low = get_low_stock()
    products = db.get_db().execute("SELECT * FROM products WHERE active = 1 ORDER BY stock_qty DESC, name").fetchall()

    if "stock value" in t or "inventory value" in t or "value of stock" in t:
        stock_cost = round(sum(float(p["cost_price"] or 0) * int(p["stock_qty"] or 0) for p in products), 2)
        stock_retail = round(sum(float(p["selling_price"] or 0) * int(p["stock_qty"] or 0) for p in products), 2)
        potential_margin = round(stock_retail - stock_cost, 2)
        return {"answer": (
            "Current stock valuation\\n"
            f"• Cost value: {_money(stock_cost)}\\n"
            f"• Retail value: {_money(stock_retail)}\\n"
            f"• Potential gross margin: {_money(potential_margin)}\\n"
            f"• Products counted: {len(products)}"
        )}

    if any(k in t for k in ("report", "sales", "revenue", "profit", "expenses", "performance", "how much", "summary")):
        best = summary.get("best_selling_item") or "No sales yet"
        low_names = ", ".join(f"{p['name']} ({p['stock_qty']})" for p in low[:8]) or "None"
        answer = (
            f"{range_key.title()} ShopNexa report\n"
            f"• Sales/revenue: {_money(financials['revenue'])}\n"
            f"• Cost of goods: {_money(financials['cost_of_goods'])}\n"
            f"• Expenses: {_money(financials['total_expenses'])}\n"
            f"• Net profit: {_money(financials['net_profit'])}\n"
            f"• Units sold: {summary['total_units']}\n"
            f"• Best seller: {best}\n"
            f"• Low stock: {low_names}"
        )
        if low:
            answer += f"\n• Recommendation: Restock {low[0]['name']} before it reaches zero."
        return {"answer": answer, "summary": summary, "financials": financials}

    if any(k in t for k in ("low stock", "running out", "restock", "stock below", "highest stock", "most stock", "inventory")):
        if "highest" in t or "most stock" in t:
            top = list(products[:10])
            answer = "Products with the highest stock:\n" + "\n".join(f"• {p['name']}: {p['stock_qty']}" for p in top) if top else "There are no products yet."
        else:
            answer = "Low-stock products:\n" + "\n".join(f"• {p['name']}: {p['stock_qty']} left (minimum {p['min_stock']})" for p in low[:20]) if low else "No products are currently below their minimum stock level."
        return {"answer": answer}

    if any(k in t for k in ("best seller", "selling best", "top product", "most sold")):
        top = get_top_products(sales, 10)
        answer = "Top products:\n" + "\n".join(f"• {p['item']}: {p['units']} units, {_money(p['revenue'])}" for p in top) if top else f"There are no recorded sales for {range_key}."
        return {"answer": answer}

    if any(k in t for k in ("recommend", "suggest")) and ("product" in t or "customer" in t or "purchase" in t):
        top = get_top_products(sales, 8)
        sold_names = {x["item"].lower() for x in top}
        candidates = [p for p in products if p["name"].lower() not in sold_names and p["stock_qty"] > 0]
        candidates = candidates[:5] if candidates else list(products[:5])
        if candidates:
            return {"answer": "Suggested products with stock available:\\n" + "\\n".join(
                f"• {p['name']} — {_money(p['selling_price'])}, {p['stock_qty']} in stock"
                for p in candidates
            )}
        return {"answer": "There are no stocked products available to recommend right now."}

    if any(k in t for k in ("product", "find", "available", "price", "how many")):
        rows = db.get_db().execute("SELECT * FROM products WHERE active = 1 ORDER BY name").fetchall()
        # Exact or partial product lookup first.
        matches = [p for p in rows if p["name"].lower() in t or p["category"].lower() in t]
        if matches:
            answer = "Product information:\n" + "\n".join(
                f"• {p['name']} — {_money(p['selling_price'])}, {p['stock_qty']} in stock, category {p['category']}"
                for p in matches[:12]
            )
            return {"answer": answer}
        return {"answer": "Available products:\n" + "\n".join(f"• {p['name']} — {_money(p['selling_price'])}, {p['stock_qty']} in stock" for p in rows[:30]) if rows else "No products have been added yet."}

    if any(k in t for k in ("staff", "manager", "employee")):
        if user["role"] == "manager" and not app.config.get("WORK_AI_OWNER_MODE"):
            rows = db.get_db().execute("SELECT username, display_name, role FROM users WHERE role = 'employee' ORDER BY display_name").fetchall()
        else:
            rows = db.get_db().execute("SELECT username, display_name, role FROM users ORDER BY role DESC, display_name").fetchall()
        return {"answer": "Accessible staff accounts:\n" + "\n".join(f"• {r['display_name']} ({r['role']})" for r in rows)}
    return None


def _ai_should_search(text):
    # Work AI web search is ON for every question in this build. This removes
    # keyword gating so users can ask naturally without saying "search web".
    return True


def _ai_web_query(text):
    t = text.strip()
    prefixes = ("search the web for ", "search online for ", "search for ", "look up ", "find online ")
    low = t.lower()
    for p in prefixes:
        if low.startswith(p):
            t = t[len(p):].strip()
            break

    # Web search is intentionally always enabled, but credential-like values
    # must never leave the ShopNexa server.
    t = re.sub(
        r"(?i)(password|passwd|secret|api[_ -]?key|token|csrf[_ -]?token|session(?:[_ -]?cookie)?)"
        r"\s*(?:is|=|:)?\s*[^,;\n]+",
        r"\1 [redacted]",
        t,
    )
    return t[:500]


@app.route("/work-ai")
@login_required
def work_ai():
    username, user = current_user()
    settings = get_settings()
    range_key, start, end = resolve_range("today")
    sales = fetch_sales(start, end)
    expenses = fetch_expenses(start, end)
    financials = compute_financials(sales, expenses)
    summary = compute_summary(sales)
    low = get_low_stock()
    top = get_top_products(sales, 5)
    staff_preview = []
    if user["role"] in ("manager", "owner") or app.config.get("WORK_AI_OWNER_MODE"):
        conn = db.get_db()
        where = "role = ?" if user["role"] == "manager" and not app.config.get("WORK_AI_OWNER_MODE") else "1=1"
        params = ["employee"] if user["role"] == "manager" and not app.config.get("WORK_AI_OWNER_MODE") else []
        rows = conn.execute(
            f"SELECT display_name, role, action, ts FROM activity_log WHERE {where} ORDER BY id DESC LIMIT 5",
            tuple(params),
        ).fetchall()
        staff_preview = [dict(r) for r in rows]
    overview = {
        "revenue": financials.get("revenue", 0),
        "expenses": financials.get("total_expenses", 0),
        "profit": financials.get("net_profit", 0),
        "transactions": len(sales),
        "best_seller": summary.get("best_selling_item") or "No sales yet",
        "best_seller_units": next((x.get("units", 0) for x in top if x.get("item") == summary.get("best_selling_item")), 0),
        "low_stock": [{"name": p["name"], "stock": p["stock_qty"]} for p in low[:4]],
        "staff_preview": staff_preview,
    }
    return render_template("work_ai.html", user=user, settings=settings, ai_overview=overview)


# ---------------------------------------------------------------------------
# Work AI: real command execution through OpenAI Responses + local business tools
# ---------------------------------------------------------------------------
WORK_AI_TOOL_DEFS = [
    {"type":"function","name":"list_products","description":"List active ShopNexa products and their current price and stock.","parameters":{"type":"object","properties":{},"additionalProperties":False}},
    {"type":"function","name":"add_product","description":"Create a new product. Use when the user asks to add/create a product.","parameters":{"type":"object","properties":{"name":{"type":"string"},"category":{"type":"string"},"selling_price":{"type":"number"},"cost_price":{"type":"number"},"stock_qty":{"type":"integer"},"min_stock":{"type":"integer"}},"required":["name","selling_price","stock_qty"],"additionalProperties":False}},
    {"type":"function","name":"edit_product","description":"Edit an existing product by id or exact name. Only supplied fields change.","parameters":{"type":"object","properties":{"product_id":{"type":"integer"},"name":{"type":"string"},"category":{"type":"string"},"selling_price":{"type":"number"},"cost_price":{"type":"number"},"stock_qty":{"type":"integer"},"min_stock":{"type":"integer"}},"required":["product_id"],"additionalProperties":False}},
    {"type":"function","name":"remove_product","description":"Remove/deactivate an active product by id.","parameters":{"type":"object","properties":{"product_id":{"type":"integer"}},"required":["product_id"],"additionalProperties":False}},
    {"type":"function","name":"record_sale","description":"Record a sale and reduce stock. Use exact product ids and quantities; selling price may be omitted to use the current price.","parameters":{"type":"object","properties":{"lines":{"type":"array","items":{"type":"object","properties":{"product_id":{"type":"integer"},"quantity":{"type":"integer"},"unit_price":{"type":"number"}},"required":["product_id","quantity"],"additionalProperties":False}}},"required":["lines"],"additionalProperties":False}},
    {"type":"function","name":"add_expense","description":"Record a business expense.","parameters":{"type":"object","properties":{"category":{"type":"string","enum":db.EXPENSE_CATEGORIES},"amount":{"type":"number"},"note":{"type":"string"}},"required":["category","amount"],"additionalProperties":False}},
    {"type":"function","name":"delete_expense","description":"Delete an expense by id.","parameters":{"type":"object","properties":{"expense_id":{"type":"integer"}},"required":["expense_id"],"additionalProperties":False}},
    {"type":"function","name":"create_staff","description":"Create an employee or manager account without handling a password. The account is created with a random temporary credential and must be finalized through the secure Backstage password workflow. Never ask the model to collect or return passwords.","parameters":{"type":"object","properties":{"username":{"type":"string"},"display_name":{"type":"string"},"role":{"type":"string","enum":["employee","manager"]}},"required":["username","role"],"additionalProperties":False}},
    {"type":"function","name":"list_staff","description":"List staff accounts and roles. Never returns passwords, hashes, reset tokens or session data.","parameters":{"type":"object","properties":{},"additionalProperties":False}},
    {"type":"function","name":"change_staff_role","description":"Change a staff account role between employee, manager and owner.","parameters":{"type":"object","properties":{"username":{"type":"string"},"role":{"type":"string","enum":["employee","manager","owner"]}},"required":["username","role"],"additionalProperties":False}},
    {"type":"function","name":"remove_staff","description":"Remove a staff account other than the currently signed-in account.","parameters":{"type":"object","properties":{"username":{"type":"string"}},"required":["username"],"additionalProperties":False}},
    {"type":"function","name":"update_shop_settings","description":"Update ShopNexa public shop name and/or currency.","parameters":{"type":"object","properties":{"shop_name":{"type":"string"},"currency":{"type":"string"}},"additionalProperties":False}},
    {"type":"function","name":"business_report","description":"Return sales, expenses, profit, transactions and top products for a period.","parameters":{"type":"object","properties":{"range":{"type":"string","enum":["today","yesterday","week","month","all"]}},"additionalProperties":False}},
    {"type":"function","name":"staff_activity","description":"Return recent staff activity. Owner Power Mode allows the AI to see all staff activity.","parameters":{"type":"object","properties":{"limit":{"type":"integer","minimum":1,"maximum":100}},"additionalProperties":False}},
    {"type":"function","name":"reset_today_sales","description":"Clear today's sales and restore the stock consumed by those sales. This is destructive; only do it when the user explicitly asks to reset/clear today's sales.","parameters":{"type":"object","properties":{"confirm":{"type":"boolean"}},"required":["confirm"],"additionalProperties":False}},
    {"type":"function","name":"open_page","description":"Open a ShopNexa page in the browser after the user asks to go/open a section.","parameters":{"type":"object","properties":{"page":{"type":"string","enum":["sales","products","expenses","staff","reports","backstage","team","history","work-ai","welcome"]}},"required":["page"],"additionalProperties":False}},
    {"type":"function","name":"generate_image","description":"Generate a ShopNexa image from a creative prompt. Use when the user asks the AI to create, draw, design or generate an image.","parameters":{"type":"object","properties":{"prompt":{"type":"string"},"size":{"type":"string","enum":["1024x1024","1024x1536","1536x1024","auto"]},"quality":{"type":"string","enum":["low","medium","high","auto"]}},"required":["prompt"],"additionalProperties":False}},
    {"type":"function","name":"generate_video","description":"Start a ShopNexa AI video generation job from a creative prompt. Use when the user asks the AI to create or generate a video.","parameters":{"type":"object","properties":{"prompt":{"type":"string"},"seconds":{"type":"string","enum":["4","8","12"]},"size":{"type":"string","enum":["720x1280","1280x720","1024x1792","1792x1024"]}},"required":["prompt"],"additionalProperties":False}},
    {"type":"function","name":"search_media","description":"Search the public web for images or videos. Use when the user asks to find, search for, browse or show online images/videos, not only products.","parameters":{"type":"object","properties":{"query":{"type":"string"},"media_type":{"type":"string","enum":["image","video","web"]},"limit":{"type":"integer","minimum":1,"maximum":10}},"required":["query","media_type"],"additionalProperties":False}},
]

WORK_AI_PAGE_URLS = {"sales":"/", "products":"/products", "expenses":"/expenses", "staff":"/staff", "reports":"/reports", "backstage":"/backstage", "team":"/team", "history":"/history", "work-ai":"/work-ai", "welcome":"/welcome"}

def _ai_owner_power_allowed(user):
    # Work AI's business-operation tools (add/edit/delete products, sales,
    # expenses, staff role changes, staff removal, shop settings, and
    # destructive resets) carry the same authority as the Owner-only
    # /backstage routes. WORK_AI_OWNER_MODE only controls whether that
    # authority is exposed through the AI at all — it must never substitute
    # for an actual role check, or any signed-in employee/manager could ask
    # the AI to do things like "make me the owner" or "remove staff X" and
    # have it execute, bypassing @owner_required entirely.
    return bool(user) and user.get("role") == "owner" and bool(app.config.get("WORK_AI_OWNER_MODE"))

def _ai_product_row(conn, product_id):
    return conn.execute("SELECT * FROM products WHERE id = ? AND active = 1", (int(product_id),)).fetchone()

def _ai_tool_execute(name, args, user):
    conn = db.get_db()
    if not _ai_owner_power_allowed(user):
        return {"ok":False,"error":"Work AI owner operations are disabled for this account."}
    try:
        if name == "list_products":
            rows = conn.execute("SELECT id,name,category,selling_price,cost_price,stock_qty,min_stock FROM products WHERE active=1 ORDER BY LOWER(name)").fetchall()
            return {"ok":True,"products":[dict(r) for r in rows]}
        if name == "add_product":
            data={"name":args.get("name",""),"category":args.get("category","General"),"selling_price":args.get("selling_price"),"cost_price":args.get("cost_price",0),"stock_qty":args.get("stock_qty",0),"min_stock":args.get("min_stock",0)}
            product,error=_parse_product_form(data)
            if error:return {"ok":False,"error":error}
            if conn.execute("SELECT id FROM products WHERE name=? AND active=1",(product["name"],)).fetchone():return {"ok":False,"error":f"'{product['name']}' already exists."}
            conn.execute("INSERT INTO products (name,category,selling_price,cost_price,stock_qty,min_stock,active,created_at) VALUES (?,?,?,?,?,?,1,?)",(product["name"],product["category"],product["selling_price"],product["cost_price"],product["stock_qty"],product["min_stock"],datetime.now().strftime(DATE_FMT)))
            conn.commit(); row=conn.execute("SELECT * FROM products WHERE name=? AND active=1",(product["name"],)).fetchone(); log_activity(f"Work AI added product '{product['name']}'")
            return {"ok":True,"product":dict(row)}
        if name == "edit_product":
            row=_ai_product_row(conn,args.get("product_id"))
            if not row:return {"ok":False,"error":"Product not found."}
            data={k:args[k] for k in ("name","category","selling_price","cost_price","stock_qty","min_stock") if k in args}
            merged={"name":data.get("name",row["name"]),"category":data.get("category",row["category"]),"selling_price":data.get("selling_price",row["selling_price"]),"cost_price":data.get("cost_price",row["cost_price"]),"stock_qty":data.get("stock_qty",row["stock_qty"]),"min_stock":data.get("min_stock",row["min_stock"])}
            product,error=_parse_product_form(merged)
            if error:return {"ok":False,"error":error}
            dup=conn.execute("SELECT id FROM products WHERE name=? AND active=1 AND id!=?",(product["name"],row["id"])).fetchone()
            if dup:return {"ok":False,"error":f"'{product['name']}' already exists."}
            conn.execute("UPDATE products SET name=?,category=?,selling_price=?,cost_price=?,stock_qty=?,min_stock=? WHERE id=?",(product["name"],product["category"],product["selling_price"],product["cost_price"],product["stock_qty"],product["min_stock"],row["id"])); conn.commit(); updated=conn.execute("SELECT * FROM products WHERE id=?",(row["id"],)).fetchone(); log_activity(f"Work AI edited product '{updated['name']}'"); return {"ok":True,"product":dict(updated)}
        if name == "remove_product":
            row=_ai_product_row(conn,args.get("product_id"));
            if not row:return {"ok":False,"error":"Product not found."}
            conn.execute("UPDATE products SET active=0 WHERE id=?",(row["id"],)); conn.commit(); log_activity(f"Work AI removed product '{row['name']}'"); return {"ok":True,"removed":row["name"]}
        if name == "record_sale":
            lines=args.get("lines") or []
            if not lines:return {"ok":False,"error":"No sale lines supplied."}
            prepared=[]
            for line in lines:
                product=_ai_product_row(conn,line.get("product_id")); qty=int(line.get("quantity",0))
                if not product:return {"ok":False,"error":"A product in the sale was not found."}
                if qty<=0:return {"ok":False,"error":"Quantity must be greater than zero."}
                if qty>product["stock_qty"]:return {"ok":False,"error":f"Only {product['stock_qty']} left for {product['name']}."}
                price=float(line.get("unit_price",product["selling_price"]))
                if price<=0:return {"ok":False,"error":"Selling price must be greater than zero."}
                prepared.append((product,qty,price))
            receipt_id=uuid.uuid4().hex[:10]; now=datetime.now().strftime(DATE_FMT); total=0
            for product,qty,price in prepared:
                line_total=round(price*qty,2); total+=line_total
                conn.execute("INSERT INTO sales (product_id,item,unit_price,cost_price,quantity,line_total,profit,username,receipt_id,ts) VALUES (?,?,?,?,?,?,?,?,?,?)",(product["id"],product["name"],price,product["cost_price"],qty,line_total,round((price-product["cost_price"])*qty,2),user["username"],receipt_id,now))
                conn.execute("UPDATE products SET stock_qty=stock_qty-? WHERE id=?",(qty,product["id"]))
            conn.commit(); log_activity(f"Work AI recorded a sale (receipt #{receipt_id})")
            return {"ok":True,"receipt_id":receipt_id,"total":round(total,2),"items":[{"name":p["name"],"quantity":q,"unit_price":pr} for p,q,pr in prepared]}
        if name == "add_expense":
            category=str(args.get("category","")); amount=float(args.get("amount",0)); note=str(args.get("note","") or "")
            if category not in db.EXPENSE_CATEGORIES:return {"ok":False,"error":"Invalid expense category."}
            if amount<=0:return {"ok":False,"error":"Amount must be greater than zero."}
            conn.execute("INSERT INTO expenses (category,amount,note,username,ts) VALUES (?,?,?,?,?)",(category,amount,note,user["username"],datetime.now().strftime(DATE_FMT))); conn.commit(); log_activity(f"Work AI recorded {category} expense of {amount:.2f}"); return {"ok":True,"category":category,"amount":amount}
        if name == "delete_expense":
            row=conn.execute("SELECT id,category,amount FROM expenses WHERE id=?",(int(args.get("expense_id")),)).fetchone()
            if not row:return {"ok":False,"error":"Expense not found."}
            conn.execute("DELETE FROM expenses WHERE id=?",(row["id"],)); conn.commit(); log_activity(f"Work AI removed {row['category']} expense of {row['amount']:.2f}"); return {"ok":True,"removed":dict(row)}
        if name == "create_staff":
            username=str(args.get("username","")).strip(); display=str(args.get("display_name","")).strip() or username; role=str(args.get("role","employee"))
            if not username:return {"ok":False,"error":"Username is required."}
            if role not in ("employee","manager"):return {"ok":False,"error":"AI can create employee or manager accounts; owner accounts must be created through Backstage."}
            if conn.execute("SELECT 1 FROM users WHERE username=?",(username,)).fetchone():return {"ok":False,"error":"That username already exists."}
            temp_password=secrets.token_urlsafe(18)
            conn.execute("INSERT INTO users (username,password_hash,role,display_name) VALUES (?,?,?,?)",(username,generate_password_hash(temp_password),role,display)); conn.commit(); log_activity(f"Work AI created staff account '{username}' as {role}")
            # Do not return the temporary credential to the model. The account can be given a password by the secure Backstage workflow.
            return {"ok":True,"username":username,"display_name":display,"role":role,"password_set":False,"message":"Account created. Set its login password through the secure Backstage password workflow; no password was exposed to Work AI."}
        if name == "list_staff":
            rows=conn.execute("SELECT username,display_name,role FROM users ORDER BY role DESC,display_name").fetchall(); return {"ok":True,"staff":[dict(r) for r in rows]}
        if name == "change_staff_role":
            username=str(args.get("username","")); role=str(args.get("role","")); target=conn.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
            if not target:return {"ok":False,"error":"Staff account not found."}
            if role not in ("employee","manager","owner"):return {"ok":False,"error":"Invalid role."}
            if username==user["username"] and role!="owner":return {"ok":False,"error":"You cannot demote the account currently signed in."}
            if target["role"]=="owner" and role!="owner":
                owners=conn.execute("SELECT COUNT(*) c FROM users WHERE role='owner' AND username!=?",(username,)).fetchone()["c"]
                if not owners:return {"ok":False,"error":"Cannot demote the last owner."}
            conn.execute("UPDATE users SET role=? WHERE username=?",(role,username)); conn.commit(); log_activity(f"Work AI changed '{username}' role to {role}"); return {"ok":True,"username":username,"role":role}
        if name == "remove_staff":
            username=str(args.get("username","")); target=conn.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
            if not target:return {"ok":False,"error":"Staff account not found."}
            if username==user["username"]:return {"ok":False,"error":"You cannot remove the account you are signed in with."}
            if target["role"]=="owner":
                owners=conn.execute("SELECT COUNT(*) c FROM users WHERE role='owner' AND username!=?",(username,)).fetchone()["c"]
                if not owners:return {"ok":False,"error":"Cannot remove the last owner."}
            conn.execute("DELETE FROM users WHERE username=?",(username,)); conn.commit(); log_activity(f"Work AI removed staff account '{username}'"); return {"ok":True,"removed":username}
        if name == "update_shop_settings":
            shop_name=args.get("shop_name"); currency=args.get("currency");
            if shop_name is not None and str(shop_name).strip(): conn.execute("UPDATE settings SET value=? WHERE key='shop_name'",(str(shop_name).strip(),))
            if currency is not None and str(currency).strip(): conn.execute("UPDATE settings SET value=? WHERE key='currency'",(str(currency).strip(),))
            conn.commit(); log_activity("Work AI updated shop settings"); return {"ok":True,"settings":get_settings()}
        if name == "business_report":
            rk=str(args.get("range","today")); rk,start,end=resolve_range(rk); sales=fetch_sales(start,end); expenses=fetch_expenses(start,end); return {"ok":True,"range":rk,"summary":compute_summary(sales),"financials":compute_financials(sales,expenses),"top_products":get_top_products(sales,10),"expenses":expenses[:50]}
        if name == "staff_activity":
            limit=max(1,min(100,int(args.get("limit",30)))); rows=conn.execute("SELECT id,username,display_name,role,action,ts FROM activity_log ORDER BY id DESC LIMIT ?",(limit,)).fetchall(); return {"ok":True,"activity":[dict(r) for r in rows]}
        if name == "reset_today_sales":
            if not bool(args.get("confirm")):return {"ok":False,"error":"Confirmation is required to reset today's sales."}
            _,start,end=resolve_range("today"); todays=conn.execute("SELECT * FROM sales WHERE ts>=? AND ts<?",(start.strftime(DATE_FMT),end.strftime(DATE_FMT))).fetchall()
            for sale in todays:
                if sale["product_id"] is not None: conn.execute("UPDATE products SET stock_qty=stock_qty+? WHERE id=?",(sale["quantity"],sale["product_id"]))
            conn.execute("DELETE FROM sales WHERE ts>=? AND ts<?",(start.strftime(DATE_FMT),end.strftime(DATE_FMT))); conn.commit(); log_activity(f"Work AI cleared today's sales ({len(todays)} transaction(s))"); return {"ok":True,"cleared_transactions":len(todays)}
        if name == "generate_image":
            prompt=str(args.get("prompt","")).strip()
            if not prompt:return {"ok":False,"error":"Image prompt is required."}
            if not OPENAI_API_KEY:return {"ok":False,"error":"OPENAI_API_KEY is not configured in Render."}
            size=args.get("size","1024x1024"); quality=args.get("quality","auto")
            if size not in {"1024x1024","1024x1536","1536x1024","auto"}: size="1024x1024"
            if quality not in {"low","medium","high","auto"}: quality="auto"
            headers=_openai_headers(); headers["Content-Type"]="application/json"
            rr=requests.post(f"{OPENAI_API_BASE}/images/generations",headers=headers,json={"model":WORK_AI_IMAGE_MODEL,"prompt":prompt,"size":size,"quality":quality,"output_format":"png"},timeout=120)
            if not rr.ok:
                try: detail=rr.json().get("error",{}).get("message",rr.text[:500])
                except Exception: detail=rr.text[:500]
                return {"ok":False,"error":detail}
            item=(rr.json().get("data") or [{}])[0]; b64=item.get("b64_json")
            if not b64:return {"ok":False,"error":"The image provider returned no image data."}
            filename=_safe_media_name("shopnexa_ai","png"); (MEDIA_DIR/filename).write_bytes(base64.b64decode(b64)); log_activity("Work AI generated an image")
            return {"ok":True,"type":"image","url":_media_public_url(filename),"filename":filename}
        if name == "search_media":
            query=str(args.get("query","" )).strip()[:500]
            media_type=str(args.get("media_type","web"))
            limit=max(1,min(int(args.get("limit",5)),10))
            if not query:return {"ok":False,"error":"Search query is required."}
            q=query
            if media_type == "video": q += " site:youtube.com OR site:vimeo.com"
            elif media_type == "image": q += " images photos"
            results=search_web(q,limit)
            return {"ok":True,"type":"media_search","media_type":media_type,"query":query,"results":results}
        if name == "generate_video":
            prompt=str(args.get("prompt","")).strip()
            if not prompt:return {"ok":False,"error":"Video prompt is required."}
            if not OPENAI_API_KEY:return {"ok":False,"error":"OPENAI_API_KEY is not configured in Render."}
            seconds=str(args.get("seconds","4")); size=str(args.get("size","1280x720")); model=WORK_AI_VIDEO_MODEL if WORK_AI_VIDEO_MODEL in {"sora-2","sora-2-pro"} else "sora-2"
            if seconds not in {"4","8","12"}: seconds="4"
            if size not in {"720x1280","1280x720","1024x1792","1792x1024"}: size="1280x720"
            rr=requests.post(f"{OPENAI_API_BASE}/videos",headers=_openai_headers(),files={"prompt":(None,prompt),"model":(None,model),"seconds":(None,seconds),"size":(None,size)},timeout=60)
            if not rr.ok:
                try: detail=rr.json().get("error",{}).get("message",rr.text[:500])
                except Exception: detail=rr.text[:500]
                return {"ok":False,"error":detail}
            body=rr.json(); vid=body.get("id")
            if not vid:return {"ok":False,"error":"The video provider returned no job id."}
            log_activity("Work AI started a video generation job")
            return {"ok":True,"type":"video","id":vid,"status":body.get("status","queued"),"progress":body.get("progress",0),"url":url_for("work_ai_video_status",video_id=vid)}
        if name == "open_page":
            page=args.get("page"); return {"ok":True,"page":page,"url":WORK_AI_PAGE_URLS.get(page,"/")}
        return {"ok":False,"error":f"Unknown tool: {name}"}
    except Exception as exc:
        conn.rollback()
        app.logger.exception("Work AI tool failed: %s", name)
        return {"ok":False,"error":str(exc)}

def _responses_request(payload):
    headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"}
    r=requests.post(f"{OPENAI_API_BASE}/responses",headers=headers,json=payload,timeout=90)
    if not r.ok:
        try: detail=r.json().get("error",{}).get("message",r.text[:800])
        except Exception: detail=r.text[:800]
        raise RuntimeError(detail)
    return r.json()

def _responses_output_text(body):
    if body.get("output_text"): return body["output_text"]
    chunks=[]
    for item in body.get("output",[]):
        if item.get("type")=="message":
            for c in item.get("content",[]):
                if c.get("type") in ("output_text","text") and c.get("text"): chunks.append(c["text"])
    return "\n".join(chunks).strip()

def _fallback_command(message, user):
    """Handle a small set of safe, explicit commands when the model API is unavailable.

    This keeps the assistant useful for common shop operations while full natural-language
    command planning remains available when OPENAI_API_KEY is configured.
    """
    t = re.sub(r"\s+", " ", message.strip())
    low = t.lower()
    conn = db.get_db()

    if low in {"list products", "show products", "show all products", "what products do we have", "what products are available"}:
        return _ai_tool_execute("list_products", {}, user)

    m = re.match(r"^(?:add|create) (?:a )?(?:product )?(.+?) (?:at|for)\s+(?:ghs\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:with|and)\s+([0-9]+)\s*(?:in stock|stock)?$", low, re.I)
    if m:
        name = t[m.start(1):m.end(1)].strip()
        return _ai_tool_execute("add_product", {"name": name, "selling_price": float(m.group(2)), "stock_qty": int(m.group(3))}, user)

    m = re.match(r"^(?:sell|record|record a sale of)\s+([0-9]+)\s+(.+)$", t, re.I)
    if m:
        qty = int(m.group(1)); name = m.group(2).strip().rstrip(".")
        row = conn.execute("SELECT * FROM products WHERE active=1 AND LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not row:
            rows = conn.execute("SELECT * FROM products WHERE active=1 ORDER BY LOWER(name)").fetchall()
            matches = [r for r in rows if name.lower() in r["name"].lower()]
            row = matches[0] if matches else None
        if not row:
            return {"ok": False, "error": f"I could not find a product named '{name}'. Use 'list products' first."}
        return _ai_tool_execute("record_sale", {"lines": [{"product_id": row["id"], "quantity": qty}]}, user)

    m = re.match(r"^(?:remove|delete) (?:product )?(.+)$", t, re.I)
    if m and (low.startswith("remove ") or low.startswith("delete ")):
        name = m.group(1).strip().rstrip(".")
        row = conn.execute("SELECT * FROM products WHERE active=1 AND LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not row:
            return {"ok": False, "error": f"I could not find a product named '{name}'."}
        return _ai_tool_execute("remove_product", {"product_id": row["id"]}, user)

    # Edit product: edit PRODUCT field=value...
    m = re.match(r"^(?:edit|update|change) (?:product )?(.+?)\s+(?:price|selling price)\s*(?:to|=)\s*(?:ghs\s*)?([0-9]+(?:\.[0-9]+)?)$", t, re.I)
    if m:
        name=m.group(1).strip(); price=float(m.group(2))
        row=conn.execute("SELECT * FROM products WHERE active=1 AND LOWER(name)=LOWER(?)",(name,)).fetchone()
        if not row:
            rows=conn.execute("SELECT * FROM products WHERE active=1 ORDER BY name").fetchall(); row=next((r for r in rows if name.lower() in r["name"].lower()),None)
        if not row:return {"ok":False,"error":f"I could not find a product named '{name}'."}
        return _ai_tool_execute("edit_product", {"product_id":row["id"],"selling_price":price}, user)

    # Add an expense: expense CATEGORY AMOUNT [note]
    m = re.match(r"^(?:add|record) (?:an )?expense(?: of)?\s+(?:ghs\s*)?([0-9]+(?:\.[0-9]+)?)\s*(?:for|in)?\s*(.*)$", t, re.I)
    if m:
        amount=float(m.group(1)); tail=m.group(2).strip()
        category=next((c for c in db.EXPENSE_CATEGORIES if c.lower() in tail.lower()), db.EXPENSE_CATEGORIES[0])
        return _ai_tool_execute("add_expense", {"category":category,"amount":amount,"note":tail}, user)

    if low in {"today's report","todays report","today report","daily report","show report"} or low.startswith("report for today"):
        return _ai_tool_execute("business_report", {"range":"today"}, user)
    if "weekly report" in low or "report for this week" in low or "this week report" in low:
        return _ai_tool_execute("business_report", {"range":"week"}, user)
    if "monthly report" in low or "report for this month" in low or "this month report" in low:
        return _ai_tool_execute("business_report", {"range":"month"}, user)

    # Staff listing is safe; account mutations remain owner-power tools.
    if low in {"list staff","show staff","show employees","show managers","staff list"}:
        return _ai_tool_execute("list_staff", {}, user)

    m = re.match(r"^(?:create|add) (?:an )?(employee|manager)\s+([A-Za-z0-9_.-]+)(?:\s+named\s+(.+))?$", t, re.I)
    if m:
        role=m.group(1).lower(); username=m.group(2); display=(m.group(3) or username).strip()
        return _ai_tool_execute("create_staff", {"username":username,"display_name":display,"role":role}, user)

    m = re.match(r"^(?:open|go to|show me)\s+(sales|products|expenses|staff|reports|backstage|team|history|work ai|welcome)$", low)
    if m:
        page = m.group(1).replace(" ", "-")
        return _ai_tool_execute("open_page", {"page": page}, user)

    return None


def _openai_web_search(query, media_type="web", limit=8):
    """Use OpenAI's built-in web_search tool for reliable public search.
    Returns normalized sources and answer text. Falls back to local search if unavailable.
    """
    query = (query or "").strip()[:1000]
    if not query:
        return {"answer":"", "sources":[]}
    if not OPENAI_API_KEY:
        results = search_web(query, limit)
        return {"answer":"", "sources":results}
    search_query = query
    tool = {"type":"web_search"}
    if media_type == "video":
        search_query += " (YouTube OR Vimeo)"
        tool["filters"] = {"allowed_domains":["youtube.com","vimeo.com"]}
    elif media_type == "image":
        search_query += " images photos"
    payload = {
        "model": WORK_AI_MODEL,
        "store": False,
        "tools": [tool],
        "input": search_query,
    }
    try:
        body = _responses_request(payload)
        text = _responses_output_text(body)
        sources=[]; seen=set()
        for item in body.get("output", []):
            for content in item.get("content", []) if isinstance(item,dict) else []:
                for ann in content.get("annotations", []) if isinstance(content,dict) else []:
                    url = ann.get("url") or ann.get("source_url")
                    if url and url not in seen:
                        seen.add(url); sources.append({"title":ann.get("title") or url,"url":url,"snippet":""})
        # Some Responses payloads expose sources on the web_search_call action.
        for item in body.get("output", []):
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                for src in action.get("sources", []) or []:
                    url=src.get("url")
                    if url and url not in seen:
                        seen.add(url); sources.append({"title":url,"url":url,"snippet":""})
        return {"answer":text, "sources":sources[:limit]}
    except Exception as exc:
        app.logger.warning("OpenAI web search failed: %s", exc)
        results = search_web(query, limit)
        return {"answer":"", "sources":results, "error":str(exc)}

def _work_ai_openai(message,user,attachments=None):
    system=("You are ShopNexa Work AI, an owner-power business assistant. You are connected to the authenticated ShopNexa account. "
            "You can READ and MODIFY business data through the supplied tools: products, sales, expenses, staff roles, settings, reports and navigation. "
            "Treat natural-language requests as commands. If the user says 'add', 'change', 'edit', 'sell', 'record', 'remove', 'delete', 'set', 'rename', 'open', or similar, use the matching tool instead of merely explaining how. "
            "Always inspect/list first when an exact product or staff id is unknown. Never invent ids. "
            "Do not request, reveal, store, or transmit passwords, API keys, CSRF tokens, session cookies or password hashes. Credential reset remains a dedicated security workflow. "
            "For destructive actions such as resetting today's sales, require explicit confirmation in the user's message and pass confirm=true only when clearly confirmed. "
            "Use web_search for current public information or when the user asks to search the web. Use search_media when the user asks to find online images or videos. Do not limit web/media search to products. "
            f"Current signed-in user: {user['display_name']} ({user['role']}).")
    user_input = message
    if attachments:
        content=[{"type":"input_text","text":message}]
        for a in attachments[:4]:
            name=str(a.get("name") or "attachment")[:120]
            typ=str(a.get("type") or "")
            data=a.get("data")
            if typ.startswith("image/") and isinstance(data,str) and data.startswith("data:"):
                content.append({"type":"input_image","image_url":data})
            elif isinstance(data,str):
                content.append({"type":"input_text","text":f"File {name}:\n{data[:12000]}"})
        user_input=[{"role":"user","content":content}]
    payload={"model":WORK_AI_MODEL,"store":False,"instructions":system,"tools":[{"type":"web_search"}]+WORK_AI_TOOL_DEFS,"input":user_input}
    body=_responses_request(payload)
    actions=[]
    for _ in range(6):
        calls=[x for x in body.get("output",[]) if x.get("type")=="function_call"]
        if not calls: break
        follow=[]
        for call in calls:
            try: args=json.loads(call.get("arguments") or "{}")
            except Exception: args={}
            result=_ai_tool_execute(call.get("name"),args,user)
            if result.get("type") in ("image","video") and result.get("url"):
                actions.append({"type":"media","media_type":result["type"],"url":result["url"],"id":result.get("id")})
            elif result.get("type") == "media_search":
                actions.append({"type":"media_search","media_type":result.get("media_type"),"query":result.get("query"),"results":result.get("results",[])})
            elif result.get("url") and result.get("page"):
                actions.append({"type":"navigate","url":result["url"],"page":result.get("page")})
            follow.append({"type":"function_call_output","call_id":call.get("call_id"),"output":json.dumps(result,default=str)})
        body=_responses_request({"model":WORK_AI_MODEL,"store":False,"instructions":system,"tools":[{"type":"web_search"}]+WORK_AI_TOOL_DEFS,"input":body.get("output",[])+follow})
    text=_responses_output_text(body) or "I completed the command, but the model returned no summary."
    return {"answer":text,"actions":actions,"sources":[]}

@app.route("/api/work-ai", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def work_ai_api():
    username,user=current_user()
    data=request.get_json(silent=True) or {}
    message=str(data.get("message","")).strip()
    attachments = data.get("attachments") or []
    if not message or len(message)>1200:return jsonify({"error":"Ask a question between 1 and 1200 characters."}),400
    # Handle explicit user commands and public searches deterministically first.
    # This makes the core assistant work even when the OpenAI key is missing or
    # the model/tool call fails, and prevents a web/media request from being
    # mistaken for a ShopNexa product question.
    low = message.lower()
    media_type = None
    if any(k in low for k in ("video", "videos", "youtube")) and any(k in low for k in ("search", "find", "look up", "show me")):
        media_type = "video"
    elif any(k in low for k in ("image", "images", "photo", "photos", "picture", "pictures")) and any(k in low for k in ("search", "find", "look up", "show me")):
        media_type = "image"

    if media_type:
        query = _ai_web_query(message)
        found = _openai_web_search(query, media_type=media_type, limit=8)
        results = found.get("sources", [])
        answer = found.get("answer") or (f"I searched the public web for {media_type}s." if results else "I could not find results right now. Try a broader search.")
        log_activity(f"Used Work AI media search ({media_type})")
        return jsonify({"ok":True,"mode":"media-search","answer":answer,"sources":results,"actions":[{"type":"media_search","media_type":media_type,"query":message,"results":results}]})

    if any(k in low for k in ("search the web", "search online", "look up", "find online", "on the internet", "latest", "current", "news", "market trends")):
        query = _ai_web_query(message)
        found = _openai_web_search(query, media_type="web", limit=8)
        web_results = found.get("sources", [])
        answer = found.get("answer") or ("I searched the public web." if web_results else "I could not reach a web search provider right now.")
        if web_results and not found.get("answer"):
            answer += "\n\n" + "\n".join(f"• {r['title']}\n  {r['url']}" for r in web_results)
        log_activity("Used Work AI with web search")
        return jsonify({"ok":True,"mode":"web-search","answer":answer,"sources":web_results,"actions":[]})

    fallback = _fallback_command(message, user)
    if fallback is not None:
        answer = fallback.get("message") or fallback.get("error") or json.dumps(fallback, default=str)
        log_activity("Used Work AI fallback command")
        return jsonify({"ok":bool(fallback.get("ok", True)),"mode":"fallback-command","answer":answer,"sources":[],"actions":[]})

    # Use the full model/tool agent for commands that need natural-language
    # planning. If the API fails, continue to the local read-only assistant.
    if OPENAI_API_KEY:
        try:
            result=_work_ai_openai(message,user,attachments)
            log_activity("Used Work AI command mode")
            return jsonify({"ok":True,"mode":"owner-command-ai","answer":result["answer"],"sources":result.get("sources",[]),"actions":result.get("actions",[])})
        except Exception:
            app.logger.exception("OpenAI Work AI failed")

    activity=_ai_activity(message,user)
    if activity: return jsonify({"ok":True,"mode":"shop-data","answer":activity["answer"],"sources":[]})
    internal=_ai_business(message,user)
    web_results=[]
    answer=internal["answer"] if internal else "I can use your ShopNexa data and search the public web. For full natural-language commands (add/edit/sell/delete/staff/settings) and AI media generation, configure OPENAI_API_KEY in Render."
    if not internal:
        query=_ai_web_query(message); web_results=search_web(query,5)
        if web_results: answer += "\n\nWeb sources:\n"+"\n".join(f"• {r['title']}\n  {r['snippet'][:320]}\n  {r['url']}" for r in web_results)
    log_activity("Used Work AI" + (" with web search" if web_results else ""))
    return jsonify({"ok":True,"mode":"shop-data+web" if web_results else "shop-data","answer":answer,"sources":web_results,"actions":[]})


# ---------------------------------------------------------------------------
# Work AI media studio
# ---------------------------------------------------------------------------
@app.route("/api/work-ai/media/search", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def work_ai_media_search():
    data=request.get_json(silent=True) or {}
    query=str(data.get("query","")).strip()
    media_type=str(data.get("media_type","web"))
    if not query or len(query)>500:
        return jsonify({"error":"Enter a search query between 1 and 500 characters."}),400
    if media_type not in {"web","image","video"}: media_type="web"
    try:
        found=_openai_web_search(query, media_type=media_type, limit=8)
        results=found.get("sources",[])
        log_activity(f"Work AI searched the web for {media_type}s")
        return jsonify({"ok":True,"query":query,"media_type":media_type,"results":results,"answer":found.get("answer","")})
    except Exception as exc:
        return jsonify({"error":f"Media search failed: {exc}"}),502

@app.route("/api/work-ai/media/image", methods=["POST"])
@login_required
@limiter.limit("6 per minute")
def work_ai_generate_image():
    """Generate and save an image with GPT Image 2.

    The API key stays server-side. Only the user's creative prompt is sent to
    the image provider; ShopNexa credentials and session data are never sent.
    """
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()
    size = str(data.get("size", "1024x1024"))
    quality = str(data.get("quality", "auto"))
    if not prompt or len(prompt) > 1500:
        return jsonify({"error": "Enter an image prompt between 1 and 1500 characters."}), 400
    if size not in {"1024x1024", "1024x1536", "1536x1024", "auto"}:
        size = "1024x1024"
    if quality not in {"low", "medium", "high", "auto"}:
        quality = "auto"
    try:
        headers = _openai_headers()
        headers["Content-Type"] = "application/json"
        payload = {
            "model": WORK_AI_IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
            "quality": quality,
            "output_format": "png",
        }
        r = requests.post(f"{OPENAI_API_BASE}/images/generations", headers=headers, json=payload, timeout=120)
        if not r.ok:
            try:
                detail = r.json().get("error", {}).get("message", r.text[:500])
            except Exception:
                detail = r.text[:500]
            return jsonify({"error": f"Image generation failed: {detail}"}), r.status_code if 400 <= r.status_code < 500 else 502
        body = r.json()
        item = (body.get("data") or [{}])[0]
        b64 = item.get("b64_json")
        if not b64:
            return jsonify({"error": "The image provider returned no image data."}), 502
        filename = _safe_media_name("shopnexa_ai", "png")
        (MEDIA_DIR / filename).write_bytes(base64.b64decode(b64))
        log_activity("Generated an image with ShopNexa Work AI")
        return jsonify({"ok": True, "type": "image", "url": _media_public_url(filename), "filename": filename})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/work-ai/media/video", methods=["POST"])
@login_required
@limiter.limit("3 per minute")
def work_ai_generate_video():
    """Start a Sora video generation job. Video rendering is asynchronous."""
    data = request.get_json(silent=True) or {}
    prompt = str(data.get("prompt", "")).strip()
    seconds = str(data.get("seconds", "4"))
    size = str(data.get("size", "1280x720"))
    model = str(data.get("model", WORK_AI_VIDEO_MODEL))
    if not prompt or len(prompt) > 1500:
        return jsonify({"error": "Enter a video prompt between 1 and 1500 characters."}), 400
    if seconds not in {"4", "8", "12"}:
        seconds = "4"
    if model not in {"sora-2", "sora-2-pro"}:
        model = WORK_AI_VIDEO_MODEL if WORK_AI_VIDEO_MODEL in {"sora-2", "sora-2-pro"} else "sora-2"
    if size not in {"720x1280", "1280x720", "1024x1792", "1792x1024"}:
        size = "1280x720"
    try:
        headers = _openai_headers()
        # Sora's video endpoint is multipart/form-data.
        r = requests.post(
            f"{OPENAI_API_BASE}/videos",
            headers=headers,
            files={"prompt": (None, prompt), "model": (None, model), "seconds": (None, seconds), "size": (None, size)},
            timeout=60,
        )
        if not r.ok:
            try:
                detail = r.json().get("error", {}).get("message", r.text[:500])
            except Exception:
                detail = r.text[:500]
            return jsonify({"error": f"Video generation failed: {detail}"}), r.status_code if 400 <= r.status_code < 500 else 502
        body = r.json()
        video_id = body.get("id")
        if not video_id:
            return jsonify({"error": "The video provider returned no job ID."}), 502
        log_activity("Started a video generation job with ShopNexa Work AI")
        return jsonify({"ok": True, "type": "video", "id": video_id, "status": body.get("status", "queued"), "progress": body.get("progress", 0)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


def _download_sora_clip(video_id, path):
    r=requests.get(f"{OPENAI_API_BASE}/videos/{video_id}/content",headers=_openai_headers(),timeout=300)
    r.raise_for_status()
    Path(path).write_bytes(r.content)

def _run_long_video(job_id,prompt,total_seconds,size,model):
    try:
        import imageio_ffmpeg
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        clip_seconds=12
        count=(total_seconds + clip_seconds - 1)//clip_seconds
        work=MEDIA_DIR / f"long_{job_id}"
        work.mkdir(parents=True,exist_ok=True)
        clips=[]
        for i in range(count):
            actual=min(clip_seconds,total_seconds-i*clip_seconds)
            # Sora accepts 4/8/12-second clips; the final segment is rounded up
            # and trimmed during concatenation.
            gen_seconds=str(actual if actual in (4,8,12) else 12)
            segment_prompt=(f"{prompt}\n\nThis is segment {i+1} of {count}. Maintain the same characters, setting, "
                            "visual style, lighting and brand continuity as the other segments. "
                            "No recap or title card unless requested. Keep motion natural and suitable for stitching.")
            r=requests.post(f"{OPENAI_API_BASE}/videos",headers=_openai_headers(),files={
                "prompt":(None,segment_prompt),"model":(None,model),"seconds":(None,gen_seconds),"size":(None,size)},timeout=90)
            if not r.ok: raise RuntimeError(r.text[:600])
            vid=r.json().get("id")
            if not vid: raise RuntimeError("Sora returned no video id.")
            with LONG_VIDEO_LOCK: LONG_VIDEO_JOBS[job_id]["status"]="rendering"; LONG_VIDEO_JOBS[job_id]["progress"]=round(i/count*90)
            # Poll until this segment is ready.
            for _ in range(180):
                sr=requests.get(f"{OPENAI_API_BASE}/videos/{vid}",headers=_openai_headers(),timeout=30)
                if not sr.ok: raise RuntimeError(sr.text[:600])
                sb=sr.json(); st=sb.get("status")
                if st=="completed": break
                if st=="failed": raise RuntimeError((sb.get("error") or {}).get("message","Sora segment failed"))
                import time; time.sleep(5)
            else: raise RuntimeError("Timed out waiting for a Sora segment.")
            clip=work/f"clip_{i:04d}.mp4"
            _download_sora_clip(vid,clip); clips.append(clip)
        concat=work/"concat.txt"
        concat.write_text("\n".join(f"file '{str(c).replace(chr(39),chr(39)+chr(39))}'" for c in clips),encoding="utf-8")
        out=MEDIA_DIR/f"{job_id}.mp4"
        subprocess.run([ffmpeg,"-y","-f","concat","-safe","0","-i",str(concat),"-c","copy",str(out)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=900)
        with LONG_VIDEO_LOCK: LONG_VIDEO_JOBS[job_id].update(status="completed",progress=100,url=f"/api/work-ai/media/long-video/{job_id}/content")
        log_activity("Completed a long Work AI video")
    except Exception as exc:
        app.logger.exception("Long video job failed")
        with LONG_VIDEO_LOCK: LONG_VIDEO_JOBS[job_id].update(status="failed",error=str(exc))

@app.route("/api/work-ai/media/long-video", methods=["POST"])
@login_required
@limiter.limit("1 per minute")
def work_ai_long_video():
    data=request.get_json(silent=True) or {}
    prompt=str(data.get("prompt","")).strip()
    total=int(data.get("seconds",300) or 300)
    size=str(data.get("size","1280x720"))
    model=str(data.get("model",WORK_AI_VIDEO_MODEL))
    if not prompt or len(prompt)>1500:return jsonify({"error":"Enter a video prompt between 1 and 1500 characters."}),400
    if total<300 or total>1800:return jsonify({"error":"Long videos must be between 5 and 30 minutes."}),400
    if size not in {"720x1280","1280x720","1024x1792","1792x1024"}:size="1280x720"
    if model not in {"sora-2","sora-2-pro"}:model="sora-2"
    if not OPENAI_API_KEY:return jsonify({"error":"OPENAI_API_KEY is not configured on Render."}),503
    job_id="long_"+uuid.uuid4().hex
    with LONG_VIDEO_LOCK: LONG_VIDEO_JOBS[job_id]={"status":"queued","progress":0,"seconds":total,"size":size}
    threading.Thread(target=_run_long_video,args=(job_id,prompt,total,size,model),daemon=True).start()
    return jsonify({"ok":True,"id":job_id,"status":"queued","progress":0})

@app.route("/api/work-ai/media/long-video/<job_id>", methods=["GET"])
@login_required
@limiter.limit("60 per minute")
def work_ai_long_video_status(job_id):
    with LONG_VIDEO_LOCK: job=dict(LONG_VIDEO_JOBS.get(job_id) or {})
    if not job:return jsonify({"error":"Long video job not found. It may have expired after a server restart."}),404
    return jsonify(dict(id=job_id,**job))

@app.route("/api/work-ai/media/long-video/<job_id>/content", methods=["GET"])
@login_required
@limiter.limit("12 per minute")
def work_ai_long_video_content(job_id):
    if not re.fullmatch(r"long_[A-Za-z0-9]+",job_id):return jsonify({"error":"Invalid job ID."}),400
    target=MEDIA_DIR/f"{job_id}.mp4"
    if not target.exists():return jsonify({"error":"Long video is not ready."}),404
    return Response(target.read_bytes(),mimetype="video/mp4",headers={"Cache-Control":"private, max-age=3600"})

@app.route("/api/work-ai/media/video/<video_id>", methods=["GET"])
@login_required
@limiter.limit("60 per minute")
def work_ai_video_status(video_id):
    if not re.fullmatch(r"video_[A-Za-z0-9_-]+", video_id):
        return jsonify({"error": "Invalid video ID."}), 400
    try:
        r = requests.get(f"{OPENAI_API_BASE}/videos/{video_id}", headers=_openai_headers(), timeout=30)
        if not r.ok:
            try:
                detail = r.json().get("error", {}).get("message", r.text[:500])
            except Exception:
                detail = r.text[:500]
            return jsonify({"error": detail}), r.status_code if r.status_code < 500 else 502
        body = r.json()
        out = {k: body.get(k) for k in ("id", "status", "progress", "seconds", "size", "model", "error")}
        if body.get("status") == "completed":
            out["url"] = url_for("work_ai_video_content", video_id=video_id)
        return jsonify(out)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/work-ai/media/video/<video_id>/content", methods=["GET"])
@login_required
@limiter.limit("12 per minute")
def work_ai_video_content(video_id):
    if not re.fullmatch(r"video_[A-Za-z0-9_-]+", video_id):
        return jsonify({"error": "Invalid video ID."}), 400
    filename = f"{video_id}.mp4"
    target = MEDIA_DIR / filename
    if target.exists():
        return Response(target.read_bytes(), mimetype="video/mp4", headers={"Cache-Control": "private, max-age=3600"})
    try:
        r = requests.get(f"{OPENAI_API_BASE}/videos/{video_id}/content", headers=_openai_headers(), timeout=180)
        if not r.ok:
            return jsonify({"error": "Video content is not available yet."}), r.status_code if r.status_code < 500 else 502
        target.write_bytes(r.content)
        log_activity("Viewed a generated Work AI video")
        return Response(r.content, mimetype="video/mp4", headers={"Cache-Control": "private, max-age=3600"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/work-ai/media/image/<path:filename>", methods=["GET"])
@login_required
def work_ai_media_file(filename):
    # Serve only generated PNGs from the dedicated media directory.
    if not re.fullmatch(r"shopnexa_ai_[A-Za-z0-9]+\.png", filename):
        return jsonify({"error": "Invalid media filename."}), 400
    target = MEDIA_DIR / filename
    if not target.exists():
        return jsonify({"error": "Image not found."}), 404
    return Response(target.read_bytes(), mimetype="image/png", headers={"Cache-Control": "private, max-age=3600"})

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@limiter.limit("10 per minute")

@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; font-src 'self' data:; frame-ancestors 'self'"
    )
    return response

@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    message = "Your session security token expired. Refresh the page and sign in again."
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 400
    return render_template("login.html", error=message, settings=get_settings()), 400


@app.route("/health", methods=["GET"])
def health():
    """Render health endpoint with safe diagnostics only; never exposes secrets."""
    try:
        conn = db.get_db()
        conn.execute("SELECT 1").fetchone()
        database = "postgresql" if db.USING_POSTGRES else "sqlite"
        return jsonify({
            "ok": True,
            "database": database,
            "openai_configured": bool(OPENAI_API_KEY),
            "web_search_fallback": True,
            "media_directory_writable": os.access(str(MEDIA_DIR), os.W_OK),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = get_user_row(username)
        success = bool(user and check_password_hash(user["password_hash"], password))

        conn = db.get_db()
        conn.execute(
            "INSERT INTO login_log (username, ts, success, ip) VALUES (?, ?, ?, ?)",
            (username or "(blank)", datetime.now().strftime(DATE_FMT), int(success), request.remote_addr),
        )
        conn.commit()

        if success:
            session.clear()
            session.permanent = True
            session["username"] = username
            log_activity("Signed in")
            return redirect(url_for("index"))
        error = "Incorrect username or password."

    return render_template("login.html", error=error, settings=get_settings())


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
        else:
            ok, strength_error = validate_password_strength(new_pw)
            if not ok:
                error = strength_error
            elif new_pw != confirm_pw:
                error = "New password and confirmation don't match."
            else:
                conn = db.get_db()
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE username = ?",
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
    ok, strength_error = validate_password_strength(password)
    if not ok:
        return redirect(url_for("backstage", error=strength_error))
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


@app.route("/backstage/reset-password", methods=["POST"])
@owner_required
def backstage_reset_password():
    """Owner-only password reset for any other staff account, including another owner."""
    username = request.form.get("username", "").strip()
    new_password = request.form.get("new_password", "")
    reset_token = request.form.get("credential_reset_token", "")
    current_username = session.get("username", "").strip()

    # Require a separate secret in addition to the authenticated Owner session.
    configured_token = os.environ.get("CREDENTIAL_RESET_TOKEN", "").strip()
    if not configured_token:
        return redirect(url_for("backstage", error="Credential reset is disabled until CREDENTIAL_RESET_TOKEN is configured in Render."))
    if not reset_token or not hmac.compare_digest(reset_token, configured_token):
        return redirect(url_for("backstage", error="Invalid credential reset security token."))

    if not username:
        return redirect(url_for("backstage", error="Choose a staff account."))
    ok, strength_error = validate_password_strength(new_password)
    if not ok:
        return redirect(url_for("backstage", error=strength_error))

    target = get_user_row(username)
    if not target:
        return redirect(url_for("backstage", error="Staff account not found."))

    # An owner may reset another owner's password, but never their own
    # through the administrative reset control.
    if username == current_username:
        return redirect(url_for("backstage", error="Use your own Change Password option to change your password."))

    db.get_db().execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        (generate_password_hash(new_password), username),
    )
    db.get_db().commit()
    log_activity(f"Reset password for {username} (role: {target['role']})")
    return redirect(url_for("backstage", success=f"Password changed for {username}."))

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
    return render_template("welcome.html", settings=get_settings())


# ---------------------------------------------------------------------------
# Automatic product artwork: no image folder is required in GitHub.
# ---------------------------------------------------------------------------
PRODUCT_ART_RULES = {
    "egg": ("Eggs", "egg", "#D98C3F", "#FFF0D8"),
    "rice": ("Rice", "grain", "#8C6A3E", "#F6E9CF"),
    "sugar": ("Sugar", "sugar", "#A85B72", "#F8E3EA"),
    "milk": ("Milk", "dairy", "#557A68", "#E4F0E8"),
    "yogurt": ("Yogurt", "dairy", "#557A68", "#E4F0E8"),
    "bread": ("Bakery", "bakery", "#A96A35", "#F5DFC4"),
    "cake": ("Bakery", "bakery", "#A96A35", "#F5DFC4"),
    "biscuit": ("Bakery", "bakery", "#A96A35", "#F5DFC4"),
    "cookie": ("Bakery", "bakery", "#A96A35", "#F5DFC4"),
    "cola": ("Drinks", "drink", "#8A4D3D", "#F0D6CE"),
    "soda": ("Drinks", "drink", "#8A4D3D", "#F0D6CE"),
    "water": ("Water", "drink", "#4D7C73", "#DDEDEA"),
    "juice": ("Juice", "drink", "#C06D32", "#F7DFC7"),
    "drink": ("Drinks", "drink", "#8A4D3D", "#F0D6CE"),
    "beef": ("Meat", "meat", "#A04F45", "#F2D9D4"),
    "chicken": ("Meat", "meat", "#A04F45", "#F2D9D4"),
    "fish": ("Seafood", "fish", "#4D7C73", "#DDEDEA"),
    "tomato": ("Produce", "produce", "#B75B4D", "#F5D9D4"),
    "onion": ("Produce", "produce", "#8C6A3E", "#EFE3C8"),
    "potato": ("Produce", "produce", "#8C6A3E", "#EFE3C8"),
    "soap": ("Household", "household", "#557A68", "#E4F0E8"),
    "detergent": ("Household", "household", "#557A68", "#E4F0E8"),
    "phone": ("Electronics", "electronics", "#5E586B", "#E8E5ED"),
    "battery": ("Electronics", "electronics", "#5E586B", "#E8E5ED"),
}

def _product_art_kind(name, category=""):
    text = f"{name or ''} {category or ''}".lower()
    category_rules = {
        "drink": PRODUCT_ART_RULES.get("drink"), "beverage": PRODUCT_ART_RULES.get("drink"),
        "bakery": PRODUCT_ART_RULES.get("bread"), "dairy": PRODUCT_ART_RULES.get("milk"),
        "household": PRODUCT_ART_RULES.get("soap"), "electronics": PRODUCT_ART_RULES.get("phone"),
        "produce": PRODUCT_ART_RULES.get("tomato"),
    }
    for key, value in category_rules.items():
        if key in text and value:
            return value
    for key, value in PRODUCT_ART_RULES.items():
        if key in text:
            return value
    return (category.strip()[:18].title() if category else "Shop item", "bag", "#7A6652", "#EEE5D8")


def _product_svg(name, category="", shop_name="ShopNexa"):
    import html as _html
    label, kind, ink, bg = _product_art_kind(name, category)
    safe_name = _html.escape((name or "Product")[:28])
    safe_category = _html.escape((category or label or "Shop item")[:22])
    safe_shop = _html.escape((shop_name or "ShopNexa")[:24])
    common = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 520" role="img" aria-label="{safe_name} product illustration">
<rect width="720" height="520" rx="42" fill="{bg}"/>
<circle cx="620" cy="95" r="70" fill="white" opacity=".55"/>
<circle cx="105" cy="420" r="95" fill="white" opacity=".28"/>
<text x="52" y="78" font-family="Arial,sans-serif" font-size="24" font-weight="700" fill="{ink}" letter-spacing="2">{safe_shop}</text>
<text x="52" y="116" font-family="Arial,sans-serif" font-size="18" fill="{ink}" opacity=".7">{safe_category.upper()}</text>'''
    shapes = {
      'egg': '<ellipse cx="360" cy="275" rx="92" ry="126" fill="#FFFDF7" stroke="#D98C3F" stroke-width="10"/><circle cx="360" cy="300" r="38" fill="#F2B84B"/>',
      'grain': '<path d="M280 165h160l34 40v210H246V205z" fill="#FFFDF4" stroke="#8C6A3E" stroke-width="10"/><path d="M280 165h160v75H280z" fill="#D9B77A"/><path d="M320 275h80M320 310h80M320 345h80" stroke="#8C6A3E" stroke-width="12" stroke-linecap="round"/>',
      'sugar': '<path d="M285 155h150l28 34v230H257V189z" fill="#FFF7F9" stroke="#A85B72" stroke-width="10"/><path d="M285 155h150v72H285z" fill="#D98BA1"/><text x="360" y="295" text-anchor="middle" font-family="Arial" font-size="34" font-weight="700" fill="#A85B72">SUGAR</text>',
      'dairy': '<path d="M300 150h120l18 55v210H282V205z" fill="#FAFCF8" stroke="#557A68" stroke-width="10"/><path d="M300 150h120l18 55H282z" fill="#9BB8A4"/><circle cx="360" cy="290" r="42" fill="#D7E7DA"/>',
      'bakery': '<path d="M245 310c0-78 50-128 115-128s115 50 115 128c0 42-30 68-115 68s-115-26-115-68z" fill="#D89A58" stroke="#A96A35" stroke-width="10"/><path d="M285 280c25-35 55-35 80 0M345 270c25-35 55-35 80 0" fill="none" stroke="#F5DFC4" stroke-width="12" stroke-linecap="round"/>',
      'drink': '<path d="M315 155h90v45l22 28v190H293V228l22-28z" fill="#FFF8F0" stroke="#8A4D3D" stroke-width="10"/><path d="M293 260h134v92H293z" fill="#C97958"/><circle cx="360" cy="306" r="30" fill="#F0D6CE"/>',
      'meat': '<path d="M255 320c0-95 95-145 185-90 54 33 52 105 5 142-54 43-190 45-190-52z" fill="#C9796C" stroke="#A04F45" stroke-width="10"/><circle cx="370" cy="298" r="35" fill="#F2D9D4"/><circle cx="370" cy="298" r="16" fill="#A04F45"/>',
      'fish': '<path d="M225 300c80-115 230-115 310 0-80 115-230 115-310 0z" fill="#77A79B" stroke="#4D7C73" stroke-width="10"/><circle cx="475" cy="300" r="12" fill="#24312E"/><path d="M235 300l-70-60v120z" fill="#4D7C73"/>',
      'produce': '<path d="M250 230c30-70 70-75 110-40 40-35 80-30 110 40l-20 145H270z" fill="#B75B4D" stroke="#8A4D3D" stroke-width="10"/><path d="M360 190c-5-40 20-70 55-75M360 190c5-40-20-70-55-75" fill="none" stroke="#557A68" stroke-width="12" stroke-linecap="round"/>',
      'household': '<path d="M295 170h130l28 48v190H267V218z" fill="#F9FCF8" stroke="#557A68" stroke-width="10"/><path d="M295 170h130l28 48H267z" fill="#9BB8A4"/><path d="M330 285h60v65h-60z" fill="#D5E7DA"/>',
      'electronics': '<rect x="255" y="170" width="210" height="245" rx="28" fill="#3F3A49"/><rect x="275" y="190" width="170" height="195" rx="18" fill="#EEEAF2"/><circle cx="360" cy="215" r="7" fill="#8C8396"/>',
      'bag': '<path d="M255 220h210l-18 190H273z" fill="#D7C4AA" stroke="#7A6652" stroke-width="10"/><path d="M300 220c0-75 120-75 120 0" fill="none" stroke="#7A6652" stroke-width="12"/><path d="M310 285h100" stroke="#7A6652" stroke-width="10" stroke-linecap="round"/>'
    }
    return common + shapes.get(kind, shapes['bag']) + f'''<text x="360" y="470" text-anchor="middle" font-family="Arial,sans-serif" font-size="24" font-weight="700" fill="{ink}">{safe_name}</text></svg>'''

@app.get("/product-image")
def product_image():
    name = request.args.get("name", "Product")
    category = request.args.get("category", "")
    shop_name = get_settings().get("shop_name", "ShopNexa")
    return Response(_product_svg(name, category, shop_name), mimetype="image/svg+xml", headers={"Cache-Control": "no-cache, must-revalidate"})

@app.route("/")
@login_required
def index():
    username, user = current_user()
    # Render the current product list into the page as a first-class fallback.
    # The browser then refreshes it from /api/products, but a slow/failed
    # JavaScript request can no longer leave the native sales selector empty.
    rows = db.get_db().execute(
        "SELECT id, name, category, selling_price, cost_price, stock_qty, min_stock, active, created_at "
        "FROM products WHERE active = 1 ORDER BY LOWER(name)"
    ).fetchall()
    return render_template(
        "index.html", user=user, settings=get_settings(),
        initial_products=[dict(r) for r in rows]
    )


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
    if len(password) < 6:
        return redirect(url_for("team_dashboard", error="Password must be at least 6 characters."))

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
    category = str(data.get("category", "General") or "General").strip()[:60] or "General"
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
        "name": name, "category": category,
        "selling_price": selling_price, "cost_price": cost_price,
        "stock_qty": stock_qty, "min_stock": min_stock,
    }, None


@app.route("/api/products", methods=["GET"])
@login_required
def api_products():
    # Keep the sales desk in sync with the same live database used by
    # Products/Backstage.  No browser cache should be able to hide a newly
    # added or edited product.
    rows = db.get_db().execute(
        "SELECT id, name, category, selling_price, cost_price, stock_qty, min_stock, active, created_at "
        "FROM products WHERE active = 1 ORDER BY LOWER(name)"
    ).fetchall()
    response = jsonify({"products": [dict(r) for r in rows], "count": len(rows)})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


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
        "INSERT INTO products (name, category, selling_price, cost_price, stock_qty, min_stock, active, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (product["name"], product["category"], product["selling_price"], product["cost_price"],
         product["stock_qty"], product["min_stock"], datetime.now().strftime(DATE_FMT)),
    )
    conn.commit()
    new_row = conn.execute("SELECT * FROM products WHERE name = ? AND active = 1", (product["name"],)).fetchone()
    log_activity(f"Added product '{product['name']}'")
    return jsonify({"ok": True, "product": dict(new_row) if new_row else None}), 201


@app.route("/api/products/<int:product_id>", methods=["PUT"])
@owner_required
def api_edit_product(product_id):
    product, error = _parse_product_form(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400

    conn = db.get_db()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        return jsonify({"error": "Product not found."}), 404

    duplicate = conn.execute(
        "SELECT id FROM products WHERE name = ? AND active = 1 AND id != ?", (product["name"], product_id)
    ).fetchone()
    if duplicate:
        return jsonify({"error": f"'{product['name']}' already exists."}), 400

    conn.execute(
        "UPDATE products SET name = ?, category = ?, selling_price = ?, cost_price = ?, stock_qty = ?, min_stock = ? WHERE id = ?",
        (product["name"], product["category"], product["selling_price"], product["cost_price"],
         product["stock_qty"], product["min_stock"], product_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    changed = []
    for field in ("name", "category", "selling_price", "cost_price", "stock_qty", "min_stock"):
        if row[field] != updated[field]:
            changed.append(f"{field}: {row[field]} → {updated[field]}")
    detail = "; ".join(changed) if changed else "no values changed"
    log_activity(f"Edited product '{updated['name']}' ({detail})")
    return jsonify({"ok": True, "product": dict(updated) if updated else None})


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

    if not isinstance(lines_in, list) or not lines_in:
        return jsonify({"error": "Add at least one item to the basket."}), 400

    conn = db.get_db()
    prepared = []
    for line in lines_in:
        try:
            product_id = int(line.get("product_id"))
            quantity = int(line.get("quantity"))
        except (TypeError, ValueError):
            return jsonify({"error": "Each basket line needs a product and a whole-number quantity."}), 400

        if quantity <= 0:
            return jsonify({"error": "Quantity must be greater than 0"}), 400

        product = conn.execute("SELECT * FROM products WHERE id = ? AND active = 1", (product_id,)).fetchone()
        if not product:
            return jsonify({"error": "One of the basket items no longer exists."}), 400

        try:
            unit_price = float(line.get("unit_price") or product["selling_price"])
        except (TypeError, ValueError):
            return jsonify({"error": "Selling price must be a number."}), 400
        if unit_price <= 0:
            return jsonify({"error": "Selling price must be greater than 0."}), 400

        if quantity > product["stock_qty"]:
            return jsonify({"error": f"Only {product['stock_qty']} left in stock for {product['name']}."}), 400

        prepared.append({"product": product, "quantity": quantity, "unit_price": unit_price})

    # Re-check combined quantities per product (basket could list the same product twice).
    combined = {}
    for p in prepared:
        combined[p["product"]["id"]] = combined.get(p["product"]["id"], 0) + p["quantity"]
    for product_id, qty in combined.items():
        stock = conn.execute("SELECT stock_qty, name FROM products WHERE id = ?", (product_id,)).fetchone()
        if qty > stock["stock_qty"]:
            return jsonify({"error": f"Only {stock['stock_qty']} left in stock for {stock['name']}."}), 400

    receipt_id = uuid.uuid4().hex[:10]
    now = datetime.now().strftime(DATE_FMT)
    receipt_lines = []

    for p in prepared:
        product, quantity, unit_price = p["product"], p["quantity"], p["unit_price"]
        cost_price = product["cost_price"]
        line_total = round(unit_price * quantity, 2)
        conn.execute(
            "INSERT INTO sales (product_id, item, unit_price, cost_price, quantity, line_total, profit, "
            "username, receipt_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product["id"], product["name"], unit_price, cost_price, quantity, line_total,
             round((unit_price - cost_price) * quantity, 2), username, receipt_id, now),
        )
        conn.execute("UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?", (quantity, product["id"]))
        receipt_lines.append({"item": product["name"], "quantity": quantity, "unit_price": unit_price, "line_total": line_total})

    conn.commit()
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

    rows_html = ""
    for t in sales:
        profit = t.get("profit", 0.0)
        profit_color = "#34d399" if profit > 0 else ("#f87171" if profit < 0 else "#8a8478")
        rows_html += f"""
        <tr>
            <td>{t['ts']}</td>
            <td>{t['item']}</td>
            <td>{t['unit_price']:.2f}</td>
            <td>{t['quantity']}</td>
            <td>{t['line_total']:.2f}</td>
            <td style="color:{profit_color}; font-weight:600;">{profit:+.2f}</td>
        </tr>"""

    if not sales:
        rows_html = "<tr><td colspan='6' style='text-align:center;'>No transactions recorded</td></tr>"

    profit_color = "#34d399" if summary["total_profit"] > 0 else ("#f87171" if summary["total_profit"] < 0 else "#e8a855")
    profit_word = {"profit": "In profit", "loss": "Operating at a loss", "even": "Break even"}[summary["profit_status"]]
    shop_name = settings["shop_name"]
    currency = settings["currency"]

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
    <p class="eyebrow">{range_key.title()} Snapshot</p>
    <h1>{shop_name} — Sales Report</h1>
    <p class="meta">Generated {generated_at}</p>

    <div class="summary">
        <div class="box"><div class="label">Total sales</div><div class="value">{currency} {summary['total_sales']:.2f}</div></div>
        <div class="box"><div class="label">Transactions</div><div class="value">{summary['num_transactions']}</div></div>
        <div class="box"><div class="label">Units sold</div><div class="value">{summary['total_units']}</div></div>
        <div class="box"><div class="label">Best seller</div><div class="value">{summary['best_selling_item'] or 'N/A'}</div></div>
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
    app.run(debug=True)
