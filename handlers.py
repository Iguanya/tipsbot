"""Telegram handlers: user flow, admin tools, publishing, payment review."""
import html
import logging
import time
from functools import wraps

from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton as Btn,
                      InlineKeyboardMarkup as Kb, Update)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          PreCheckoutQueryHandler, filters)

import broadcast
import config as cfg
import db
import payments
import roles
import subscriptions as subs
from tips import store as tips_store
from payments import core, mpesa_daraja, mpesa_manual, stars

log = logging.getLogger(__name__)
_last_err = 0.0


# ---------------- helpers ----------------
def _guard(check):
    def deco(fn):
        @wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            u = update.effective_user
            if not u or not check(u.id):
                if update.callback_query:
                    await update.callback_query.answer("Not allowed", show_alert=True)
                return  # silently ignore: users never learn which admin commands exist
            return await fn(update, context)
        return wrapper
    return deco


admin_only = _guard(roles.is_admin)   # owners + admins
owner_only = _guard(roles.is_owner)   # owners only


def esc(s) -> str:
    return html.escape(str(s or ""))


# ---------------- user flow ----------------
PUBLIC_CMDS = [BotCommand("start", "Start"), BotCommand("subscribe", "Get VIP access"),
               BotCommand("status", "My subscription"), BotCommand("help", "Help")]
ADMIN_CMDS = PUBLIC_CMDS + [BotCommand("admin", "Admin panel"), BotCommand("stats", "Business stats"),
                            BotCommand("pending", "Payments to review"), BotCommand("grant", "Grant plan: id plan")]
OWNER_CMDS = ADMIN_CMDS + [BotCommand("revoke", "Revoke access: id"), BotCommand("setprice", "Set price: plan kes [stars]"),
                           BotCommand("addadmin", "Add admin: id"), BotCommand("removeadmin", "Remove admin: id"),
                           BotCommand("admins", "List admins"), BotCommand("register_c2b", "Register till URLs")]

USER_HELP = ("/subscribe – get VIP access\n/status – your subscription\n"
             "/myid – your Telegram ID\n/cancel – cancel current action")
ADMIN_HELP = ("\n\n🛡️ Admin\nSend me any text/photo/video -> pick the audience and I publish it.\n"
              "/admin – control panel\n/stats – subscribers & revenue\n/pending – payments awaiting review\n"
              "/grant <id> <plan> – give someone a plan")
OWNER_HELP = ("\n\n👑 Owner\n/revoke <id> – remove all access\n/setprice <plan> <kes> [stars]\n"
              "/addadmin <id>  /removeadmin <id>  /admins\n/register_c2b – register till URLs with Daraja")


async def apply_menu(bot, uid):
    cmds = OWNER_CMDS if roles.is_owner(uid) else ADMIN_CMDS if roles.is_admin(uid) else PUBLIC_CMDS
    try:
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(uid))
    except TelegramError as e:
        log.warning("Could not set menu for %s (they must /start the bot first): %s", uid, e)


async def remove_menu(bot, uid):
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(uid))
    except TelegramError as e:
        log.warning("Could not reset menu for %s: %s", uid, e)


async def cmd_start(update, context):
    user = update.effective_user
    await db.upsert_user(user)
    if roles.is_admin(user.id):
        await apply_menu(context.bot, user.id)
        await update.message.reply_text(
            f"{roles.LABELS[roles.role(user.id)]} mode — {cfg.BRAND_NAME}\n\n"
            "Send me any text, photo or video and I'll ask where to publish it.\n"
            "Open the control panel with /admin, or see every command with /help.\n\n"
            "Tip: to preview the customer experience, use a second Telegram account.")
        return
    await update.message.reply_text(
        f"👋 Welcome to <b>{esc(cfg.BRAND_NAME)}</b>!\n\n"
        "Free tips are posted in our channels. For premium tips, tap /subscribe.\n"
        "Check your plan with /status.\n\n"
        "<i>18+ only. Betting involves risk — never stake more than you can afford to lose.</i>",
        parse_mode=ParseMode.HTML)


