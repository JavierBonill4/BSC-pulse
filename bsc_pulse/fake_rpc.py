"""A fake BSC endpoint you can break on purpose, for demos and end-to-end tests.

    python -m bsc_pulse.fake_rpc --ports 18545,18546,18547      (or: make fake-rpc)

Each port is a separate endpoint serving the same simulated chain (one block every
0.45 s, the same hashes everywhere), so healthy endpoints agree with each other.
Change how one of them behaves while everything runs:

    curl 'http://127.0.0.1:18547/control?mode=stall'
    curl 'http://127.0.0.1:18547/control?mode=lag&blocks=30'
    curl 'http://127.0.0.1:18547/control?mode=healthy'

It is not a node. It answers only the four methods the watchdog uses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

GENESIS = 1_700_000_000.0  # unix time of simulated block 0
BLOCK_TIME = 0.45
SLOW_DELAY_S = 1.5  # above the 1 s "slow" alert threshold, below the 3 s request timeout
TIMEOUT_DELAY_S = 10.0  # longer than the watchdog waits

MODES = {
    "healthy": "follows the chain",
    "lag": "stays `blocks` behind the chain (default 20)",
    "stall": "freezes at the block it had when the mode was set",
    "slow": f"healthy, but every answer takes {SLOW_DELAY_S} s",
    "timeout": f"never answers in time (waits {TIMEOUT_DELAY_S} s)",
    "error": "answers every request with HTTP 503",
    "ratelimit": "answers every other request with HTTP 429",
    "wrong_chain": "says it is chain 97 (Chapel)",
    "syncing": "3 blocks behind and eth_syncing reports progress",
    "fork": "serves a different block at the same height as everyone else",
}


@dataclass
class State:
    """How one fake endpoint behaves right now."""

    mode: str = "healthy"
    lag_blocks: int = 20
    frozen_at: int | None = None  # set when entering "stall"
    requests: int = 0  # used to alternate in "ratelimit" mode


def chain_height(now: float) -> int:
    return int((now - GENESIS) / BLOCK_TIME)


def block_hash(number: int, fork: bool = False) -> str:
    seed = f"{'fork' if fork else 'main'}:{number}".encode()
    return "0x" + hashlib.sha256(seed).hexdigest()


def make_block(number: int, fork: bool = False) -> dict:
    """The fields of a BSC block that the watchdog reads."""
    produced = GENESIS + number * BLOCK_TIME
    return {
        "number": hex(number),
        "hash": block_hash(number, fork),
        "parentHash": block_hash(number - 1, fork),
        "timestamp": hex(int(produced)),
        "milliTimestamp": hex(int(produced * 1000)),
    }


def set_mode(state: State, mode: str, now: float, lag_blocks: int | None = None) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}, expected one of: {', '.join(MODES)}")
    state.mode = mode
    state.frozen_at = chain_height(now) if mode == "stall" else None
    if lag_blocks is not None:
        state.lag_blocks = lag_blocks


def respond(state: State, method: str, now: float) -> tuple[int, dict | str, float]:
    """Decide the answer to one request: (HTTP status, body, seconds to wait first).

    Pure apart from the request counter, so every mode is easy to test.
    """
    state.requests += 1
    mode = state.mode
    delay = {"slow": SLOW_DELAY_S, "timeout": TIMEOUT_DELAY_S}.get(mode, 0.0)

    if mode == "error":
        return 503, "backend unavailable", delay
    if mode == "ratelimit" and state.requests % 2 == 0:
        return 429, "Too Many Requests", delay

    height = chain_height(now)
    if mode == "lag":
        height -= state.lag_blocks
    elif mode == "syncing":
        height -= 3
    elif mode == "stall" and state.frozen_at is not None:
        height = state.frozen_at

    if method == "eth_chainId":
        result: object = "0x61" if mode == "wrong_chain" else "0x38"
    elif method == "web3_clientVersion":
        result = f"FakeRPC/{mode}"
    elif method == "eth_syncing":
        result = (
            {
                "startingBlock": hex(height - 100),
                "currentBlock": hex(height),
                "highestBlock": hex(height + 3),
            }
            if mode == "syncing"
            else False
        )
    elif method == "eth_getBlockByNumber":
        result = make_block(height, fork=(mode == "fork"))
    else:
        error = {"code": -32601, "message": f"the method {method} does not exist/is not available"}
        return 200, {"jsonrpc": "2.0", "id": 1, "error": error}, delay
    return 200, {"jsonrpc": "2.0", "id": 1, "result": result}, delay


def make_handler(state: State, lock: threading.Lock) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                method = request["method"]
            except (ValueError, KeyError, TypeError):
                self._send(400, "expected one JSON-RPC request")
                return
            with lock:
                status, body, delay = respond(state, method, time.time())
            time.sleep(delay)
            self._send(status, body)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path != "/control":
                self._send(404, "try /control?mode=stall")
                return
            query = parse_qs(url.query)
            try:
                with lock:
                    if "mode" in query:
                        blocks = int(query["blocks"][0]) if "blocks" in query else None
                        set_mode(state, query["mode"][0], time.time(), blocks)
                    snapshot = asdict(state)
            except ValueError as exc:
                self._send(400, str(exc))
                return
            self._send(200, {**snapshot, "modes": MODES})

        def _send(self, status: int, body: dict | str) -> None:
            data = (json.dumps(body) if isinstance(body, dict) else body).encode()
            content_type = "application/json" if isinstance(body, dict) else "text/plain"
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client gave up waiting ("timeout" mode); nothing to do

        def log_message(self, fmt: str, *args) -> None:
            pass  # one line per request would drown everything else

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fake BSC endpoints for demos and tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ports", default="18545,18546,18547", help="comma-separated")
    args = parser.parse_args(argv)

    servers = []
    for port in (int(p) for p in args.ports.split(",")):
        server = ThreadingHTTPServer((args.host, port), make_handler(State(), threading.Lock()))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        print(f"fake endpoint on http://{args.host}:{port}  (control: /control?mode=...)")
    print("modes:", ", ".join(MODES), flush=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    for server in servers:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
