"""Tiny FastAPI app receiving Safaricom callbacks (STK result + C2B till confirmations).
NB: Daraja rejects callback URLs containing words like 'mpesa'/'safaricom', hence /cb/..."""
import hmac
import logging

from fastapi import FastAPI, HTTPException, Request

import config as cfg
import db
from payments import core, mpesa_manual

log = logging.getLogger(__name__)
OK = {"ResultCode": 0, "ResultDesc": "Accepted"}


def _check(secret: str):
    if not cfg.WEBHOOK_SECRET or not hmac.compare_digest(secret, cfg.WEBHOOK_SECRET):
        raise HTTPException(404)


def create_web(application) -> FastAPI:
    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    bot = application.bot

    @web.get("/health")
    async def health():
        return {"ok": True}

    @web.post("/cb/stk/{secret}")
    async def stk_callback(secret: str, request: Request):
        _check(secret)
        try:
            cb = (await request.json())["Body"]["stkCallback"]
            checkout = cb["CheckoutRequestID"]
        except Exception:  # noqa: BLE001
            return OK
        p = await db.fetchone("SELECT * FROM payments WHERE provider='mpesa_stk' AND reference=?", (checkout,))
        if not p:
            log.warning("STK callback for unknown checkout %s", checkout)
            return OK

        if cb.get("ResultCode") == 0:
            items = {i["Name"]: i.get("Value") for i in cb.get("CallbackMetadata", {}).get("Item", [])}
            receipt, amount = items.get("MpesaReceiptNumber"), float(items.get("Amount") or 0)
            if amount >= p["amount"]:
                await core.complete(bot, p["id"], receipt=receipt)
            else:
                await core.notify_admins(bot, f"⚠️ STK payment #{p['id']} paid KES {amount}, expected {p['amount']} "
                                              f"(receipt {receipt}). Handle manually with /grant.")
        else:
            await db.execute("UPDATE payments SET status='failed' WHERE id=? AND status='pending'", (p["id"],))
            try:
                await bot.send_message(p["user_id"], f"❌ Payment not completed ({cb.get('ResultDesc', 'cancelled')}). "
                                                     "Use /subscribe to try again.")
            except Exception:  # noqa: BLE001
                pass
        return OK

    @web.post("/cb/c2b/validate/{secret}")
    async def c2b_validate(secret: str):
        _check(secret)
        return OK

    @web.post("/cb/c2b/confirm/{secret}")
    async def c2b_confirm(secret: str, request: Request):
        _check(secret)
        try:
            d = await request.json()
            trans_id, amount = d["TransID"], float(d["TransAmount"])
        except Exception:  # noqa: BLE001
            return OK
        name = " ".join(x for x in (d.get("FirstName"), d.get("LastName")) if x)
        await db.execute("INSERT OR IGNORE INTO mpesa_c2b(trans_id,amount,msisdn,name,received_at) VALUES(?,?,?,?,?)",
                         (trans_id, amount, d.get("MSISDN"), name, db.now()))
        # customer may already have submitted the code before the confirmation arrived
        p = await db.fetchone("SELECT * FROM payments WHERE provider='mpesa_manual' AND reference=? AND status='pending'",
                              (trans_id,))
        if p:
            await mpesa_manual.try_auto_verify(bot, p)
        return OK

    return web
