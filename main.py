import asyncio
import contextlib
import logging
import os
import re
import signal
import sys

from telegram import Update
from telegram.ext import ApplicationBuilder

import config as cfg
import db
import handlers
import roles
import subscriptions
from tips import commands as tips_commands
from tips import store as tips_store

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tipsbot")


def check_config() -> None:
    """Fail early with a readable message (before any asyncio code runs)."""
    if not cfg.ADMIN_IDS:
        raise SystemExit("ADMIN_IDS is empty. Put your Telegram user ID(s) in .env")
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", cfg.BOT_TOKEN or ""):
        raise SystemExit("BOT_TOKEN in .env is missing or still the placeholder. Paste the real token from @BotFather.")


async def run() -> None:
    await db.init()
    try:
        await roles.load()
        await tips_store.init()

        app = ApplicationBuilder().token(cfg.BOT_TOKEN).read_timeout(30).write_timeout(30).build()
        if app.job_queue is None:
            raise RuntimeError('JobQueue missing. Run: pip install "python-telegram-bot[job-queue]"')
        handlers.register(app)
        tips_commands.register(app)  # tips engine: scheduling, /record, /tips, /settle ...
        app.job_queue.run_repeating(subscriptions.expiry_job, interval=3600, first=30)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, stop.set)

        async with app:
            await handlers.setup_commands(app)
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            log.info("Bot is running.")

            server = task = None
            if cfg.WEBHOOK_ENABLED:
                import uvicorn  # only loaded when the Daraja webhook is enabled
                from webhook import create_web

                server = uvicorn.Server(
                    uvicorn.Config(create_web(app), host="0.0.0.0", port=cfg.PORT, log_level="warning"))
                server.install_signal_handlers = lambda: None    # we own signal handling
                server.capture_signals = contextlib.nullcontext  # newer uvicorn versions
                task = asyncio.create_task(server.serve())
                log.info("Webhook server on :%s", cfg.PORT)
            try:
                await stop.wait()
            finally:
                if server:
                    server.should_exit = True
                    await task
                await app.updater.stop()
                await app.stop()
    finally:
        await db.close()  # always stop the sqlite worker thread, even after an error


def _hard_exit(code: int) -> None:
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


async def main() -> None:
    code = 0
    try:
        await run()
    except Exception:
        log.exception("FATAL: bot stopped")  # the real error, instead of a segfault
        code = 1
    # Python 3.14.4's asyncio can segfault while cancelling tasks at shutdown (inside asyncio.run).
    # Everything is already closed here, so skip that step. Not needed on 3.12/3.13.
    if code or sys.version_info >= (3, 14):
        _hard_exit(code)


if __name__ == "__main__":
    check_config()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