async def cmd_help(update, context):
    uid = update.effective_user.id
    text = USER_HELP + (ADMIN_HELP if roles.is_admin(uid) else "") + (OWNER_HELP if roles.is_owner(uid) else "")
    await update.message.reply_text(text)


async def cmd_myid(update, context):
    uid = update.effective_user.id
    await update.message.reply_text(f"Your Telegram ID: {uid}\nRole: {roles.LABELS[roles.role(uid)]}")


async def cmd_cancel(update, context):
    context.user_data.pop("await", None)
    context.user_data.pop("post", None)
    await update.message.reply_text("Cancelled.")


async def cmd_subscribe(update, context):
    await db.upsert_user(update.effective_user)
    plans = await db.list_plans()
    if not plans:
        await update.effective_message.reply_text("No plans available right now.")
        return
    kb = Kb([[Btn(f"{p['name']} — KES {p['price_kes']}", callback_data=f"plan:{p['code']}")] for p in plans])
    await update.effective_message.reply_text("💎 Choose a plan:", reply_markup=kb)


async def cmd_status(update, context):
    rows = await db.active_subs(update.effective_user.id)
    if not rows:
        await update.message.reply_text("You have no active subscription. Use /subscribe.")
        return
    await update.message.reply_text("\n".join(f"✅ {r['tier'].upper()} — until {subs.fmt(r['expires_at'])}" for r in rows))


async def on_plan(update, context):
    q = update.callback_query
    await q.answer()
    plan = await db.get_plan(q.data.split(":", 1)[1])
    methods = payments.enabled()
    if not plan:
        return
    if not methods:
        await q.message.reply_text("Payments aren't set up yet — please contact support.")
    elif len(methods) == 1:
        await methods[0].start(update, context, plan)
    else:
        kb = Kb([[Btn(m.LABEL, callback_data=f"pay:{plan['code']}:{m.CODE}")] for m in methods])
        await q.message.reply_text(f"{plan['name']} — how would you like to pay?", reply_markup=kb)


async def on_pay(update, context):
    q = update.callback_query
    await q.answer()
    _, code, provider = q.data.split(":", 2)
    plan, mod = await db.get_plan(code), payments.PROVIDERS.get(provider)
    if plan and mod and mod in payments.enabled():
        await mod.start(update, context, plan)


# ---------------- private text router ----------------
async def on_private_message(update, context):
    msg = update.effective_message
    st = context.user_data.get("await")
    if st and msg.text:
        if msg.text.strip().lower() == "cancel":
            context.user_data.pop("await", None)
            await msg.reply_text("Cancelled.")
            return
        if st["type"] == "mpesa_code":
            return await mpesa_manual.handle_code(update, context, st)
        if st["type"] == "phone":
            return await mpesa_daraja.handle_phone(update, context, st)
    if roles.is_admin(update.effective_user.id):
        return await stage_post(update, context)
    await msg.reply_text("Use /subscribe for VIP access or /status to check your plan.")


# ---------------- admin: publishing ----------------
def _post_meta(msg):
    """(kind, text, file_id) of an admin message, so manual tips can be saved in the database."""
    for kind, obj in (("photo", msg.photo[-1] if msg.photo else None), ("video", msg.video),
                      ("gif", msg.animation), ("document", msg.document)):
        if obj:
            return kind, msg.caption or "", obj.file_id
    return "text", msg.text or "", None


async def stage_post(update, context):
    msg = update.effective_message
    context.user_data["post"] = (msg.chat_id, msg.message_id)
    context.user_data["post_meta"] = _post_meta(msg)
    kb = Kb([
        [Btn("🆓 Free", callback_data="pub:free"), Btn("💎 VIP", callback_data="pub:vip"),
         Btn("👑 VIP+", callback_data="pub:vipplus")],
        [Btn("📣 All channels", callback_data="pub:all"), Btn("✉️ DM subscribers", callback_data="pub:dm_subs")],
        [Btn("✉️ DM everyone", callback_data="pub:dm_all"), Btn("✖️ Cancel", callback_data="pub:cancel")],
    ])
    await msg.reply_text("Where should I send this?", reply_markup=kb, reply_to_message_id=msg.message_id)


