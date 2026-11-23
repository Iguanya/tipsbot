"""
End-to-end test: M-Pesa STK/C2B -> payment.complete -> subscriptions.grant
               -> renewal extension -> tier upgrade -> expiry_job.
Test user: 8188673197
Isolated DB (test_flow.db) + fake channels + MockBot.
"""
import os

# ---- MUST be set BEFORE importing config (load_dotenv uses override=False) ----
os.environ.setdefault("DB_PATH", "test_flow.db")
os.environ.setdefault("ADMIN_IDS", "111111111")
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST_TOKEN")
os.environ.setdefault("VIP_CHANNEL_ID", "-1001111111111")
os.environ.setdefault("VIPPLUS_CHANNEL_ID", "-1002222222222")
os.environ.setdefault("FREE_CHANNEL_IDS", "-1003333333333")
os.environ.setdefault("SOURCE_CHANNEL_ID", "-1004444444444")
os.environ.setdefault("MPESA_TILL", "123456")
os.environ.setdefault("MPESA_TILL_NAME", "TEST TILL")

# ---- imports after env ----
import asyncio
import logging
from types import SimpleNamespace

import config as cfg
import db
import roles
import payments
import subscriptions

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("test_flow")

# Test identities
TEST_USER_ID = 8188673197
TEST_PHONE   = "254712345678"

REF_STK   = "ws_CO_STK_TEST_0001"
REF_RENEW = "ws_CO_STK_TEST_0002"
REF_UPG   = "ws_CO_STK_TEST_0003"
RCP_STK   = "QSTK202611230001"
RCP_RENEW = "QRNW202611230002"
RCP_UPG   = "QUPG202611230003"

EXP_USER_A = 8188673198   # expiring in 2.5 days   -> rem3
EXP_USER_B = 8188673199   # expiring in 0.5 days   -> rem1
EXP_USER_C = 8188673200   # already expired        -> status expired


# ----------------------------------------------------------------------
class MockBot:
    """Fake telegram.Bot covering every call subscriptions.py makes."""

    def __init__(self):
        self.outbox: list[tuple[int, str]] = []
        self.kicks: list[tuple[int, int]] = []   # (channel, user)
        self.invites = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.outbox.append((chat_id, text))
        preview = text if len(text) < 80 else text[:77] + "..."
        log.info(f"  [BOT -> {chat_id}] {preview}")
        return SimpleNamespace(message_id=len(self.outbox))

    async def get_chat_member(self, chat_id, user_id, **kw):
        return SimpleNamespace(status="left")            # not a member yet

    async def create_chat_invite_link(self, chat_id, member_limit=1,
                                      expire_date=None, **kw):
        self.invites += 1
        return SimpleNamespace(invite_link=f"https://t.me/+TEST_{self.invites}")

    async def get_chat(self, chat_id, **kw):
        return SimpleNamespace(id=chat_id, title=f"Test Channel {chat_id}")

    async def ban_chat_member(self, chat_id, user_id, **kw):
        self.kicks.append((chat_id, user_id))
        log.info(f"  [BOT] ban {user_id} from {chat_id}")

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=True, **kw):
        log.info(f"  [BOT] unban {user_id} from {chat_id}")

    def reset(self):
        self.outbox.clear()
        self.kicks.clear()


# ----------------------------------------------------------------------
async def register(uid, username="test", first="Test", last="User"):
    await db.upsert_user(SimpleNamespace(
        id=uid, username=username, first_name=first, last_name=last,
    ))
    log.info(f"✅ registered user {uid}")


async def dump(tag, tables=("users", "payments", "subscriptions", "mpesa_c2b")):
    log.info(f"\n---- STATE: {tag} ----")
    for t in tables:
        rows = await db.fetchall(f"SELECT * FROM {t}")
        log.info(f"  {t}: {len(rows)} row(s)")
        for r in rows:
            log.info(f"    {dict(r)}")


def fmt(ts):
    return subscriptions.fmt(ts)


