"""Tests for the comparison logic. No network: probe results are built by hand."""

import pytest

from bsc_pulse import rpc
from bsc_pulse.compare import Tracker
from bsc_pulse.config import Endpoint
from bsc_pulse.probe import ProbeResult

T0 = 1_794_768_000.0  # an arbitrary "now" for the first sweep
BLOCK = 0.45  # seconds per block in these made-up chains


def block_hash(number: int, fork: str = "") -> str:
    """A fake but stable hash for a height. A different `fork` means a different block."""
    return f"0x{fork or 'main'}-{number}"


def up(
    host: str,
    number: int,
    *,
    network: str = "mainnet",
    chain_id: int = 56,
    fork: str = "",
    parent_fork: str | None = None,
    age: float = 0.2,
    latency: float = 0.03,
) -> ProbeResult:
    """A healthy endpoint whose head is `number`, produced `age` seconds ago (at T0)."""
    expected = 56 if network == "mainnet" else 97
    head = rpc.BlockHead(
        number=number,
        hash=block_hash(number, fork),
        parent_hash=block_hash(number - 1, fork if parent_fork is None else parent_fork),
        time_s=1000 + number * BLOCK,
        has_millis=True,
    )
    return ProbeResult(
        endpoint=Endpoint(network=network, chain_id=expected, url=f"https://{host}"),
        ok=True,
        latency_s=latency,
        head=head,
        head_age_s=age,
        syncing=False,
        chain_id=chain_id,
    )


def down(host: str, network: str = "mainnet", kind: str = rpc.TIMEOUT) -> ProbeResult:
    expected = 56 if network == "mainnet" else 97
    return ProbeResult(
        endpoint=Endpoint(network=network, chain_id=expected, url=f"https://{host}"),
        error_kind=kind,
        latency_s=3.0,
    )


def by_name(snapshot):
    return {e.endpoint: e for e in snapshot.endpoints}


def network(snapshot, name="mainnet"):
    return next(n for n in snapshot.networks if n.network == name)


# ---- lag ----


def test_lag_is_measured_against_the_highest_endpoint():
    snap = Tracker().update([up("a", 100), up("b", 97), up("c", 100)], now=T0)
    eps = by_name(snap)
    assert eps["a"].lag_blocks == 0
    assert eps["b"].lag_blocks == 3
    assert eps["b"].lag_s == pytest.approx(3 * BLOCK)  # from block timestamps
    assert network(snap).head_block == 100


def test_a_fast_endpoint_can_still_be_the_one_behind():
    # The situation seen live on Chapel: lower latency, but further behind.
    snap = Tracker().update([up("fast", 97, latency=0.03), up("slow", 100, latency=0.25)], now=T0)
    eps = by_name(snap)
    assert eps["fast"].latency_s < eps["slow"].latency_s
    assert eps["fast"].lag_blocks == 3
    assert eps["slow"].lag_blocks == 0


def test_networks_are_compared_separately():
    snap = Tracker().update(
        [up("m1", 100), up("m2", 99), up("t1", 5000, network="chapel", chain_id=97)], now=T0
    )
    eps = by_name(snap)
    assert eps["m2"].lag_blocks == 1
    assert eps["t1"].lag_blocks == 0  # not compared with mainnet's lower height
    assert network(snap, "chapel").head_block == 5000


def test_wrong_chain_endpoint_is_not_used_as_the_reference():
    # An endpoint serving chapel by mistake reports a much higher number.
    snap = Tracker().update([up("a", 100), up("liar", 99999, chain_id=97)], now=T0)
    eps = by_name(snap)
    assert network(snap).head_block == 100
    assert eps["a"].lag_blocks == 0
    assert not eps["liar"].up and eps["liar"].wrong_chain
    assert eps["liar"].lag_blocks is None


def test_down_endpoint_has_no_lag_and_does_not_change_the_reference():
    snap = Tracker().update([up("a", 100), down("b")], now=T0)
    eps = by_name(snap)
    assert not eps["b"].up
    assert eps["b"].error_kind == rpc.TIMEOUT
    assert eps["b"].lag_blocks is None and eps["b"].head_block is None
    assert network(snap).usable == 1
    assert network(snap).endpoints == 2


def test_network_with_no_usable_endpoint():
    snap = Tracker().update([down("a"), down("b")], now=T0)
    net = network(snap)
    assert net.usable == 0
    assert net.head_block is None and net.head_unchanged_s is None


def test_network_head_age_is_the_freshest_head():
    snap = Tracker().update([up("a", 100, age=0.4), up("b", 100, age=0.1)], now=T0)
    assert network(snap).head_age_s == pytest.approx(0.1)


# ---- stalls ----


def test_head_unchanged_grows_while_stuck_and_resets_when_it_moves():
    tracker = Tracker()
    first = tracker.update([up("a", 100)], now=T0)
    assert by_name(first)["a"].head_unchanged_s == 0

    stuck = tracker.update([up("a", 100)], now=T0 + 5)
    assert by_name(stuck)["a"].head_unchanged_s == 5

    moved = tracker.update([up("a", 111)], now=T0 + 10)
    assert by_name(moved)["a"].head_unchanged_s == 0


