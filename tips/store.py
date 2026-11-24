"""Persistence: slips, legs, distribution log (where each tip was sent), manual posts, random tip refs, record."""
import secrets
from datetime import datetime

import config as cfg
import db

TABLES = [
    """CREATE TABLE IF NOT EXISTS slips(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ref INTEGER, tier TEXT, kind TEXT DEFAULT 'regular', parent_id INTEGER,
        status TEXT DEFAULT 'draft', total_odds REAL, result_odds REAL,
        created_at INTEGER, published_at INTEGER, settled_at INTEGER,
        audience TEXT, published_by INTEGER, caption TEXT, file_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS legs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, slip_id INTEGER, sport_key TEXT, sport_title TEXT,
        event_id TEXT, home TEXT, away TEXT, commence_at INTEGER, market TEXT, selection TEXT, point REAL,
        odds REAL, fair_prob REAL, edge REAL, books INTEGER, best_book TEXT,
        status TEXT DEFAULT 'open', score TEXT)""",
    # one row per chat a slip was sent to (so results can be posted to the same places)
    """CREATE TABLE IF NOT EXISTS slip_posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, slip_id INTEGER, chat_id INTEGER, message_id INTEGER,
        audience TEXT, posted_at INTEGER)""",
    # tips an admin posted by hand (text/photo/video...) through the bot
    """CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ref INTEGER, admin_id INTEGER, audience TEXT, kind TEXT,
        text TEXT, file_id TEXT, ok INTEGER, failed INTEGER, created_at INTEGER)""",
]
NEW_SLIP_COLUMNS = [("ref", "INTEGER"), ("audience", "TEXT"), ("published_by", "INTEGER"),
                    ("caption", "TEXT"), ("file_id", "TEXT")]
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_legs_slip ON legs(slip_id)",
    "CREATE INDEX IF NOT EXISTS idx_slips_status ON slips(status)",
    "CREATE INDEX IF NOT EXISTS idx_slip_posts_slip ON slip_posts(slip_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_slips_ref ON slips(ref)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_ref ON posts(ref)",
]


async def init():
    for stmt in TABLES:
        await db.execute(stmt)
    have = {r["name"] for r in await db.fetchall("PRAGMA table_info(slips)")}
    for name, ddl in NEW_SLIP_COLUMNS:  # upgrade databases created by the previous version
        if name not in have:
            await db.execute(f"ALTER TABLE slips ADD COLUMN {name} {ddl}")
    for stmt in INDEXES:
        await db.execute(stmt)
    for r in await db.fetchall("SELECT id FROM slips WHERE ref IS NULL"):  # give old slips a random ref too
        await db.execute("UPDATE slips SET ref=? WHERE id=?", (await new_ref(), r["id"]))


async def new_ref() -> int:
    """Random 6-digit tracking number, unique across slips and manual posts (not sequential on purpose)."""
    for _ in range(50):
        ref = 100000 + secrets.randbelow(900000)
        if not await db.scalar("SELECT 1 FROM slips WHERE ref=? UNION SELECT 1 FROM posts WHERE ref=?", (ref, ref)):
            return ref
    raise RuntimeError("could not allocate a tip reference")


async def create_slip(tier, kind, legs, parent_id=None) -> int:
    total = 1.0
    for l in legs:
        total *= l["odds"]
    cur = await db.execute("INSERT INTO slips(ref,tier,kind,parent_id,total_odds,created_at) VALUES(?,?,?,?,?,?)",
                           (await new_ref(), tier, kind, parent_id, round(total, 2), db.now()))
    sid = cur.lastrowid
    for l in legs:
        await db.execute(
            """INSERT INTO legs(slip_id,sport_key,sport_title,event_id,home,away,commence_at,market,selection,
               point,odds,fair_prob,edge,books,best_book) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, l["sport_key"], l["sport_title"], l["event_id"], l["home"], l["away"], l["commence_at"],
             l["market"], l["selection"], l["point"], l["odds"], l["fair_prob"], l["edge"], l["books"], l["best_book"]))
    return sid


async def get_slip(sid):
    return await db.fetchone("SELECT * FROM slips WHERE id=?", (sid,))


async def find_slip(token):
    """Look a slip up by its public reference (preferred) or internal id."""
    return (await db.fetchone("SELECT * FROM slips WHERE ref=?", (token,))
            or await db.fetchone("SELECT * FROM slips WHERE id=?", (token,)))


async def get_legs(sid):
    return await db.fetchall("SELECT * FROM legs WHERE slip_id=? ORDER BY id", (sid,))


async def recent_event_ids(hours=48):
    """Events already used in any slip lately (any audience) so each scan suggests fresh matches."""
    rows = await db.fetchall("SELECT DISTINCT l.event_id FROM legs l JOIN slips s ON s.id=l.slip_id "
                             "WHERE s.created_at>=?", (db.now() - hours * 3600,))
    return {r[0] for r in rows}


async def transition(sid, expect, new, **cols) -> bool:
    """Atomic status change; True only for the caller that actually flipped it."""
    sets = "".join(f", {k}=?" for k in cols)
    cur = await db.execute(f"UPDATE slips SET status=?{sets} WHERE id=? AND status=?",
                           (new, *cols.values(), sid, expect))
    return cur.rowcount == 1


async def set_leg(leg_id, status, score=None):
    await db.execute("UPDATE legs SET status=?, score=COALESCE(?, score) WHERE id=?", (status, score, leg_id))


async def save_draft(sid, caption, file_id):
    await db.execute("UPDATE slips SET caption=?, file_id=? WHERE id=?", (caption, file_id, sid))


async def add_posts(sid, audience, rows):
    """rows: [(chat_id, message_id)] that were just sent."""
    for chat_id, message_id in rows:
        await db.execute("INSERT INTO slip_posts(slip_id,chat_id,message_id,audience,posted_at) VALUES(?,?,?,?,?)",
                         (sid, chat_id, message_id, audience, db.now()))
    await db.execute("UPDATE slips SET audience=CASE WHEN audience IS NULL OR audience='' THEN ? "
                     "ELSE audience||','||? END WHERE id=?", (audience, audience, sid))


async def posted_chats(sid) -> list[int]:
    return [r[0] for r in await db.fetchall("SELECT DISTINCT chat_id FROM slip_posts WHERE slip_id=?", (sid,))]


async def log_post(admin_id, audience, kind, text, file_id, ok, failed) -> int:
    ref = await new_ref()
    await db.execute("INSERT INTO posts(ref,admin_id,audience,kind,text,file_id,ok,failed,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (ref, admin_id, audience, kind, text, file_id, ok, failed, db.now()))
    return ref


async def recovery_today(tier) -> bool:
    start = datetime.now(cfg.TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return bool(await db.scalar("SELECT COUNT(*) FROM slips WHERE kind='recovery' AND tier=? AND created_at>=?",
                                (tier, int(start))))


async def record(days=30, tier=None) -> dict:
    q = "SELECT status, result_odds FROM slips WHERE status IN ('won','lost') AND settled_at>=?"
    args = [db.now() - days * 86400]
    if tier:
        q += " AND tier=?"
        args.append(tier)
    won = lost = 0
    units = 0.0
    for r in await db.fetchall(q, args):
        if r["status"] == "won":
            won += 1
            units += (r["result_odds"] or 1) - 1
        else:
            lost += 1
            units -= 1
    n = won + lost
    return {"won": won, "lost": lost, "n": n, "rate": (100 * won / n) if n else 0.0, "units": units}