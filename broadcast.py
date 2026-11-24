"""Rate-limited broadcasting using copy_message (keeps captions/media, no parse errors)."""
import asyncio
import logging

from telegram.error import Forbidden, RetryAfter, TelegramError

import config as cfg
import db

log = logging.getLogger(__name__)
BATCH = 20  # Telegram allows ~30 msgs/sec overall; 20 per second leaves headroom


async def resolve(audience: str) -> list[int]:
    if audience in cfg.POST_CHANNELS:
        return list(cfg.POST_CHANNELS[audience])
    if audience == "all":
        seen: list[int] = []
        for chans in cfg.POST_CHANNELS.values():
            seen += [c for c in chans if c not in seen]
        return seen
    if audience == "dm_subs":
        return await db.active_subscriber_ids()
    if audience == "dm_all":
        return await db.active_user_ids()
    return []


async def _copy(bot, target, from_chat, message_id) -> bool:
    for _ in range(3):
        try:
            await bot.copy_message(chat_id=target, from_chat_id=from_chat, message_id=message_id)
            return True
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Forbidden:
            await db.mark_inactive(target)  # user blocked the bot (no-op for channels)
            log.info("Forbidden for %s (blocked / bot not admin)", target)
            return False
        except TelegramError as e:
            log.warning("copy to %s failed: %s", target, e)
            return False
    return False


async def send_many(bot, targets, from_chat, message_id) -> tuple[int, int]:
    ok = 0
    for i in range(0, len(targets), BATCH):
        batch = targets[i : i + BATCH]
        res = await asyncio.gather(*(_copy(bot, t, from_chat, message_id) for t in batch))
        ok += sum(res)
        if i + BATCH < len(targets):
            await asyncio.sleep(1)
    return ok, len(targets) - ok


async def _copy_id(bot, target, from_chat, message_id):
    for _ in range(3):
        try:
            m = await bot.copy_message(chat_id=target, from_chat_id=from_chat, message_id=message_id)
            return m.message_id
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Forbidden:
            await db.mark_inactive(target)
            log.info("Forbidden for %s (blocked / bot not admin)", target)
            return None
        except TelegramError as e:
            log.warning("copy to %s failed: %s", target, e)
            return None
    return None


async def copy_many(bot, targets, from_chat, message_id) -> list[tuple[int, int]]:
    """Like send_many but returns [(chat_id, new_message_id)] for every successful send."""
    out: list[tuple[int, int]] = []
    for i in range(0, len(targets), BATCH):
        batch = targets[i : i + BATCH]
        res = await asyncio.gather(*(_copy_id(bot, t, from_chat, message_id) for t in batch))
        out += [(t, r) for t, r in zip(batch, res) if r is not None]
        if i + BATCH < len(targets):
            await asyncio.sleep(1)
    return out