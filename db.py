"""SQLite storage using stdlib sqlite3 + asyncio.to_thread (no aiosqlite)."""
import asyncio
import sqlite3
import threading
import time

import config as cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
    joined_at INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS plans(
    code TEXT PRIMARY KEY, name TEXT, tier TEXT, days INTEGER,
    price_kes INTEGER, price_stars INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS subscriptions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_code TEXT, tier TEXT,
    starts_at INTEGER, expires_at INTEGER, status TEXT DEFAULT 'active',
    rem3 INTEGER DEFAULT 0, rem1 INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(user_id, tier, status);
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_code TEXT, provider TEXT,
    amount INTEGER, currency TEXT DEFAULT 'KES', status TEXT DEFAULT 'pending',
    reference TEXT, receipt TEXT, phone TEXT, created_at INTEGER, paid_at INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_ref ON payments(provider, reference)
    WHERE reference IS NOT NULL AND status IN ('pending','paid');
CREATE TABLE IF NOT EXISTS mpesa_c2b(
    trans_id TEXT PRIMARY KEY, amount REAL, msisdn TEXT, name TEXT,
    received_at INTEGER, claimed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS forwarded(
    chat_id INTEGER, message_id INTEGER, PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS broadcasts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, audience TEXT,
    ok INTEGER, failed INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS admins(
    user_id INTEGER PRIMARY KEY, added_by INTEGER, added_at INTEGER);
"""

DEFAULT_PLANS = [
    ("vip_week", "VIP Weekly", "vip", 7, 700, 100),
    ("vip_month", "VIP Monthly", "vip", 30, 2100, 700),
    ("vipplus_month", "VIP+ Monthly", "vipplus", 30, 5000, 700),
]

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

def now() -> int:
    return int(time.time())

def _connect_sync():
    global _conn
    _conn = sqlite3.connect(cfg.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    _conn.executemany(
        "INSERT OR IGNORE INTO plans(code,name,tier,days,price_kes,price_stars) VALUES(?,?,?,?,?,?)",
        DEFAULT_PLANS,
    )
    _conn.commit()
    return _conn

async def init():
    await asyncio.to_thread(_connect_sync)

async def close():
    global _conn
    if _conn is None:
        return
    conn = _conn
    _conn = None
    await asyncio.to_thread(conn.close)

def _fetchall_sync(q, a):
    with _lock:
        return _conn.execute(q, a).fetchall()

def _fetchone_sync(q, a):
    with _lock:
        return _conn.execute(q, a).fetchone()

def _execute_sync(q, a):
    with _lock:
        cur = _conn.execute(q, a)
        _conn.commit()
        return cur

async def fetchall(q, a=()):
    return await asyncio.to_thread(_fetchall_sync, q, a)

async def fetchone(q, a=()):
    return await asyncio.to_thread(_fetchone_sync, q, a)

async def scalar(q, a=()):
    r = await fetchone(q, a)
    return r[0] if r else None

async def execute(q, a=()):
    return await asyncio.to_thread(_execute_sync, q, a)

# ---------- helpers (identical signatures) ----------
async def upsert_user(u):
    full = " ".join(x for x in (u.first_name, u.last_name) if x) or "Unknown"
    await execute(
        """INSERT INTO users(user_id,username,full_name,joined_at) VALUES(?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
           full_name=excluded.full_name, active=1""",
        (u.id, u.username, full, now()),
    )

async def mark_inactive(user_id):
    await execute("UPDATE users SET active=0 WHERE user_id=?", (user_id,))

async def get_plan(code):
    return await fetchone("SELECT * FROM plans WHERE code=?", (code,))

async def list_plans():
    return await fetchall("SELECT * FROM plans WHERE active=1 ORDER BY price_kes")

async def active_subs(user_id):
    return await fetchall(
        "SELECT * FROM subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at",
        (user_id, now()),
    )

async def active_subscriber_ids():
    rows = await fetchall(
        """SELECT DISTINCT s.user_id FROM subscriptions s JOIN users u USING(user_id)
           WHERE s.status='active' AND s.expires_at>? AND u.active=1""",
        (now(),),
    )
    return [r[0] for r in rows]

async def active_user_ids():
    return [r[0] for r in await fetchall("SELECT user_id FROM users WHERE active=1")]