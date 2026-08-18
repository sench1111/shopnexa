"""
ShopNexa / Sench Sales Tracker
Role-based login and backstage access.

Roles:
- Owner: full access, reports, reset, backstage, user/role overview.
- Manager: sales, transactions, reports, backstage. Cannot reset or manage access.
- Employee: record sales and view operational sales data. No reset/admin actions.

Set production credentials with environment variables:
OWNER_PASSWORD, MANAGER_PASSWORD, EMPLOYEE_PASSWORD, SESSION_SECRET.
Never hard-code real passwords in source control.
"""

from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
from datetime import datetime
from functools import wraps
import os
import csv
import io
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SESSION_SECRET", "change-this-secret-before-production")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"

# In-memory sales data (kept from the original app).
transactions = []
next_id = 1

# Local testing uses deterministic demo credentials. On Render, environment variables
# are used so production credentials can be changed without editing the code.
LOCAL_USERS = {
    "owner": "Owner@12345",
    "manager": "Manager@12345",
    "employee": "Employee@12345",
}
if os.environ.get("RENDER", "").lower() == "true":
    _RAW_USERS = {
        "owner": os.environ.get("OWNER_PASSWORD", "Owner@12345"),
        "manager": os.environ.get("MANAGER_PASSWORD", "Manager@12345"),
        "employee": os.environ.get("EMPLOYEE_PASSWORD", "Employee@12345"),
    }
else:
    _RAW_USERS = LOCAL_USERS.copy()
USERS = {role: generate_password_hash(password) for role, password in _RAW_USERS.items()}

ROLE_LABELS = {"owner": "Owner", "manager": "Manager", "employee": "Employee"}
ROLE_PERMISSIONS = {
    "owner": {
        "record_sale": True, "view_sales": True, "reports": True,
        "reset_day": True, "backstage": True, "manage_access": True,
    },
    "manager": {
        "record_sale": True, "view_sales": True, "reports": True,
        "reset_day": False, "backstage": True, "manage_access": False,
    },
    "employee": {
        "record_sale": True, "view_sales": True, "reports": False,
        "reset_day": False, "backstage": True, "manage_access": False,
    },
}


def current_role():
    role = session.get("role")
    return role if role in USERS else None


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_role():
            return jsonify({"error": "Login required", "login_required": True}), 401
        return fn(*args, **kwargs)
    return wrapped


def permission_required(permission):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            role = current_role()
            if not role:
                return jsonify({"error": "Login required", "login_required": True}), 401
            if not ROLE_PERMISSIONS[role].get(permission, False):
                return jsonify({"error": "You do not have permission for this action."}), 403
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def compute_summary():
    total_sales = 0.0
    total_units = 0
    quantity_by_item = {}
    for t in transactions:
        total_sales += t["total"]
        total_units += t["quantity"]
        quantity_by_item[t["item"]] = quantity_by_item.get(t["item"], 0) + t["quantity"]

    best_item = None
    best_qty = 0
    for item_name, qty in quantity_by_item.items():
        if qty > best_qty:
            best_qty = qty
            best_item = item_name

    return {
        "total_sales": round(total_sales, 2),
        "num_transactions": len(transactions),
        "total_units": total_units,
        "best_item": best_item,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if current_role():
            return redirect(url_for("index"))
        return render_template("login.html")

    data = request.get_json(silent=True) or request.form
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))

    if username not in USERS or not check_password_hash(USERS[username], password):
        return jsonify({"error": "Invalid username or password."}), 401

    session.clear()
    session["role"] = username
    session["login_at"] = datetime.utcnow().isoformat()
    return jsonify({
        "ok": True,
        "role": username,
        "role_label": ROLE_LABELS[username],
        "redirect": "/",
    })


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    if request.method == "GET":
        return redirect(url_for("login"))
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    role = current_role()
    if not role:
        return jsonify({"authenticated": False}), 401
    return jsonify({
        "authenticated": True,
        "username": role,
        "role": role,
        "role_label": ROLE_LABELS[role],
        "permissions": ROLE_PERMISSIONS[role],
    })


@app.route("/")
def index():
    if not current_role():
        return redirect(url_for("login"))
    return render_template("index.html")


@app.route("/backstage")
@login_required
@permission_required("backstage")
def backstage():
    return render_template("backstage.html")


@app.route("/api/backstage")
@login_required
@permission_required("backstage")
def backstage_api():
    role = current_role()
    return jsonify({
        "role": role,
        "role_label": ROLE_LABELS[role],
        "permissions": ROLE_PERMISSIONS[role],
        "available_features": [
            {"name": "Record sales", "enabled": True, "description": "Add sales and update the live dashboard."},
            {"name": "View sales", "enabled": True, "description": "View transactions, totals and best sellers."},
            {"name": "Reports", "enabled": ROLE_PERMISSIONS[role]["reports"], "description": "Download HTML and CSV sales reports."},
            {"name": "Reset day", "enabled": ROLE_PERMISSIONS[role]["reset_day"], "description": "Clear today's transactions. Owner only."},
            {"name": "Manage access", "enabled": ROLE_PERMISSIONS[role]["manage_access"], "description": "View role/access information. Owner only."},
        ]
    })


