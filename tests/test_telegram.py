"""Tests for sending to Telegram, with a fake HTTP session."""

import pytest
import requests

from bsc_pulse import telegram
from tests.fakes import FakeResponse

TOKEN = "123456:SECRET-TOKEN"


class FakeTelegram:
    def __init__(self, response=None, fail=None):
        self.response = response or FakeResponse(200, '{"ok": true, "result": {}}')
        self.fail = fail
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        if self.fail:
            raise self.fail
        return self.response


def test_send_posts_html_message_to_the_chat():
    session = FakeTelegram()
    telegram.send(TOKEN, "-100123", "<b>hi</b>", session=session)
    (post,) = session.posts
    assert post["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert post["json"]["chat_id"] == "-100123"
    assert post["json"]["text"] == "<b>hi</b>"
    assert post["json"]["parse_mode"] == "HTML"


def test_api_error_is_reported_with_telegrams_description():
    response = FakeResponse(400, '{"ok": false, "description": "Bad Request: chat not found"}')
    with pytest.raises(telegram.TelegramError, match="chat not found"):
        telegram.send(TOKEN, "1", "x", session=FakeTelegram(response=response))


def test_network_error_never_reveals_the_token():
    # requests puts the URL (which contains the token) in its exception messages.
    failure = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    with pytest.raises(telegram.TelegramError) as caught:
        telegram.send(TOKEN, "1", "x", session=FakeTelegram(fail=failure))
    assert "SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
