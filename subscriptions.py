"""Grant, extend, remind and expire VIP subscriptions + invite-link handling."""
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import config as cfg
import db
import roles

log = logging.getLogger(__name__)
DAY = 86400


def fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, cfg.TZ).strftime("%d %b %Y, %H:%M")


async def _dm(bot, user_id, text, markup=None) -> bool:
    try:
        await bot.send_message(user_id, text, reply_markup=markup)
        return True
    except TelegramError as e:
        log.warning("DM to %s failed: %s", user_id, e)
        return False


async def channels_for_user(user_id) -> set[int]:
    rows = await db.fetchall(
        "SELECT DISTINCT tier FROM subscriptions WHERE user_id=? AND status='active' AND expires_at>?",
        (user_id, db.now()),
    )
    chans: set[int] = set()
    for r in rows:
        chans.update(cfg.TIER_ACCESS.get(r["tier"], []))
    return chans


async def send_invites(bot, user_id, tier):
    buttons = []
    for ch in cfg.TIER_ACCESS.get(tier, []):
        try:
            m = await bot.get_chat_member(ch, user_id)
            if m.status in ("member", "administrator", "creator"):
                continue  # already inside (renewal)
        except TelegramError:
            pass
        try:
            link = await bot.create_chat_invite_link(ch, member_limit=1, expire_date=db.now() + DAY)
            title = (await bot.get_chat(ch)).title or "VIP channel"
            buttons.append([InlineKeyboardButton(f"➡️ Join {title}", url=link.invite_link)])
        except TelegramError as e:
            log.error("Invite link failed for channel %s: %s", ch, e)
            for a in roles.all_ids():
                await _dm(bot, a, f"⚠️ Could not create invite link for channel {ch} (user {user_id}): {e}\n"
                                  "Is the bot an admin with 'Invite users' permission?")
    if buttons:
        await _dm(bot, user_id, "Your personal invite link(s) — single use, valid 24h:",
                  InlineKeyboardMarkup(buttons))


async def grant(bot, user_id, plan) -> int:
    add = plan["days"] * DAY
    cur = await db.fetchone(
        """SELECT id, expires_at FROM subscriptions
           WHERE user_id=? AND tier=? AND status='active' AND expires_at>?
           ORDER BY expires_at DESC LIMIT 1""",
        (user_id, plan["tier"], db.now()),
    )
    if cur:  # renewal: stack on top of the remaining time
        expires = cur["expires_at"] + add
        await db.execute(
            "UPDATE subscriptions SET expires_at=?, plan_code=?, rem3=0, rem1=0 WHERE id=?",
            (expires, plan["code"], cur["id"]),
        )
    else:
        expires = db.now() + add
        await db.execute(
            "INSERT INTO subscriptions(user_id,plan_code,tier,starts_at,expires_at) VALUES(?,?,?,?,?)",
            (user_id, plan["code"], plan["tier"], db.now(), expires),
        )
    await _dm(bot, user_id, f"✅ {plan['name']} is active until {fmt(expires)}.")
    await send_invites(bot, user_id, plan["tier"])
    return expires


async def revoke_channels(bot, user_id, channels):
    if roles.is_admin(user_id):
        return
    for ch in channels:
        try:
            await bot.ban_chat_member(ch, user_id)
            await bot.unban_chat_member(ch, user_id, only_if_banned=True)  # kick, but allow rejoin on renewal
        except TelegramError as e:
            log.warning("Could not remove %s from %s: %s", user_id, ch, e)


async def revoke_all(bot, user_id):
    lost = set()
    for r in await db.fetchall("SELECT DISTINCT tier FROM subscriptions WHERE user_id=? AND status='active'", (user_id,)):
        lost.update(cfg.TIER_ACCESS.get(r["tier"], []))
    await db.execute("UPDATE subscriptions SET status='expired' WHERE user_id=? AND status='active'", (user_id,))
    await revoke_channels(bot, user_id, lost)


async def expiry_job(context):
    """Runs hourly: reminders (3d / 1d), then expire + remove."""
    bot, now = context.bot, db.now()

    for days, col in ((3, "rem3"), (1, "rem1")):
        rows = await db.fetchall(
            f"SELECT * FROM subscriptions WHERE status='active' AND {col}=0 AND expires_at>? AND expires_at<=?",
            (now, now + days * DAY),
        )
        for r in rows:
            await db.execute(
                "UPDATE subscriptions SET rem1=1, rem3=1 WHERE id=?" if days == 1
                else "UPDATE subscriptions SET rem3=1 WHERE id=?", (r["id"],))
            await _dm(bot, r["user_id"],
                      f"⏰ Your {r['tier'].upper()} access expires on {fmt(r['expires_at'])}. Renew with /subscribe "
                      "to keep your spot (time is added on top of what's left).")

    expired = await db.fetchall("SELECT * FROM subscriptions WHERE status='active' AND expires_at<=?", (now,))
    for r in expired:
        await db.execute("UPDATE subscriptions SET status='expired' WHERE id=?", (r["id"],))
        still = await channels_for_user(r["user_id"])
        lost = set(cfg.TIER_ACCESS.get(r["tier"], [])) - still
        await revoke_channels(bot, r["user_id"], lost)
        await _dm(bot, r["user_id"], f"Your {r['tier'].upper()} access has ended. Renew any time with /subscribe.")

    # stale STK pushes that never got a callback
    await db.execute("UPDATE payments SET status='failed' WHERE provider='mpesa_stk' AND status='pending' AND created_at<?",
                     (now - 900,))
