"""Tests for the notifier: every failure still results in an alert going out."""

import json
import logging
import threading
from http.server import ThreadingHTTPServer

import pytest
import requests

from bsc_pulse import notifier
from bsc_pulse.prom import ContextUnavailable
from tests.test_notes import alert, context


class Recorder:
    """A sender that remembers what it was asked to send, or fails on purpose."""

    def __init__(self, fail=False):
        self.sent: list[str] = []
        self.fail = fail

    def __call__(self, text):
        if self.fail:
            raise RuntimeError("telegram unreachable")
        self.sent.append(text)


def test_firing_alert_gets_a_full_note():
    sender = Recorder()
    ok = notifier.Notifier(lambda labels: context(), sender).handle({"alerts": [alert()]})
    assert ok
    assert "answers quickly but serves old data" in sender.sent[0]


def test_prometheus_down_still_sends_a_note_without_numbers():
    def broken(labels):
        raise ContextUnavailable("connection refused")

    sender = Recorder()
    assert notifier.Notifier(broken, sender).handle({"alerts": [alert()]})
    assert "Live numbers unavailable" in sender.sent[0]
    assert "Likely causes" in sender.sent[0]


def test_note_builder_crash_falls_back_to_the_plain_alert():
    def crash(alert, context):
        raise KeyError("bug")

    sender = Recorder()
    n = notifier.Notifier(lambda labels: {}, sender, note_builder=crash)
    assert n.handle({"alerts": [alert()]})
    assert sender.sent[0].startswith("FIRING: BscEndpointLagging (warning)")


def test_resolved_alerts_do_not_query_prometheus():
    asked = []
    sender = Recorder()
    n = notifier.Notifier(lambda labels: asked.append(labels) or {}, sender)
    n.handle({"alerts": [alert(status="resolved")]})
    assert asked == []
    assert sender.sent[0].startswith("<b>RESOLVED</b>")


def test_failed_delivery_is_reported_but_other_alerts_are_still_tried():
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("telegram unreachable")

    n = notifier.Notifier(lambda labels: {}, flaky)
    assert n.handle({"alerts": [alert(), alert("BscEndpointDown")]}) is False
    assert len(calls) == 2


def test_without_telegram_settings_messages_go_to_the_log(caplog):
    sender = notifier.make_sender("", "")
    with caplog.at_level(logging.INFO, logger="bsc_pulse.notifier"):
        sender("hello")
    assert "DRY RUN" in caplog.text and "hello" in caplog.text


def test_sample_note_can_be_built():
    note = notifier.notes.build_note(notifier.SAMPLE_ALERT, notifier.SAMPLE_CONTEXT)
    assert "TEST from BSC Pulse" in note


# ---- over real HTTP, the way Alertmanager calls it ----


@pytest.fixture
def server():
    """Start the webhook on a free local port. Yields (base_url, sender)."""
    sender = Recorder()
    handler = notifier.make_handler(notifier.Notifier(lambda labels: context(), sender))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", sender
    httpd.shutdown()


@pytest.fixture
def http():
    session = requests.Session()
    session.trust_env = False  # ignore any proxy settings for these local calls
    return session


def test_webhook_delivers_and_answers_200(server, http):
    url, sender = server
    response = http.post(f"{url}/alert", data=json.dumps({"alerts": [alert()]}))
    assert response.status_code == 200
    assert len(sender.sent) == 1


def test_webhook_answers_502_when_delivery_fails_so_alertmanager_retries(server, http):
    url, sender = server
    sender.fail = True
    response = http.post(f"{url}/alert", data=json.dumps({"alerts": [alert()]}))
    assert response.status_code == 502


@pytest.mark.parametrize("body", ["not json", "[1, 2]"])
def test_webhook_rejects_bad_bodies(server, http, body):
    url, _ = server
    assert http.post(f"{url}/alert", data=body).status_code == 400


def test_webhook_health_and_unknown_paths(server, http):
    url, _ = server
    assert http.get(f"{url}/healthz").text == "ok"
    assert http.get(f"{url}/nope").status_code == 404
    assert http.post(f"{url}/nope", data="{}").status_code == 404
