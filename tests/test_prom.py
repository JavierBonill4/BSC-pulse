"""Tests for fetching alert context from Prometheus, with a fake HTTP session."""

import json

import pytest
import requests

from bsc_pulse.prom import ContextUnavailable, PrometheusClient, gather, matchers
from tests.fakes import FakeResponse


class FakePrometheus:
    """Answers /api/v1/query by matching the start of the PromQL against `answers`."""

    def __init__(self, answers: dict, fail: Exception | None = None):
        self.answers = answers
        self.fail = fail
        self.queries: list[str] = []

    def get(self, url, params=None, timeout=None):
        if self.fail:
            raise self.fail
        query = params["query"]
        self.queries.append(query)
        result = []
        for prefix, rows in self.answers.items():
            if query.startswith(prefix):
                result = [{"metric": labels, "value": [0, str(v)]} for labels, v in rows]
                break
        body = {"status": "success", "data": {"resultType": "vector", "result": result}}
        return FakeResponse(200, json.dumps(body))


def client(answers=None, fail=None):
    return PrometheusClient("http://prom:9090/", session=FakePrometheus(answers or {}, fail))


def test_gather_collects_endpoint_and_network_numbers():
    answers = {
        "bsc_network_head_block": [({}, 1000)],
        "bsc_network_usable_endpoints": [({}, 6)],
        "bsc_endpoint_info": [({"url": "https://a.example", "client_version": "Geth/v1.8.2"}, 1)],
        "bsc_endpoint_lag_blocks": [({}, 14)],
        "histogram_quantile": [({}, 0.04)],
        "sum by (outcome)": [({"outcome": "timeout"}, 12), ({"outcome": "rate_limited"}, 3)],
    }
    ctx = gather(client(answers), {"network": "mainnet", "endpoint": "a.example"})

    assert ctx["network"]["head_block"] == 1000
    assert ctx["network"]["usable"] == 6
    assert ctx["network"]["block_interval_s"] is None  # Prometheus had nothing
    ep = ctx["endpoint"]
    assert ep["url"] == "https://a.example"
    assert ep["client_version"] == "Geth/v1.8.2"
    assert ep["lag_blocks"] == 14
    assert ep["latency_p95_s"] == 0.04
    assert ep["errors_5m"] == {"timeout": 12, "rate_limited": 3}


def test_network_alert_does_not_ask_about_an_endpoint():
    prom = client()
    ctx = gather(prom, {"network": "chapel"})
    assert "endpoint" not in ctx
    assert all('endpoint="' not in q for q in prom.session.queries)


def test_nan_counts_as_unknown():
    # e.g. an error ratio of 0/0 when no requests were made
    prom = client({"bsc_endpoint_up": [({}, float("nan"))]})
    assert prom.value('bsc_endpoint_up{network="x"}') is None


def test_unreachable_prometheus_raises_context_unavailable():
    with pytest.raises(ContextUnavailable):
        gather(client(fail=requests.ConnectionError()), {"network": "mainnet"})


def test_prometheus_error_raises_context_unavailable():
    class Broken:
        def get(self, url, params=None, timeout=None):
            return FakeResponse(400, json.dumps({"status": "error", "error": "parse error"}))

    with pytest.raises(ContextUnavailable, match="parse error"):
        PrometheusClient("http://prom", session=Broken()).query("bad{")


def test_matchers_escape_quotes():
    assert matchers(network="main", endpoint='a"b') == 'network="main",endpoint="a\\"b"'
