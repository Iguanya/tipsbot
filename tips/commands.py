"""Telegram commands/callbacks + scheduling for the tips engine."""
import logging
import time
from datetime import datetime, time as dtime

from telegram import BotCommand
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, filters

import config as cfg
import db
import handlers as H
from payments import core
from . import info, model, service, stats, store
from . import settings as S
from .oddsapi import OddsAPIError, client

log = logging.getLogger(__name__)
AUDIENCES = ("free", "vip", "vipplus", "all", "dm_subs")


def _d(ts) -> str:
    return datetime.fromtimestamp(ts, cfg.TZ).strftime("%d %b %H:%M")


async def cmd_record(update, context):
    # Beautified output for /record
    lines = ["📈 <b>Our track record</b> <i>(every published tip, flat 1-unit stakes)</i>\n"]
    for days in (7, 30, 90):
        r = await store.record(days)
        if r["n"]:
            lines.append(f"<b>Last {days}d:</b> {r['won']}W–{r['lost']}L (<b>{r['rate']:.0f}%</b>) · <b>{r['units']:+.1f} units</b>")
        else:
            lines.append(f"<b>Last {days}d:</b> <i>no settled tips yet</i>")
    lines.append("\n<i>Results are posted automatically from official scores. 18+ | Bet responsibly.</i>")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


@H.admin_only
async def cmd_tips(update, context):
    msg = update.message
    await msg.reply_text("🔎 Scanning markets… drafts will arrive in a moment.")

    async def job():
        try:
            ids = await service.generate_and_deliver(context.bot)
        except OddsAPIError as e:
            await msg.reply_text(f"⚠️ Scan failed: {e}")
            return
        refs = [(await store.get_slip(i))["ref"] for i in ids]
        await msg.reply_text(f"✅ Drafted tip(s): {', '.join(str(r) for r in refs)}\n\n{model.describe_scan()}" if ids else
                             "ℹ️ Nothing drafted.\n\n" + model.describe_scan())

    context.application.create_task(job())


@H.admin_only
async def cmd_slips(update, context):
    rows = await db.fetchall("SELECT * FROM slips WHERE status IN ('draft','open') ORDER BY id DESC LIMIT 15")
    if not rows:
        await update.message.reply_text("No draft/open tips.")
        return
    out = []
    for s in rows:
        legs = await store.get_legs(s["id"])
        out.append(f"<code>{s['ref']}</code> · {s['tier']}/{s['kind']} [{s['status']}] @ {s['total_odds']:.2f}\n" +
                   "\n".join(f"   {l['home']} v {l['away']} → <b>{l['selection']}</b> ({l['status']})" for l in legs))
    await update.message.reply_text("\n\n".join(out), parse_mode='HTML')


@H.admin_only
async def cmd_history(update, context):
    n = 10
    if context.args and context.args[0].isdigit():
        n = max(1, min(int(context.args[0]), 30))
    slips = await db.fetchall("SELECT * FROM slips ORDER BY created_at DESC, id DESC LIMIT ?", (n,))
    posts = await db.fetchall("SELECT * FROM posts ORDER BY created_at DESC, id DESC LIMIT ?", (n,))
    items = [(r["created_at"], f"🎯 <code>{r['ref']}</code> · {_d(r['created_at'])} · {r['tier']}/{r['kind']} · @ {r['total_odds']:.2f} · "
                               f"{r['status']}" + (f" → {r['audience']}" if r["audience"] else "")) for r in slips]
    items += [(r["created_at"], f"📝 <code>{r['ref']}</code> · {_d(r['created_at'])} · manual {r['kind']} → {r['audience']} "
                                f"({r['ok']} sent)") for r in posts]
    items.sort(key=lambda x: x[0], reverse=True)
    if not items:
        await update.message.reply_text("Nothing saved yet.")
        return
    await update.message.reply_text("\n".join(t for _, t in items[:n]) + "\n\nDetails: /tip &lt;ref&gt;", parse_mode='HTML')