@app.route("/api/backstage/users", methods=["GET", "POST"])
@permission_required("manage_access")
def backstage_users():
    global USERS, ROLE_PERMISSIONS
    if request.method == "GET":
        return jsonify({
            "roles": [
                {"role": role, "label": ROLE_LABELS[role], "permissions": ROLE_PERMISSIONS[role]}
                for role in ("owner", "manager", "employee")
            ]
        })

    data = request.get_json(silent=True) or {}
    role = str(data.get("role", "")).strip().lower()
    if role not in USERS:
        return jsonify({"error": "Unknown role."}), 400

    # Keep the owner protected from accidental removal of critical access.
    if role == "owner":
        ROLE_PERMISSIONS["owner"] = {
            "record_sale": True, "view_sales": True, "reports": True,
            "reset_day": True, "backstage": True, "manage_access": True,
        }
    else:
        allowed_keys = {"record_sale", "view_sales", "reports", "backstage"}
        submitted = data.get("permissions") or {}
        for key in allowed_keys:
            if key in submitted:
                ROLE_PERMISSIONS[role][key] = bool(submitted[key])
        # Reset and access management remain owner-only.
        ROLE_PERMISSIONS[role]["reset_day"] = False
        ROLE_PERMISSIONS[role]["manage_access"] = False

    new_password = str(data.get("password", ""))
    if new_password:
        if len(new_password) < 8:
            return jsonify({"error": "Password must be at least 8 characters."}), 400
        USERS[role] = generate_password_hash(new_password)

    return jsonify({"ok": True, "role": role, "permissions": ROLE_PERMISSIONS[role]})


@app.route("/api/sale", methods=["POST"])
@permission_required("record_sale")
def add_sale():
    global next_id
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data received"}), 400

    item = str(data.get("item", "")).strip()
    unit_price = data.get("unit_price")
    quantity = data.get("quantity")

    if not item:
        return jsonify({"error": "Item name is required"}), 400
    try:
        unit_price = float(unit_price)
        quantity = int(quantity)
    except (TypeError, ValueError):
        return jsonify({"error": "Unit price and quantity must be numbers"}), 400
    if unit_price <= 0 or quantity <= 0:
        return jsonify({"error": "Unit price and quantity must be positive"}), 400

    transaction = {
        "id": next_id,
        "item": item[:120],
        "unit_price": round(unit_price, 2),
        "quantity": quantity,
        "total": round(unit_price * quantity, 2),
        "time": datetime.now().strftime("%H:%M:%S"),
        "sold_by": current_role(),
    }
    transactions.append(transaction)
    next_id += 1

    return jsonify({
        "transaction": transaction,
        "summary": compute_summary(),
        "transactions": transactions,
    }), 201


@app.route("/api/transactions")
@permission_required("view_sales")
def get_transactions():
    return jsonify({"transactions": transactions})


@app.route("/api/summary")
@permission_required("view_sales")
def get_summary():
    return jsonify(compute_summary())


@app.route("/api/reset", methods=["POST"])
@permission_required("reset_day")
def reset_day():
    global transactions, next_id
    transactions = []
    next_id = 1
    return jsonify({"summary": compute_summary(), "transactions": transactions})


@app.route("/api/report/csv")
@permission_required("reports")
def download_report_csv():
    today = datetime.now().strftime("%Y-%m-%d")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ID", "Item", "Unit Price", "Quantity", "Total", "Time", "Sold By"])
    for t in transactions:
        writer.writerow([
            t["id"], t["item"], t["unit_price"], t["quantity"],
            t["total"], t["time"], t.get("sold_by", "-")
        ])
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=sales_{today}.csv"},
    )


@app.route("/api/report")
@permission_required("reports")
def download_report():
    summary = compute_summary()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = ""
    for t in transactions:
        rows += f"""
        <tr><td>{t['id']}</td><td>{t['item']}</td>
        <td>${t['unit_price']:.2f}</td><td>{t['quantity']}</td>
        <td>${t['total']:.2f}</td><td>{t['time']}</td>
        <td>{t.get('sold_by', '-')}</td></tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>Sales Report {today}</title>
    <style>body{{font-family:Arial;margin:40px}}table{{width:100%;border-collapse:collapse}}
    th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}th{{background:#f4f6f8}}
    </style></head><body><h1>🛒 Sench Sales Tracker</h1>
    <p>Daily Sales Report — {today}</p>
    <p>Total Sales: ${summary['total_sales']:.2f} |
    Transactions: {summary['num_transactions']} |
    Units: {summary['total_units']} |
    Best Seller: {summary['best_item'] or '-'}</p>
    <table><thead><tr><th>#</th><th>Item</th><th>Unit Price</th><th>Qty</th>
    <th>Total</th><th>Time</th><th>Sold By</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="7">No sales recorded.</td></tr>'}</tbody>
    </table></body></html>"""
    return Response(
        html, mimetype="text/html",
        headers={"Content-Disposition": f"attachment; filename=sales_report_{today}.html"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
