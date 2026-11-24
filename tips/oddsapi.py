"""Thin async client for The Odds API v4 (https://the-odds-api.com).
Free calls: /sports, /events.  Paid calls: /odds = markets x regions credits, /scores = 2 credits (daysFrom).
Every call is logged (path, status, latency, credits) WITHOUT the API key, and kept in `client.history`."""
import logging
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from . import settings as S

log = logging.getLogger(__name__)
BASE = "https://api.the-odds-api.com/v4"


class OddsAPIError(Exception):
    pass


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class Client:
    def __init__(self):
        self.remaining: int | None = None
        self.used: int | None = None
        self.history: deque = deque(maxlen=20)  # newest last

    def _record(self, path, status, started, last=None, error=None):
        ms = int((time.perf_counter() - started) * 1000)
        self.history.append({"ts": time.time(), "path": path, "status": status, "ms": ms,
                             "cost": last, "left": self.remaining, "error": error})
        if error:
            log.error("OddsAPI GET %s FAILED after %d ms: %s", path, ms, error)
        else:
            log.info("OddsAPI GET %s -> HTTP %s in %d ms (cost %s, credits left %s)",
                     path, status, ms, last, self.remaining)

    async def _get(self, path, **params):
        if not S.ODDS_API_KEY:
            raise OddsAPIError("ODDS_API_KEY is not set in .env")
        params["apiKey"] = S.ODDS_API_KEY
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(BASE + path, params=params)
        except httpx.HTTPError as e:
            self._record(path, None, started, error=f"network error: {e}")
            raise OddsAPIError(f"network error: {e}") from e
        self.remaining = _int(r.headers.get("x-requests-remaining")) if "x-requests-remaining" in r.headers else self.remaining
        self.used = _int(r.headers.get("x-requests-used")) if "x-requests-used" in r.headers else self.used
        last = r.headers.get("x-requests-last")
        if r.status_code != 200:
            self._record(path, r.status_code, started, last, error=r.text[:120])
            raise OddsAPIError(f"HTTP {r.status_code}: {r.text[:200]}")
        self._record(path, r.status_code, started, last)
        return r.json()

    def _guard(self):
        if self.remaining is not None and self.remaining < S.MIN_CREDITS:
            raise OddsAPIError(f"Only {self.remaining} Odds API credits left (floor {S.MIN_CREDITS}). "
                               "Upgrade the plan or wait for the monthly reset.")

    async def ping(self) -> dict:
        """Free live test: proves the key works and the API is reachable."""
        data = await self.sports()
        h = self.history[-1]
        return {"status": h["status"], "ms": h["ms"], "sports": len(data),
                "active": sum(1 for s in data if s.get("active"))}

    async def sports(self):
        return await self._get("/sports/")

    async def events(self, sport, t_from, t_to):
        return await self._get(f"/sports/{sport}/events/", dateFormat="iso",
                               commenceTimeFrom=iso(t_from), commenceTimeTo=iso(t_to))

    async def odds(self, sport, t_from, t_to):
        self._guard()
        return await self._get(f"/sports/{sport}/odds/", regions=S.REGIONS, markets=S.MARKETS, oddsFormat="decimal",
                               dateFormat="iso", commenceTimeFrom=iso(t_from), commenceTimeTo=iso(t_to))

    async def scores(self, sport, days=3):
        self._guard()
        return await self._get(f"/sports/{sport}/scores/", daysFrom=days, dateFormat="iso")


client = Client()