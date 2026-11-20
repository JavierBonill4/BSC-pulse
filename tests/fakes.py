"""Stand-ins for the network, so tests never call a real endpoint."""

from __future__ import annotations

import json


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


def answer(result) -> FakeResponse:
    """A successful JSON-RPC answer carrying `result`."""
    return FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))


class FakeSession:
    """Looks like requests.Session, but answers from a dict of {method: reply}.

    A reply can be a FakeResponse, or an exception instance to raise instead.
    """

    def __init__(self, replies: dict):
        self.replies = replies
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        reply = self.replies[json["method"]]
        if isinstance(reply, Exception):
            raise reply
        return reply


# A real mainnet block header, trimmed to the fields we read.
# Returned by bsc-dataseed.bnbchain.org on 2026-11-15 (block 133785820).
REAL_BLOCK = {
    "number": "0x7f968dc",
    "hash": "0xfa55b66bc0a30ea939cbb58348b678612d026ef90db0702c2b5818fb2e2c8ffa",
    "parentHash": "0x01ed9373bf5a0fc8e3a9347e44c70b16f8d643510899f21931d52362105fe0ad",
    "timestamp": "0x6af9fd9f",
    "milliTimestamp": "0x1a1e086b6da",
}
