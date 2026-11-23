"""Roles: owner (ADMIN_IDS in .env, cannot be removed) > admin (stored in DB, managed via /addadmin) > user."""
import config as cfg
import db

_admins: set[int] = set()
LABELS = {"owner": "👑 Owner", "admin": "🛡️ Admin", "user": "👤 User"}


async def load():
    global _admins
    _admins = {r[0] for r in await db.fetchall("SELECT user_id FROM admins")}


def is_owner(uid: int) -> bool:
    return uid in cfg.ADMIN_IDS


def is_admin(uid: int) -> bool:  # owners count as admins
    return uid in cfg.ADMIN_IDS or uid in _admins


def role(uid: int) -> str:
    return "owner" if is_owner(uid) else "admin" if uid in _admins else "user"


def all_ids() -> set[int]:
    return set(cfg.ADMIN_IDS) | _admins


def db_admins() -> list[int]:
    return sorted(_admins)


async def add(uid: int, by: int):
    await db.execute("INSERT OR IGNORE INTO admins(user_id,added_by,added_at) VALUES(?,?,?)", (uid, by, db.now()))
    _admins.add(uid)


async def remove(uid: int) -> bool:
    if uid in cfg.ADMIN_IDS or uid not in _admins:
        return False
    await db.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    _admins.discard(uid)
    return True