# ----------------------------------------------------------------------
async def main():
    await db.init()
    await roles.load()
    bot = MockBot()

    await register(TEST_USER_ID)

    # ================================================================
    # SCENARIO 1 — STK push → payment.complete → grant
    # ================================================================
    log.info("\n========== SCENARIO 1: STK push -> grant ==========")
    plan_vip = await db.get_plan("vip_week")

    pid = await payments.create(
        TEST_USER_ID, plan_vip, provider="mpesa_stk",
        amount=plan_vip["price_kes"],
        reference=REF_STK, phone=TEST_PHONE,
    )
    log.info(f"💸 pending payment #{pid}  ({REF_STK})")

    # simulate the C2B row a webhook would insert
    await db.execute(
        """INSERT OR REPLACE INTO mpesa_c2b
           (trans_id, amount, msisdn, name, received_at, claimed)
           VALUES(?,?,?,?,?,0)""",
        (RCP_STK, float(plan_vip["price_kes"]), TEST_PHONE, "Test User", db.now()),
    )
    await dump("after create")

    assert await payments.complete(bot, pid, receipt=RCP_STK) is True
    await db.execute("UPDATE mpesa_c2b SET claimed=1 WHERE trans_id=?", (RCP_STK,))

    subs = await db.active_subs(TEST_USER_ID)
    assert len(subs) == 1 and subs[0]["plan_code"] == "vip_week"
    log.info(f"✅ vip_week active until {fmt(subs[0]['expires_at'])}")

    # idempotency
    assert await payments.complete(bot, pid, receipt=RCP_STK) is False
    log.info("✅ idempotency OK (second complete() returned False)")

    # invite links + user DM were sent
    assert any(uid == TEST_USER_ID for uid, _ in bot.outbox)
    log.info("✅ user received DM + invite link")

    # ================================================================
    # SCENARIO 2 — renewal extends existing subscription
    # ================================================================
    log.info("\n========== SCENARIO 2: renewal extends ==========")
    before = (await db.active_subs(TEST_USER_ID))[0]["expires_at"]

    pid2 = await payments.create(
        TEST_USER_ID, plan_vip, provider="mpesa_stk",
        amount=plan_vip["price_kes"],
        reference=REF_RENEW, phone=TEST_PHONE,
    )
    await payments.complete(bot, pid2, receipt=RCP_RENEW)

    after = (await db.active_subs(TEST_USER_ID))[0]["expires_at"]
    added = after - before
    expected = plan_vip["days"] * subscriptions.DAY
    assert added == expected, f"expected +{expected}s, got +{added}s"
    log.info(f"✅ renewal added {expected}s: "
             f"{fmt(before)} -> {fmt(after)}")

    # ================================================================
    # SCENARIO 3 — upgrade to VIP+
    # ================================================================
    log.info("\n========== SCENARIO 3: upgrade to VIP+ ==========")
    plan_plus = await db.get_plan("vipplus_month")
    pid3 = await payments.create(
        TEST_USER_ID, plan_plus, provider="mpesa_stk",
        amount=plan_plus["price_kes"],
        reference=REF_UPG, phone=TEST_PHONE,
    )
    await payments.complete(bot, pid3, receipt=RCP_UPG)

    subs = await db.active_subs(TEST_USER_ID)
    tiers = {s["tier"] for s in subs}
    assert "vip" in tiers and "vipplus" in tiers, tiers

    chans = await subscriptions.channels_for_user(TEST_USER_ID)
    assert cfg.VIP_CHANNEL in chans
    assert cfg.VIPPLUS_CHANNEL in chans
    log.info(f"✅ both tiers active; channel access = {sorted(chans)}")

    # ================================================================
    # SCENARIO 4 — expiry_job (reminders + expiry + kick)
    # ================================================================
    log.info("\n========== SCENARIO 4: expiry_job ==========")
    await register(EXP_USER_A, username="exp_a")
    await register(EXP_USER_B, username="exp_b")
    await register(EXP_USER_C, username="exp_c")

    async def quick_vip(uid, ref, receipt):
        pid = await payments.create(
            uid, plan_vip, "mpesa_stk", plan_vip["price_kes"],
            reference=ref, phone=TEST_PHONE,
        )
        await payments.complete(bot, pid, receipt=receipt)

    await quick_vip(EXP_USER_A, "ws_A", "QA")
    await quick_vip(EXP_USER_B, "ws_B", "QB")
    await quick_vip(EXP_USER_C, "ws_C", "QC")

    now = db.now()
    await db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE user_id=?",
        (now + int(2.5 * subscriptions.DAY), EXP_USER_A),
    )
    await db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE user_id=?",
        (now + int(0.5 * subscriptions.DAY), EXP_USER_B),
    )
    await db.execute(
        "UPDATE subscriptions SET expires_at=? WHERE user_id=?",
        (now - 60, EXP_USER_C),
    )

    bot.reset()
    ctx = SimpleNamespace(bot=bot)
    await subscriptions.expiry_job(ctx)

    # 3-day reminder
    assert any(uid == EXP_USER_A for uid, _ in bot.outbox), "A reminder missing"
    # 1-day reminder
    assert any(uid == EXP_USER_B for uid, _ in bot.outbox), "B reminder missing"
    # expired status
    row = await db.fetchone(
        "SELECT status FROM subscriptions WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (EXP_USER_C,),
    )
    assert row["status"] == "expired", row["status"]
    # C was kicked from VIP channel
    assert any(uid == EXP_USER_C for _, uid in bot.kicks), bot.kicks
    # rem flags persisted
    ra = await db.fetchone(
        "SELECT rem3, rem1 FROM subscriptions WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (EXP_USER_A,),
    )
    rb = await db.fetchone(
        "SELECT rem3, rem1 FROM subscriptions WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (EXP_USER_B,),
    )
    assert ra["rem3"] == 1 and ra["rem1"] == 0
    assert rb["rem3"] == 1 and rb["rem1"] == 1
    log.info("✅ 3d reminder sent, 1d reminder sent, expiry kicked, flags persisted")

    # stale STK marking (>15 min pending → failed)
    stale = await payments.create(
        TEST_USER_ID, plan_vip, "mpesa_stk", plan_vip["price_kes"],
        reference="ws_STALE", phone=TEST_PHONE,
    )
    await db.execute(
        "UPDATE payments SET created_at=? WHERE id=?",
        (db.now() - 1000, stale),
    )
    await subscriptions.expiry_job(ctx)
    row = await db.fetchone("SELECT status FROM payments WHERE id=?", (stale,))
    assert row["status"] == "failed", row["status"]
    log.info("✅ stale pending STK (>15 min) marked failed")

    # ================================================================
    await dump("FINAL")
    await db.close()
    log.info("\n🎉 All scenarios passed.")


if __name__ == "__main__":
    # fresh test db each run
    for f in ("test_flow.db", "test_flow.db-wal", "test_flow.db-shm"):
        if os.path.exists(f):
            os.remove(f)
    asyncio.run(main())