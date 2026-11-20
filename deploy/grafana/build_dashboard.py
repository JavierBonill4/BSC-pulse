"""Build the Grafana dashboard JSON from this script.

    python deploy/grafana/build_dashboard.py          write dashboards/bsc-pulse.json
    python deploy/grafana/build_dashboard.py --check  fail if the JSON is out of date

Hand-edited dashboard JSON is hundreds of lines that are hard to review. Here each
panel is one function call: a title, a PromQL query, a unit, and a sentence that
explains what to look for (shown as the panel's tooltip). A test checks that the
committed JSON matches this script, and that every query uses metrics that exist.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUTPUT = Path(__file__).parent / "dashboards" / "bsc-pulse.json"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}
NET = 'network="$network"'

_next_id = 0


def _id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def _thresholds(*steps: tuple[str, float | None]) -> dict:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def target(expr: str, legend: str = "", ref: str = "A", table: bool = False) -> dict:
    t = {"datasource": DATASOURCE, "expr": expr, "refId": ref, "legendFormat": legend}
    if table:
        t.update(instant=True, range=False, format="table")
    return t


def row(title: str, y: int) -> dict:
    return {
        "type": "row",
        "id": _id(),
        "title": title,
        "collapsed": False,
        "panels": [],
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
    }


def text(content: str, x: int, y: int, w: int, h: int) -> dict:
    return {
        "type": "text",
        "id": _id(),
        "title": "",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {"mode": "markdown", "content": content},
    }


def stat(
    title: str,
    expr: str,
    x: int,
    y: int,
    description: str,
    unit: str = "none",
    decimals: int | None = None,
    thresholds: dict | None = None,
    w: int = 4,
) -> dict:
    defaults: dict = {
        "unit": unit,
        "thresholds": thresholds or _thresholds(("blue", None)),
        "color": {"mode": "thresholds"},
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "id": _id(),
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": 4},
        "targets": [target(expr)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value",
            "justifyMode": "center",
        },
    }


def timeseries(
    title: str,
    targets: list[dict],
    x: int,
    y: int,
    description: str,
    unit: str = "none",
    alert_at: float | None = None,
    bars: bool = False,
    w: int = 12,
    h: int = 8,
) -> dict:
    custom: dict = {
        "drawStyle": "bars" if bars else "line",
        "lineWidth": 1,
        "fillOpacity": 60 if bars else 0,
        "showPoints": "never",
        "spanNulls": False,
        "stacking": {"mode": "normal" if bars else "none"},
    }
    thresholds = _thresholds(("transparent", None))
    if alert_at is not None:
        # Draw the alert threshold as a red dashed line, so the panel shows how close
        # each endpoint is to firing.
        custom["thresholdsStyle"] = {"mode": "dashed"}
        thresholds = _thresholds(("transparent", None), ("red", alert_at))
    return {
        "type": "timeseries",
        "id": _id(),
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": custom,
                "thresholds": thresholds,
                "color": {"mode": "palette-classic"},
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def table(
    title: str,
    targets: list[dict],
    x: int,
    y: int,
    w: int,
    h: int,
    description: str,
    transformations: list[dict],
    overrides: list[dict] | None = None,
) -> dict:
    return {
        "type": "table",
        "id": _id(),
        "title": title,
        "description": description,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "transformations": transformations,
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto"}, "thresholds": _thresholds(("text", None))},
            "overrides": overrides or [],
        },
        "options": {"showHeader": True, "cellHeight": "sm"},
    }


def column(name: str, **properties) -> dict:
    """A table column override: unit, decimals, colours or value mappings by column name."""
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": key, "value": value} for key, value in properties.items()],
    }


def colored(thresholds: dict) -> dict:
    return {"thresholds": thresholds, "custom.cellOptions": {"type": "color-background"}}


# ---- the dashboard ----

INTRO = """\
**How to read this:** every endpoint on a network is compared with the others.
**Lag** = how far behind the highest endpoint it is. **Latency** = how fast it answers.
They are different things: an endpoint can answer in 30 ms and still serve a block
from several seconds ago. Red dashed lines are alert thresholds.
Pick the network at the top. The `demo` network appears when running `make demo`."""


def endpoints_table(y: int) -> dict:
    # One instant query per column. `max by (endpoint)` gives every query the same
    # single label, so Grafana's "merge" lines them up into one row per endpoint.
    columns = [
        ("A", "Up", f"max by (endpoint) (bsc_endpoint_up{{{NET}}})"),
        ("B", "Lag (blocks)", f"max by (endpoint) (bsc_endpoint_lag_blocks{{{NET}}})"),
        ("C", "Lag", f"max by (endpoint) (bsc_endpoint_lag_seconds{{{NET}}})"),
        ("D", "Head age", f"max by (endpoint) (bsc_endpoint_head_age_seconds{{{NET}}})"),
        (
            "E",
            "Latency p95 (5m)",
            "histogram_quantile(0.95, sum by (le, endpoint) "
            f"(rate(bsc_rpc_latency_seconds_bucket{{{NET}}}[5m])))",
        ),
        (
            "F",
            "Failed (5m)",
            f'sum by (endpoint) (rate(bsc_rpc_requests_total{{{NET},outcome!="ok"}}[5m]))'
            f" / sum by (endpoint) (rate(bsc_rpc_requests_total{{{NET}}}[5m]))",
        ),
        ("G", "Syncing", f"max by (endpoint) (bsc_endpoint_syncing{{{NET}}})"),
    ]
    rename = {"endpoint": "Endpoint", **{f"Value #{ref}": name for ref, name, _ in columns}}
    order = {"Endpoint": 0, **{name: i for i, (_, name, _) in enumerate(columns, 1)}}
    up_down = [
        {
            "type": "value",
            "options": {
                "0": {"text": "DOWN", "color": "red"},
                "1": {"text": "up", "color": "green"},
            },
        }
    ]
    yes_no = [{"type": "value", "options": {"0": {"text": "no"}, "1": {"text": "YES"}}}]
    return table(
        "Endpoints right now",
        [target(expr, ref=ref, table=True) for ref, _, expr in columns],
        0,
        y,
        24,
        8,
        "One row per endpoint. Lag and Latency are coloured at their alert thresholds "
        "(5 s and 1 s). An empty cell means the endpoint did not return a usable block.",
        [
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True},
                    "renameByName": rename,
                    "indexByName": order,
                },
            },
        ],
        [
            column("Up", mappings=up_down, **colored(_thresholds(("text", None)))),
            column("Lag (blocks)", decimals=0),
            column(
                "Lag",
                unit="s",
                decimals=2,
                **colored(_thresholds(("green", None), ("orange", 2), ("red", 5))),
            ),
            column("Head age", unit="s", decimals=2),
            column(
                "Latency p95 (5m)",
                unit="s",
                decimals=3,
                **colored(_thresholds(("green", None), ("orange", 0.5), ("red", 1))),
            ),
            column(
                "Failed (5m)",
                unit="percentunit",
                decimals=1,
                **colored(_thresholds(("green", None), ("orange", 0.01), ("red", 0.1))),
            ),
            column("Syncing", mappings=yes_no),
        ],
    )


def build() -> dict:
    global _next_id
    _next_id = 0
    panels = [text(INTRO, 0, 0, 24, 3)]

    y = 3
    panels.append(row("Network right now", y))
    y += 1
    panels += [
        stat(
            "Highest block",
            f"bsc_network_head_block{{{NET}}}",
            0,
            y,
            "The highest block any usable endpoint on this network reports.",
            unit="locale",
        ),
        stat(
            "Usable endpoints",
            f"bsc_network_usable_endpoints{{{NET}}}",
            4,
            y,
            "Endpoints that returned a valid latest block on the right chain. "
            "Below 2, lag cannot be measured by comparison.",
            thresholds=_thresholds(("red", None), ("orange", 2), ("green", 3)),
        ),
        stat(
            "Block interval (measured)",
            f"bsc_network_block_interval_seconds{{{NET}}}",
            8,
            y,
            "Measured from block timestamps, not assumed. About 0.45 s since the Fermi hard fork.",
            unit="s",
            decimals=3,
        ),
        stat(
            "Freshest head age",
            f"bsc_network_head_age_seconds{{{NET}}}",
            12,
            y,
            "How old the newest block we can see is, by this server's clock. If this "
            "grows, the whole network looks stalled.",
            unit="s",
            decimals=2,
            thresholds=_thresholds(("green", None), ("orange", 5), ("red", 30)),
        ),
        stat(
            "Firing alerts (all networks)",
            'count(ALERTS{alertstate="firing"}) or vector(0)',
            16,
            y,
            "Alerts currently firing in Prometheus, across every network.",
            thresholds=_thresholds(("green", None), ("red", 1)),
        ),
        stat(
            "Replaced blocks (24h)",
            f"sum(increase(bsc_reorg_blocks_total{{{NET}}}[24h]))",
            20,
            y,
            "Blocks we had seen that were later replaced by a different block at the "
            "same height (reorgs). Sampled, not every block is checked.",
            decimals=0,
            thresholds=_thresholds(("green", None), ("orange", 1)),
        ),
    ]
    y += 4

    panels.append(row("Endpoints right now", y))
    y += 1
    panels.append(endpoints_table(y))
    y += 8
    panels += [
        table(
            "Firing alerts",
            [target('ALERTS{alertstate="firing"}', table=True)],
            0,
            y,
            14,
            6,
            "Every alert currently firing in Prometheus. Telegram gets a note for each.",
            [
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Time": True,
                            "Value": True,
                            "__name__": True,
                            "alertstate": True,
                            "job": True,
                            "instance": True,
                        },
                        "indexByName": {"alertname": 0, "severity": 1, "network": 2},
                    },
                }
            ],
        ),
        table(
            "Client versions",
            [target(f"max by (endpoint, client_version) (bsc_endpoint_info{{{NET}}})", table=True)],
            14,
            y,
            10,
            6,
            "What web3_clientVersion reports, refreshed about once a minute. Useful "
            "around hard forks. A proxy in front of the node may report its own version.",
            [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True}}}],
        ),
    ]
    y += 6

    panels.append(row("Fast is not the same as fresh", y))
    y += 1
    panels += [
        timeseries(
            "Lag behind the highest endpoint",
            [target(f"bsc_endpoint_lag_seconds{{{NET}}}", "{{endpoint}}")],
            0,
            y,
            "How much older each endpoint's head is than the newest head on the network, "
            "from block timestamps. Alert: above 5 s for 2 minutes.",
            unit="s",
            alert_at=5,
        ),
        timeseries(
            "Latency p95",
            [
                target(
                    "histogram_quantile(0.95, sum by (le, endpoint) "
                    f"(rate(bsc_rpc_latency_seconds_bucket{{{NET}}}[$__rate_interval])))",
                    "{{endpoint}}",
                )
            ],
            12,
            y,
            "95th percentile of successful latest-block requests. Compare with the lag "
            "panel: the fastest endpoint is not always the freshest. Alert: above 1 s "
            "for 10 minutes.",
            unit="s",
            alert_at=1,
        ),
    ]
    y += 8
    panels += [
        timeseries(
            "Lag in blocks",
            [target(f"bsc_endpoint_lag_blocks{{{NET}}}", "{{endpoint}}")],
            0,
            y,
            "The same lag counted in blocks. One or two blocks is normal: a block lasts "
            "about as long as some round trips.",
        ),
        timeseries(
            "Time since head last moved",
            [target(f"bsc_endpoint_head_unchanged_seconds{{{NET}}}", "{{endpoint}}")],
            12,
            y,
            "A line that climbs steadily while others stay near zero is a stalled "
            "endpoint. Alert: above 30 s while the network moves.",
            unit="s",
            alert_at=30,
        ),
    ]
    y += 8

    panels.append(row("Errors", y))
    y += 1
    panels += [
        timeseries(
            "Failed requests by kind",
            [
                target(
                    "sum by (outcome) "
                    f'(rate(bsc_rpc_requests_total{{{NET},outcome!="ok"}}[$__rate_interval]))',
                    "{{outcome}}",
                )
            ],
            0,
            y,
            "Each kind points somewhere different: timeout (overload or path), "
            "connection (DNS/TLS/host), http_error (gateway), rate_limited, rpc_error, "
            "bad_response (maintenance page).",
            unit="reqps",
            bars=True,
        ),
        timeseries(
            "Failed requests per endpoint",
            [
                target(
                    "sum by (endpoint) "
                    f'(rate(bsc_rpc_requests_total{{{NET},outcome!="ok"}}[$__rate_interval]))'
                    " / sum by (endpoint) "
                    f"(rate(bsc_rpc_requests_total{{{NET}}}[$__rate_interval]))",
                    "{{endpoint}}",
                )
            ],
            12,
            y,
            "Share of JSON-RPC requests that failed. Alert: above 10% for 5 minutes.",
            unit="percentunit",
            alert_at=0.1,
        ),
    ]
    y += 8

    panels.append(row("Chain", y))
    y += 1
    panels += [
        timeseries(
            "Measured block interval",
            [target(f"bsc_network_block_interval_seconds{{{NET}}}", "block interval")],
            0,
            y,
            "Average time per block between sweeps, from block timestamps.",
            unit="s",
            w=8,
        ),
        timeseries(
            "Time since the network's highest head moved",
            [target(f"bsc_network_head_unchanged_seconds{{{NET}}}", "network head")],
            8,
            y,
            "If this climbs, no endpoint is seeing new blocks: a chain stall, or every "
            "endpoint shares one broken upstream. Alert: above 30 s.",
            unit="s",
            alert_at=30,
            w=8,
        ),
        timeseries(
            "Block hash events",
            [
                target(f"increase(bsc_reorg_blocks_total{{{NET}}}[5m])", "replaced blocks", "A"),
                target(
                    f"increase(bsc_hash_disagreements_total{{{NET}}}[5m])", "disagreements", "B"
                ),
                target(
                    f"sum(increase(bsc_endpoint_head_regressions_total{{{NET}}}[5m]))",
                    "heads going backwards",
                    "C",
                ),
            ],
            16,
            y,
            "Replaced blocks = reorg seen. Disagreements = endpoints named different "
            "blocks for one height. Heads going backwards = usually a load balancer.",
            bars=True,
            w=8,
        ),
    ]

    return {
        "uid": "bsc-pulse",
        "title": "BSC Pulse",
        "description": "Public BSC RPC endpoints compared with each other",
        "tags": ["bsc", "rpc"],
        "timezone": "browser",
        "editable": False,
        "refresh": "10s",
        "schemaVersion": 39,
        "time": {"from": "now-30m", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "network",
                    "label": "Network",
                    "type": "query",
                    "datasource": DATASOURCE,
                    "query": {"query": "label_values(bsc_network_head_block, network)"},
                    "definition": "label_values(bsc_network_head_block, network)",
                    "refresh": 2,
                    "current": {"text": "mainnet", "value": "mainnet"},
                    "includeAll": False,
                    "multi": False,
                    "sort": 1,
                }
            ]
        },
        "panels": panels,
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main(argv: list[str]) -> int:
    if "--check" in argv:
        if not OUTPUT.exists() or OUTPUT.read_text() != render():
            print(f"{OUTPUT} is out of date: run python deploy/grafana/build_dashboard.py")
            return 1
        print("dashboard JSON is up to date")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
