"""
db.py — ShopNexa database layer.

Database selection:
- Local development: SQLite at ./data/shop.db
- Render with no DATABASE_URL: SQLite at DATA_DIR/shop.db (defaults to a writable /tmp path)
- If DATABASE_URL is set: PostgreSQL is used automatically.

The application code uses a small compatibility layer so the same Flask routes
work with either SQLite or PostgreSQL.
"""

import os
import sqlite3
from datetime import datetime

from flask import g
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Render and some providers may expose the legacy postgres:// scheme.
# psycopg2 accepts both, but normalising it makes the connection behaviour
# explicit and avoids driver/version edge cases.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USING_POSTGRES = bool(DATABASE_URL)

# SQLite path is only relevant when PostgreSQL is not selected.
# Render Free web services do not provide a writable /var/data directory
# unless a persistent disk is explicitly attached. Keep the SQLite fallback
# deployable by using a writable ephemeral directory. Production deployments
# that need persistence should set DATABASE_URL to PostgreSQL or DATA_DIR to
# a mounted persistent-disk path.
DATA_DIR = os.environ.get(
    "DATA_DIR",
    "/tmp/shopnexa_data" if os.environ.get("RENDER") else os.path.join(BASE_DIR, "data"),
)
# Never attempt to create /var/data on Render unless a persistent disk is
# explicitly mounted there. A stale environment variable can otherwise make
# the process crash before Gunicorn starts. PostgreSQL is preferred when
# DATABASE_URL is configured.
if os.environ.get("RENDER") and DATA_DIR.startswith("/var/data"):
    DATA_DIR = "/tmp/shopnexa_data"

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(DATA_DIR, "shop.db"))
if os.environ.get("RENDER") and DB_PATH.startswith("/var/data") and not USING_POSTGRES:
    DB_PATH = os.path.join(DATA_DIR, "shop.db")

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('employee', 'manager', 'owner')),
    display_name TEXT NOT NULL
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
    category TEXT NOT NULL DEFAULT 'General',
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

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('employee', 'manager', 'owner')),
    display_name TEXT NOT NULL
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
    category TEXT NOT NULL DEFAULT 'General',
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

DEFAULT_USERS = [
    ("employee", os.environ.get("EMPLOYEE_PASSWORD", "staff123"), "employee", "Store Assistant"),
    ("manager", os.environ.get("MANAGER_PASSWORD", "sales123"), "manager", "Shop Manager"),
    ("admin", os.environ.get("OWNER_PASSWORD", "change-me-now"), "owner", "Business Owner"),
]

DEFAULT_SETTINGS = {
    "shop_name": "ShopNexa",
    "currency": "GHS",
}


class PostgresCursor:
    """Cursor compatibility wrapper: supports qmark-style ? placeholders and dict rows."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        sql = sql.replace("?", "%s")
        self._cursor.execute(sql, params or ())
        return self

    def executemany(self, sql, seq_of_params):
        sql = sql.replace("?", "%s")
        self._cursor.executemany(sql, seq_of_params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self):
        return self._cursor.rowcount


class PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=None):
        cur = self._connection.cursor()
        return PostgresCursor(cur).execute(sql, params)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def _connect_postgres():
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg2-binary. Run: "
            "python -m pip install -r requirements.txt"
        ) from exc

    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as exc:
        raise RuntimeError(
            "ShopNexa could not connect to PostgreSQL. "
            "Set DATABASE_URL to the internal Render PostgreSQL connection string."
        ) from exc
    return PostgresConnection(conn)


def get_db():
    """Return a request-scoped database connection."""
    if "db" not in g:
        if USING_POSTGRES:
            g.db = _connect_postgres()
        else:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            g.db = sqlite3.connect(DB_PATH, timeout=30)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA busy_timeout = 30000")
    return g.db


def close_db(exc=None):
    database = g.pop("db", None)
    if database is not None:
        database.close()


def _migrate_sqlite(conn):
    """Bring a SQLite database created by an earlier version up to the current schema."""
    sales_cols = {row["name"] for row in conn.execute("PRAGMA table_info(sales)").fetchall()}
    if "receipt_id" not in sales_cols:
        conn.execute("ALTER TABLE sales ADD COLUMN receipt_id TEXT")

    product_cols = {row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "category" not in product_cols:
        conn.execute("ALTER TABLE products ADD COLUMN category TEXT NOT NULL DEFAULT 'General'")

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
                display_name TEXT NOT NULL
            );
            INSERT INTO users SELECT * FROM users_old;
            DROP TABLE users_old;
        """)


def init_db(app):
    """Create tables and seed default users/settings on first run."""
    if USING_POSTGRES:
        conn = _connect_postgres()
        try:
            # PostgreSQL doesn't support SQLite's executescript, so execute each
            # schema statement separately.
            for statement in [s.strip() for s in POSTGRES_SCHEMA.split(";") if s.strip()]:
                conn.execute(statement)

            # Safe migration for databases created by an older ShopNexa build.
            conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'General'")

            user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            if user_count == 0:
                for username, password, role, display_name in DEFAULT_USERS:
                    conn.execute(
                        "INSERT INTO users (username, password_hash, role, display_name) "
                        "VALUES (?, ?, ?, ?)",
                        (username, generate_password_hash(password), role, display_name),
                    )

            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO NOTHING",
                    (key, value),
                )
            conn.commit()
        finally:
            conn.close()
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SQLITE_SCHEMA)
            _migrate_sqlite(conn)

            if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
                for username, password, role, display_name in DEFAULT_USERS:
                    conn.execute(
                        "INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
                        (username, generate_password_hash(password), role, display_name),
                    )

            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

            conn.commit()
        finally:
            conn.close()

    app.teardown_appcontext(close_db)
