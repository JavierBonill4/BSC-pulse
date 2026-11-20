"""Tests for the Prometheus exporter: what appears on /metrics and when."""

from prometheus_client import CollectorRegistry, generate_latest

from bsc_pulse import rpc
from bsc_pulse.compare import Tracker
from bsc_pulse.metrics import Exporter
from bsc_pulse.probe import CallRecord
from tests.test_compare import T0, down, up


def scrape(registry: CollectorRegistry) -> dict[str, float]:
    """Parse /metrics text into {'name{labels}': value}, skipping comments."""
    samples = {}
    for line in generate_latest(registry).decode().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.rpartition(" ")
            samples[key] = float(value)
    return samples


def setup(results):
    registry = CollectorRegistry()
    exporter = Exporter([r.endpoint for r in results], registry)
    return registry, exporter


def test_nothing_but_zeroed_counters_before_the_first_sweep():
    registry, _ = setup([up("a", 100)])
    samples = scrape(registry)
    assert not any(key.startswith("bsc_endpoint_up") for key in samples)
    key = (
        'bsc_rpc_requests_total{endpoint="a",method="eth_syncing",'
        'network="mainnet",outcome="timeout"}'
    )
    assert samples[key] == 0  # exists at zero, so the first timeout is visible to rate()


def test_gauges_describe_the_latest_sweep():
    results = [up("a", 100), up("b", 97)]
    registry, exporter = setup(results)
    exporter.record(results, Tracker().update(results, now=T0), sweep_duration_s=0.12)
    s = scrape(registry)

    assert s['bsc_endpoint_up{endpoint="b",network="mainnet"}'] == 1
    assert s['bsc_endpoint_lag_blocks{endpoint="b",network="mainnet"}'] == 3
    assert s['bsc_endpoint_head_block{endpoint="a",network="mainnet"}'] == 100
    assert s['bsc_endpoint_syncing{endpoint="a",network="mainnet"}'] == 0
    assert s['bsc_endpoint_wrong_chain{endpoint="a",network="mainnet"}'] == 0
    assert s['bsc_network_head_block{network="mainnet"}'] == 100
    assert s['bsc_network_usable_endpoints{network="mainnet"}'] == 2
    assert s["bsc_last_sweep_timestamp_seconds"] == T0
    assert s["bsc_sweep_duration_seconds"] == 0.12


def test_down_endpoint_reports_up_zero_and_no_stale_lag():
    tracker = Tracker()
    healthy = [up("a", 100), up("b", 97)]
    registry, exporter = setup(healthy)
    exporter.record(healthy, tracker.update(healthy, now=T0), 0.1)

    later = [up("a", 111), down("b")]
    exporter.record(later, tracker.update(later, now=T0 + 5), 0.1)
    s = scrape(registry)

    assert s['bsc_endpoint_up{endpoint="b",network="mainnet"}'] == 0
    # The old lag of 3 must not linger once b stops answering.
    assert 'bsc_endpoint_lag_blocks{endpoint="b",network="mainnet"}' not in s


def test_requests_are_counted_by_outcome():
    ok, failed = up("a", 100), down("b")
    ok.calls = [CallRecord("eth_chainId", "ok"), CallRecord("eth_getBlockByNumber", "ok")]
    failed.calls = [CallRecord("eth_getBlockByNumber", rpc.RATE_LIMITED)]
    results = [ok, failed]
    registry, exporter = setup(results)
    exporter.record(results, Tracker().update(results, now=T0), 0.1)
    s = scrape(registry)

    def requests(endpoint, method, outcome):
        return s[
            f'bsc_rpc_requests_total{{endpoint="{endpoint}",method="{method}",'
            f'network="mainnet",outcome="{outcome}"}}'
        ]

    assert requests("a", "eth_getBlockByNumber", "ok") == 1
    assert requests("b", "eth_getBlockByNumber", "rate_limited") == 1
    assert requests("b", "eth_getBlockByNumber", "ok") == 0


def test_latency_histogram_only_counts_successful_head_requests():
    results = [up("a", 100, latency=0.04), down("b")]
    registry, exporter = setup(results)
    exporter.record(results, Tracker().update(results, now=T0), 0.1)
    s = scrape(registry)

    assert s['bsc_rpc_latency_seconds_count{endpoint="a",network="mainnet"}'] == 1
    assert s['bsc_rpc_latency_seconds_bucket{endpoint="a",le="0.05",network="mainnet"}'] == 1
    assert s['bsc_rpc_latency_seconds_count{endpoint="b",network="mainnet"}'] == 0


def test_events_are_added_to_counters():
    tracker = Tracker()
    first = [up("a", 100)]
    registry, exporter = setup(first)
    exporter.record(first, tracker.update(first, now=T0), 0.1)

    second = [up("a", 99, fork="f")]  # went backwards, onto a different block 99
    exporter.record(second, tracker.update(second, now=T0 + 5), 0.1)
    s = scrape(registry)

    assert s['bsc_endpoint_head_regressions_total{endpoint="a",network="mainnet"}'] == 1
    assert s['bsc_reorg_blocks_total{network="mainnet"}'] == 1
    assert s["bsc_sweeps_total"] == 2
