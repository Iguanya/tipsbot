"""Football context from API-Football (api-sports.io): fixture matching, injuries, form/predictions, league tables.
Free plan = 100 requests/day, so every response is cached in SQLite and subscriber lookups keep a reserve for admin drafts."""
import difflib
import json
import logging
import re
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone

import httpx

import config as cfg
import db
from . import settings as S

log = logging.getLogger(__name__)
BASE = "https://v3.football.api-sports.io"

# /table shortcuts -> API-Football league id
TABLE_ALIASES = {"epl": 39, "premierleague": 39, "pl": 39, "championship": 40, "ligue1": 61, "bundesliga": 78,
                 "seriea": 135, "laliga": 140, "eredivisie": 88, "primeira": 94, "portugal": 94, "belgium": 144,
                 "scotland": 179, "turkey": 203, "superlig": 203, "championsleague": 2, "ucl": 2, "europaleague": 3,
                 "uel": 3, "conference": 848, "uecl": 848}
TABLE_HELP = "epl, laliga, seriea, bundesliga, ligue1, ucl, uel, championship, eredivisie, primeira, belgium, scotland, turkey"


class StatsError(Exception):
    pass


_schema_ready = False


async def _ensure_schema():
    """Self-healing: create the cache table on first use, whatever version of store.py is installed."""
    global _schema_ready
    if not _schema_ready:
        await db.execute("CREATE TABLE IF NOT EXISTS stats_cache(key TEXT PRIMARY KEY, value TEXT, ts INTEGER)")
        _schema_ready = True


# ---------------- client ----------------
class Client:
    def __init__(self):
        self.remaining: int | None = None
        self.limit: int | None = None
        self.history: deque = deque(maxlen=20)

    def enabled(self) -> bool:
        return bool(S.API_FOOTBALL_KEY)

    def _record(self, path, status, started, error=None):
        ms = int((time.perf_counter() - started) * 1000)
        self.history.append({"ts": time.time(), "path": path, "status": status, "ms": ms, "left": self.remaining, "error": error})
        if error:
            log.error("FootballAPI GET %s FAILED after %d ms: %s", path, ms, error)
        else:
            log.info("FootballAPI GET %s -> HTTP %s in %d ms (requests left today: %s)", path, status, ms, self.remaining)

    async def get(self, path, ttl=0, reserve=0, **params):
        """Returns the API's `response` payload. ttl>0 caches it in SQLite. reserve = requests that must stay unused."""
        if not self.enabled():
            raise StatsError("API_FOOTBALL_KEY is not set in .env")
        await _ensure_schema()
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        if ttl:
            hit = await db.fetchone("SELECT value, ts FROM stats_cache WHERE key=?", (key,))
            if hit and db.now() - hit["ts"] < ttl:
                return json.loads(hit["value"])
        if self.remaining is not None and self.remaining <= reserve:
            raise StatsError(f"daily football-data allowance is nearly used up ({self.remaining} left)")
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(BASE + path, params=params, headers={"x-apisports-key": S.API_FOOTBALL_KEY})
        except httpx.HTTPError as e:
            self._record(path, None, started, error=f"network error: {e}")
            raise StatsError(f"network error: {e}") from e
        for attr, h in (("remaining", "x-ratelimit-requests-remaining"), ("limit", "x-ratelimit-requests-limit")):
            if h in r.headers:
                try:
                    setattr(self, attr, int(float(r.headers[h])))
                except ValueError:
                    pass
        if r.status_code != 200:
            self._record(path, r.status_code, started, error=r.text[:120])
            raise StatsError(f"HTTP {r.status_code}")
        body = r.json()
        errors = body.get("errors")  # API-Football reports plan/limit problems with HTTP 200 + an errors object
        if errors:
            msg = "; ".join(f"{k}: {v}" for k, v in errors.items()) if isinstance(errors, dict) else str(errors)
            self._record(path, 200, started, error=msg[:160])
            raise StatsError(msg[:200])
        self._record(path, 200, started)
        data = body.get("response", [])
        if ttl:
            await db.execute("INSERT OR REPLACE INTO stats_cache(key,value,ts) VALUES(?,?,?)", (key, json.dumps(data), db.now()))
        return data


client = Client()