@admin_only
async def on_publish(update, context):
    q = update.callback_query
    await q.answer()
    aud = q.data.split(":", 1)[1]
    post = context.user_data.pop("post", None)
    meta = context.user_data.pop("post_meta", ("text", "", None))
    if aud == "cancel" or not post:
        await q.edit_message_text("Cancelled." if aud == "cancel" else "Nothing pending — send the content again.")
        return
    targets = await broadcast.resolve(aud)
    if not targets:
        await q.edit_message_text(f"No targets for '{aud}'. Is the channel ID set in .env?")
        return
    await q.edit_message_text(f"📤 Sending to {len(targets)} target(s)…")
    admin_id = update.effective_user.id

    async def job():
        ok, fail = await broadcast.send_many(context.bot, targets, post[0], post[1])
        await db.execute("INSERT INTO broadcasts(admin_id,audience,ok,failed,created_at) VALUES(?,?,?,?,?)",
                         (admin_id, aud, ok, fail, db.now()))
        ref = await tips_store.log_post(admin_id, aud, meta[0], meta[1], meta[2], ok, fail)  # saved in the database
        await q.edit_message_text(f"✅ '{aud}': {ok} sent, {fail} failed. Ref {ref}")

    context.application.create_task(job())  # don't block other updates (e.g. payments)


async def on_source_post(update, context):
    m = update.channel_post
    cur = await db.execute("INSERT OR IGNORE INTO forwarded(chat_id,message_id) VALUES(?,?)", (m.chat_id, m.message_id))
    if cur.rowcount != 1:
        return  # already handled (persists across restarts)
    targets = await broadcast.resolve(cfg.SOURCE_AUDIENCE)
    context.application.create_task(broadcast.send_many(context.bot, targets, m.chat_id, m.message_id))


# ---------------- admin: payments & subscribers ----------------
@admin_only
async def on_payment_review(update, context):
    q = update.callback_query
    action, pid = q.data.split(":")
    pid = int(pid)
    who = update.effective_user.full_name
    if action == "appr":
        done = await core.complete(context.bot, pid)
        verdict = f"✅ Approved by {who}" if done else "ℹ️ Already processed"
    else:
        cur = await db.execute("UPDATE payments SET status='rejected' WHERE id=? AND status='pending'", (pid,))
        verdict = f"❌ Rejected by {who}" if cur.rowcount else "ℹ️ Already processed"
        if cur.rowcount:
            p = await db.fetchone("SELECT user_id FROM payments WHERE id=?", (pid,))
            try:
                await context.bot.send_message(p["user_id"], "We couldn't verify your payment. Please check the code "
                                                             "and try again with /subscribe, or contact support.")
            except TelegramError:
                pass
    await q.answer(verdict)
    await q.edit_message_text(f"{q.message.text}\n\n{verdict}")


@admin_only
async def cmd_pending(update, context):
    rows = await db.fetchall("SELECT * FROM payments WHERE status='pending' AND provider='mpesa_manual' ORDER BY id")
    if not rows:
        await update.effective_message.reply_text("No payments waiting for review.")
    for p in rows:
        await update.effective_message.reply_text(f"#{p['id']} user {p['user_id']} · {p['plan_code']} · KES {p['amount']} · "
                                        f"code {p['reference']}", reply_markup=mpesa_manual.review_keyboard(p["id"]))


@admin_only
async def cmd_grant(update, context):
    try:
        uid, code = int(context.args[0]), context.args[1]
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /grant <user_id> <plan_code>")
        return
    plan = await db.get_plan(code)
    if not plan:
        await update.message.reply_text("Unknown plan. Plans: " + ", ".join(p["code"] for p in await db.list_plans()))
        return
    exp = await subs.grant(context.bot, uid, plan)
    await update.message.reply_text(f"✅ Granted {plan['name']} to {uid} until {subs.fmt(exp)} "
                                    "(the user must have started the bot to receive the link).")


@owner_only
async def cmd_revoke(update, context):
    try:
        uid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /revoke <user_id>")
        return
    await subs.revoke_all(context.bot, uid)
    await update.message.reply_text(f"🗑️ Revoked all access for {uid}.")


