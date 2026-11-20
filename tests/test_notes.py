"""Tests for incident notes: the right causes for the situation, and safe output."""

from bsc_pulse import notes


def alert(name="BscEndpointLagging", status="firing", **labels):
    return {
        "status": status,
        "labels": {
            "alertname": name,
            "severity": "warning",
            "network": "mainnet",
            "endpoint": "bsc-dataseed.defibit.io",
            **labels,
        },
        "annotations": {"summary": "bsc-dataseed.defibit.io (mainnet) is 6.3s behind"},
        "startsAt": "2026-11-20T12:01:00Z",
        "endsAt": "2026-11-20T12:08:30Z",
        "generatorURL": "http://127.0.0.1:9090/graph?g0.expr=x",
    }


def context(**endpoint):
    return {
        "endpoint": {
            "url": "https://bsc-dataseed.defibit.io",
            "lag_blocks": 14,
            "lag_s": 6.3,
            "latency_p95_s": 0.04,
            **endpoint,
        },
        "network": {"head_block": 133785880, "usable": 7, "endpoints": 7, "block_interval_s": 0.45},
    }


def test_lagging_but_fast_points_at_the_node_not_the_network():
    note = notes.build_note(alert(), context(latency_p95_s=0.04))
    assert note.startswith("<b>FIRING</b> · warning · BscEndpointLagging\n")
    assert note.splitlines()[1] == "bsc-dataseed.defibit.io · mainnet"
    assert "lag: 14 blocks (6.3 s)" in note
    assert "latency p95 (5m): 40 ms" in note
    assert "network head: 133,785,880, 7/7 endpoints usable, block interval 450 ms" in note
    assert "answers quickly but serves old data" in note
    assert "slow and behind" not in note


def test_lagging_and_slow_points_at_overload_or_the_path():
    note = notes.build_note(alert(), context(latency_p95_s=1.2))
    assert "slow and behind at the same time" in note
    assert "answers quickly" not in note


def test_head_going_backwards_suggests_a_load_balancer():
    note = notes.build_note(alert(), context(regressions_15m=4.0))
    assert "head went backwards (15m): 4 times" in note
    assert "load balancer" in note


def test_next_checks_include_a_ready_to_run_command_for_this_endpoint():
    note = notes.build_note(alert(), context())
    assert '"method":"eth_getBlockByNumber"' in note.replace("&quot;", '"')
    assert "https://bsc-dataseed.defibit.io | jq -r .result.number" in note
    assert "{url}" not in note


def test_endpoint_down_cause_follows_the_dominant_error_kind():
    timeouts = notes.build_note(
        alert("BscEndpointDown"), context(up=0, errors_5m={"timeout": 30.0, "http_error": 2.0})
    )
    assert "status: <b>down</b>" in timeouts
    assert "Requests time out" in timeouts

    limited = notes.build_note(alert("BscEndpointDown"), context(errors_5m={"rate_limited": 12.0}))
    assert "rate-limiting us" in limited
    assert "Requests time out" not in limited


def test_dig_uses_the_bare_hostname():
    ctx = context(url="https://bsc-dataseed.defibit.io:8545/path")
    note = notes.build_note(alert("BscEndpointDown"), ctx)
    assert "<code>dig +short bsc-dataseed.defibit.io</code>" in note


def test_lagging_endpoint_whose_head_is_frozen_is_called_stuck():
    note = notes.build_note(alert(), context(head_unchanged_s=95.0))
    assert "it is stuck, not just slow" in note


def test_wrong_chain_explains_itself_in_endpoint_down():
    note = notes.build_note(alert("BscEndpointDown"), context(wrong_chain=1))
    assert "different chain" in note


def test_stalled_endpoint_mentions_hard_forks_and_its_version():
    note = notes.build_note(
        alert("BscEndpointStalled", severity="critical"),
        context(client_version="Geth/v1.7.6/linux-amd64", head_unchanged_s=95.0),
    )
    assert "head unchanged for: 95.0 s" in note
    assert "hard fork" in note
    assert "<code>Geth/v1.7.6/linux-amd64</code>" in note


def test_chain_stall_mentions_shared_upstream_and_missing_endpoints():
    ctx = context()
    ctx["network"]["usable"] = 2
    ctx["network"]["endpoints"] = 3
    note = notes.build_note(alert("BscChainStalled", endpoint=""), ctx)
    assert "chain itself may have halted" in note
    assert "bnbchain.org" in note
    assert "Only 2 of 3 endpoints are usable" in note


def test_without_context_the_note_still_has_causes_and_checks():
    note = notes.build_note(alert(), None)
    assert "Live numbers unavailable" in note
    assert "<b>Likely causes</b>" in note
    assert "<b>Next checks</b>" in note
    # Falls back to an https URL built from the endpoint label
    assert "https://bsc-dataseed.defibit.io" in note


def test_resolved_note_is_short_and_says_how_long_it_lasted():
    note = notes.build_note(alert(status="resolved"), None)
    assert note.startswith("<b>RESOLVED</b> · BscEndpointLagging · lasted 7m")
    assert "Likely causes" not in note


def test_unknown_alert_uses_the_default_play():
    note = notes.build_note(alert("SomethingNew"), context())
    assert "No specific playbook" in note


def test_every_rule_in_the_rules_file_has_a_playbook_entry():
    import pathlib

    import yaml

    rules_file = pathlib.Path(__file__).parent.parent / "deploy/prometheus/rules/bsc.yml"
    names = {
        rule["alert"]
        for group in yaml.safe_load(rules_file.read_text())["groups"]
        for rule in group["rules"]
    }
    assert names - set(notes.PLAYBOOK) == set()


def test_outside_values_are_escaped():
    bad = alert(endpoint="<script>x</script>")
    bad["annotations"]["summary"] = "a < b & c"
    note = notes.build_note(bad, context(client_version="<b>v</b>"))
    assert "<script>" not in note
    assert "&lt;script&gt;" in note
    assert "a &lt; b &amp; c" in note
    assert "<code>&lt;b&gt;v&lt;/b&gt;</code>" in note


def test_a_note_too_long_for_telegram_falls_back_to_plain_text():
    huge = alert()
    huge["annotations"]["summary"] = "x" * 5000
    note = notes.build_note(huge, context())
    assert len(note) <= notes.MAX_LENGTH
    assert note.startswith("FIRING: BscEndpointLagging")


def test_plain_text_has_the_essentials():
    text = notes.plain_text(alert())
    assert text.splitlines()[0] == "FIRING: BscEndpointLagging (warning)"
    assert "6.3s behind" in text
    assert "endpoint=bsc-dataseed.defibit.io" in text
