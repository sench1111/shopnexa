"""
db.py — database layer for ShopNexa.

Database selection is automatic:
  • Local development: SQLite at ./data/shop.db
  • Render without PostgreSQL: SQLite at /var/data/shop.db
  • PostgreSQL: set DATABASE_URL (works locally or on Render)

The rest of the Flask application uses a small compatibility wrapper so its
existing SQLite-style ? placeholders work with both SQLite and PostgreSQL.
"""

import os
import re
import sqlite3
import secrets
from datetime import datetime

from flask import g
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Database selection is intentionally automatic.
#
# Normal local run:
#   -> SQLite at ./data/shop.db
#
# Render run:
#   -> SQLite at /var/data/shop.db (the persistent disk mounted by render.yaml)
#
# PostgreSQL:
#   -> automatically selected when the hosting platform provides DATABASE_URL.
#      This keeps PostgreSQL supported without requiring the user to edit code.
#
# An explicit DATABASE_PATH/DATA_DIR can still be supplied by an administrator,
# but normal users never need to choose a database.

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_PRODUCTION = bool(
    os.environ.get("RENDER")
    or os.environ.get("RENDER_SERVICE_ID")
    or os.environ.get("FLASK_ENV") == "production"
)
IS_RENDER = bool(
    os.environ.get("RENDER")
    or os.environ.get("RENDER_SERVICE_ID")
    or os.environ.get("RENDER_INSTANCE_ID")
)

if DATABASE_URL:
    # PostgreSQL is available/configured. Use it automatically.
    DB_KIND = "postgresql"
    DB_PATH = None
