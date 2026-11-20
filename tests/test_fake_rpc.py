"""Tests for the fake endpoint: each demo mode produces the failure it promises."""

import json

import pytest

from bsc_pulse import fake_rpc, rpc
from bsc_pulse.fake_rpc import State, respond, set_mode

NOW = fake_rpc.GENESIS + 1000 * fake_rpc.BLOCK_TIME + 0.1  # simulated block 1000


def head(state, now=NOW):
    status, body, _ = respond(state, "eth_getBlockByNumber", now)
    assert status == 200
    return rpc.parse_block(body["result"])


def test_healthy_endpoints_agree_on_the_simulated_chain():
    a, b = head(State()), head(State())
    assert a.number == 1000
    assert a == b
    assert a.time_s == pytest.approx(fake_rpc.GENESIS + 1000 * fake_rpc.BLOCK_TIME)
    assert a.has_millis


def test_the_chain_moves_at_the_block_time():
    later = head(State(), NOW + 10 * fake_rpc.BLOCK_TIME)
    assert later.number == 1010


def test_parent_hash_links_consecutive_blocks():
    assert head(State(), NOW + fake_rpc.BLOCK_TIME).parent_hash == head(State()).hash


def test_lag_mode_stays_behind():
    state = State()
    set_mode(state, "lag", NOW, lag_blocks=30)
    assert head(state).number == 970


def test_stall_mode_freezes_where_it_was():
    state = State()
    set_mode(state, "stall", NOW)
    assert head(state, NOW + 60).number == 1000


def test_fork_mode_disagrees_at_the_same_height():
    state = State()
    set_mode(state, "fork", NOW)
    forked, normal = head(state), head(State())
    assert forked.number == normal.number
    assert forked.hash != normal.hash


def test_wrong_chain_and_syncing():
    state = State()
    set_mode(state, "wrong_chain", NOW)
    assert respond(state, "eth_chainId", NOW)[1]["result"] == "0x61"

    set_mode(state, "syncing", NOW)
    syncing = respond(state, "eth_syncing", NOW)[1]["result"]
    assert rpc.parse_syncing(syncing) is True
    assert respond(State(), "eth_syncing", NOW)[1]["result"] is False


@pytest.mark.parametrize(
    "mode, expected_kind",
    [("error", rpc.HTTP_ERROR), ("ratelimit", rpc.RATE_LIMITED)],
)
def test_error_modes_produce_the_matching_error_kind(mode, expected_kind):
    state = State()
    set_mode(state, mode, NOW)
    kinds = []
    for _ in range(2):
        status, body, _ = respond(state, "eth_getBlockByNumber", NOW)
        text = json.dumps(body) if isinstance(body, dict) else body
        kinds.append(rpc.interpret(status, text).error_kind)
    assert expected_kind in kinds


def test_ratelimit_lets_every_other_request_through():
    state = State()
    set_mode(state, "ratelimit", NOW)
    statuses = [respond(state, "eth_chainId", NOW)[0] for _ in range(4)]
    assert statuses == [200, 429, 200, 429]


def test_slow_and_timeout_modes_delay_the_answer():
    for mode, delay in [("slow", fake_rpc.SLOW_DELAY_S), ("timeout", fake_rpc.TIMEOUT_DELAY_S)]:
        state = State()
        set_mode(state, mode, NOW)
        assert respond(state, "eth_chainId", NOW)[2] == delay


def test_unknown_method_is_a_json_rpc_error():
    status, body, _ = respond(State(), "eth_getLogs", NOW)
    assert status == 200 and body["error"]["code"] == -32601


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown mode"):
        set_mode(State(), "explode", NOW)
