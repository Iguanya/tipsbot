"""Orchestration: build slips -> admin picks the destination -> publish -> settle -> win card / apology + recovery."""
import asyncio
import logging
import time
from collections import defaultdict

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
from telegram.error import RetryAfter, TelegramError

import broadcast
import config as cfg
import db
import roles
from payments import core
from . import model, slipimg, stats, store
from . import settings as S
from .oddsapi import OddsAPIError, client

log = logging.getLogger(__name__)
EMOJI = {"soccer": "⚽", "basketball": "🏀", "tennis": "🎾", "icehockey": "🏒", "americanfootball": "🏈",
         "baseball": "⚾", "cricket": "🏏", "rugbyunion": "🏉", "rugbyleague": "🏉", "mma": "🥊", "boxing": "🥊"}
_last_check: dict[str, float] = {}
_alerted: set[int] = set()
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def label(slip) -> str:
    if slip["kind"] == "recovery":
        return "🔁 Recovery pick"
    return "💎 VIP slip" if slip["tier"] == "vip" else "🆓 Free tip"


def caption(slip, legs) -> str:
    lines = [f"{label(slip)} · Ref {slip['ref']}", ""]
    for l in legs:
        e = EMOJI.get(l["sport_key"].split("_")[0], "🎯")
        mo = model.min_odds({"fair_prob": l["fair_prob"]})
        lines.append(f"{e} {l['home']} vs {l['away']}\n   ➜ {model.pick_text(l)} @ {l['odds']:.2f} (take ≥ {mo:.2f})")
    lines.append(f"\nTotal odds: {slip['total_odds']:.2f}")
    lines.append("Same stake as usual — never chase losses." if slip["kind"] == "recovery"
                 else "Odds move: only bet at the minimum shown or better.")
    lines.append("18+ | Bet responsibly")
    return "\n".join(lines)


def notes(slip, legs, extra: str = "") -> str:
    rows = [f"• {l['home']} v {l['away']}: fair {l['fair_prob']*100:.0f}% · best {l['odds']:.2f} ({l['best_book']}) · "
            f"edge {l['edge']*100:+.1f}% · {l['books']} books" for l in legs]
    warn = ""
    weakest = min(l["edge"] for l in legs)
    if weakest < S.MIN_EDGE:
        where = "slightly below" if weakest < 0 else "only just above"
        warn = (f"\n\n⚠️ FALLBACK pick: nothing cleared the {S.MIN_EDGE * 100:.0f}% value bar today, so this is the "
                f"best-priced likely winner (edge {weakest * 100:+.1f}%, {where} the market's fair price). It should win "
                "often, but there is no real value edge here. Your call whether to publish.")
    body = (f"🔬 Model notes — tip {slip['ref']} (admins only)\n" + "\n".join(rows) + warn +
            (f"\n\n{extra}" if extra else "") +
            "\n\nLine-ups are only known ~1h before kickoff, so recheck close to the match.")
    return body[:3800]


def chooser(sid, mid) -> Kb:
    cb = lambda aud: f"slip:{aud}:{sid}:{mid}"  # noqa: E731
    rows = [[Btn("🆓 Free", callback_data=cb("free")), Btn("💎 VIP", callback_data=cb("vip"))]]
    if cfg.POST_CHANNELS["vipplus"]:
        rows.append([Btn("👑 VIP+", callback_data=cb("vipplus"))])
    rows.append([Btn("📣 All channels", callback_data=cb("all")), Btn("✉️ DM subscribers", callback_data=cb("dm_subs"))])
    rows.append([Btn("⏭ Skip this tip", callback_data=cb("skip"))])
    return Kb(rows)


# ---------- build & deliver ----------
async def build_slips(kind="regular", tiers=("free", "vip"), parent=None, max_age=0) -> list[int]:
    cands = await model.find_candidates(max_age)
    excl = await store.recent_event_ids()
    ids, seen = [], set()
    for tier in tiers:
        n = S.RECOVERY_LEGS if kind == "recovery" else (1 if tier == "free" else S.SLIP_LEGS)
        legs = model.pick_legs(cands, n, excl)
        if not legs:
            continue
        key = tuple(sorted((l["event_id"], l["market"], l["selection"]) for l in legs))
        if key in seen:  # same pick as the draft we already made: the admin can send that one anywhere
            continue
        seen.add(key)
        ids.append(await store.create_slip(tier, kind, legs, parent))
    return ids


