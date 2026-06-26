from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Telegram allows up to 30 messages per second globally;
# we stay well under that to be safe.
RATE_LIMIT_PER_SECOND = 25
RATE_LIMIT_DELAY = 1.0 / RATE_LIMIT_PER_SECOND


@dataclass
class BroadcastResult:
    sent: int
    failed: int
    total: int


async def broadcast_message(
    bot,
    chat_ids: list[int],
    text: str,
    media_file_id: str | None = None,
    media_type: str | None = None,
    progress_callback=None,
) -> BroadcastResult:
    """
    Send a message to a list of chat_ids using the provided bot instance.
    Respects Telegram rate limits (25 msg/sec).

    Args:
        bot: A python-telegram-bot Bot instance (mother bot).
        chat_ids: List of Telegram chat IDs to send to.
        text: Message text (HTML parse mode).
        media_file_id: Optional file_id for photo/document.
        media_type: 'photo' or 'document'.
        progress_callback: Optional async callable(sent, failed, total) called every 10 sends.

    Returns:
        BroadcastResult with sent/failed/total counts.
    """
    from telegram.constants import ParseMode
    from telegram.error import TelegramError

    sent = 0
    failed = 0
    total = len(chat_ids)

    for idx, chat_id in enumerate(chat_ids):
        try:
            if media_file_id and media_type == "photo":
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=media_file_id,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )
            elif media_file_id and media_type == "document":
                await bot.send_document(
                    chat_id=chat_id,
                    document=media_file_id,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
            sent += 1
        except TelegramError as exc:
            failed += 1
            logger.warning(
                "Broadcast send failed: chat_id=%s error=%s", chat_id, exc
            )
        except Exception as exc:
            failed += 1
            logger.exception(
                "Unexpected broadcast error: chat_id=%s", chat_id
            )

        # Rate limiting
        await asyncio.sleep(RATE_LIMIT_DELAY)

        # Report progress every 10 sends or on the last message
        if progress_callback and ((idx + 1) % 10 == 0 or idx == total - 1):
            try:
                await progress_callback(sent, failed, total)
            except Exception:
                logger.debug("Progress callback error", exc_info=True)

    return BroadcastResult(sent=sent, failed=failed, total=total)
