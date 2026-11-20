"""Tests for one sweep across several endpoints, using fake sessions."""

import pytest
import requests

from bsc_pulse import probe, rpc
from bsc_pulse.config import Endpoint
from tests.fakes import REAL_BLOCK, FakeSession, answer

BLOCK_TIME = 1794768287.450  # the timestamp inside REAL_BLOCK


def block_at(number: int) -> dict:
    return {**REAL_BLOCK, "number": hex(number)}


def healthy(number: int = 133785820, chain_id: int = 56, syncing=False) -> FakeSession:
    return FakeSession(
        {
            "eth_chainId": answer(hex(chain_id)),
            "web3_clientVersion": answer("Geth/v1.8.2/linux-amd64/go1.26.10"),
            "eth_getBlockByNumber": answer(block_at(number)),
            "eth_syncing": answer(syncing),
        }
    )


def endpoints(*hosts: str) -> list[Endpoint]:
    return [Endpoint(network="mainnet", chain_id=56, url=f"https://{h}") for h in hosts]


def run(sessions_by_host: dict) -> list[probe.ProbeResult]:
    eps = endpoints(*sessions_by_host)
    sessions = {f"https://{host}": session for host, session in sessions_by_host.items()}
    return probe.sweep(eps, sessions, timeout=1)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pretend every answer arrives 0.5 s after REAL_BLOCK was produced."""
    monkeypatch.setattr(rpc.time, "time", lambda: BLOCK_TIME + 0.5)


def test_healthy_endpoint():
    (result,) = run({"a.test": healthy()})
    assert result.usable
    assert result.head.number == 133785820
    assert result.head_age_s == pytest.approx(0.5)
    assert result.syncing is False
    assert result.chain_id == 56
    assert result.client_version.startswith("Geth/v1.8.2")
    assert result.latency_s is not None


def test_head_is_requested_after_identity_so_the_connection_is_warm():
    session = healthy()
    run({"a.test": session})
    methods = [c["json"]["method"] for c in session.calls]
    assert methods == ["eth_chainId", "web3_clientVersion", "eth_getBlockByNumber", "eth_syncing"]


def test_every_request_is_recorded_with_its_outcome():
    session = healthy()
    session.replies["eth_syncing"] = requests.ReadTimeout()
    (result,) = run({"a.test": session})
    assert [(c.method, c.outcome) for c in result.calls] == [
        ("eth_chainId", "ok"),
        ("web3_clientVersion", "ok"),
        ("eth_getBlockByNumber", "ok"),
        ("eth_syncing", rpc.TIMEOUT),
    ]
    assert result.syncing is None  # unknown, not "not syncing"


def test_client_version_can_be_skipped():
    session = healthy()
    eps = endpoints("a.test")
    (result,) = probe.sweep(eps, {"https://a.test": session}, 1, include_client_version=False)
    assert "web3_clientVersion" not in [c["json"]["method"] for c in session.calls]
    assert result.chain_id == 56 and result.client_version is None


def test_one_dead_endpoint_does_not_affect_the_others():
    dead = FakeSession(
        {"eth_chainId": requests.ConnectTimeout(), "eth_getBlockByNumber": requests.ReadTimeout()}
    )
    good, bad = run({"good.test": healthy(), "dead.test": dead})
    assert good.usable
    assert not bad.ok
    assert bad.error_kind == rpc.TIMEOUT
    assert bad.head is None and bad.syncing is None


def test_results_come_back_in_config_order():
    results = run({"c.test": healthy(), "a.test": healthy(), "b.test": healthy()})
    assert [r.endpoint.name for r in results] == ["c.test", "a.test", "b.test"]


def test_wrong_chain_is_flagged_and_not_usable():
    (result,) = run({"a.test": healthy(chain_id=97)})
    assert result.ok  # it did return a block...
    assert result.wrong_chain  # ...but for the wrong network
    assert not result.usable


def test_syncing_node_is_reported():
    (result,) = run({"a.test": healthy(syncing={"currentBlock": "0x1", "highestBlock": "0x9"})})
    assert result.syncing is True


def test_block_without_expected_fields_is_bad_response():
    session = healthy()
    session.replies["eth_getBlockByNumber"] = answer(None)  # node has no latest block
    (result,) = run({"a.test": session})
    assert not result.ok
    assert result.error_kind == rpc.BAD_RESPONSE


def test_rate_limited_endpoint_is_reported_as_such():
    session = healthy()
    session.replies["eth_getBlockByNumber"] = answer(None)
    session.replies["eth_getBlockByNumber"].status_code = 429
    (result,) = run({"a.test": session})
    assert result.error_kind == rpc.RATE_LIMITED


# ---- the printed table ----


def test_report_shows_how_far_behind_each_endpoint_is():
    results = run({"fresh.test": healthy(number=1000), "stale.test": healthy(number=997)})
    report = probe.format_report(results)
    lines = report.splitlines()

    assert "highest block 1000, 2/2 endpoints usable" in lines[0]
    fresh_row = next(line for line in lines if "fresh.test" in line).split()
    stale_row = next(line for line in lines if "stale.test" in line).split()
    assert fresh_row[1:3] == ["1000", "0"]
    assert stale_row[1:3] == ["997", "3"]


def test_report_shows_errors_and_wrong_chain():
    dead = FakeSession(
        {"eth_chainId": requests.ConnectionError(), "eth_getBlockByNumber": requests.ReadTimeout()}
    )
    results = run({"good.test": healthy(), "dead.test": dead, "odd.test": healthy(chain_id=97)})
    report = probe.format_report(results)

    assert "1/3 endpoints usable" in report
    assert "ERROR timeout (ReadTimeout)" in report
    assert "WRONG CHAIN: expected 56, endpoint says 97" in report


def test_main_exit_code_reflects_health(monkeypatch, capsys):
    eps = endpoints("a.test")
    monkeypatch.setattr(probe, "load_endpoints", lambda path: eps)

    monkeypatch.setattr(probe, "make_sessions", lambda _: {"https://a.test": healthy()})
    assert probe.main([]) == 0

    monkeypatch.setattr(probe, "make_sessions", lambda _: {"https://a.test": healthy(chain_id=97)})
    assert probe.main([]) == 1
    assert "a.test" in capsys.readouterr().out