async def deliver_drafts(bot, slip_ids):
    admins = sorted(roles.all_ids())
    for sid in slip_ids:
        slip, legs = await store.get_slip(sid), await store.get_legs(sid)
        png, cap = slipimg.render(slip, legs, cfg.BRAND_NAME), caption(slip, legs)
        try:
            extra = await stats.briefs(legs)  # injuries, form, table, second opinion (cached)
        except Exception:  # noqa: BLE001 - never let team news block a draft
            log.exception("team-news lookup failed")
            extra = ""
        saved, previews = False, []
        for a in admins:
            try:
                m = await bot.send_photo(a, photo=png, caption=cap)
                if not saved:
                    await store.save_draft(sid, cap, m.photo[-1].file_id)  # keep the tip in the database
                    saved = True
                previews.append((a, m.message_id))
                await bot.send_message(a, notes(slip, legs, extra) + "\n\n📍 Where should I send this tip?",
                                       reply_markup=chooser(sid, m.message_id))
            except TelegramError as e:
                log.warning("Draft to admin %s failed: %s", a, e)
        if not saved:
            await store.save_draft(sid, cap, None)
        if S.AUTO_PUBLISH and previews:
            await publish(bot, sid, previews[0][0], previews[0][1], slip["tier"], by=0)


async def publish(bot, sid, chat_id, message_id, audience, by) -> str:
    """Send the preview message to an audience. Can be called again later for another audience."""
    async with _locks[sid]:
        slip = await store.get_slip(sid)
        if not slip or slip["status"] not in ("draft", "open"):
            return "This tip is closed."
        targets = await broadcast.resolve(audience)
        if not targets:
            return f"No destination configured for '{audience}' (check the channel IDs in .env)."
        already = set(await store.posted_chats(sid))
        targets = [t for t in targets if t not in already]
        if not targets:
            return "Already sent there."
        first = slip["status"] == "draft"
        if first:
            await store.transition(sid, "draft", "open", published_at=db.now(), published_by=by)
        sent = await broadcast.copy_many(bot, targets, chat_id, message_id)
        if not sent:
            if first:
                await store.transition(sid, "open", "draft")  # nothing went out: allow another try
            return "Sending failed everywhere — is the bot an admin in the channel?"
        await store.add_posts(sid, audience, sent)
        return f"Sent to {len(sent)} chat(s), {len(targets) - len(sent)} failed."


async def skip(sid) -> str:
    return "Skipped." if await store.transition(sid, "draft", "skipped") else "Can't skip: already published or closed."


async def generate_and_deliver(bot, kind="regular", tiers=("free", "vip"), parent=None, max_age=0) -> list[int]:
    ids = await build_slips(kind, tiers, parent, max_age)
    if ids:
        await deliver_drafts(bot, ids)
    return ids


async def scheduled_run(context):
    try:
        ids = await generate_and_deliver(context.bot, max_age=S.SCAN_REUSE_HOURS * 3600)
    except OddsAPIError as e:
        await core.notify_admins(context.bot, f"⚠️ Tip scan failed: {e}")
        return
    if not ids:
        await core.notify_admins(context.bot, "ℹ️ Scheduled scan: nothing drafted.\n\n" + model.describe_scan() +
                                 "\n\nPost manually, or relax the thresholds in .env.")


# ---------- posting helpers ----------
async def _post(bot, targets, photo=None, text=None, cap=None):
    file_id, ok = None, 0
    for t in dict.fromkeys(targets):
        for _ in range(3):
            try:
                if photo is not None:
                    m = await bot.send_photo(t, photo=file_id or photo, caption=cap)
                    file_id = file_id or m.photo[-1].file_id
                else:
                    await bot.send_message(t, text)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError as e:
                log.warning("post to %s failed: %s", t, e)
                break
        await asyncio.sleep(0.05)
    return ok


async def _result_targets(slip, win: bool) -> list[int]:
    """Results go to the chats the tip was actually sent to; VIP wins are also shown in the free channels."""
    chats = await store.posted_chats(slip["id"]) or await broadcast.resolve(slip["tier"])
    if win:
        vip = set(cfg.POST_CHANNELS["vip"]) | set(cfg.POST_CHANNELS["vipplus"])
        if vip & set(chats):
            chats = chats + await broadcast.resolve("free")
    return list(dict.fromkeys(chats))