@H.admin_only
async def cmd_tip(update, context):
    try:
        ref = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /tip &lt;ref&gt;", parse_mode='HTML')
        return
    slip = await store.find_slip(ref)
    if slip:
        legs, posted = await store.get_legs(slip["id"]), await store.posted_chats(slip["id"])
        lines = [f"🎯 <b>Tip {slip['ref']}</b> · {slip['tier']}/{slip['kind']} · {slip['status'].upper()} · total {slip['total_odds']:.2f}",
                 f"Created {_d(slip['created_at'])}" + (f" · published {_d(slip['published_at'])}" if slip["published_at"] else "")
                 + (f" → {slip['audience']}" if slip["audience"] else "")]
        lines += [f"• {l['home']} v {l['away']}: <b>{model.pick_text(l)}</b> @ {l['odds']:.2f} [{l['status']}"
                  f"{' ' + l['score'] if l['score'] else ''}]" for l in legs]
        lines.append(f"Sent to {len(posted)} chat(s).")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
        return
    post = await db.fetchone("SELECT * FROM posts WHERE ref=?", (ref,))
    if post:
        text = (post["text"] or "")[:600]
        await update.message.reply_text(f"📝 Manual {post['kind']} <code>{post['ref']}</code> · {_d(post['created_at'])} → {post['audience']} "
                                        f"({post['ok']} sent, {post['failed']} failed)\n\n{text or '(no text)'}")
    else:
        await update.message.reply_text("No tip with that reference.")


@H.admin_only
async def cmd_settle(update, context):
    try:
        token, res = int(context.args[0]), context.args[1].lower()
        assert res in ("won", "lost", "void")
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text("Usage: /settle &lt;ref&gt; &lt;won|lost|void&gt;", parse_mode='HTML')
        return
    await update.message.reply_text(await service.manual_settle(context.bot, token, res))


@H.admin_only
async def cmd_credits(update, context):
    try:
        await client.sports()  # free call; refreshes the usage header
        await update.message.reply_text(f"Odds API credits remaining: <b>{client.remaining}</b> (floor {S.MIN_CREDITS})", parse_mode='HTML')
    except OddsAPIError as e:
        await update.message.reply_text(f"⚠️ {e}")


@H.admin_only
async def cmd_leagues(update, context):
    """Which configured leagues have fixtures in the scan window (free calls, no credits)."""
    msg = update.message
    await msg.reply_text("📅 Checking fixtures (free calls, takes ~20s)…")

    async def job():
        try:
            keys = await model.resolve_sports()
            now, lines = time.time(), []
            for k in keys[:30]:
                n = len(await client.events(k, now + S.LEAD_MIN * 60, now + S.HORIZON_HOURS * 3600))
                lines.append(f"{'✅' if n else '▫️'} {k}: <b>{n}</b> fixture(s)")
        except OddsAPIError as e:
            await msg.reply_text(f"⚠️ {e}")
            return
        head = f"Window: next {S.HORIZON_HOURS}h · each scan uses the first {S.MAX_SPORTS} leagues with fixtures\n"
        await msg.reply_text(head + ("\n".join(lines) if lines else "No league in SPORTS is in season right now."), parse_mode='HTML')

    context.application.create_task(job())


@H.admin_only
async def cmd_apicheck(update, context):
    """Live proof that the bot can reach The Odds API (uses a free call) + the last API calls."""
    await update.message.reply_text("🔌 Contacting The Odds API…")
    try:
        info = await client.ping()
        head = [f"✅ <b>Reached The Odds API</b> — HTTP {info['status']} in {info['ms']} ms",
                f"Sports listed: {info['sports']} ({info['active']} in season)",
                f"Credits remaining: <b>{client.remaining}</b> · used: {client.used}"]
    except OddsAPIError as e:
        head = [f"❌ <b>Could not use The Odds API:</b> {e}"]
    rows = []
    for h in list(client.history)[-8:]:
        t = datetime.fromtimestamp(h["ts"], cfg.TZ).strftime("%H:%M:%S")
        res = f"HTTP {h['status']}" if h["status"] else "no response"
        rows.append(f"<code>{t}</code> {h['path']} → {res}, {h['ms']} ms, cost {h['cost']}, left {h['left']}")
    await update.message.reply_text("\n".join(head + ["", "<b>Recent API calls:</b>"] + (rows or ["(none yet)"])), parse_mode='HTML')


