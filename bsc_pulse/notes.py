"""Turn a firing alert plus recent metrics into a short incident note.

This is a runbook written as code. Each alert has:

- likely causes, chosen by looking at the live numbers. For example, a lagging
  endpoint that answers quickly points somewhere different from one that is
  both slow and lagging.
- next checks: concrete things to run or look at, with the endpoint URL filled in.

It is deliberately rule-based: the same input always gives the same note, every
sentence can be traced to a line here, and it cannot invent facts. The notifier
falls back to a plain message if anything in this module fails.

Output uses Telegram's HTML subset (<b>, <i>, <code>, <a>), and every value that
comes from outside (labels, annotations) is escaped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlparse

# Telegram rejects messages over 4096 characters. Keep a margin.
MAX_LENGTH = 4000

# A latest-block request this fast means the endpoint itself is responsive.
FAST_LATENCY_S = 0.3

# {url} is filled in per alert (with str.replace, so the JSON braces stay as they are).
CURL_HEAD = (
    "curl -s -H 'Content-Type: application/json' --data "
    """'{"jsonrpc":"2.0","id":1,"method":"eth_getBlockByNumber","params":["latest",false]}' """
    "{url} | jq -r .result.number"
)
CURL_CHAIN_ID = (
    "curl -s -H 'Content-Type: application/json' --data "
    """'{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' {url}"""
)
RELEASES = "https://github.com/bnb-chain/bsc/releases"

ERROR_CAUSES = {
    "timeout": "Requests time out: the endpoint is overloaded, or the network path to it "
    "is dropping packets.",
    "connection": "Connections fail before any HTTP answer (DNS, TCP or TLS): the host may "
    "be down, or its DNS or certificate changed.",
    "http_error": "It answers with HTTP errors, often 502/503 from a gateway whose backend "
    "nodes are unhealthy.",
    "rate_limited": "It is rate-limiting us (HTTP 429 or error -32005). Our load is about "
    "180 requests per 5 minutes; the documented limit is 10K. The limit may be per IP and "
    "shared with other traffic, or lower than documented.",
    "rpc_error": "The node returns JSON-RPC errors for basic calls: it may be starting up "
    "or misconfigured.",
    "bad_response": "The answers are not JSON-RPC at all: often a maintenance page or a "
    "proxy error page.",
}


# ---- small formatting helpers ----


def _seconds(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.1f} s"


def _num(value: float | None) -> str:
    return "?" if value is None else f"{value:,.0f}"


def _code(text: str) -> str:
    return f"<code>{escape(text)}</code>"


def _list(items: list[str], numbered: bool = True) -> str:
    return "\n".join(f"{i}. {item}" if numbered else f"• {item}" for i, item in enumerate(items, 1))


def _dominant_error(ep: dict) -> str | None:
    errors = ep.get("errors_5m") or {}
    return max(errors, key=errors.get) if errors else None


# ---- the playbook ----


@dataclass(frozen=True)
class Play:
    causes: Callable[[dict, dict], list[str]]  # (endpoint context, network context) -> causes
    checks: list[str]  # may contain {url}, {host}, {network}; filled in later


def _lagging_causes(ep: dict, net: dict) -> list[str]:
    causes = []
    unchanged = ep.get("head_unchanged_s")
    if unchanged is not None and unchanged > 30:
        causes.append(
            f"Its head has not moved for {_seconds(unchanged)}: it is stuck, not just slow."
        )
    p95 = ep.get("latency_p95_s")
    if p95 is not None and p95 < FAST_LATENCY_S:
        causes.append(
            "It answers quickly but serves old data: the node behind it imports blocks "
            "slower than the chain produces them (disk I/O, CPU) or has lost peers."
        )
    elif p95 is not None:
        causes.append(
            "It is slow and behind at the same time: likely an overloaded node, or a "
            "congested path between this server and the endpoint."
        )
    if ep.get("regressions_15m"):
        causes.append(
            "Its head also went backwards recently: probably a load balancer sending "
            "requests to nodes at different heights."
        )
    causes.append(
        "The provider may route this URL to a node that is catching up after a restart "
        "or an upgrade."
    )
    return causes


def _stalled_causes(ep: dict, net: dict) -> list[str]:
    causes = []
    version = ep.get("client_version")
    hint = f" It reports {_code(version)}." if version else ""
    causes.append(
        "A node that was not upgraded before a hard fork stops following the chain at "
        f"the fork block. Compare its version with the latest bsc release.{hint}"
    )
    causes.append("Block import has stopped: crashed sync, full disk or a corrupted database.")
    causes.append("A cache or proxy in front of the node keeps returning a stored answer.")
    if ep.get("regressions_15m"):
        causes.append("Its head also went backwards recently: a load balancer may be involved.")
    return causes


def _chain_stalled_causes(ep: dict, net: dict) -> list[str]:
    causes = [
        "If every endpoint stopped at the same height, the chain itself may have halted "
        "(validator or consensus problem, or a hard fork going wrong).",
        "All watched endpoints may share one upstream. On Chapel all three are on "
        "bnbchain.org, so one provider problem looks like a chain stall.",
    ]
    if net.get("usable") is not None and net.get("endpoints") and net["usable"] < net["endpoints"]:
        causes.append(
            f"Only {_num(net['usable'])} of {_num(net['endpoints'])} endpoints are usable, "
            "so part of the picture is missing."
        )
    return causes


def _down_causes(ep: dict, net: dict) -> list[str]:
    if ep.get("wrong_chain") == 1:
        return ["It answers, but eth_chainId is for a different chain."]
    kind = _dominant_error(ep)
    if kind in ERROR_CAUSES:
        return [ERROR_CAUSES[kind]]
    return [
        "The endpoint stopped returning valid blocks. Check the error panels to see which "
        "kind of failure dominates."
    ]


def _error_rate_causes(ep: dict, net: dict) -> list[str]:
    kind = _dominant_error(ep)
    first = ERROR_CAUSES.get(kind, "Requests fail intermittently.")
    return [first, "A partial outage: some backend nodes behind the URL fail, others answer."]


PLAYBOOK: dict[str, Play] = {
    "BscEndpointLagging": Play(
        causes=_lagging_causes,
        checks=[
            "Ask it directly, then ask another endpoint, and compare: " + _code(CURL_HEAD),
            "On the dashboard, see whether the lag grows (falling behind) or holds steady "
            "(a constant offset, often a caching layer).",
            f"Compare its client version with the latest release: {RELEASES}",
        ],
    ),
    "BscEndpointStalled": Play(
        causes=_stalled_causes,
        checks=[
            "Run this twice, a few seconds apart. Does the number change? " + _code(CURL_HEAD),
            f"Check for a recent or upcoming hard fork and the required version: {RELEASES}",
            "Look for an announcement from the endpoint's operator.",
        ],
    ),
    "BscChainStalled": Play(
        causes=_chain_stalled_causes,
        checks=[
            "Check the latest block on a block explorer and on a provider we do not watch.",
            "Check BNB Chain's official channels for an incident notice.",
            "If only our endpoints are stuck, add an independent endpoint to the config.",
        ],
    ),
    "BscEndpointDown": Play(
        causes=_down_causes,
        checks=[
            "See where it fails (DNS, TLS, HTTP status): " + _code("curl -sv {url}"),
            "Check that the name still resolves: " + _code("dig +short {host}"),
            "Try from another machine to rule out this server's network.",
        ],
    ),
    "BscEndpointWrongChain": Play(
        causes=lambda ep, net: [
            "DNS or a load balancer now points this URL at a node for another network.",
            "The node behind it was started with the wrong network configuration.",
        ],
        checks=[
            "Confirm the chain ID it returns (56 mainnet, 97 Chapel): " + _code(CURL_CHAIN_ID),
            "Compare its head block with the other endpoints on {network}.",
        ],
    ),
    "BscEndpointSyncing": Play(
        causes=lambda ep, net: [
            "The node restarted and is catching up, or it is a freshly started node.",
            "It fell far behind and switched back to sync mode.",
        ],
        checks=[
            "Look at the progress eth_syncing returns (currentBlock vs highestBlock).",
            "Watch its lag on the dashboard: it should shrink steadily.",
        ],
    ),
    "BscEndpointErrorRate": Play(
        causes=_error_rate_causes,
        checks=[
            "Open the dashboard's error panel to see which methods and error kinds fail.",
            "Reproduce by hand: " + _code(CURL_HEAD),
        ],
    ),
    "BscEndpointRateLimited": Play(
        causes=lambda ep, net: [ERROR_CAUSES["rate_limited"]],
        checks=[
            "Check BSC_PULSE_INTERVAL and how many endpoints share this provider.",
            "Check whether anything else on this server calls the same endpoint.",
        ],
    ),
    "BscEndpointSlow": Play(
        causes=lambda ep, net: [
            "The endpoint is far from this server (check latency from another region).",
            "The endpoint is overloaded; other users of the same public endpoint add load.",
        ],
        checks=[
            "Time a request by hand: " + _code("time " + CURL_HEAD),
            "Compare its p95 with the other endpoints on {network} on the dashboard.",
        ],
    ),
    "BscEndpointHeadFlapping": Play(
        causes=lambda ep, net: [
            "A load balancer is spreading requests across nodes at different heights.",
        ],
        checks=[
            "Send ten requests in a row and look at the block numbers: "
            + _code("for i in $(seq 10); do " + CURL_HEAD + "; done"),
        ],
    ),
    "BscHashDisagreement": Play(
        causes=lambda ep, net: [
            "Competing blocks at the tip. With fast finality this should settle within a "
            "couple of blocks.",
            "One endpoint follows a minority fork, for example a node not upgraded for a "
            "hard fork.",
        ],
        checks=[
            "Ask each endpoint for the same recent block number and compare the hashes.",
            "Compare the 'finalized' block across endpoints: those must agree.",
        ],
    ),
    "BscReorgObserved": Play(
        causes=lambda ep, net: [
            "A block near the head was replaced by a competing block.",
            "An endpoint briefly served a block from a minority fork, then switched back.",
        ],
        checks=[
            "Compare the 'finalized' block across endpoints: a reorg below it would be serious.",
            "Check whether BscHashDisagreement fired at the same time.",
        ],
    ),
    "BscTooFewEndpoints": Play(
        causes=lambda ep, net: [
            "Several endpoints are down at once, so lag can no longer be measured by comparison.",
        ],
        checks=[
            "See which endpoints are down on the dashboard and check their own alerts.",
            "Consider adding more endpoints from the official list to config/endpoints.yaml.",
        ],
    ),
    "BscPulseDown": Play(
        causes=lambda ep, net: [
            "The watchdog container crashed or is restarting.",
            "The server is out of memory or disk.",
        ],
        checks=[
            _code("docker compose ps") + " and " + _code("docker compose logs bsc-pulse"),
            _code("df -h") + " and " + _code("free -m") + " on the server.",
        ],
    ),
    "BscPulseSweepsStale": Play(
        causes=lambda ep, net: [
            "The sweep loop is stuck, for example waiting on a request that never ends.",
        ],
        checks=[_code("docker compose logs --tail 50 bsc-pulse")],
    ),
}

DEFAULT_PLAY = Play(
    causes=lambda ep, net: ["No specific playbook for this alert yet."],
    checks=["Open the dashboard for this endpoint and network."],
)


# ---- building the message ----


def _facts(ep: dict, net: dict) -> list[str]:
    """The live numbers worth showing, skipping anything unknown."""
    facts = []
    if ep:
        if ep.get("up") == 0:
            facts.append("status: <b>down</b>")
        if ep.get("lag_blocks") is not None:
            facts.append(f"lag: {_num(ep['lag_blocks'])} blocks ({_seconds(ep.get('lag_s'))})")
        if ep.get("head_unchanged_s") is not None:
            facts.append(f"head unchanged for: {_seconds(ep['head_unchanged_s'])}")
        if ep.get("latency_p95_s") is not None:
            facts.append(f"latency p95 (5m): {_seconds(ep['latency_p95_s'])}")
        if ep.get("error_ratio") is not None:
            facts.append(f"failed requests (5m): {ep['error_ratio']:.0%}")
        if ep.get("errors_5m"):
            kinds = ", ".join(f"{escape(k)} {v:.0f}" for k, v in ep["errors_5m"].items())
            facts.append(f"errors (5m): {kinds}")
        if ep.get("regressions_15m"):
            facts.append(f"head went backwards (15m): {ep['regressions_15m']:.0f} times")
        if ep.get("client_version"):
            facts.append(f"client: {_code(ep['client_version'])}")
    if net:
        line = f"network head: {_num(net.get('head_block'))}"
        if net.get("usable") is not None:
            line += f", {_num(net['usable'])}/{_num(net.get('endpoints'))} endpoints usable"
        if net.get("block_interval_s") is not None:
            line += f", block interval {_seconds(net['block_interval_s'])}"
        facts.append(line)
    return facts


def _when(timestamp: str | None) -> datetime | None:
    if not timestamp or timestamp.startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(start: datetime | None, end: datetime | None) -> str | None:
    if start is None or end is None:
        return None
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


def build_note(alert: dict, context: dict | None) -> str:
    """The full incident note for one alert from Alertmanager's webhook payload."""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    name = labels.get("alertname", "unknown alert")
    status = alert.get("status", "firing")
    summary = escape(annotations.get("summary", ""))
    started = _when(alert.get("startsAt"))

    endpoint = labels.get("endpoint", "")
    network = labels.get("network", "")
    where = " · ".join(escape(x) for x in (endpoint, network) if x)

    if status == "resolved":
        lasted = _duration(started, _when(alert.get("endsAt")))
        header = f"<b>RESOLVED</b> · {escape(name)}" + (f" · lasted {lasted}" if lasted else "")
        lines = [header, where, f"was: {summary}" if summary else ""]
        return "\n".join(line for line in lines if line)

    ep = (context or {}).get("endpoint") or {}
    net = (context or {}).get("network") or {}
    url = ep.get("url") or (f"https://{endpoint}" if endpoint else "<endpoint url>")
    play = PLAYBOOK.get(name, DEFAULT_PLAY)

    parts = [
        f"<b>FIRING</b> · {escape(labels.get('severity', '?'))} · {escape(name)}"
        + (f"\n{where}" if where else ""),
        summary,
    ]
    if context is None:
        parts.append("<i>Live numbers unavailable (Prometheus did not answer).</i>")
    else:
        facts = _facts(ep, net)
        if facts:
            parts.append("<b>Now</b>\n" + _list(facts, numbered=False))
    parts.append("<b>Likely causes</b>\n" + _list(play.causes(ep, net)))
    host = urlparse(url).hostname or endpoint
    checks = [
        c.replace("{url}", escape(url))
        .replace("{host}", escape(host))
        .replace("{network}", escape(network))
        for c in play.checks
    ]
    parts.append("<b>Next checks</b>\n" + _list(checks))

    footer = []
    if started:
        footer.append(f"since {started.astimezone(timezone.utc):%H:%M} UTC")
    if alert.get("generatorURL"):
        footer.append(f'<a href="{escape(alert["generatorURL"], quote=True)}">graph</a>')
    if footer:
        parts.append(" · ".join(footer))

    note = "\n\n".join(p for p in parts if p)
    # Cutting HTML in the middle could leave a tag open, which Telegram rejects.
    return note if len(note) <= MAX_LENGTH else plain_text(alert)


def plain_text(alert: dict) -> str:
    """The fallback: what Alertmanager knows, with no context and no playbook."""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    label_text = ", ".join(
        f"{k}={v}" for k, v in sorted(labels.items()) if k not in ("alertname", "severity")
    )
    lines = [
        f"{alert.get('status', 'firing').upper()}: {labels.get('alertname', 'unknown alert')} "
        f"({labels.get('severity', '?')})",
        annotations.get("summary", ""),
        label_text,
    ]
    return escape("\n".join(line for line in lines if line))[:MAX_LENGTH]
