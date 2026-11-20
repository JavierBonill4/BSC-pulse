"""Tests for the watchdog service: one step end to end with fake sessions, plus settings."""

import pytest
from prometheus_client import CollectorRegistry

from bsc_pulse import main as service
from bsc_pulse.compare import Tracker
from bsc_pulse.config import Endpoint
from bsc_pulse.metrics import Exporter
from tests.test_compare import T0, down, up
from tests.test_metrics import scrape
from tests.test_probe import healthy


def make_watchdog(sessions_by_host):
    endpoints = [Endpoint("mainnet", 56, f"https://{host}") for host in sessions_by_host]
    sessions = {f"https://{host}": s for host, s in sessions_by_host.items()}
    registry = CollectorRegistry()
    watchdog = service.Watchdog(endpoints, Exporter(endpoints, registry), sessions, timeout=1)
    return watchdog, registry


def test_one_step_probes_compares_and_exports():
    watchdog, registry = make_watchdog({"a.test": healthy(100), "b.test": healthy(96)})
    snapshot = watchdog.step()

    assert {e.endpoint: e.lag_blocks for e in snapshot.endpoints} == {"a.test": 0, "b.test": 4}
    s = scrape(registry)
    assert s['bsc_endpoint_lag_blocks{endpoint="b.test",network="mainnet"}'] == 4
    key = (
        'bsc_rpc_requests_total{endpoint="a.test",method="eth_getBlockByNumber",'
        'network="mainnet",outcome="ok"}'
    )
    assert s[key] == 1


def test_client_version_is_only_asked_now_and_then():
    session = healthy()
    watchdog, _ = make_watchdog({"a.test": session})
    for _ in range(service.CLIENT_VERSION_EVERY + 1):
        watchdog.step()
    asked = [c for c in session.calls if c["json"]["method"] == "web3_clientVersion"]
    assert len(asked) == 2  # on the first sweep and on sweep number CLIENT_VERSION_EVERY


def test_summarize_mentions_lag_and_failures():
    snap = Tracker().update([up("a", 100), up("b", 95), down("c")], now=T0)
    (line,) = service.summarize(snap)
    assert "head=100 usable=2/3" in line
    assert "max_lag=5 (b)" in line
    assert "down: c=timeout" in line


@pytest.mark.parametrize(
    "value, expected",
    [
        ("127.0.0.1:9105", ("127.0.0.1", 9105)),
        ("0.0.0.0:80", ("0.0.0.0", 80)),
        (":9", ("0.0.0.0", 9)),
    ],
)
def test_parse_listen(value, expected):
    assert service.parse_listen(value) == expected


@pytest.mark.parametrize("value", ["9105", "host:", "host:port"])
def test_parse_listen_rejects_bad_values(value):
    with pytest.raises(ValueError):
        service.parse_listen(value)


def test_settings_come_from_environment(monkeypatch):
    monkeypatch.setenv("BSC_PULSE_INTERVAL", "10")
    monkeypatch.setenv("BSC_PULSE_LISTEN", "0.0.0.0:9105")
    args = service.parse_args([])
    assert args.interval == 10
    assert args.listen == "0.0.0.0:9105"
    assert service.parse_args(["--interval", "7"]).interval == 7  # command line wins


def test_timeout_must_be_shorter_than_interval():
    with pytest.raises(SystemExit):
        service.parse_args(["--interval", "2", "--timeout", "3"])