async def startup_check(context):
    try:
        info = await client.ping()
        log.info("Odds API reachable: HTTP %s, %s sports listed, credits remaining %s",
                 info["status"], info["sports"], client.remaining)
    except OddsAPIError as e:
        log.error("Odds API NOT usable: %s", e)
        await core.notify_admins(context.bot, f"⚠️ Odds API check failed at startup: {e}\nTips scans won't work until this is fixed.")


# ---------------- subscriber commands ----------------
async def _reply_long(msg, text):
    """Telegram messages max out at 4096 chars: split on line breaks."""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await msg.reply_text(chunk, parse_mode='HTML')
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await msg.reply_text(chunk.rstrip(), parse_mode='HTML')


async def cmd_today(update, context):
    await db.upsert_user(update.effective_user)
    await _reply_long(update.message, await info.today_text(update.effective_user.id))


async def cmd_results(update, context):
    await db.upsert_user(update.effective_user)
    await _reply_long(update.message, await info.results_text())


async def cmd_about(update, context):
    await update.message.reply_text(info.about_text(), parse_mode='HTML')


async def cmd_support(update, context):
    await update.message.reply_text(info.support_text(), parse_mode='HTML')


def _background(context, msg, make_text, wait_note):
    """Run a slow lookup without blocking other updates (payments!). Never leak API errors to users."""
    async def job():
        try:
            text = await make_text()
        except OddsAPIError as e:
            log.warning("lookup failed: %s", e)
            text = "That information is temporarily unavailable. Please try again in a few minutes."
        await _reply_long(msg, text)

    async def start():
        await msg.reply_text(wait_note)
        await job()

    context.application.create_task(start())


async def cmd_fixtures(update, context):
    await db.upsert_user(update.effective_user)
    _background(context, update.message, lambda: info.fixtures_text(" ".join(context.args)), "📅 Loading fixtures…")


async def cmd_preview(update, context):
    await db.upsert_user(update.effective_user)
    uid, q = update.effective_user.id, " ".join(context.args)
    _background(context, update.message, lambda: info.preview_text(uid, q), "🔎 Pulling the match data…")


async def cmd_table(update, context):
    await db.upsert_user(update.effective_user)
    _background(context, update.message, lambda: info.table_text(" ".join(context.args)), "🏆 Loading the table…")


@H.admin_only
async def cmd_statscheck(update, context):
    """Live proof the football-data key works, which plan/season it covers, and today's request allowance."""
    if not stats.client.enabled():
        await update.message.reply_text("❌ API_FOOTBALL_KEY is not set in .env (get a free key at api-football.com).")
        return
    await update.message.reply_text("🔌 Contacting API-Football…")
    lines = []
    try:
        st = await stats.client.get("/status")  # does not count against the daily quota
        plan = (st.get("subscription") or {}).get("plan", "?")
        req = st.get("requests") or {}
        lines.append(f"✅ <b>Key accepted</b> — plan: {plan} · requests today: <b>{req.get('current', '?')}/{req.get('limit_day', '?')}</b>")
        day = datetime.now(cfg.TZ).strftime("%Y-%m-%d")
        fx = await stats.client.get("/fixtures", ttl=6 * 3600, date=day)
        lines.append(f"✅ Today's fixtures readable: <b>{len(fx)}</b> matches worldwide (current-season data is available)")
        lines.append(f"Requests left today: <b>{stats.client.remaining}</b> (admin drafts keep a reserve of {S.STATS_RESERVE})")
    except stats.StatsError as e:
        lines.append(f"❌ {e}")
        lines.append("<i>If this mentions a plan/season restriction, the free plan can't read current-season data. "
                     "Upgrade the plan on api-football.com, or leave API_FOOTBALL_KEY empty to run without team news.</i>")
    for h in list(stats.client.history)[-6:]:
        t = datetime.fromtimestamp(h["ts"], cfg.TZ).strftime("%H:%M:%S")
        lines.append(f"<code>{t}</code> {h['path']} → {('HTTP ' + str(h['status'])) if h['status'] else 'no response'}, {h['ms']} ms"
                     + (f" ({h['error']})" if h["error"] else ""))
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


