"""Compare endpoints on the same network, and remember what changed between sweeps.

probe.py answers "what did each endpoint say just now?". This module answers
"what does that mean?":

- lag:     how far each endpoint is behind the highest endpoint on its network,
           in blocks and in seconds
- stalls:  how long since each endpoint's head (and the network's highest head)
           last moved. An endpoint that is not moving while the network is moving
           is stalled. A network whose highest head is not moving is a chain stall.
- hashes:  whether two endpoints disagree about which block sits at a height, and
           whether a block we saw earlier was later replaced by a different one (a reorg)
- regressions: an endpoint whose head went backwards (often a load balancer
           switching between nodes that are at different heights)

Nothing here touches the network or the clock: `Tracker.update` is given the probe
results and the current time, which makes all of it easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bsc_pulse.probe import ProbeResult

# How many recent block heights to remember hashes for, per network.
# At about 0.45 s per block this covers roughly the last four minutes.
HASH_HISTORY_BLOCKS = 512


@dataclass(frozen=True)
class EndpointView:
    """What one sweep concluded about one endpoint."""

    network: str
    endpoint: str  # short name, used as the metric label
    url: str
    up: bool  # returned a valid latest block on the right chain
    error_kind: str | None
    chain_id: int | None
    wrong_chain: bool
    client_version: str | None  # last known; refreshed about once a minute
    latency_s: float | None
    syncing: bool | None
    # The fields below are only set when `up` is True.
    head_block: int | None = None
    head_age_s: float | None = None
    lag_blocks: int | None = None
    lag_s: float | None = None
    head_unchanged_s: float | None = None


@dataclass(frozen=True)
class NetworkView:
    """What one sweep concluded about a whole network."""

    network: str
    chain_id: int
    endpoints: int
    usable: int
    # The fields below are only set when at least one endpoint is usable.
    head_block: int | None = None  # the highest head any usable endpoint reported
    head_age_s: float | None = None  # age of the freshest head we saw
    head_unchanged_s: float | None = None  # time since the highest head last moved
    block_interval_s: float | None = None  # measured, not assumed


@dataclass(frozen=True)
class Events:
    """Things that happened during this sweep, to be added to counters."""

    reorg_blocks: dict[str, int] = field(default_factory=dict)  # network -> heights replaced
    disagreements: dict[str, int] = field(default_factory=dict)  # network -> heights in dispute
    regressions: list[tuple[str, str]] = field(default_factory=list)  # (network, endpoint)


@dataclass(frozen=True)
class Snapshot:
    """The result of comparing one sweep."""

    taken_at: float
    endpoints: list[EndpointView]
    networks: list[NetworkView]
    events: Events


class Tracker:
    """Keeps the little state that comparisons need from one sweep to the next."""

    def __init__(self, history_blocks: int = HASH_HISTORY_BLOCKS):
        self._history_blocks = history_blocks
        self._endpoint_heads: dict[str, tuple[int, float]] = {}  # url -> (block, changed at)
        # network -> (highest block, that block's timestamp, when it last changed)
        self._network_heads: dict[str, tuple[int, float, float]] = {}
        self._intervals: dict[str, float] = {}  # network -> last measured block interval
        self._hashes: dict[str, dict[int, str]] = {}  # network -> {height: hash}
        self._versions: dict[str, str] = {}  # url -> last known client version

    def update(self, results: list[ProbeResult], now: float) -> Snapshot:
        endpoint_views: list[EndpointView] = []
        network_views: list[NetworkView] = []
        events = Events()

        networks = list(dict.fromkeys(r.endpoint.network for r in results))  # keep config order
        for network in networks:
            rows = [r for r in results if r.endpoint.network == network]
            usable = [r for r in rows if r.usable and r.head is not None]
            # The reference is the highest head on this network. We cannot know the
            # "true" height from outside; the highest endpoint is the best estimate.
            reference = max(usable, key=lambda r: r.head.number, default=None)

            for result in rows:
                endpoint_views.append(self._endpoint_view(result, reference, now, events))
            network_views.append(self._network_view(network, rows, usable, reference, now))

            replaced, disputed = self._check_hashes(network, usable)
            events.reorg_blocks[network] = replaced
            events.disagreements[network] = disputed

        return Snapshot(now, endpoint_views, network_views, events)

    # ---- per endpoint ----

    def _endpoint_view(
        self, r: ProbeResult, reference: ProbeResult | None, now: float, events: Events
    ) -> EndpointView:
        url, name = r.endpoint.url, r.endpoint.name
        if r.client_version:
            self._versions[url] = r.client_version
        common = dict(
            network=r.endpoint.network,
            endpoint=name,
            url=url,
            up=r.usable,
            error_kind=r.error_kind,
            chain_id=r.chain_id,
            wrong_chain=r.wrong_chain,
            client_version=self._versions.get(url),
            latency_s=r.latency_s,
            syncing=r.syncing,
        )
        if not r.usable or r.head is None or reference is None or reference.head is None:
            return EndpointView(**common)

        number = r.head.number
        previous = self._endpoint_heads.get(url)
        if previous is None or previous[0] != number:
            if previous is not None and number < previous[0]:
                events.regressions.append((r.endpoint.network, name))
            self._endpoint_heads[url] = (number, now)

        return EndpointView(
            **common,
            head_block=number,
            head_age_s=r.head_age_s,
            lag_blocks=reference.head.number - number,
            # Block timestamps, not our clock: "how much older is this endpoint's head
            # than the newest head", which does not depend on network round trips.
            lag_s=max(0.0, reference.head.time_s - r.head.time_s),
            head_unchanged_s=now - self._endpoint_heads[url][1],
        )

    # ---- per network ----

    def _network_view(
        self,
        network: str,
        rows: list[ProbeResult],
        usable: list[ProbeResult],
        reference: ProbeResult | None,
        now: float,
    ) -> NetworkView:
        view = NetworkView(
            network=network,
            chain_id=rows[0].endpoint.chain_id,
            endpoints=len(rows),
            usable=len(usable),
        )
        if reference is None or reference.head is None:
            return view

        head = reference.head
        previous = self._network_heads.get(network)
        if previous is None or head.number > previous[0]:
            if previous is not None:
                # Average time per block since the last time the head moved.
                self._intervals[network] = (head.time_s - previous[1]) / (head.number - previous[0])
            self._network_heads[network] = (head.number, head.time_s, now)
        # If the highest head went *down* (the leading endpoint failed), keep the old
        # record: the network has not produced anything new that we can see.

        return NetworkView(
            network=view.network,
            chain_id=view.chain_id,
            endpoints=view.endpoints,
            usable=view.usable,
            head_block=head.number,
            head_age_s=min(r.head_age_s for r in usable if r.head_age_s is not None),
            head_unchanged_s=now - self._network_heads[network][2],
            block_interval_s=self._intervals.get(network),
        )

    # ---- block hashes ----

    def _check_hashes(self, network: str, usable: list[ProbeResult]) -> tuple[int, int]:
        """Return (heights whose block was replaced, heights endpoints disagree on).

        Each head tells us two facts: the hash at its own height, and (through
        parentHash) the hash at the height below. If two endpoints give different
        hashes for the same height in the same sweep, they disagree. If a height we
        saw in an earlier sweep now has a different hash, that block was replaced: a reorg.

        This only sees heights that happen to be reported, so it samples the chain
        rather than checking every block.
        """
        seen: dict[int, set[str]] = {}
        for r in usable:
            assert r.head is not None
            seen.setdefault(r.head.number, set()).add(r.head.hash)
            seen.setdefault(r.head.number - 1, set()).add(r.head.parent_hash)

        disputed = sum(1 for hashes in seen.values() if len(hashes) > 1)

        history = self._hashes.setdefault(network, {})
        replaced = 0
        for height, hashes in seen.items():
            if len(hashes) > 1:
                continue  # endpoints disagree right now; record nothing until they settle
            (block_hash,) = hashes
            previous = history.get(height)
            if previous is not None and previous != block_hash:
                replaced += 1
            history[height] = block_hash

        if history:
            cutoff = max(history) - self._history_blocks
            for height in [h for h in history if h <= cutoff]:
                del history[height]
        return replaced, disputed
