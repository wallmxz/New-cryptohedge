from __future__ import annotations
import logging
import httpx

logger = logging.getLogger(__name__)


async def post_alert(*, url: str, level: str, message: str, data: dict | None = None) -> None:
    """Post a JSON alert to a webhook (Slack/Discord/Telegram-compatible).

    No-op if URL is empty. Errors are logged but don't propagate.
    """
    if not url:
        return
    payload = {
        "level": level,
        "text": f"[{level.upper()}] {message}",
        "data": data or {},
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
        logger.info(f"Alert posted: {level} - {message}")
    except Exception as e:
        logger.error(f"Failed to post alert: {e}")