else:
    DB_KIND = "sqlite"
    if IS_RENDER:
        # Only this directory is persistent when a Render disk is attached.
        DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
    else:
        DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))

    DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(DATA_DIR, "shop.db"))

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('employee', 'manager', 'owner')),
    display_name TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ts TEXT NOT NULL,
    success INTEGER NOT NULL,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT,
    action TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    selling_price REAL NOT NULL,
    cost_price REAL NOT NULL DEFAULT 0,
    stock_qty INTEGER NOT NULL DEFAULT 0,
    min_stock INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    item TEXT NOT NULL,
    unit_price REAL NOT NULL,
    cost_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    line_total REAL NOT NULL,
    profit REAL NOT NULL,
    username TEXT,
    receipt_id TEXT,
    ts TEXT NOT NULL,
    FOREIGN KEY(product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT,
    username TEXT,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_ts ON sales(ts);
CREATE INDEX IF NOT EXISTS idx_sales_receipt ON sales(receipt_id);
CREATE INDEX IF NOT EXISTS idx_expenses_ts ON expenses(ts);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('employee', 'manager', 'owner')),
    display_name TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_log (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL,
    ts TEXT NOT NULL,
    success INTEGER NOT NULL,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT,
    action TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    selling_price DOUBLE PRECISION NOT NULL,
    cost_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    stock_qty INTEGER NOT NULL DEFAULT 0,
    min_stock INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT,
    item TEXT NOT NULL,
    unit_price DOUBLE PRECISION NOT NULL,
    cost_price DOUBLE PRECISION NOT NULL,
    quantity INTEGER NOT NULL,
    line_total DOUBLE PRECISION NOT NULL,
    profit DOUBLE PRECISION NOT NULL,
    username TEXT,
    receipt_id TEXT,
    ts TEXT NOT NULL,
    FOREIGN KEY(product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS expenses (
    id BIGSERIAL PRIMARY KEY,
    category TEXT NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    note TEXT,
    username TEXT,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_ts ON sales(ts);
CREATE INDEX IF NOT EXISTS idx_sales_receipt ON sales(receipt_id);
CREATE INDEX IF NOT EXISTS idx_expenses_ts ON expenses(ts);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts);
"""

EXPENSE_CATEGORIES = ["Electricity", "Transport", "Rent", "Staff payments", "Supplies", "Other"]
ROLE_RANK = {"employee": 1, "manager": 2, "owner": 3}

# No static/default passwords are embedded in the application.
# For local development, missing passwords are generated once per process and
# printed to the terminal on first database creation. Production requires all
# three passwords to be supplied through environment variables.
LOCAL_BOOTSTRAP_PASSWORDS = {
    "employee": os.environ.get("EMPLOYEE_PASSWORD") or secrets.token_urlsafe(16),
    "manager": os.environ.get("MANAGER_PASSWORD") or secrets.token_urlsafe(16),
    "admin": os.environ.get("OWNER_PASSWORD") or secrets.token_urlsafe(16),
}
DEFAULT_USERS = [
    ("employee", LOCAL_BOOTSTRAP_PASSWORDS["employee"], "employee", "Store Assistant"),
    ("manager", LOCAL_BOOTSTRAP_PASSWORDS["manager"], "manager", "Shop Manager"),
    ("admin", LOCAL_BOOTSTRAP_PASSWORDS["admin"], "owner", "Business Owner"),
]

DEFAULT_SETTINGS = {
    "shop_name": "ShopNexa",
    "currency": "GHS",
}


class DBConnection:
    """Tiny adapter exposing the same execute/commit API for SQLite/PostgreSQL."""

    def __init__(self, raw, kind):
        self.raw = raw
        self.kind = kind

    @staticmethod
    def _convert_qmarks(sql):
        # The app's SQL uses SQLite's ? parameters. PostgreSQL uses %s.
        return sql.replace("?", "%s")

    def execute(self, sql, params=()):
        if self.kind == "sqlite":
            return self.raw.execute(sql, params)

        cur = self.raw.cursor()
        cur.execute(self._convert_qmarks(sql), params)
        return cur

    def executescript(self, sql):
        if self.kind == "sqlite":
            return self.raw.executescript(sql)
        cur = self.raw.cursor()
        cur.execute(sql)
        cur.close()

    def commit(self):
        return self.raw.commit()

    def rollback(self):
        return self.raw.rollback()

    def close(self):
        return self.raw.close()


def _connect():
    if DB_KIND == "sqlite":
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return DBConnection(conn, "sqlite")

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL was selected because DATABASE_URL is set, but "
            "psycopg2-binary is not installed. Run: pip install -r requirements.txt"
        ) from exc

    raw = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return DBConnection(raw, "postgresql")


def get_db():
    """Return a request-scoped connection cached on flask.g."""
    if "db" not in g:
        g.db = _connect()
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate(conn):
    """Compatibility migrations for existing SQLite/PostgreSQL databases."""
    if DB_KIND == "sqlite":
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "must_change_password" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sales)").fetchall()}
        if "receipt_id" not in cols:
            conn.execute("ALTER TABLE sales ADD COLUMN receipt_id TEXT")

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        if row and "'employee'" not in row["sql"]:
            conn.executescript("""
                ALTER TABLE users RENAME TO users_old;
                CREATE TABLE users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('employee', 'manager', 'owner')),
                    display_name TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO users (username,password_hash,role,display_name)
                    SELECT username,password_hash,role,display_name FROM users_old;
                DROP TABLE users_old;
            """)
    else:
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password INTEGER NOT NULL DEFAULT 0")


def init_db(app):
    """Create tables and seed default users/settings on first run."""
    if DB_KIND == "sqlite":
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = _connect()
    try:
        if DB_KIND == "sqlite":
            conn.raw.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_SQLITE)
        else:
            conn.executescript(SCHEMA_POSTGRES)

        _migrate(conn)

        user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        if user_count == 0 and IS_PRODUCTION:
            missing = [
                name for name, value in (
                    ("EMPLOYEE_PASSWORD", os.environ.get("EMPLOYEE_PASSWORD")),
                    ("MANAGER_PASSWORD", os.environ.get("MANAGER_PASSWORD")),
                    ("OWNER_PASSWORD", os.environ.get("OWNER_PASSWORD")),
                ) if not value
            ]
            if missing:
                raise RuntimeError(
                    "Production database is empty. Set strong EMPLOYEE_PASSWORD, "
                    "MANAGER_PASSWORD and OWNER_PASSWORD environment variables before first start."
                )

        if user_count == 0:
            for username, password, role, display_name in DEFAULT_USERS:
                if DB_KIND == "sqlite":
                    conn.execute(
                        "INSERT INTO users (username, password_hash, role, display_name, must_change_password) VALUES (?, ?, ?, ?, 0)",
                        (username, generate_password_hash(password), role, display_name),
                    )
                else:
                    conn.execute(
                        "INSERT INTO users (username, password_hash, role, display_name, must_change_password) VALUES (?, ?, ?, ?, 0)",
                        (username, generate_password_hash(password), role, display_name),
                    )

        # Never permit an existing production installation to continue
        # unattended if its bootstrap credentials were not configured.
        if IS_PRODUCTION and any(
            not os.environ.get(name) for name in
            ("EMPLOYEE_PASSWORD", "MANAGER_PASSWORD", "OWNER_PASSWORD")
        ):
            conn.execute("UPDATE users SET must_change_password = 1")

        for key, value in DEFAULT_SETTINGS.items():
            if DB_KIND == "sqlite":
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            else:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO NOTHING",
                    (key, value),
                )

        conn.commit()
        if user_count == 0 and not IS_PRODUCTION:
            print("Local bootstrap accounts were created. Save these credentials:")
            for username, password, _, _ in DEFAULT_USERS:
                print(f"  {username}: {password}")
    finally:
        conn.close()

    app.teardown_appcontext(close_db)
