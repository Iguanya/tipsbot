"""M-Pesa Till, no API needed: customer pays the till, then submits the confirmation code.
If C2B URLs are registered (see /register_c2b) the code is verified automatically; otherwise an admin approves."""
import re
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config as cfg
import db
from . import core

CODE = "mpesa_manual"
LABEL = "📲 Pay to M-Pesa Till"
_CODE_RE = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{10}\b")


def available() -> bool:
    return bool(cfg.MPESA_TILL)


def extract_code(text: str):
    m = _CODE_RE.search(text.upper())
    return m.group(0) if m else None


def review_keyboard(pid: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"appr:{pid}"),
                                  InlineKeyboardButton("❌ Reject", callback_data=f"rej:{pid}")]])


async def start(update, context, plan):
    context.user_data["await"] = {"type": "mpesa_code", "plan": plan["code"]}
    name = f" ({cfg.MPESA_TILL_NAME})" if cfg.MPESA_TILL_NAME else ""
    await update.effective_message.reply_text(
        f"💳 {plan['name']} — KES {plan['price_kes']}\n\n"
        "1. M-Pesa → Lipa na M-Pesa → Buy Goods & Services\n"
        f"2. Till number: {cfg.MPESA_TILL}{name}\n"
        f"3. Amount: KES {plan['price_kes']}\n"
        "4. Paste the M-Pesa confirmation SMS (or just its code) here.\n\n"
        "Send 'cancel' to abort."
    )


async def try_auto_verify(bot, payment) -> bool:
    c = await db.fetchone("SELECT * FROM mpesa_c2b WHERE trans_id=?", (payment["reference"],))
    if not c or c["amount"] < payment["amount"]:
        return False
    cur = await db.execute("UPDATE mpesa_c2b SET claimed=1 WHERE trans_id=? AND claimed=0", (c["trans_id"],))
    if cur.rowcount != 1:
        return False
    return await core.complete(bot, payment["id"], receipt=payment["reference"])


async def handle_code(update, context, state):
    msg, user = update.effective_message, update.effective_user
    code = extract_code(msg.text or "")
    if not code:
        await msg.reply_text("I couldn't find an M-Pesa code (10 letters/digits, e.g. SGL1ABC2DE). Paste the SMS again, or send 'cancel'.")
        return
    plan = await db.get_plan(state["plan"])
    try:
        pid = await core.create(user.id, plan, CODE, plan["price_kes"], reference=code)
    except sqlite3.IntegrityError:
        await msg.reply_text("⚠️ That code has already been submitted. If this is a mistake, contact support.")
        return
    context.user_data.pop("await", None)

    payment = await db.fetchone("SELECT * FROM payments WHERE id=?", (pid,))
    if await try_auto_verify(context.bot, payment):
        return  # grant() already messaged the user
    await msg.reply_text("🧾 Code received. We're confirming your payment — you'll get your access link shortly.")
    await core.notify_admins(
        context.bot,
        f"💰 Payment to verify (#{pid})\nUser: {user.full_name} (@{user.username}) id {user.id}\n"
        f"Plan: {plan['name']}\nExpected: KES {plan['price_kes']}\nM-Pesa code: {code}\n\n"
        "Match it against your till SMS/portal before approving.",
        review_keyboard(pid),
    )
