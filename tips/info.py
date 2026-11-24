"""Subscriber-facing information (pure data -> text). Telegram wrappers live in commands.py."""
import asyncio
import logging
import re
import time
from datetime import datetime

import config as cfg
import db
import roles
from . import model, service, stats, store
from . import settings as S
from .oddsapi import client as odds

log = logging.getLogger(__name__)
_up = {"ts": 0.0, "items": []}
_lock = asyncio.Lock()
FOOTER = "<i>Information only — not betting advice. 18+ | Bet responsibly.</i>"


def _d(ts) -> str:
    return datetime.fromtimestamp(ts, cfg.TZ).strftime("%d %b")


# ---------------- access & usage ----------------
async def is_vip(user_id) -> bool:
    return roles.is_admin(user_id) or bool(await db.active_subs(user_id))


_usage_ready = False


async def _ensure_usage_table():
    global _usage_ready
    if not _usage_ready:
        await db.execute("CREATE TABLE IF NOT EXISTS usage(user_id INTEGER, day TEXT, kind TEXT, n INTEGER DEFAULT 0, "
                         "PRIMARY KEY(user_id, day, kind))")
        _usage_ready = True


def _day() -> str:
    return datetime.now(cfg.TZ).strftime("%Y-%m-%d")


async def used(user_id, kind) -> int:
    await _ensure_usage_table()
    return await db.scalar("SELECT n FROM usage WHERE user_id=? AND day=? AND kind=?", (user_id, _day(), kind)) or 0


async def bump(user_id, kind):
    await _ensure_usage_table()
    await db.execute("INSERT INTO usage(user_id,day,kind,n) VALUES(?,?,?,1) "
                     "ON CONFLICT(user_id,day,kind) DO UPDATE SET n=n+1", (user_id, _day(), kind))


# ---------------- /today ----------------
async def today_text(user_id) -> str:
    rows = await db.fetchall("SELECT * FROM slips WHERE status IN ('open','won','lost') AND published_at>=? "
                             "ORDER BY published_at DESC", (db.now() - 24 * 3600,))
    vip, shown, hidden = await is_vip(user_id), [], 0
    tag = {"open": "⏳ Pending", "won": "✅ Won", "lost": "❌ Lost"}
    for s in rows:
        aud = (s["audience"] or "").split(",")
        if "free" in aud or "all" in aud or vip:
            cap = s["caption"] or service.caption(s, await store.get_legs(s["id"]))
            # Make the status pop out
            shown.append(f"<b>{cap}</b>\n└ <i>Status: {tag[s['status']]}</i>")
        else:
            hidden += 1
            
    out = ["\n\n".join(shown)] if shown else []
    if hidden:
        out.append(f"🔒 <b>{hidden} VIP tip(s)</b> published in the last 24h — /subscribe to unlock them.")
    if not out:
        times = " and ".join(S.PICK_TIMES)
        return f"No tips published in the last 24 hours. We normally publish around <b>{times}</b> (EAT). Try /fixtures to see what's on."
    return "\n\n".join(out)


# ---------------- /results ----------------
async def results_text(limit=10) -> str:
    rec = await store.record(30)
    if rec["n"]:
        head = (f"📈 <b>Last 30 days:</b> {rec['won']}W–{rec['lost']}L (<b>{rec['rate']:.0f}%</b>) · "
                f"<b>{rec['units']:+.1f} units</b> at flat stakes")
    else:
        head = "No settled tips yet — every result will appear here."
        
    rows = await db.fetchall("SELECT * FROM slips WHERE status IN ('won','lost') ORDER BY settled_at DESC LIMIT ?", (limit,))
    lines = [head, ""]
    for s in rows:
        legs = await store.get_legs(s["id"])
        if not legs:
            continue
        more = f" <i>+{len(legs) - 1} more</i>" if len(legs) > 1 else ""
        odds_ = s["result_odds"] if s["status"] == "won" and s["result_odds"] else s["total_odds"]
        # Use code block for odds to make them stand out
        lines.append(f"{'✅' if s['status'] == 'won' else '❌'} <code>{s['ref']}</code> · {_d(s['settled_at'])} · "
                     f"{legs[0]['home']} v {legs[0]['away']}: <b>{model.pick_text(legs[0])}</b>{more} @ <code>{odds_:.2f}</code>")
    lines.append(f"\n<i>Every published tip is tracked — wins and losses.</i>\n{FOOTER}")
    return "\n".join(lines)


# ---------------- /fixtures ----------------
async def _fetch_upcoming():
    now, keys = time.time(), await model.resolve_sports()
    sem = asyncio.Semaphore(4)

    async def one(k):
        async with sem:
            return k, await odds.events(k, now, now + S.HORIZON_HOURS * 3600)  # free call, no credits

    items = []
    for k, evs in await asyncio.gather(*(one(k) for k in keys[:30])):
        for e in evs:
            items.append({"key": k, "league": e.get("sport_title") or k, "home": e["home_team"],
                          "away": e["away_team"], "ts": model.parse_ts(e["commence_time"])})
    items.sort(key=lambda x: x["ts"])
    return items


async def upcoming(max_age=1800):
    async with _lock:
        if time.time() - _up["ts"] > max_age:
            _up.update(ts=time.time(), items=await _fetch_upcoming())
        return list(_up["items"])


