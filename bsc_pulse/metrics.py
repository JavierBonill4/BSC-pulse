"""Expose the latest comparison as Prometheus metrics.

There are two kinds of metric here:

- Counters and a histogram (requests by outcome, latency, reorgs...). These only
  ever go up, so they are updated once per sweep with prometheus_client's own types.
- Gauges that describe "right now" (lag, head age, up...). These are rebuilt from
  the latest Snapshot every time Prometheus scrapes. Rebuilding means a value never
  outlives its meaning: when an endpoint goes down, its lag simply disappears
  instead of staying frozen at the last value it had while it was up.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from bsc_pulse import rpc
from bsc_pulse.compare import Snapshot
from bsc_pulse.config import Endpoint
from bsc_pulse.probe import ProbeResult

OUTCOMES = (
    "ok",
    rpc.TIMEOUT,
    rpc.CONNECTION,
    rpc.RATE_LIMITED,
    rpc.HTTP_ERROR,
    rpc.RPC_ERROR,
    rpc.BAD_RESPONSE,
)
METHODS = ("eth_chainId", "web3_clientVersion", "eth_getBlockByNumber", "eth_syncing")

# Chosen around what we saw live: ~30 ms on a warm connection, a few hundred ms for
# far-away or busy endpoints. Anything over a second is slow for a read this small.
LATENCY_BUCKETS = (0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 3.0, 5.0)

ENDPOINT_LABELS = ["network", "endpoint"]


class Exporter:
    """Owns every bsc_* metric and updates them after each sweep."""

    def __init__(self, endpoints: list[Endpoint], registry: CollectorRegistry):
        self._snapshot: Snapshot | None = None
        self._sweep_duration_s: float | None = None

        self.requests = Counter(
            "bsc_rpc_requests",
            "JSON-RPC requests sent, by method and outcome",
            [*ENDPOINT_LABELS, "method", "outcome"],
            registry=registry,
        )
        self.latency = Histogram(
            "bsc_rpc_latency_seconds",
            "Latency of successful eth_getBlockByNumber(latest) requests",
            ENDPOINT_LABELS,
            buckets=LATENCY_BUCKETS,
            registry=registry,
        )
        self.reorg_blocks = Counter(
            "bsc_reorg_blocks",
            "Block heights whose hash changed after we had already seen them",
            ["network"],
            registry=registry,
        )
        self.disagreements = Counter(
            "bsc_hash_disagreements",
            "Block heights where endpoints reported different hashes in the same sweep",
            ["network"],
            registry=registry,
        )
        self.regressions = Counter(
            "bsc_endpoint_head_regressions",
            "Times an endpoint reported a lower head than in the previous sweep",
            ENDPOINT_LABELS,
            registry=registry,
        )
        self.sweeps = Counter("bsc_sweeps", "Sweeps completed", registry=registry)

        # Create every series at zero up front. A counter that first appears at 1
        # looks like "no change" to rate() and increase(), so the first error would
        # be missed by alerts.
        for endpoint in endpoints:
            labels = (endpoint.network, endpoint.name)
            for method in METHODS:
                for outcome in OUTCOMES:
                    self.requests.labels(*labels, method, outcome)
            self.latency.labels(*labels)
            self.regressions.labels(*labels)
        for network in dict.fromkeys(e.network for e in endpoints):
            self.reorg_blocks.labels(network)
            self.disagreements.labels(network)

        registry.register(_SnapshotCollector(self))

    def record(
        self, results: list[ProbeResult], snapshot: Snapshot, sweep_duration_s: float
    ) -> None:
        """Add one sweep to the counters and make its snapshot the current one."""
        for r in results:
            labels = (r.endpoint.network, r.endpoint.name)
            for call in r.calls:
                self.requests.labels(*labels, call.method, call.outcome).inc()
            # Failed requests are left out of the histogram: a 5 s timeout is an
            # error (counted above), not a latency sample.
            if r.ok and r.latency_s is not None:
                self.latency.labels(*labels).observe(r.latency_s)

        events = snapshot.events
        for network, count in events.reorg_blocks.items():
            self.reorg_blocks.labels(network).inc(count)
        for network, count in events.disagreements.items():
            self.disagreements.labels(network).inc(count)
        for network, endpoint in events.regressions:
            self.regressions.labels(network, endpoint).inc()
        self.sweeps.inc()

        self._snapshot = snapshot
        self._sweep_duration_s = sweep_duration_s


def _gauge(
    name: str, doc: str, labels: list[str], rows: Iterable[tuple[list[str], float | None]]
) -> GaugeMetricFamily:
    """Build one gauge family, leaving out rows whose value is unknown."""
    family = GaugeMetricFamily(name, doc, labels=labels)
    for label_values, value in rows:
        if value is not None:
            family.add_metric(label_values, float(value))
    return family


def _flag(value: bool | None) -> float | None:
    return None if value is None else float(value)


class _SnapshotCollector(Collector):
    """Turns the current Snapshot into gauges each time Prometheus scrapes."""

    def __init__(self, exporter: Exporter):
        self._exporter = exporter

    def collect(self) -> Iterator[GaugeMetricFamily]:
        snapshot = self._exporter._snapshot
        if snapshot is None:
            return  # no sweep finished yet

        eps = snapshot.endpoints

        def per_endpoint(attr: str) -> list[tuple[list[str], float | None]]:
            return [([e.network, e.endpoint], getattr(e, attr)) for e in eps]

        yield _gauge(
            "bsc_endpoint_up",
            "1 if the endpoint returned a valid latest block on the right chain",
            ENDPOINT_LABELS,
            [([e.network, e.endpoint], float(e.up)) for e in eps],
        )
        yield _gauge(
            "bsc_endpoint_head_block",
            "Latest block number reported by the endpoint",
            ENDPOINT_LABELS,
            per_endpoint("head_block"),
        )
        yield _gauge(
            "bsc_endpoint_lag_blocks",
            "Blocks behind the highest endpoint on the same network",
            ENDPOINT_LABELS,
            per_endpoint("lag_blocks"),
        )
        yield _gauge(
            "bsc_endpoint_lag_seconds",
            "How much older this endpoint's head is than the newest head on the network",
            ENDPOINT_LABELS,
            per_endpoint("lag_s"),
        )
        yield _gauge(
            "bsc_endpoint_head_age_seconds",
            "This machine's clock minus the timestamp of the endpoint's latest block",
            ENDPOINT_LABELS,
            per_endpoint("head_age_s"),
        )
        yield _gauge(
            "bsc_endpoint_head_unchanged_seconds",
            "Seconds since the endpoint's head block number last changed",
            ENDPOINT_LABELS,
            per_endpoint("head_unchanged_s"),
        )
        yield _gauge(
            "bsc_endpoint_latency_seconds",
            "Latency of the latest eth_getBlockByNumber(latest) request in the last sweep",
            ENDPOINT_LABELS,
            per_endpoint("latency_s"),
        )
        yield _gauge(
            "bsc_endpoint_syncing",
            "1 if eth_syncing says the node is syncing",
            ENDPOINT_LABELS,
            [([e.network, e.endpoint], _flag(e.syncing)) for e in eps],
        )
        yield _gauge(
            "bsc_endpoint_wrong_chain",
            "1 if eth_chainId does not match the network's expected chain ID",
            ENDPOINT_LABELS,
            [([e.network, e.endpoint], float(e.wrong_chain)) for e in eps if e.chain_id],
        )
        yield _gauge(
            "bsc_endpoint_info",
            "Static details about the endpoint (value is always 1)",
            [*ENDPOINT_LABELS, "url", "client_version"],
            [([e.network, e.endpoint, e.url, e.client_version or ""], 1.0) for e in eps],
        )

        nets = snapshot.networks

        def per_network(attr: str) -> list[tuple[list[str], float | None]]:
            return [([n.network], getattr(n, attr)) for n in nets]

        yield _gauge(
            "bsc_network_endpoints", "Endpoints configured", ["network"], per_network("endpoints")
        )
        yield _gauge(
            "bsc_network_usable_endpoints",
            "Endpoints that returned a valid latest block on the right chain",
            ["network"],
            per_network("usable"),
        )
        yield _gauge(
            "bsc_network_head_block",
            "Highest block number reported by any usable endpoint",
            ["network"],
            per_network("head_block"),
        )
        yield _gauge(
            "bsc_network_head_age_seconds",
            "Age of the freshest head reported by any usable endpoint",
            ["network"],
            per_network("head_age_s"),
        )
        yield _gauge(
            "bsc_network_head_unchanged_seconds",
            "Seconds since the network's highest head last moved",
            ["network"],
            per_network("head_unchanged_s"),
        )
        yield _gauge(
            "bsc_network_block_interval_seconds",
            "Measured average time between blocks since the head last moved",
            ["network"],
            per_network("block_interval_s"),
        )

        yield _gauge(
            "bsc_last_sweep_timestamp_seconds",
            "Unix time at which the last sweep finished",
            [],
            [([], snapshot.taken_at)],
        )
        yield _gauge(
            "bsc_sweep_duration_seconds",
            "How long the last sweep took",
            [],
            [([], self._exporter._sweep_duration_s)],
        )
