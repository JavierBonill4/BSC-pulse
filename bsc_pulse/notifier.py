"""Receive alerts from Alertmanager, write an incident note, send it to Telegram.

    python -m bsc_pulse.notifier            serve the webhook (or: make notify)
    python -m bsc_pulse.notifier --sample   send one sample note and exit (test Telegram setup)

Each step can fail without stopping the alert from going out:

    1. fetch recent numbers from Prometheus  -> on failure: note says numbers are unavailable
    2. build the incident note               -> on failure: plain alert text instead
    3. send to Telegram                      -> on failure: answer 502 so Alertmanager retries
       (no token configured: print the message to the log instead, a "dry run")

Settings (environment variables):

    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   where to send; both empty means dry run
    PROMETHEUS_URL                          default http://127.0.0.1:9090
    NOTIFIER_LISTEN                         default 127.0.0.1:9106
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bsc_pulse import notes, telegram
from bsc_pulse.main import parse_listen
from bsc_pulse.prom import PrometheusClient, gather

log = logging.getLogger("bsc_pulse.notifier")

MAX_BODY_BYTES = 1_000_000

ContextSource = Callable[[dict], dict]
Sender = Callable[[str], None]
NoteBuilder = Callable[[dict, "dict | None"], str]


class Notifier:
    """The steps above, with each dependency passed in so tests can replace it."""

    def __init__(
        self,
        context_source: ContextSource,
        sender: Sender,
        note_builder: NoteBuilder = notes.build_note,
    ):
        self.context_source = context_source
        self.sender = sender
        self.note_builder = note_builder

    def message_for(self, alert: dict) -> str:
        context = None
        if alert.get("status") == "firing":  # a resolved message needs no numbers
            try:
                context = self.context_source(alert.get("labels", {}))
            except Exception as exc:
                log.warning("no context for alert: %s", exc)
        try:
            return self.note_builder(alert, context)
        except Exception:
            log.exception("note builder failed, sending the plain alert instead")
            return notes.plain_text(alert)

    def handle(self, payload: dict) -> bool:
        """Deliver every alert in one Alertmanager webhook call. False if any failed."""
        delivered_all = True
        for alert in payload.get("alerts", []):
            name = alert.get("labels", {}).get("alertname", "?")
            try:
                self.sender(self.message_for(alert))
                log.info("delivered %s %s", alert.get("status"), name)
            except Exception as exc:
                log.error("could not deliver %s: %s", name, exc)
                delivered_all = False
        return delivered_all


def make_sender(token: str, chat_id: str) -> Sender:
    if token and chat_id:
        return partial(telegram.send, token, chat_id)
    log.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set: dry run, messages go to the log")
    return lambda text: log.info("DRY RUN, would send:\n%s", text)


def make_handler(notifier: Notifier) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (name required by BaseHTTPRequestHandler)
            if self.path == "/healthz":
                self._reply(200, "ok")
            else:
                self._reply(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/alert":
                self._reply(404, "not found")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(400, "missing or oversized body")
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                self._reply(400, "body is not JSON")
                return
            if not isinstance(payload, dict):
                self._reply(400, "body is not a JSON object")
                return
            # 502 makes Alertmanager try again later; 200 means "done".
            if notifier.handle(payload):
                self._reply(200, "delivered")
            else:
                self._reply(502, "delivery failed")

        def _reply(self, status: int, text: str) -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


SAMPLE_ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "BscEndpointLagging",
        "severity": "warning",
        "scope": "endpoint",
        "network": "mainnet",
        "endpoint": "bsc-dataseed.example",
    },
    "annotations": {"summary": "TEST from BSC Pulse: this is a sample alert, nothing is wrong."},
}
SAMPLE_CONTEXT = {
    "endpoint": {
        "url": "https://bsc-dataseed.example",
        "lag_blocks": 14,
        "lag_s": 6.3,
        "latency_p95_s": 0.04,
        "error_ratio": 0.0,
        "client_version": "Geth/v1.8.2/linux-amd64/go1.26.10",
    },
    "network": {"head_block": 133785880, "usable": 7, "endpoints": 7, "block_interval_s": 0.45},
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alertmanager webhook -> Telegram notes.")
    parser.add_argument("--sample", action="store_true", help="send one sample note and exit")
    parser.add_argument("--listen", default=os.environ.get("NOTIFIER_LISTEN", "127.0.0.1:9106"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    sender = make_sender(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    )
    if args.sample:
        sender(notes.build_note(SAMPLE_ALERT, SAMPLE_CONTEXT))
        log.info("sample sent")
        return 0

    prometheus = PrometheusClient(os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090"))
    notifier = Notifier(context_source=partial(gather, prometheus), sender=sender)
    host, port = parse_listen(args.listen)
    server = ThreadingHTTPServer((host, port), make_handler(notifier))
    log.info("listening for Alertmanager on http://%s:%d/alert", host, port)
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C stops it quietly
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
