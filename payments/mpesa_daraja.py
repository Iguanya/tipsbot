"""M-Pesa STK push via Safaricom Daraja (works with a Till via CustomerBuyGoodsOnline)."""
import base64
import logging
import re
import time
from datetime import datetime

import httpx

import config as cfg
import db
from . import core

log = logging.getLogger(__name__)
CODE = "mpesa_stk"
LABEL = "📱 M-Pesa STK push (PIN prompt)"


def available() -> bool:
    return all([cfg.DARAJA_KEY, cfg.DARAJA_SECRET, cfg.DARAJA_PASSKEY, cfg.DARAJA_SHORTCODE,
                cfg.DARAJA_PARTY_B, cfg.WEBHOOK_ENABLED])


class _Client:
    _tok, _exp = None, 0.0

    async def token(self):
        if self._tok and time.time() < self._exp:
            return self._tok
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{cfg.DARAJA_BASE}/oauth/v1/generate", params={"grant_type": "client_credentials"},
                            auth=(cfg.DARAJA_KEY, cfg.DARAJA_SECRET))
            r.raise_for_status()
            d = r.json()
        self._tok, self._exp = d["access_token"], time.time() + int(d["expires_in"]) - 60
        return self._tok

    async def post(self, path, payload):
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(cfg.DARAJA_BASE + path, json=payload,
                             headers={"Authorization": f"Bearer {await self.token()}"})
            try:
                return r.json()
            except ValueError:
                return {"errorMessage": r.text[:200]}


client = _Client()


def normalize_phone(raw: str):
    d = re.sub(r"\D", "", raw)
    if len(d) == 10 and d.startswith("0"):
        d = "254" + d[1:]
    elif len(d) == 9 and d[0] in "71":
        d = "254" + d
    return d if re.fullmatch(r"254[17]\d{8}", d) else None


async def stk_push(phone, amount, account_ref):
    ts = datetime.now(cfg.TZ).strftime("%Y%m%d%H%M%S")
    pwd = base64.b64encode(f"{cfg.DARAJA_SHORTCODE}{cfg.DARAJA_PASSKEY}{ts}".encode()).decode()
    return await client.post("/mpesa/stkpush/v1/processrequest", {
        "BusinessShortCode": cfg.DARAJA_SHORTCODE, "Password": pwd, "Timestamp": ts,
        "TransactionType": cfg.DARAJA_TXN_TYPE, "Amount": int(amount),
        "PartyA": phone, "PartyB": cfg.DARAJA_PARTY_B, "PhoneNumber": phone,
        "CallBackURL": f"{cfg.PUBLIC_URL}/cb/stk/{cfg.WEBHOOK_SECRET}",
        "AccountReference": account_ref[:12], "TransactionDesc": "VIP tips",
    })


async def register_c2b():
    """Registers confirmation/validation URLs so every till payment is recorded (auto-verification)."""
    return await client.post("/mpesa/c2b/v2/registerurl", {
        "ShortCode": cfg.DARAJA_SHORTCODE or cfg.MPESA_TILL, "ResponseType": "Completed",
        "ConfirmationURL": f"{cfg.PUBLIC_URL}/cb/c2b/confirm/{cfg.WEBHOOK_SECRET}",
        "ValidationURL": f"{cfg.PUBLIC_URL}/cb/c2b/validate/{cfg.WEBHOOK_SECRET}",
    })


async def start(update, context, plan):
    context.user_data["await"] = {"type": "phone", "plan": plan["code"]}
    await update.effective_message.reply_text(
        f"💳 {plan['name']} — KES {plan['price_kes']}\n\nSend the M-Pesa number to charge "
        "(e.g. 0712345678). Send 'cancel' to abort.")


async def handle_phone(update, context, state):
    msg, user = update.effective_message, update.effective_user
    phone = normalize_phone(msg.text or "")
    if not phone:
        await msg.reply_text("That doesn't look like a Safaricom number. Try 07XXXXXXXX, or send 'cancel'.")
        return
    context.user_data.pop("await", None)
    plan = await db.get_plan(state["plan"])
    pid = await core.create(user.id, plan, CODE, plan["price_kes"], phone=phone)
    try:
        res = await stk_push(phone, plan["price_kes"], f"VIP{pid}")
    except Exception as e:  # noqa: BLE001
        log.exception("STK push error")
        res = {"errorMessage": str(e)}
    if res.get("ResponseCode") == "0":
        await db.execute("UPDATE payments SET reference=? WHERE id=?", (res["CheckoutRequestID"], pid))
        await msg.reply_text(f"📲 Check your phone ({phone[-9:]}) and enter your M-Pesa PIN. "
                             "Your access link arrives automatically once paid.")
    else:
        await db.execute("UPDATE payments SET status='failed' WHERE id=?", (pid,))
        log.error("STK failed: %s", res)
        await msg.reply_text("❌ Couldn't start the M-Pesa prompt. Please try again or use another payment method.")