@H.admin_only
async def on_slip_cb(update, context):
    """callback_data = slip:<destination|skip>:<slip id>:<preview message id>"""
    q = update.callback_query
    try:
        _, action, sid, mid = q.data.split(":")
        sid, mid = int(sid), int(mid)
    except ValueError:
        await q.answer("Bad button.")
        return
    keep_buttons = True
    if action == "skip":
        out, keep_buttons = await service.skip(sid), False
    elif action in AUDIENCES:
        # the preview photo lives in this same admin chat; copy it exactly as shown
        out = await service.publish(context.bot, sid, q.message.chat_id, mid, action, by=update.effective_user.id)
    else:
        await q.answer("Unknown destination.")
        return
    await q.answer(out, show_alert=True)
    try:
        # Adding parse_mode here ensures any existing HTML tags in the admin preview remain rendered
        await q.edit_message_text(f"{q.message.text}\n\n▸ {action}: {out}",
                                  reply_markup=q.message.reply_markup if keep_buttons else None,
                                  parse_mode='HTML')
    except TelegramError:
        pass


def register(app):
    for name, fn in [("record", cmd_record), ("tips", cmd_tips), ("slips", cmd_slips), ("history", cmd_history),
                     ("tip", cmd_tip), ("settle", cmd_settle), ("credits", cmd_credits),
                     ("apicheck", cmd_apicheck), ("leagues", cmd_leagues), ("statscheck", cmd_statscheck),
                     ("today", cmd_today), ("results", cmd_results), ("about", cmd_about), ("support", cmd_support),
                     ("fixtures", cmd_fixtures), ("preview", cmd_preview), ("table", cmd_table)]:
        app.add_handler(CommandHandler(name, fn, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_slip_cb, pattern=r"^slip:"))

    # extend the role-based command menus + help text defined in handlers.py (must run before setup_commands)
    public_extra = [BotCommand("today", "Latest tips"), BotCommand("fixtures", "Upcoming matches"),
                    BotCommand("results", "Recent results"), BotCommand("preview", "Match preview (VIP): team"),
                    BotCommand("table", "League table: league"), BotCommand("record", "Our track record"),
                    BotCommand("about", "How our tips work"), BotCommand("support", "Get help")]
    admin_extra = [BotCommand("tips", "Generate tips now"), BotCommand("slips", "Draft/open tips"),
                   BotCommand("history", "Saved tips log"), BotCommand("tip", "Tip details: ref"),
                   BotCommand("settle", "Settle: ref won|lost|void"), BotCommand("credits", "Odds API credits"),
                   BotCommand("apicheck", "Test Odds API connection"), BotCommand("statscheck", "Test football data API"),
                   BotCommand("leagues", "Leagues with fixtures")]
    H.PUBLIC_CMDS.extend(public_extra)
    H.ADMIN_CMDS.extend(public_extra + admin_extra)
    H.OWNER_CMDS.extend(public_extra + admin_extra)
    H.USER_HELP += ("\n/today – latest tips\n/fixtures – upcoming matches\n/results – recent results & record\n"
                    "/preview &lt;team&gt; – match preview (VIP)\n/table &lt;league&gt; – league table\n/about – how our tips work\n"
                    "/support – get help")

    for t in S.PICK_TIMES:
        hh, mm = (int(x) for x in t.split(":"))
        app.job_queue.run_daily(service.scheduled_run, time=dtime(hh, mm, tzinfo=cfg.TZ), name=f"tips-{t}")
    app.job_queue.run_once(startup_check, 5, name="odds-api-startup-check")
    app.job_queue.run_repeating(service.settle_job, interval=S.SETTLE_EVERY_MIN * 60, first=180, name="settle")