# ---------- settlement ----------
async def settle_job(context):
    bot, now = context.bot, db.now()
    legs = await db.fetchall(
        """SELECT l.*, s.ref AS slip_ref FROM legs l JOIN slips s ON s.id=l.slip_id
           WHERE s.status='open' AND l.status='open' AND l.commence_at + ? < ?""", (S.SETTLE_DELAY_MIN * 60, now))
    by_sport: dict[str, list] = {}
    for l in legs:
        by_sport.setdefault(l["sport_key"], []).append(l)
    touched = set()
    for sport, ls in by_sport.items():
        if time.time() - _last_check.get(sport, 0) < S.SETTLE_EVERY_MIN * 60 - 30:
            continue
        _last_check[sport] = time.time()
        try:
            events = {e["id"]: e for e in await client.scores(sport, 3)}
        except OddsAPIError as e:
            log.warning("scores %s failed: %s", sport, e)
            continue
        for l in ls:
            res, score = model.grade(l, events.get(l["event_id"]))
            if res:
                await store.set_leg(l["id"], res, score)
                touched.add(l["slip_id"])
            elif now - l["commence_at"] > 3 * 86400 - 7200 and l["slip_id"] not in _alerted:
                _alerted.add(l["slip_id"])
                await core.notify_admins(bot, f"⚠️ No result found for tip {l['slip_ref']} ({l['home']} v {l['away']}). "
                                              f"Settle it manually: /settle {l['slip_ref']} won|lost|void")
    for sid in touched:
        await evaluate(bot, sid)


async def evaluate(bot, sid):
    legs = await store.get_legs(sid)
    st = [l["status"] for l in legs]
    if "lost" in st:
        result = "lost"
    elif "open" in st:
        return
    elif all(s == "void" for s in st):
        result = "void"
    else:
        result = "won"
    eff = 1.0
    for l in legs:
        if l["status"] == "won":
            eff *= l["odds"]
    if await store.transition(sid, "open", result, settled_at=db.now(), result_odds=round(eff, 2)):
        await announce(bot, sid, result)


async def manual_settle(bot, token, result) -> str:
    slip = await store.find_slip(token)
    if not slip or slip["status"] != "open":
        return "Tip not found, or it isn't open (only published, unsettled tips can be settled)."
    legs = await store.get_legs(slip["id"])
    open_legs = [l for l in legs if l["status"] == "open"]
    if result == "lost":
        if not any(l["status"] == "lost" for l in legs) and open_legs:
            await store.set_leg(open_legs[0]["id"], "lost")
            open_legs = open_legs[1:]
        for l in open_legs:
            await store.set_leg(l["id"], "void")
    else:
        for l in open_legs:
            await store.set_leg(l["id"], result)
    await evaluate(bot, slip["id"])
    return f"Tip {slip['ref']} settled as {result}."


async def announce(bot, sid, result):
    slip, legs = await store.get_slip(sid), await store.get_legs(sid)
    ref = slip["ref"]
    if result == "won":
        rec = await store.record(30)
        cap = (f"✅ {label(slip)} · Ref {ref} WON @ {slip['result_odds']:.2f}\n\n"
               f"📈 Last 30 days: {rec['won']}W–{rec['lost']}L ({rec['rate']:.0f}%) · {rec['units']:+.1f} units at flat stakes\n"
               "Every tip we publish is tracked — wins and losses.\n\n18+ | Bet responsibly")
        await _post(bot, await _result_targets(slip, True),
                    photo=slipimg.render(slip, legs, cfg.BRAND_NAME, "won"), cap=cap)
    elif result == "lost":
        text = (f"😔 {label(slip)} (Ref {ref}) didn't come through, and we're sorry about that.\n\n"
                "We publish every result — wins and losses — and this one is a loss. It's part of betting.\n"
                "Our next selection will follow as soon as one meets our criteria. Please keep your stakes the same "
                "as usual and never chase losses.\n\n18+ | Bet responsibly")
        await _post(bot, await _result_targets(slip, False), text=text)
        if slip["kind"] == "regular" and S.AUTO_RECOVERY and not await store.recovery_today(slip["tier"]):
            try:
                ids = await generate_and_deliver(bot, "recovery", (slip["tier"],), sid, max_age=4 * 3600)
            except OddsAPIError as e:
                ids = []
                log.warning("recovery scan failed: %s", e)
            if not ids:
                await core.notify_admins(bot, f"ℹ️ Tip {ref} lost, but no qualifying recovery pick was found. "
                                              "Post one manually when you have it.")
    else:
        await core.notify_admins(bot, f"ℹ️ Tip {ref} was void (all legs void/pushed).")
    await core.notify_admins(bot, f"📣 Tip {ref} settled: {result.upper()}")