"""A minimal JSON-RPC client for BSC endpoints.

It does two jobs:

1. Send one JSON-RPC request and time it.
2. Sort every way the request can fail into a small set of named kinds, so the
   rest of the program (and later the metrics and alerts) can tell "the endpoint
   is slow" apart from "the endpoint is rate limiting us" or "it sent nonsense".

It also holds the helpers that turn raw JSON-RPC values (hex strings) into numbers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import Any

import requests

# The kinds of failure we distinguish.
TIMEOUT = "timeout"  # no answer within the time limit
CONNECTION = "connection"  # DNS, TCP or TLS failed before we got any HTTP answer
RATE_LIMITED = "rate_limited"  # the endpoint told us to slow down
HTTP_ERROR = "http_error"  # an HTTP status other than 200 (and other than 429)
RPC_ERROR = "rpc_error"  # HTTP was fine, but the JSON-RPC answer is an error object
BAD_RESPONSE = "bad_response"  # the body is not the JSON-RPC answer we expected

# JSON-RPC error code for "limit exceeded" (EIP-1474). Seen live on 2026-11-15:
# bsc-dataseed.bnbchain.org answers eth_getLogs with HTTP 200 and this code.
# So a rate limit is not always an HTTP 429; it can hide inside a 200 answer.
LIMIT_EXCEEDED_CODE = -32005


@dataclass(frozen=True)
class RpcResult:
    """The outcome of one JSON-RPC call."""

    ok: bool
    result: Any = None  # the "result" field of the answer, when ok
    error_kind: str | None = None  # one of the kinds above, when not ok
    error_detail: str = ""
    http_status: int | None = None
    latency_s: float = 0.0  # how long the call took
    finished_at: float = 0.0  # unix time at which the answer arrived


def interpret(http_status: int, body: str) -> RpcResult:
    """Decide what an HTTP answer means. Pure function: no network, no clock."""
    if http_status == 429:
        return RpcResult(False, error_kind=RATE_LIMITED, error_detail="HTTP 429", http_status=429)
    if http_status != 200:
        detail = f"HTTP {http_status}: {body[:120]}".strip()
        return RpcResult(False, error_kind=HTTP_ERROR, error_detail=detail, http_status=http_status)

    try:
        payload = json.loads(body)
    except ValueError:
        detail = f"body is not JSON: {body[:120]}"
        return RpcResult(False, error_kind=BAD_RESPONSE, error_detail=detail, http_status=200)
    if not isinstance(payload, dict):
        return RpcResult(
            False, error_kind=BAD_RESPONSE, error_detail="JSON is not an object", http_status=200
        )

    error = payload.get("error")
    if error:
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message", "") if isinstance(error, dict) else str(error)
        kind = RATE_LIMITED if code == LIMIT_EXCEEDED_CODE else RPC_ERROR
        return RpcResult(False, error_kind=kind, error_detail=f"{code}: {message}", http_status=200)

    if "result" not in payload:
        return RpcResult(
            False, error_kind=BAD_RESPONSE, error_detail="no result field", http_status=200
        )
    return RpcResult(True, result=payload["result"], http_status=200)


def call(
    session: requests.Session,
    url: str,
    method: str,
    params: list | None = None,
    timeout: float = 5.0,
) -> RpcResult:
    """Send one JSON-RPC request and return what happened, including how long it took.

    This never raises for network problems: a failed call is a result we want to
    count, not a crash. `timeout` applies to connecting and to each wait for data.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    started = time.perf_counter()
    try:
        response = session.post(url, json=payload, timeout=timeout)
    except requests.Timeout as exc:
        outcome = RpcResult(False, error_kind=TIMEOUT, error_detail=type(exc).__name__)
    except requests.RequestException as exc:
        outcome = RpcResult(False, error_kind=CONNECTION, error_detail=type(exc).__name__)
    else:
        outcome = interpret(response.status_code, response.text)
    return replace(outcome, latency_s=time.perf_counter() - started, finished_at=time.time())


# ---- Turning raw JSON-RPC values into Python values ----


def hex_to_int(value: object) -> int:
    """JSON-RPC sends numbers as hex strings such as "0x38". Convert one to an int."""
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"not a hex quantity: {value!r}")
    return int(value, 16)


@dataclass(frozen=True)
class BlockHead:
    """The few fields of a block that this tool needs."""

    number: int
    hash: str
    parent_hash: str
    time_s: float  # when the block was produced, as unix seconds
    has_millis: bool  # True if time_s came from the millisecond field


def parse_block(raw: object) -> BlockHead:
    """Pick the fields we need out of an eth_getBlockByNumber result.

    The standard "timestamp" field is in whole seconds. With blocks arriving more
    than once per second, that is too coarse to measure how old the head is. BSC
    nodes also return "milliTimestamp" (seen live on all ten endpoints on
    2026-11-15), so we prefer it and fall back to whole seconds if it is missing.
    """
    if not isinstance(raw, dict):
        raise ValueError("block is not an object")
    try:
        if "milliTimestamp" in raw:
            time_s, has_millis = hex_to_int(raw["milliTimestamp"]) / 1000, True
        else:
            time_s, has_millis = float(hex_to_int(raw["timestamp"])), False
        return BlockHead(
            number=hex_to_int(raw["number"]),
            hash=str(raw["hash"]),
            parent_hash=str(raw["parentHash"]),
            time_s=time_s,
            has_millis=has_millis,
        )
    except KeyError as exc:
        raise ValueError(f"block is missing field {exc}") from exc


def parse_syncing(raw: object) -> bool:
    """eth_syncing returns false when in sync, or an object with progress when syncing."""
    if raw is False:
        return False
    if isinstance(raw, dict):
        return True
    raise ValueError(f"unexpected eth_syncing result: {raw!r}")