def test_stalled_endpoint_versus_stalled_chain():
    tracker = Tracker()
    tracker.update([up("a", 100), up("b", 100)], now=T0)

    # b keeps answering with block 100 while a moves on: b is stalled, the chain is not.
    snap = tracker.update([up("a", 133), up("b", 100)], now=T0 + 15)
    eps = by_name(snap)
    assert eps["b"].head_unchanged_s == 15
    assert eps["b"].lag_blocks == 33
    assert network(snap).head_unchanged_s == 0

    # Now nobody moves: the network's highest head is stuck. That is a chain stall.
    snap = tracker.update([up("a", 133), up("b", 133)], now=T0 + 45)
    assert network(snap).head_unchanged_s == 30


def test_block_interval_is_measured_from_block_timestamps():
    tracker = Tracker()
    first = tracker.update([up("a", 100)], now=T0)
    assert network(first).block_interval_s is None  # needs two sweeps

    second = tracker.update([up("a", 111)], now=T0 + 5)
    assert network(second).block_interval_s == pytest.approx(BLOCK)


def test_leading_endpoint_failing_does_not_look_like_the_chain_moving():
    tracker = Tracker()
    tracker.update([up("lead", 120), up("slow", 100)], now=T0)
    snap = tracker.update([down("lead"), up("slow", 105)], now=T0 + 5)
    # The highest head we can see dropped from 120 to 105. Nothing new was produced
    # above 120 as far as we can tell, so the network head counts as unchanged.
    assert network(snap).head_unchanged_s == 5
    assert network(snap).head_block == 105


def test_endpoint_head_going_backwards_is_a_regression():
    tracker = Tracker()
    tracker.update([up("a", 100)], now=T0)
    snap = tracker.update([up("a", 97)], now=T0 + 5)
    assert snap.events.regressions == [("mainnet", "a")]
    assert by_name(snap)["a"].head_unchanged_s == 0  # it did change


def test_client_version_is_remembered_between_sweeps():
    tracker = Tracker()
    first = up("a", 100)
    first.client_version = "Geth/v1.8.2"
    tracker.update([first], now=T0)
    snap = tracker.update([up("a", 111)], now=T0 + 5)  # this sweep did not ask
    assert by_name(snap)["a"].client_version == "Geth/v1.8.2"


# ---- block hashes: disagreements and reorgs ----


def test_endpoints_that_agree_produce_no_events():
    tracker = Tracker()
    tracker.update([up("a", 100), up("b", 99)], now=T0)
    snap = tracker.update([up("a", 111), up("b", 110)], now=T0 + 5)
    assert snap.events.reorg_blocks == {"mainnet": 0}
    assert snap.events.disagreements == {"mainnet": 0}


def test_two_endpoints_with_different_blocks_at_one_height_disagree():
    snap = Tracker().update([up("a", 100), up("b", 100, fork="f")], now=T0)
    # They disagree at 100 (their heads) and at 99 (their parents).
    assert snap.events.disagreements == {"mainnet": 2}
    assert snap.events.reorg_blocks == {"mainnet": 0}


def test_parent_hash_reveals_disagreement_with_a_lower_endpoint():
    # a is at 100. b is at 101, but its parent (block 100) is not a's block 100.
    snap = Tracker().update([up("a", 100), up("b", 101, parent_fork="f")], now=T0)
    assert snap.events.disagreements == {"mainnet": 1}


def test_block_replaced_between_sweeps_is_a_reorg():
    tracker = Tracker()
    tracker.update([up("a", 100)], now=T0)  # records hashes for 100 and 99
    # Next sweep: the endpoint is at 101, and its parent is a different block 100.
    snap = tracker.update([up("a", 101, parent_fork="f")], now=T0 + 1)
    assert snap.events.reorg_blocks == {"mainnet": 1}


def test_heights_in_dispute_are_not_recorded():
    tracker = Tracker()
    tracker.update([up("a", 100), up("b", 100, fork="f")], now=T0)  # dispute at 100
    # When the dispute settles on b's block, that is not counted as a reorg,
    # because we never recorded a winner for height 100.
    snap = tracker.update([up("a", 100, fork="f"), up("b", 100, fork="f")], now=T0 + 1)
    assert snap.events.reorg_blocks == {"mainnet": 0}


def test_hash_history_is_bounded():
    tracker = Tracker(history_blocks=10)
    tracker.update([up("a", 100)], now=T0)
    tracker.update([up("a", 200)], now=T0 + 45)
    # Height 100 has been forgotten, so a different block there is not a reorg.
    snap = tracker.update([up("a", 101, fork="f")], now=T0 + 50)
    assert snap.events.reorg_blocks == {"mainnet": 0}
    assert max(tracker._hashes["mainnet"]) - min(tracker._hashes["mainnet"]) <= 10
