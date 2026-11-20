"""Probe every configured endpoint once and print a comparison table.

    python -m bsc_pulse.probe        (or: make probe)

Exit code: 0 if every endpoint returned a usable latest block on the right chain,
1 otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

from bsc_pulse import rpc
from bsc_pulse.config import Endpoint, load_endpoints

DEFAULT_TIMEOUT_S = 5.0
USER_AGENT = "bsc-pulse/0.1"


@dataclass(frozen=True)
class CallRecord:
    """One JSON-RPC request and how it ended. Used for the request and error counters."""

    method: str
    outcome: str  # "ok", or one of the error kinds in rpc.py


@dataclass
class ProbeResult:
    """Everything one sweep learned about one endpoint."""

    endpoint: Endpoint
    ok: bool = False  # did we get a usable latest block?
    error_kind: str | None = None
    error_detail: str = ""
    latency_s: float | None = None  # time taken by the latest-block call
    head: rpc.BlockHead | None = None
    head_age_s: float | None = None  # our clock minus the block's timestamp
    syncing: bool | None = None  # None means we could not find out
    chain_id: int | None = None
    client_version: str | None = None
    calls: list[CallRecord] = field(default_factory=list)  # every request sent this sweep

    @property
    def wrong_chain(self) -> bool:
        return self.chain_id is not None and self.chain_id != self.endpoint.chain_id

    @property
    def usable(self) -> bool:
        return self.ok and not self.wrong_chain


def _call(
    session: requests.Session,
    result: ProbeResult,
    method: str,
    params: list | None,
    timeout: float,
) -> rpc.RpcResult:
    """Send one request for this endpoint and remember how it ended."""
    reply = rpc.call(session, result.endpoint.url, method, params, timeout)
    result.calls.append(CallRecord(method, "ok" if reply.ok else str(reply.error_kind)))
    return reply


# ---- The three steps of a sweep. Each one fills in part of a ProbeResult. ----


def probe_identity(
    session: requests.Session,
    result: ProbeResult,
    timeout: float,
    include_client_version: bool = True,
) -> None:
    """Ask which chain the endpoint serves and (optionally) what software it runs.

    eth_chainId is asked every sweep: it is cheap, it catches an endpoint that starts
    serving the wrong network, and it opens the connection before the head step.
    web3_clientVersion rarely changes, so the long-running watchdog asks for it less often.
    """
    reply = _call(session, result, "eth_chainId", None, timeout)
    if not reply.ok:
        return  # unreachable right now; the head step will record the error
    with contextlib.suppress(ValueError):  # a garbled answer just leaves chain_id unknown
        result.chain_id = rpc.hex_to_int(reply.result)
    if not include_client_version:
        return
    reply = _call(session, result, "web3_clientVersion", None, timeout)
    if reply.ok and isinstance(reply.result, str):
        result.client_version = reply.result


def probe_head(session: requests.Session, result: ProbeResult, timeout: float) -> None:
    """Fetch the latest block. This is the measurement everything else is built on.

    eth_getBlockByNumber is used instead of eth_blockNumber because one call gives
    the height, the hash and the timestamp together.
    """
    reply = _call(session, result, "eth_getBlockByNumber", ["latest", False], timeout)
    result.latency_s = reply.latency_s
    if not reply.ok:
        result.error_kind, result.error_detail = reply.error_kind, reply.error_detail
        return
    try:
        head = rpc.parse_block(reply.result)
    except ValueError as exc:
        result.error_kind, result.error_detail = rpc.BAD_RESPONSE, str(exc)
        return
    result.ok = True
    result.head = head
    result.head_age_s = reply.finished_at - head.time_s


def probe_syncing(session: requests.Session, result: ProbeResult, timeout: float) -> None:
    """Ask the node whether it thinks it is still syncing."""
    if not result.ok:
        return
    reply = _call(session, result, "eth_syncing", None, timeout)
    if reply.ok:
        with contextlib.suppress(ValueError):  # a garbled answer leaves syncing unknown
            result.syncing = rpc.parse_syncing(reply.result)


def make_sessions(endpoints: list[Endpoint]) -> dict[str, requests.Session]:
    """One HTTP session per endpoint, so each keeps its own open connection."""
    sessions = {}
    for endpoint in endpoints:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        sessions[endpoint.url] = session
    return sessions


def sweep(
    endpoints: list[Endpoint],
    sessions: dict[str, requests.Session],
    timeout: float = DEFAULT_TIMEOUT_S,
    include_client_version: bool = True,
) -> list[ProbeResult]:
    """Probe every endpoint once. Returns one result per endpoint, in the same order.

    Each step runs on all endpoints at the same time (one thread per endpoint) and
    finishes everywhere before the next step starts. The order matters:

    1. identity - also opens the HTTPS connection, so step 2 does not pay for the
       TLS handshake and its latency reflects the endpoint, not the connection setup
    2. head     - every endpoint is asked for its latest block at the same moment.
       Asking them one after another would make later ones look "ahead" simply
       because the chain moved on while we were asking.
    3. syncing
    """
    results = [ProbeResult(endpoint=endpoint) for endpoint in endpoints]
    if not results:
        return results

    with ThreadPoolExecutor(max_workers=len(results)) as pool:

        def on_all(step, **options) -> None:
            # list() waits until every endpoint has finished this step
            list(pool.map(lambda r: step(sessions[r.endpoint.url], r, timeout, **options), results))

        on_all(probe_identity, include_client_version=include_client_version)
        on_all(probe_head)
        on_all(probe_syncing)
    return results


# ---- Printing ----


def format_report(results: list[ProbeResult]) -> str:
    """Render the results as one table per network."""
    lines: list[str] = []
    networks = list(dict.fromkeys(r.endpoint.network for r in results))  # keep config order
    for network in networks:
        rows = [r for r in results if r.endpoint.network == network]
        heights = [r.head.number for r in rows if r.usable and r.head]
        highest = max(heights) if heights else None
        width = max(len(r.endpoint.name) for r in rows)

        lines.append(
            f"{network} (chain {rows[0].endpoint.chain_id}): "
            f"highest block {highest if highest is not None else 'unknown'}, "
            f"{len(heights)}/{len(rows)} endpoints usable"
        )
        lines.append(
            f"  {'ENDPOINT':<{width}}  {'BLOCK':>10}  {'BEHIND':>6}  {'HEAD AGE':>8}  "
            f"{'LATENCY':>7}  {'SYNCING':<7}  CLIENT"
        )
        for r in rows:
            name = f"  {r.endpoint.name:<{width}}"
            if not r.ok or r.head is None:
                took = f" after {r.latency_s * 1000:.0f}ms" if r.latency_s is not None else ""
                lines.append(f"{name}  ERROR {r.error_kind} ({r.error_detail}){took}")
                continue
            behind = highest - r.head.number if highest is not None else 0
            syncing = {True: "YES", False: "no", None: "?"}[r.syncing]
            lines.append(
                f"{name}  {r.head.number:>10}  {behind:>6}  {r.head_age_s:>7.2f}s  "
                f"{r.latency_s * 1000:>5.0f}ms  {syncing:<7}  {(r.client_version or '?')[:44]}"
            )
            if r.wrong_chain:
                lines.append(
                    f"    ^ WRONG CHAIN: expected {r.endpoint.chain_id}, endpoint says {r.chain_id}"
                )
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe BSC RPC endpoints once and compare them.")
    parser.add_argument("--config", help="endpoints file (default: config/endpoints.yaml)")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds per request"
    )
    args = parser.parse_args(argv)

    endpoints = load_endpoints(args.config)
    results = sweep(endpoints, make_sessions(endpoints), args.timeout)
    print(format_report(results))
    return 0 if all(r.usable for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
