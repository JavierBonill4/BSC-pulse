"""Ask Prometheus for the recent numbers behind an alert.

An alert only says "endpoint X is lagging". To write a useful note, the notifier
also wants to know: how far behind, how fast it answers, what errors it returns,
what the rest of the network looks like. This module fetches those numbers.
"""

from __future__ import annotations

import math

import requests


class ContextUnavailable(Exception):
    """Prometheus could not be asked, or gave an answer we cannot use."""


class PrometheusClient:
    def __init__(self, base_url: str, session: requests.Session | None = None, timeout=3.0):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def query(self, promql: str) -> list[tuple[dict, float]]:
        """Run an instant query. Returns [(labels, value)]."""
        try:
            response = self.session.get(
                f"{self.base_url}/api/v1/query", params={"query": promql}, timeout=self.timeout
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ContextUnavailable(f"query failed: {type(exc).__name__}") from exc
        if response.status_code != 200 or payload.get("status") != "success":
            raise ContextUnavailable(f"HTTP {response.status_code}: {payload.get('error', '')}")
        return [(r["metric"], float(r["value"][1])) for r in payload["data"]["result"]]

    def value(self, promql: str) -> float | None:
        """The first value a query returns, or None if it returns nothing usable."""
        rows = self.query(promql)
        if not rows or math.isnan(rows[0][1]):
            return None
        return rows[0][1]


def matchers(**labels: str) -> str:
    """network="mainnet",endpoint="x" with quotes and backslashes escaped."""
    escaped = {k: v.replace("\\", "\\\\").replace('"', '\\"') for k, v in labels.items()}
    return ",".join(f'{k}="{v}"' for k, v in escaped.items())


def gather(client: PrometheusClient, labels: dict) -> dict:
    """Collect the numbers that explain an alert, based on its network/endpoint labels.

    Returns {"network": {...}, "endpoint": {...}} with whatever applies. A value is
    None when Prometheus has no data for it. Raises ContextUnavailable if
    Prometheus cannot be reached at all.
    """
    context: dict = {}
    network, endpoint = labels.get("network"), labels.get("endpoint")

    if network:
        n = "{" + matchers(network=network) + "}"
        context["network"] = {
            "head_block": client.value(f"bsc_network_head_block{n}"),
            "usable": client.value(f"bsc_network_usable_endpoints{n}"),
            "endpoints": client.value(f"bsc_network_endpoints{n}"),
            "block_interval_s": client.value(f"bsc_network_block_interval_seconds{n}"),
            "head_unchanged_s": client.value(f"bsc_network_head_unchanged_seconds{n}"),
        }

    if network and endpoint:
        m = matchers(network=network, endpoint=endpoint)
        e = "{" + m + "}"
        buckets = f"bsc_rpc_latency_seconds_bucket{e}"
        info = client.query(f"bsc_endpoint_info{e}")
        errors = client.query(
            f'sum by (outcome) (increase(bsc_rpc_requests_total{{{m},outcome!="ok"}}[5m])) > 0'
        )
        context["endpoint"] = {
            "url": info[0][0].get("url") if info else None,
            "client_version": (info[0][0].get("client_version") or None) if info else None,
            "up": client.value(f"bsc_endpoint_up{e}"),
            "lag_blocks": client.value(f"bsc_endpoint_lag_blocks{e}"),
            "lag_s": client.value(f"bsc_endpoint_lag_seconds{e}"),
            "head_age_s": client.value(f"bsc_endpoint_head_age_seconds{e}"),
            "head_unchanged_s": client.value(f"bsc_endpoint_head_unchanged_seconds{e}"),
            "wrong_chain": client.value(f"bsc_endpoint_wrong_chain{e}"),
            "syncing": client.value(f"bsc_endpoint_syncing{e}"),
            "latency_p95_s": client.value(
                f"histogram_quantile(0.95, sum by (le) (rate({buckets}[5m])))"
            ),
            "error_ratio": client.value(
                f'sum(rate(bsc_rpc_requests_total{{{m},outcome!="ok"}}[5m]))'
                f" / sum(rate(bsc_rpc_requests_total{e}[5m]))"
            ),
            "regressions_15m": client.value(
                f"sum(increase(bsc_endpoint_head_regressions_total{e}[15m]))"
            ),
            # e.g. {"timeout": 12.0, "rate_limited": 3.0}: what kind of failures, how many
            "errors_5m": {row_labels.get("outcome", "?"): v for row_labels, v in errors},
        }
    return context