async def fixtures_text(query="") -> str:
    items, q = await upcoming(), query.strip().lower()
    if q:
        items = [i for i in items if q in i["key"].lower() or q in i["league"].lower()
                 or q in stats.norm(i["home"]) or q in stats.norm(i["away"])]
    if not items:
        return f"No matches found in the next {S.HORIZON_HOURS}h" + (f" for '<b>{query}</b>'." if q else ".")
        
    lines = [f"📅 <b>Upcoming matches (EAT)</b>" + (f" — <i>{query}</i>" if q else "")]
    day = None
    
    for i in items[:25]:
        dt = datetime.fromtimestamp(i["ts"], cfg.TZ)
        if dt.date() != day:
            day = dt.date()
            lines.append(f"\n🗓 <b>{dt.strftime('%a %d %b')}</b>")
        # Use <code> for the time so it aligns nicely
        lines.append(f"<code>{dt.strftime('%H:%M')}</code> · {i['home']} v {i['away']} · <i>{i['league']}</i>")
        
    if len(items) > 25:
        lines.append(f"\n…and <b>{len(items) - 25} more</b>. Narrow it down, e.g. <code>/fixtures epl</code>")
        
    lines.append("\n💡 <i>Want detail on a match? VIP members can use</i> <code>/preview &lt;team&gt;</code>.")
    return "\n".join(lines)


# ---------------- /preview (VIP) ----------------
def _match_team(items, query):
    q, best = stats.norm(query), None
    for it in items:
        s = max(stats.similarity(query, it["home"]), stats.similarity(query, it["away"]))
        if q and (q in stats.norm(it["home"]) or q in stats.norm(it["away"])):
            s = max(s, 0.95)
        if s >= 0.7 and (best is None or (s, -it["ts"]) > (best[0], -best[1]["ts"])):
            best = (s, it)
    return best[1] if best else None


async def preview_text(user_id, query) -> str:
    if not stats.client.enabled():
        return "Match previews aren't switched on yet."
    if not await is_vip(user_id):
        return "🔒 <b>VIP Only</b>\nMatch previews (form, injuries, head-to-head, table) are for VIP members. See /subscribe."
    if not query.strip():
        return "Usage: <code>/preview &lt;team&gt;</code>   e.g. <code>/preview Arsenal</code>"
    if not roles.is_admin(user_id) and await used(user_id, "preview") >= S.PREVIEW_PER_DAY:
        return f"You've used today's <b>{S.PREVIEW_PER_DAY}</b> previews. They reset at midnight (EAT)."
        
    it = _match_team(await upcoming(), query)
    if not it:
        return f"No upcoming match found for '<b>{query}</b>' in the next {S.HORIZON_HOURS}h in our leagues. Try /fixtures."
        
    try:
        info = await stats.match_info(it["home"], it["away"], it["ts"], reserve=S.STATS_RESERVE)
    except stats.StatsError as e:
        log.warning("preview failed: %s", e)
        return "⚠️ Football data is unavailable right now. Please try again later."
        
    if not info:
        return "I couldn't find that fixture in the football data yet. Try again closer to kickoff."
        
    await bump(user_id, "preview")
    return stats.format_info(info) + "\n\n" + FOOTER


# ---------------- /table ----------------
def resolve_league(query):
    q = re.sub(r"[^a-z0-9]", "", query.lower())
    if q in stats.TABLE_ALIASES:
        return stats.TABLE_ALIASES[q]
    if len(q) >= 4:
        for alias, lid in stats.TABLE_ALIASES.items():
            if len(alias) >= 4 and (q in alias or alias in q):
                return lid
    return None


async def table_text(query) -> str:
    if not stats.client.enabled():
        return "League tables aren't switched on yet."
    lid = resolve_league(query) if query.strip() else None
    if not lid:
        return f"Usage: <code>/table &lt;league&gt;</code>\nTry: <i>{stats.TABLE_HELP}</i>"
        
    try:
        t = await stats.standings(lid, stats.season_for(time.time()), reserve=S.STATS_RESERVE)
    except stats.StatsError as e:
        log.warning("table failed: %s", e)
        return "⚠️ Football data is unavailable right now. Please try again later."
        
    if not t["rows"]:
        return "No table available for that competition right now."
        
    # Use <pre> block so the columns align perfectly on Telegram
    lines = [f"🏆 <b>{t['name']}</b>\n"]
    lines.append("<pre>")
    lines.append(f"{'Pos':<4} {'Team':<18} {'P':<3} {'GD':<4} {'Pts':<4}")
    lines.append("-" * 36)
    
    for r in t["rows"][:20]:
        a = r.get("all") or {}
        team_name = r['team']['name']
        # Truncate long team names so they don't break the alignment
        if len(team_name) > 16:
            team_name = team_name[:14] + ".."
            
        gd = f"{r.get('goalsDiff', 0):+d}" if r.get('goalsDiff') is not None else "0"
        pts = r['points']
        
        lines.append(f"{r['rank']:>2}.  {team_name:<16} {a.get('played', '?'):>2} {gd:>3}  {pts:>3}")
        
    lines.append("</pre>")
    return "\n".join(lines)


# ---------------- static pages ----------------
def about_text() -> str:
    return (f"ℹ️ <b>About {cfg.BRAND_NAME}</b>\n\n"
            "• We compare bookmaker prices across Europe and check form and team news before we publish.\n"
            "• Every tip carries a reference number, and every result — wins and losses — is posted and tracked "
            "(/results, /record).\n"
            "• <b>Take at odds</b> show the lowest price that is still worth it. If your bookmaker pays less, skip it.\n"
            "• <i>No tip is guaranteed. Stake the same small amount each time and never chase losses.</i>\n\n"
            "<b>18+ only.</b> If betting stops being fun, take a break and talk to someone you trust.")


def support_text() -> str:
    who = S.SUPPORT_CONTACT or "an admin in our channel"
    return (f"🛟 <b>Need help with payments or access?</b>\n\n"
            f"Contact {who}.\n"
            "Please include your Telegram ID (send <code>/myid</code>) and, for payments, your M-Pesa code.")