@owner_only
async def cmd_setprice(update, context):
    try:
        code, kes = context.args[0], int(context.args[1])
        stars_price = int(context.args[2]) if len(context.args) > 2 else None
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /setprice <plan_code> <KES> [stars]")
        return
    cur = await db.execute("UPDATE plans SET price_kes=?, price_stars=COALESCE(?, price_stars) WHERE code=?",
                           (kes, stars_price, code))
    await update.message.reply_text("Updated." if cur.rowcount else "Unknown plan code.")


@admin_only
async def cmd_stats(update, context):
    now, month = db.now(), db.now() - 30 * 86400
    users = await db.scalar("SELECT COUNT(*) FROM users")
    live = await db.scalar("SELECT COUNT(*) FROM users WHERE active=1")
    tiers = await db.fetchall("SELECT tier, COUNT(DISTINCT user_id) n FROM subscriptions "
                              "WHERE status='active' AND expires_at>? GROUP BY tier", (now,))
    kes = await db.scalar("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid' AND currency='KES' AND paid_at>?", (month,))
    xtr = await db.scalar("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid' AND currency='XTR' AND paid_at>?", (month,))
    soon = await db.scalar("SELECT COUNT(*) FROM subscriptions WHERE status='active' AND expires_at BETWEEN ? AND ?", (now, now + 3 * 86400))
    pend = await db.scalar("SELECT COUNT(*) FROM payments WHERE status='pending' AND provider='mpesa_manual'")
    lines = [f"👥 Users: {users} ({live} reachable)"]
    lines += [f"💎 {t['tier'].upper()} active: {t['n']}" for t in tiers] or ["💎 No active subscribers"]
    lines += [f"💰 Last 30d: KES {kes:,} + {xtr:,} ⭐", f"⏳ Expiring in 3 days: {soon}", f"🧾 Awaiting review: {pend}"]
    await update.effective_message.reply_text("\n".join(lines))


@owner_only
async def cmd_register_c2b(update, context):
    if not cfg.WEBHOOK_ENABLED:
        await update.message.reply_text("Set PUBLIC_URL and WEBHOOK_SECRET first.")
        return
    try:
        res = await mpesa_daraja.register_c2b()
    except Exception as e:  # noqa: BLE001
        res = {"error": str(e)}
    await update.message.reply_text(f"Daraja response:\n{res}")


# ---------------- admin panel & admin management ----------------
HOWTO = ("📣 Publishing\n1. Send me the tip (text, photo, video or GIF) in this chat.\n"
         "2. Tap the audience: Free, VIP, VIP+, all channels, or DM.\n3. I copy it exactly as you wrote it.\n\n"
         "Tip: set SOURCE_CHANNEL_ID in .env to auto-copy a channel's posts.")


def panel_kb(uid):
    rows = [[Btn("📊 Stats", callback_data="adm:stats"), Btn("🧾 Pending payments", callback_data="adm:pending")],
            [Btn("📣 How to publish", callback_data="adm:howto")]]
    if roles.is_owner(uid):
        rows.append([Btn("👥 Admins", callback_data="adm:admins"), Btn("💰 Plans & prices", callback_data="adm:plans")])
    return Kb(rows)


@admin_only
async def cmd_admin(update, context):
    uid = update.effective_user.id
    await update.effective_message.reply_text(f"{roles.LABELS[roles.role(uid)]} panel", reply_markup=panel_kb(uid))


async def _who(uid):
    r = await db.fetchone("SELECT username, full_name FROM users WHERE user_id=?", (uid,))
    if not r:
        return f"• {uid}"
    return f"• {r['full_name']}" + (f" @{r['username']}" if r["username"] else "") + f" ({uid})"


@owner_only
async def cmd_admins(update, context):
    lines = ["👑 Owners (set in .env):"] + [await _who(i) for i in sorted(cfg.ADMIN_IDS)]
    extra = roles.db_admins()
    lines += ["", "🛡️ Admins:"] + ([await _who(i) for i in extra] or ["(none) — add with /addadmin <user_id>"])
    await update.effective_message.reply_text("\n".join(lines))


