"""The long-running watchdog: sweep every few seconds and serve Prometheus metrics.

    python -m bsc_pulse.main        (or: make run)

Settings come from environment variables, which suits Docker, and can be
overridden on the command line:

    BSC_PULSE_CONFIG    endpoints file             (default: config/endpoints.yaml)
    BSC_PULSE_INTERVAL  seconds between sweeps     (default: 5)
    BSC_PULSE_TIMEOUT   seconds per request        (default: 3)
    BSC_PULSE_LISTEN    where to serve /metrics    (default: 127.0.0.1:9105)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

import requests
from prometheus_client import CollectorRegistry, start_http_server

from bsc_pulse.compare import Snapshot, Tracker
from bsc_pulse.config import Endpoint, load_endpoints
from bsc_pulse.metrics import Exporter
from bsc_pulse.probe import make_sessions, sweep

log = logging.getLogger("bsc_pulse")

# web3_clientVersion barely ever changes, so ask for it once every N sweeps
# (about once a minute at the default interval) instead of every sweep.
CLIENT_VERSION_EVERY = 12


class Watchdog:
    """One sweep, compared and recorded. Separate from the loop so tests can call it."""

    def __init__(
        self,
        endpoints: list[Endpoint],
        exporter: Exporter,
        sessions: dict[str, requests.Session],
        timeout: float,
    ):
        self.endpoints = endpoints
        self.exporter = exporter
        self.sessions = sessions
        self.timeout = timeout
        self.tracker = Tracker()
        self.sweeps = 0

    def step(self) -> Snapshot:
        started = time.monotonic()
        results = sweep(
            self.endpoints,
            self.sessions,
            self.timeout,
            include_client_version=self.sweeps % CLIENT_VERSION_EVERY == 0,
        )
        duration = time.monotonic() - started
        snapshot = self.tracker.update(results, now=time.time())
        self.exporter.record(results, snapshot, duration)
        self.sweeps += 1
        return snapshot


def summarize(snapshot: Snapshot) -> list[str]:
    """One log line per network, for `docker compose logs`."""
    lines = []
    for net in snapshot.networks:
        eps = [e for e in snapshot.endpoints if e.network == net.network]
        parts = [f"{net.network}: head={net.head_block} usable={net.usable}/{net.endpoints}"]
        if net.block_interval_s is not None:
            parts.append(f"interval={net.block_interval_s:.3f}s")
        laggard = max(
            (e for e in eps if e.lag_blocks is not None), key=lambda e: e.lag_blocks, default=None
        )
        if laggard is not None and laggard.lag_blocks:
            parts.append(f"max_lag={laggard.lag_blocks} ({laggard.endpoint})")
        failed = [f"{e.endpoint}={e.error_kind or 'wrong_chain'}" for e in eps if not e.up]
        if failed:
            parts.append("down: " + ", ".join(failed))
        lines.append(" ".join(parts))
    return lines


def parse_listen(value: str) -> tuple[str, int]:
    """'127.0.0.1:9105' -> ('127.0.0.1', 9105)"""
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"expected HOST:PORT, got {value!r}")
    return host or "0.0.0.0", int(port)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    env = os.environ
    parser = argparse.ArgumentParser(description="Watch BSC RPC endpoints and export metrics.")
    parser.add_argument("--config", default=env.get("BSC_PULSE_CONFIG"))
    parser.add_argument("--interval", type=float, default=float(env.get("BSC_PULSE_INTERVAL", "5")))
    parser.add_argument("--timeout", type=float, default=float(env.get("BSC_PULSE_TIMEOUT", "3")))
    parser.add_argument("--listen", default=env.get("BSC_PULSE_LISTEN", "127.0.0.1:9105"))
    args = parser.parse_args(argv)
    if args.timeout >= args.interval:
        parser.error("--timeout must be shorter than --interval, or sweeps will overlap")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    endpoints = load_endpoints(args.config)
    registry = CollectorRegistry()
    exporter = Exporter(endpoints, registry)
    host, port = parse_listen(args.listen)
    start_http_server(port, addr=host, registry=registry)
    log.info("serving metrics on http://%s:%d/metrics, %d endpoints", host, port, len(endpoints))

    watchdog = Watchdog(endpoints, exporter, make_sessions(endpoints), args.timeout)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):  # docker stop sends SIGTERM
        signal.signal(sig, lambda *_: stop.set())

    while not stop.is_set():
        started = time.monotonic()
        for line in summarize(watchdog.step()):
            log.info(line)
        # Sleep for the rest of the interval, so sweeps start on a steady rhythm.
        stop.wait(max(0.0, args.interval - (time.monotonic() - started)))

    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
