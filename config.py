"""All configuration comes from environment variables (.env)."""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TZ = ZoneInfo("Africa/Nairobi")


def _ints(name):
    return [int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x]


def _int(name):
    return int(os.getenv(name) or 0)


def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(_ints("ADMIN_IDS"))
BRAND_NAME = os.getenv("BRAND_NAME", "Tips Bot")
DB_PATH = os.getenv("DB_PATH", "tipsbot.db")

# ---- channels ----
FREE_CHANNELS = _ints("FREE_CHANNEL_IDS")
VIP_CHANNEL = _int("VIP_CHANNEL_ID")
VIPPLUS_CHANNEL = _int("VIPPLUS_CHANNEL_ID")
VIPPLUS_INCLUDES_VIP = _bool("VIPPLUS_INCLUDES_VIP", True)
SOURCE_CHANNEL_ID = _int("SOURCE_CHANNEL_ID")
SOURCE_AUDIENCE = os.getenv("SOURCE_AUDIENCE", "free")


def _chs(*ids):
    return [i for i in ids if i]


# where a post for an audience is published
POST_CHANNELS = {
    "free": FREE_CHANNELS,
    "vip": _chs(VIP_CHANNEL),
    "vipplus": _chs(VIPPLUS_CHANNEL),
}
# which channels a paying tier may join
TIER_ACCESS = {
    "vip": _chs(VIP_CHANNEL),
    "vipplus": _chs(VIPPLUS_CHANNEL, VIP_CHANNEL if VIPPLUS_INCLUDES_VIP else 0),
}

# ---- payments ----
PAYMENT_METHODS = [m.strip() for m in os.getenv("PAYMENT_METHODS", "mpesa_manual").split(",") if m.strip()]
MPESA_TILL = os.getenv("MPESA_TILL", "").strip()
MPESA_TILL_NAME = os.getenv("MPESA_TILL_NAME", "").strip()

DARAJA_ENV = os.getenv("DARAJA_ENV", "sandbox").strip().lower()
DARAJA_KEY = os.getenv("DARAJA_KEY", "")
DARAJA_SECRET = os.getenv("DARAJA_SECRET", "")
DARAJA_PASSKEY = os.getenv("DARAJA_PASSKEY", "")
DARAJA_SHORTCODE = os.getenv("DARAJA_SHORTCODE", "").strip()
DARAJA_BASE = "https://api.safaricom.co.ke" if DARAJA_ENV == "production" else "https://sandbox.safaricom.co.ke"
# Till (Buy Goods): BusinessShortCode = store number, PartyB = till number
DARAJA_TXN_TYPE = "CustomerBuyGoodsOnline" if MPESA_TILL else "CustomerPayBillOnline"
DARAJA_PARTY_B = MPESA_TILL or DARAJA_SHORTCODE

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = _int("PORT") or 8080
WEBHOOK_ENABLED = bool(PUBLIC_URL and WEBHOOK_SECRET)
