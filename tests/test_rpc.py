"""Tests for the JSON-RPC client: error sorting and value parsing."""

import json

import pytest
import requests

from bsc_pulse import rpc
from tests.fakes import REAL_BLOCK, FakeResponse, FakeSession, answer

# ---- interpret(): what does an HTTP answer mean? ----


def test_successful_answer_returns_result():
    outcome = rpc.interpret(200, '{"jsonrpc":"2.0","id":1,"result":"0x38"}')
    assert outcome.ok
    assert outcome.result == "0x38"


def test_null_result_is_still_a_valid_answer():
    # e.g. asking for a block the node does not have. The caller decides what it means.
    outcome = rpc.interpret(200, '{"jsonrpc":"2.0","id":1,"result":null}')
    assert outcome.ok
    assert outcome.result is None


def test_http_429_is_rate_limited():
    outcome = rpc.interpret(429, "Too Many Requests")
    assert not outcome.ok
    assert outcome.error_kind == rpc.RATE_LIMITED


def test_limit_exceeded_inside_http_200_is_rate_limited():
    # Exact body returned by bsc-dataseed.bnbchain.org for eth_getLogs on 2026-11-15.
    body = '{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"limit exceeded"}}'
    outcome = rpc.interpret(200, body)
    assert outcome.error_kind == rpc.RATE_LIMITED
    assert "limit exceeded" in outcome.error_detail


def test_other_rpc_errors_are_rpc_error():
    # Exact body returned by bsc-dataseed.bnbchain.org for an unknown method.
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32601,
                "message": "The method eth_doesNotExist does not exist/is not available",
            },
        }
    )
    outcome = rpc.interpret(200, body)
    assert outcome.error_kind == rpc.RPC_ERROR
    assert "-32601" in outcome.error_detail


def test_http_5xx_is_http_error():
    outcome = rpc.interpret(502, "<html>Bad Gateway</html>")
    assert outcome.error_kind == rpc.HTTP_ERROR
    assert outcome.http_status == 502


@pytest.mark.parametrize(
    "body",
    [
        "<html>maintenance</html>",  # not JSON at all
        "[1, 2, 3]",  # JSON, but not an object
        '{"jsonrpc":"2.0","id":1}',  # neither result nor error
    ],
)
def test_unexpected_bodies_are_bad_response(body):
    assert rpc.interpret(200, body).error_kind == rpc.BAD_RESPONSE


# ---- call(): sending the request and timing it ----


def test_call_sends_a_json_rpc_request_and_times_it():
    session = FakeSession({"eth_chainId": answer("0x38")})
    outcome = rpc.call(session, "https://example.test", "eth_chainId", timeout=3)

    assert outcome.ok and outcome.result == "0x38"
    assert outcome.latency_s >= 0
    assert outcome.finished_at > 0
    sent = session.calls[0]
    assert sent["json"] == {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    assert sent["timeout"] == 3


def test_call_reports_timeout_instead_of_raising():
    session = FakeSession({"eth_chainId": requests.ReadTimeout()})
    outcome = rpc.call(session, "https://example.test", "eth_chainId")
    assert not outcome.ok
    assert outcome.error_kind == rpc.TIMEOUT
    assert outcome.error_detail == "ReadTimeout"


def test_call_reports_connection_failure_instead_of_raising():
    session = FakeSession({"eth_chainId": requests.ConnectionError()})
    outcome = rpc.call(session, "https://example.test", "eth_chainId")
    assert outcome.error_kind == rpc.CONNECTION


def test_call_passes_http_errors_through_interpret():
    session = FakeSession({"eth_chainId": FakeResponse(429, "slow down")})
    assert rpc.call(session, "https://example.test", "eth_chainId").error_kind == rpc.RATE_LIMITED


# ---- parsing raw values ----


def test_hex_to_int():
    assert rpc.hex_to_int("0x38") == 56
    assert rpc.hex_to_int("0x61") == 97


@pytest.mark.parametrize("bad", ["38", "", None, 56, "0xzz"])
def test_hex_to_int_rejects_non_hex(bad):
    with pytest.raises(ValueError):
        rpc.hex_to_int(bad)


def test_parse_block_prefers_millisecond_timestamp():
    head = rpc.parse_block(REAL_BLOCK)
    assert head.number == 133785820
    assert head.hash == REAL_BLOCK["hash"]
    assert head.parent_hash == REAL_BLOCK["parentHash"]
    assert head.time_s == pytest.approx(1794768287.450)
    assert head.has_millis


def test_parse_block_falls_back_to_whole_seconds():
    block = {k: v for k, v in REAL_BLOCK.items() if k != "milliTimestamp"}
    head = rpc.parse_block(block)
    assert head.time_s == 1794768287.0
    assert not head.has_millis


@pytest.mark.parametrize("raw", [None, "0x1", {"number": "0x1"}])
def test_parse_block_rejects_incomplete_blocks(raw):
    with pytest.raises(ValueError):
        rpc.parse_block(raw)


def test_parse_syncing():
    assert rpc.parse_syncing(False) is False
    assert rpc.parse_syncing({"currentBlock": "0x1", "highestBlock": "0x9"}) is True
    with pytest.raises(ValueError):
        rpc.parse_syncing("maybe")
