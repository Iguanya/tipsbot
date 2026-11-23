"""Telegram Stars (XTR) — no provider token or business registration needed."""
from telegram import LabeledPrice

import db
from . import core

CODE = "stars"
LABEL = "⭐ Telegram Stars"


def available() -> bool:
    return True


async def start(update, context, plan):
    if not plan["price_stars"]:
        await update.effective_message.reply_text("Stars aren't available for this plan.")
        return
    pid = await core.create(update.effective_user.id, plan, CODE, plan["price_stars"], currency="XTR")
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id, title=plan["name"],
        description=f"{plan['days']} days of {plan['tier'].upper()} access",
        payload=f"pay:{pid}", provider_token="", currency="XTR",
        prices=[LabeledPrice(plan["name"], plan["price_stars"])],
    )


async def precheckout(update, context):
    q = update.pre_checkout_query
    try:
        pid = int(q.invoice_payload.split(":")[1])
        p = await db.fetchone("SELECT status FROM payments WHERE id=?", (pid,))
        ok = bool(p and p["status"] == "pending")
    except (ValueError, IndexError):
        ok = False
    await q.answer(ok=ok, error_message=None if ok else "This invoice is no longer valid.")


async def successful(update, context):
    sp = update.effective_message.successful_payment
    pid = int(sp.invoice_payload.split(":")[1])
    await db.execute("UPDATE payments SET reference=? WHERE id=?", (sp.telegram_payment_charge_id, pid))
    await core.complete(context.bot, pid, receipt=sp.telegram_payment_charge_id)