# ---------------- team-name matching (Odds API names differ from API-Football) ----------------
_STOP = {"fc", "cf", "afc", "sc", "ac", "as", "ssc", "us", "ud", "cd", "rcd", "fk", "sk", "bk", "if", "sv", "vfb",
         "vfl", "tsg", "fsv", "bsc", "ssv", "calcio", "club", "de", "the", "and", "1", "04", "05", "09", "96", "98"}
_ALIASES = {"utd": "united", "man": "manchester", "spurs": "tottenham", "wolves": "wolverhampton",
            "nottm": "nottingham", "psg": "paris saint germain"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", " and "))
    return " ".join(_ALIASES.get(t, t) for t in s.split() if t not in _STOP)


def similarity(a: str, b: str) -> float:
    ca, cb = norm(a).replace(" ", ""), norm(b).replace(" ", "")
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    if min(len(ca), len(cb)) >= 4 and (ca in cb or cb in ca):
        return 0.92
    return difflib.SequenceMatcher(None, ca, cb).ratio()


def season_for(ts: float) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.year if d.month >= 7 else d.year - 1


def _ord(n) -> str:
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ---------------- data ----------------
async def find_fixture(home, away, kickoff_ts, reserve=0):
    day = datetime.fromtimestamp(kickoff_ts, timezone.utc).strftime("%Y-%m-%d")
    best = None
    for f in await client.get("/fixtures", ttl=6 * 3600, reserve=reserve, date=day):
        if abs(f["fixture"]["timestamp"] - kickoff_ts) > 3 * 3600:
            continue
        score = min(similarity(home, f["teams"]["home"]["name"]), similarity(away, f["teams"]["away"]["name"]))
        if score >= 0.72 and (best is None or score > best[0]):
            best = (score, f)
    return best[1] if best else None


async def standings(league_id, season, reserve=0):
    data = await client.get("/standings", ttl=24 * 3600, reserve=reserve, league=league_id, season=season)
    if not data:
        return {"name": "", "rows": []}
    lg = data[0]["league"]
    return {"name": lg.get("name", ""), "rows": [r for group in lg.get("standings", []) for r in group]}


async def match_info(home, away, kickoff_ts, reserve=0):
    fx = await find_fixture(home, away, kickoff_ts, reserve)
    if not fx:
        return None
    fid = fx["fixture"]["id"]
    info = {"fixture": fx, "injuries": [], "prediction": None, "table": "", "errors": []}
    try:
        info["injuries"] = await client.get("/injuries", ttl=3 * 3600, reserve=reserve, fixture=fid)
        pred = await client.get("/predictions", ttl=3 * 3600, reserve=reserve, fixture=fid)
        info["prediction"] = pred[0] if pred else None
        t = await standings(fx["league"]["id"], fx["league"]["season"], reserve)
        pos = {r["team"]["id"]: r for r in t["rows"]}
        h, a = pos.get(fx["teams"]["home"]["id"]), pos.get(fx["teams"]["away"]["id"])
        if h and a:
            info["table"] = (f"Table: {fx['teams']['home']['name']} {_ord(h['rank'])} ({h['points']} pts) · "
                             f"{fx['teams']['away']['name']} {_ord(a['rank'])} ({a['points']} pts)")
    except StatsError as e:
        info["errors"].append(str(e))
    return info


# ---------------- formatting ----------------
def _pct(s):
    try:
        return int(str(s).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _form(team_block):
    return ((team_block or {}).get("league", {}) or {}).get("form", "")[-5:] or "n/a"


def format_info(info) -> str:
    fx = info["fixture"]
    th = fx["teams"]["home"]["name"]
    ta = fx["teams"]["away"]["name"]
    ko = datetime.fromtimestamp(fx["fixture"]["timestamp"], cfg.TZ).strftime("%a %d %b %H:%M")
    league = fx['league']['name']
    
    # Header
    lines = [
        f"<b>⚽ {th} vs {ta}</b>",
        f"<i>🏆 {league}</i> | 📅 {ko} EAT\n"
    ]
    
    pred = info.get("prediction")
    
    # League Table
    if info.get("table"):
        lines.append(f"🏆 <b>{info['table']}</b>\n")
        
    if pred:
        t = pred.get("teams", {})
        
        # Form
        lines.append("📈 <b>Recent Form (Last 5):</b>")
        lines.append(f"  🔴 <b>{th}:</b> <code>{_form(t.get('home'))}</code>")
        lines.append(f"  🔵 <b>{ta}:</b> <code>{_form(t.get('away'))}</code>\n")
        
        # API-Football View
        pc = (pred.get("predictions") or {}).get("percent") or {}
        if pc:
            lines.append("📊 <b>API-Football View:</b>")
            lines.append(f"  {th}: <b>{pc.get('home', '?')}</b> | Draw: <b>{pc.get('draw', '?')}</b> | {ta}: <b>{pc.get('away', '?')}</b>\n")
            
        # Advice
        advice = (pred.get("predictions") or {}).get("advice")
        if advice:
            lines.append(f"💡 <b>Advice:</b> <i>{advice}</i>\n")
            
        # Head-to-head
        hid, aid = (t.get("home") or {}).get("id"), (t.get("away") or {}).get("id")
        tally, n = {hid: 0, aid: 0, "d": 0}, 0
        for m in pred.get("h2h") or []:
            gh, ga = (m.get("goals") or {}).get("home"), (m.get("goals") or {}).get("away")
            if gh is None or ga is None:
                continue
            n += 1
            tally["d" if gh == ga else m["teams"]["home"]["id"] if gh > ga else m["teams"]["away"]["id"]] += 1
        if n:
            lines.append("⚔️ <b>Head-to-Head:</b>")
            lines.append(f"  {th}: <b>{tally[hid]}W</b> | <b>{tally['d']}D</b> | {ta}: <b>{tally[aid]}W</b>\n")
            
    # Injuries
    inj = info.get("injuries") or []
    if inj:
        lines.append("🏥 <b>Absentees (? = doubtful):</b>")
        by: dict[str, list[str]] = {}
        for r in inj:
            p = r.get("player") or {}
            q = "?" if str(p.get("type", "")).lower().startswith("quest") else ""
            reason = p.get('reason') or 'unavailable'
            by.setdefault(r["team"]["name"], []).append(f"{p.get('name', '?')} ({reason}){q}")
            
        for team, players in by.items():
            lines.append(f"  <b>{team}:</b>")
            # Show up to 4 players per team to keep it tidy
            display_players = players[:4]
            for p in display_players:
                lines.append(f"    • {p}")
            if len(players) > 4:
                lines.append(f"    • <i>+{len(players)-4} more...</i>")
        lines.append("")
    else:
        lines.append("🏥 <b>Absentees:</b> None reported yet.\n")
        
    # Errors
    for e in info.get("errors") or []:
        lines.append(f"⚠️ <i>Missing data: {e}</i>")
        
    return "\n".join(lines)

def disagreement(info, leg) -> str:
    """Warn admins when API-Football rates our selection much lower than the bookmaker consensus does."""
    pred = info.get("prediction")
    if leg["market"] != "h2h" or not pred:
        return ""
    fx = info["fixture"]["teams"]
    h, a = similarity(leg["selection"], fx["home"]["name"]), similarity(leg["selection"], fx["away"]["name"])
    if max(h, a) < 0.7:
        return ""
    pct = _pct(((pred.get("predictions") or {}).get("percent") or {}).get("home" if h >= a else "away"))
    if pct is not None and leg["fair_prob"] * 100 - pct >= 15:
        return (f"⚠️ Models disagree: the market gives {leg['selection']} {leg['fair_prob'] * 100:.0f}% but API-Football "
                f"gives {pct}%. Check team news carefully before publishing.")
    return ""


async def briefs(legs) -> str:
    """Team-news block for the admin notes of a draft."""
    if not client.enabled():
        return ("ℹ️ Team news is switched off. Add API_FOOTBALL_KEY to .env to see injuries, form and a second opinion "
                "on every pick.")
    blocks = []
    for l in legs:
        try:
            info = await match_info(l["home"], l["away"], l["commence_at"])
        except StatsError as e:
            blocks.append(f"ℹ️ {l['home']} v {l['away']}: football data unavailable ({e}).")
            continue
        if not info:
            blocks.append(f"ℹ️ {l['home']} v {l['away']}: no matching fixture in the football data (team names may differ). "
                          "Check team news manually.")
            continue
        warn = disagreement(info, l)
        blocks.append(format_info(info) + (f"\n{warn}" if warn else ""))
    return "\n\n".join(blocks)