"""Value-finding model: bookmaker consensus (de-vigged, sharp books weighted) vs the best available price.

fair_prob = weighted average of each bookmaker's margin-free probability
edge      = fair_prob * best_price - 1       (>0 means the best price is better than fair)
It cannot see injuries/lineups/motivation, which is why every slip is reviewed by an admin before publishing."""
import logging
import math
import time
from collections import Counter
from datetime import datetime

from . import settings as S
from .oddsapi import client

log = logging.getLogger(__name__)
_cache = {"ts": 0.0, "cands": []}


def parse_ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def consensus(per_book: dict) -> dict:
    """per_book: {book: {outcome: decimal_price}} (same outcomes for every book)
    -> {outcome: (fair_prob, best_price, best_book, n_books)}"""
    probs, best, wsum, n = {}, {}, 0.0, 0
    for book, prices in per_book.items():
        if not prices or any(p <= 1 for p in prices.values()):
            continue
        inv = {o: 1 / p for o, p in prices.items()}
        total = sum(inv.values())
        w = 3.0 if book in S.SHARP_BOOKS else 1.0
        wsum += w
        n += 1
        for o, v in inv.items():
            probs[o] = probs.get(o, 0.0) + w * v / total
            if o not in best or prices[o] > best[o][0]:
                best[o] = (prices[o], book)
    if not wsum:
        return {}
    return {o: (probs[o] / wsum, best[o][0], best[o][1], n) for o in probs}


def _market_books(event, mkey):
    rows = [(b["key"], m.get("outcomes", [])) for b in event.get("bookmakers", [])
            for m in b.get("markets", []) if m.get("key") == mkey]
    if mkey == "h2h":
        sets = Counter(frozenset(o["name"] for o in outs) for _, outs in rows)
        if not sets:
            return None, {}
        ref = sets.most_common(1)[0][0]
        return None, {bk: {o["name"]: float(o["price"]) for o in outs}
                      for bk, outs in rows if frozenset(o["name"] for o in outs) == ref}
    if mkey == "totals":
        pts = Counter(o.get("point") for _, outs in rows for o in outs if o.get("name") == "Over")
        if not pts:
            return None, {}
        point = pts.most_common(1)[0][0]
        books = {}
        for bk, outs in rows:
            d = {o["name"]: float(o["price"]) for o in outs if o.get("point") == point}
            if set(d) == {"Over", "Under"}:
                books[bk] = d
        return point, books
    return None, {}


def event_candidates(event) -> list[dict]:
    out, ct = [], parse_ts(event["commence_time"])
    for mkey in S.MARKETS_LIST:
        point, books = _market_books(event, mkey)
        if len(books) < S.MIN_BOOKS:
            continue
        for outcome, (p, best, book, n) in consensus(books).items():
            if outcome == "Draw":
                continue
            out.append(dict(sport_key=event["sport_key"], sport_title=event.get("sport_title", ""),
                            event_id=event["id"], home=event["home_team"], away=event["away_team"],
                            commence_at=ct, market=mkey, selection=outcome, point=point, odds=best,
                            best_book=book, fair_prob=p, edge=p * best - 1, books=n))
    return out


last_scan: dict = {}


def qualify_reason(c, now):
    """None if the selection passes every rule, else the first rule it failed."""
    if not (S.MIN_LEG_ODDS <= c["odds"] <= S.MAX_LEG_ODDS):
        return "odds"
    if c["fair_prob"] < S.MIN_PROB:
        return "prob"
    if c["edge"] < S.MIN_EDGE:
        return "edge"
    if not (now + S.LEAD_MIN * 60 <= c["commence_at"] <= now + S.HORIZON_HOURS * 3600):
        return "window"
    return None


def fallback_ok(c, now) -> bool:
    """Looser rules used only on days when nothing passes the strict ones."""
    return (S.MIN_LEG_ODDS <= c["odds"] <= S.MAX_LEG_ODDS and c["fair_prob"] >= S.FALLBACK_MIN_PROB
            and c["edge"] >= S.FALLBACK_MIN_EDGE
            and now + S.LEAD_MIN * 60 <= c["commence_at"] <= now + S.HORIZON_HOURS * 3600)


def qualifies(c, now) -> bool:
    return qualify_reason(c, now) is None


def min_odds(c) -> float:
    """Lowest price at which the pick is still fair value (take it at this or better)."""
    return math.ceil(100 / c["fair_prob"]) / 100


def pick_text(l) -> str:
    if l["market"] == "h2h":
        return "Draw" if l["selection"] == "Draw" else f"{l['selection']} to win"
    return f"{l['selection']} {l['point']:g}"


async def resolve_sports() -> list[str]:
    """Active league keys matching the SPORTS prefixes, in priority order (free call)."""
    active = [x for x in await client.sports() if x.get("active") and not x.get("has_outrights")]
    keys: list[str] = []
    for pref in S.SPORTS:
        keys += [x["key"] for x in active if x["key"].startswith(pref) and x["key"] not in keys]
    return keys


async def _pick_sports(now) -> list[str]:
    chosen = []
    for k in await resolve_sports():  # /events is free -> only spend credits on leagues that have fixtures
        if len(chosen) >= S.MAX_SPORTS:
            break
        if await client.events(k, now + S.LEAD_MIN * 60, now + S.HORIZON_HOURS * 3600):
            chosen.append(k)
    return chosen


def _brief(c) -> str:
    return (f"• {c['home']} v {c['away']} → {pick_text(c)}: {c['fair_prob'] * 100:.0f}% win chance · "
            f"{c['odds']:.2f} · edge {c['edge'] * 100:+.1f}%")