@owner_only
async def cmd_addadmin(update, context):
    try:
        uid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addadmin <user_id>  (they can get it from /myid)")
        return
    if roles.is_admin(uid):
        await update.message.reply_text("They already have admin rights.")
        return
    await roles.add(uid, update.effective_user.id)
    log.info("Admin %s added by owner %s", uid, update.effective_user.id)
    await apply_menu(context.bot, uid)
    try:
        await context.bot.send_message(uid, "🛡️ You've been made an admin. Send /start to open your panel.")
        note = ""
    except TelegramError:
        note = "\n(They haven't started the bot yet — ask them to send /start.)"
    await update.message.reply_text(f"✅ {uid} is now an admin.{note}")


@owner_only
async def cmd_removeadmin(update, context):
    try:
        uid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    if await roles.remove(uid):
        log.info("Admin %s removed by owner %s", uid, update.effective_user.id)
        await remove_menu(context.bot, uid)
        await update.message.reply_text(f"🗑️ {uid} is no longer an admin.")
    else:
        await update.message.reply_text("Not an admin, or an owner (owners are managed in .env).")


@admin_only
async def on_admin_panel(update, context):
    q = update.callback_query
    await q.answer()
    act, uid = q.data.split(":")[1], update.effective_user.id
    if act == "stats":
        await cmd_stats(update, context)
    elif act == "pending":
        await cmd_pending(update, context)
    elif act == "howto":
        await q.message.reply_text(HOWTO)
    elif act == "admins" and roles.is_owner(uid):
        await cmd_admins(update, context)
    elif act == "plans" and roles.is_owner(uid):
        rows = await db.fetchall("SELECT * FROM plans ORDER BY price_kes")
        await q.message.reply_text("💰 Plans (change with /setprice <code> <kes> [stars])\n\n" + "\n".join(
            f"{p['code']} — {p['name']} · {p['days']}d · KES {p['price_kes']} · {p['price_stars']}⭐" for p in rows))


# ---------------- errors & wiring ----------------
async def on_error(update, context):
    global _last_err
    log.error("Unhandled error", exc_info=context.error)
    if time.time() - _last_err > 600:  # at most one alert per 10 minutes
        _last_err = time.time()
        await core.notify_admins(context.bot, f"🚨 Bot error: {type(context.error).__name__}: {str(context.error)[:300]}")


async def setup_commands(app):
    await app.bot.set_my_commands(PUBLIC_CMDS)
    for uid in roles.all_ids():
        await apply_menu(app.bot, uid)


def register(app):
    app.add_error_handler(on_error)
    for name, fn in [("start", cmd_start), ("help", cmd_help), ("myid", cmd_myid), ("cancel", cmd_cancel),
                     ("subscribe", cmd_subscribe), ("plans", cmd_subscribe), ("status", cmd_status),
                     ("stats", cmd_stats), ("pending", cmd_pending), ("grant", cmd_grant),
                     ("revoke", cmd_revoke), ("setprice", cmd_setprice), ("register_c2b", cmd_register_c2b),
                     ("admin", cmd_admin), ("admins", cmd_admins), ("addadmin", cmd_addadmin),
                     ("removeadmin", cmd_removeadmin)]:
        app.add_handler(CommandHandler(name, fn, filters=filters.ChatType.PRIVATE))

    app.add_handler(CallbackQueryHandler(on_plan, pattern=r"^plan:"))
    app.add_handler(CallbackQueryHandler(on_pay, pattern=r"^pay:"))
    app.add_handler(CallbackQueryHandler(on_publish, pattern=r"^pub:"))
    app.add_handler(CallbackQueryHandler(on_payment_review, pattern=r"^(appr|rej):"))
    app.add_handler(CallbackQueryHandler(on_admin_panel, pattern=r"^adm:"))

    app.add_handler(PreCheckoutQueryHandler(stars.precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, stars.successful))

    if cfg.SOURCE_CHANNEL_ID:
        app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & filters.Chat(cfg.SOURCE_CHANNEL_ID), on_source_post))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT, on_private_message))