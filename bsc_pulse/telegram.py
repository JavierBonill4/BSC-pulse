"""Send a message to a Telegram chat through the Bot API."""

from __future__ import annotations

import requests

API = "https://api.telegram.org"


class TelegramError(Exception):
    """The message was not delivered."""


def send(
    token: str,
    chat_id: str,
    text: str,
    session: requests.Session | None = None,
    timeout: float = 10.0,
) -> None:
    """Send `text` (Telegram's HTML subset) to `chat_id`. Raises TelegramError on failure."""
    url = f"{API}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = (session or requests).post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        # The URL contains the bot token, and requests puts the URL in its error
        # messages. Report only the kind of error, and drop the original exception
        # (`from None`) so the token cannot end up in a log.
        raise TelegramError(f"request failed: {type(exc).__name__}") from None

    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("ok"):
        description = body.get("description", "no description")
        raise TelegramError(f"HTTP {response.status_code}: {description}")