def collect(events, now, stats) -> list[dict]:
    out = []
    fb, near, near_prob = stats.setdefault("fb", []), stats.setdefault("near", []), stats.setdefault("near_prob", [])
    for ev in events:
        stats["events"] += 1
        cs = event_candidates(ev)
        if not cs:
            stats["thin"] += 1
            continue
        for c in cs:
            stats["selections"] += 1
            reason = qualify_reason(c, now)
            if reason:
                stats["rejected"][reason] += 1
                if reason == "edge":
                    near.append(c)  # a favourite, but priced at/below fair value
                elif reason == "prob" and c["edge"] >= S.MIN_EDGE and S.MIN_LEG_ODDS <= c["odds"] <= S.MAX_LEG_ODDS:
                    near_prob.append(c)  # real value, but under the win-chance floor
                if fallback_ok(c, now):
                    fb.append(c)
            else:
                out.append(c)
    return out


def finalize(cands, stats) -> list[dict]:
    """Sort, apply the fallback when nothing qualified, and store the scan summary."""
    fb = sorted(stats.pop("fb", []), key=lambda c: c["fair_prob"], reverse=True)
    stats["near"] = sorted(stats.get("near", []), key=lambda c: c["edge"], reverse=True)[:3]
    stats["near_prob"] = sorted(stats.get("near_prob", []), key=lambda c: c["edge"], reverse=True)[:2]
    cands = sorted(cands, key=lambda c: c["fair_prob"], reverse=True)  # hit-rate first, among positive-edge picks
    if not cands and S.FALLBACK and fb:
        cands, stats["fallback_used"] = fb, True
    stats["qualified"] = len(cands)
    return cands


def describe_scan() -> str:
    s = last_scan
    if not s:
        return "No scan has run yet."
    lines = [f"🔎 Last scan: {s['qualified']} qualifying selection(s)"]
    if s.get("fallback_used"):
        lines.append(f"⚠️ No value picks today, so the FALLBACK was used (win chance ≥ {S.FALLBACK_MIN_PROB * 100:.0f}%, "
                     f"edge ≥ {S.FALLBACK_MIN_EDGE * 100:+.0f}%).")
    if not s["sports"]:
        lines.append(f"None of the leagues in SPORTS has fixtures in the next {S.HORIZON_HOURS}h "
                     "(checked with free calls — no credits used). Use /leagues to see what is on, "
                     "add leagues to SPORTS, or raise HORIZON_HOURS.")
        return "\n".join(lines)
    lines.append("Leagues scanned: " + ", ".join(s["sports"]))
    lines.append(f"Fixtures with odds: {s['events']} ({s['thin']} skipped: fewer than {S.MIN_BOOKS} bookmakers)")
    lines.append(f"Selections checked: {s['selections']}")
    r, parts = s["rejected"], []
    if r["odds"]:
        parts.append(f"odds outside {S.MIN_LEG_ODDS}-{S.MAX_LEG_ODDS}: {r['odds']}")
    if r["prob"]:
        parts.append(f"win chance under {S.MIN_PROB * 100:.0f}%: {r['prob']}")
    if r["edge"]:
        parts.append(f"edge under {S.MIN_EDGE * 100:.0f}%: {r['edge']}")
    if r["window"]:
        parts.append(f"kickoff outside window: {r['window']}")
    if parts:
        lines.append("Rejected: " + " · ".join(parts))
    if s.get("near"):
        lines.append("Closest favourites (edge too thin):\n" + "\n".join(_brief(c) for c in s["near"]))
    if s.get("near_prob"):
        lines.append("Value, but under the win-chance floor:\n" + "\n".join(_brief(c) for c in s["near_prob"]))
    return "\n".join(lines)


async def find_candidates(max_age: int = 0) -> list[dict]:
    """Qualifying selections, best first. max_age>0 reuses a recent scan (saves API credits)."""
    now = time.time()
    if max_age and now - _cache["ts"] < max_age:
        return [c for c in _cache["cands"] if c["commence_at"] >= now + S.LEAD_MIN * 60]
    stats = {"at": now, "sports": [], "events": 0, "thin": 0, "selections": 0, "rejected": Counter(), "qualified": 0}
    cands = []
    stats["sports"] = await _pick_sports(now)
    for key in stats["sports"]:
        cands += collect(await client.odds(key, now + S.LEAD_MIN * 60, now + S.HORIZON_HOURS * 3600), now, stats)
    cands = finalize(cands, stats)
    last_scan.clear()
    last_scan.update(stats)
    _cache.update(ts=now, cands=cands)
    log.info("Scan done. %s | API credits left: %s", describe_scan().replace("\n", " | "), client.remaining)
    return list(cands)


def pick_legs(cands, n, exclude_events) -> list[dict]:
    seen, chosen = set(exclude_events), []
    for c in cands:
        if c["event_id"] in seen:
            continue
        seen.add(c["event_id"])
        chosen.append(c)
        if len(chosen) == n:
            break
    return chosen


def grade(leg, event):
    """-> (result, score_text) where result in won/lost/void or None if not final yet."""
    if not event or not event.get("completed"):
        return None, None
    sc = {}
    for s in event.get("scores") or []:
        try:
            sc[s["name"]] = float(s["score"])
        except (KeyError, TypeError, ValueError):
            pass
    h, a = sc.get(leg["home"]), sc.get(leg["away"])
    if h is None or a is None:
        return None, None
    text = f"{h:g}-{a:g}"
    if leg["market"] == "h2h":
        winner = leg["home"] if h > a else leg["away"] if a > h else "Draw"
        return ("won" if leg["selection"] == winner else "lost"), text
    if leg["market"] == "totals":
        total = h + a
        if total == leg["point"]:
            return "void", text
        return ("won" if (leg["selection"] == "Over") == (total > leg["point"]) else "lost"), text
    return None, None