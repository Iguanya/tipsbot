"""Tips-engine settings (all from .env; config.py has already called load_dotenv())."""
import os


def _list(name, default):
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

# Sport-key PREFIXES in priority order. Add e.g. cricket_, rugbyunion_, icehockey_nhl, mma_ ...
SPORTS = _list("SPORTS", "soccer_uefa_champs_league,soccer_epl,soccer_spain_la_liga,soccer_italy_serie_a,"
                         "soccer_germany_bundesliga,soccer_france_ligue_one,soccer_uefa_europa,soccer_efl_champ,"
                         "soccer_netherlands_eredivisie,soccer_portugal_primeira_liga,soccer_turkey_super_league,"
                         "soccer_belgium_first_div,soccer_scotland_premiership,soccer_fa_cup,soccer_england_efl_cup,"
                         "soccer_uefa_nations_league")
MAX_SPORTS = int(os.getenv("MAX_SPORTS", "6"))        # sports scanned per run (credit control)
REGIONS = os.getenv("REGIONS", "eu")                   # each extra region multiplies credit cost
MARKETS_LIST = _list("MARKETS", "h2h")                 # h2h, totals  (each extra market multiplies cost)
MARKETS = ",".join(MARKETS_LIST)
SHARP_BOOKS = set(_list("SHARP_BOOKS", "pinnacle,betfair_ex_eu"))  # weighted x3 in the consensus

# Selection rules
MIN_BOOKS = int(os.getenv("MIN_BOOKS", "4"))           # bookmakers needed for a trustworthy consensus
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.01"))        # best price must beat fair price by >=1%
MIN_PROB = float(os.getenv("MIN_PROB", "0.50"))        # model probability floor (favours hit-rate)
MIN_LEG_ODDS = float(os.getenv("MIN_LEG_ODDS", "1.30"))  # shorter than this pays too little to bother
MAX_LEG_ODDS = float(os.getenv("MAX_LEG_ODDS", "2.30"))
# Fallback: only used when NO selection meets the strict rules above (so there is still a draft for admins).
# It picks the most likely winners whose price is slightly BELOW the market's fair price. Marked clearly in admin notes.
FALLBACK = _bool("FALLBACK", True)
FALLBACK_MIN_PROB = float(os.getenv("FALLBACK_MIN_PROB", "0.55"))
FALLBACK_MIN_EDGE = float(os.getenv("FALLBACK_MIN_EDGE", "-0.02"))
SLIP_LEGS = int(os.getenv("SLIP_LEGS", "2"))           # VIP slip size (free tip is always 1 leg)
RECOVERY_LEGS = int(os.getenv("RECOVERY_LEGS", "1"))
HORIZON_HOURS = int(os.getenv("HORIZON_HOURS", "48"))  # only events starting within this window
LEAD_MIN = int(os.getenv("LEAD_MIN", "45"))            # skip events starting sooner than this

# Scheduling (Africa/Nairobi time)
PICK_TIMES = _list("PICK_TIMES", "08:00,15:00")
# Credit saver: a scheduled run may reuse a scan that is younger than this many hours (0 = always scan fresh).
# e.g. 8 => the 15:00 run reuses the 08:00 scan and costs nothing; it drafts the next-best picks from it.
SCAN_REUSE_HOURS = int(os.getenv("SCAN_REUSE_HOURS", "0"))
# Football context (API-Football, api-sports.io). Free plan = 100 requests/day.
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
STATS_RESERVE = int(os.getenv("STATS_RESERVE", "25"))      # requests always kept for admin drafts (subscriber lookups stop above this)
PREVIEW_PER_DAY = int(os.getenv("PREVIEW_PER_DAY", "5"))   # /preview lookups per VIP member per day
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "").strip() # shown by /support, e.g. @YourSupportHandle
AUTO_PUBLISH = _bool("AUTO_PUBLISH", False)            # False = admin taps Publish on every slip
AUTO_RECOVERY = _bool("AUTO_RECOVERY", True)
SETTLE_EVERY_MIN = int(os.getenv("SETTLE_EVERY_MIN", "60"))
SETTLE_DELAY_MIN = int(os.getenv("SETTLE_DELAY_MIN", "150"))  # start checking scores this long after kickoff
MIN_CREDITS = int(os.getenv("MIN_CREDITS", "25"))      # stop calling the API below this many credits