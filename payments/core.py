"""Provider-independent payment bookkeeping."""
import logging

import config as cfg
import db
import roles
import subscriptions

log = logging.getLogger(__name__)


async def create(user_id, plan, provider, amount, currency="KES", reference=None, phone=None) -> int:
    cur = await db.execute(
        """INSERT INTO payments(user_id,plan_code,provider,amount,currency,reference,phone,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (user_id, plan["code"], provider, amount, currency, reference, phone, db.now()),
    )
    return cur.lastrowid


async def notify_admins(bot, text, markup=None):
    for a in roles.all_ids():
        try:
            await bot.send_message(a, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            log.warning("admin notify %s failed: %s", a, e)


async def complete(bot, payment_id, receipt=None) -> bool:
    """Idempotent: only the first caller flips pending->paid and grants access."""
    cur = await db.execute(
        "UPDATE payments SET status='paid', paid_at=?, receipt=COALESCE(?, receipt) "
        "WHERE id=? AND status IN ('pending','failed')",
        (db.now(), receipt, payment_id),
    )
    if cur.rowcount != 1:
        return False
    p = await db.fetchone("SELECT * FROM payments WHERE id=?", (payment_id,))
    plan = await db.get_plan(p["plan_code"])
    try:
        await subscriptions.grant(bot, p["user_id"], plan)
    except Exception:  # noqa: BLE001
        log.exception("grant failed for payment %s", payment_id)
        await notify_admins(bot, f"🚨 Payment #{payment_id} is PAID but granting failed for user {p['user_id']}. "
                                 f"Use /grant {p['user_id']} {p['plan_code']}")
    return True
