"""Tests for the Grafana dashboard: up to date, well laid out, and only real metrics."""

import importlib.util
import json
import re
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest

from bsc_pulse.compare import Tracker
from bsc_pulse.metrics import Exporter
from tests.test_compare import T0, up

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_dashboard", ROOT / "deploy/grafana/build_dashboard.py"
)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

DASHBOARD = builder.build()
PANELS = DASHBOARD["panels"]


def all_exprs():
    return [t["expr"] for p in PANELS for t in p.get("targets", [])]


def test_committed_json_matches_the_builder():
    # If this fails: python deploy/grafana/build_dashboard.py
    assert json.loads(builder.OUTPUT.read_text()) == DASHBOARD


def test_panel_ids_are_unique():
    ids = [p["id"] for p in PANELS]
    assert len(ids) == len(set(ids))


def test_panels_fit_the_grid_without_overlapping():
    taken = set()
    for p in PANELS:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24, p["title"]
        cells = {
            (x, y) for x in range(g["x"], g["x"] + g["w"]) for y in range(g["y"], g["y"] + g["h"])
        }
        assert not cells & taken, f"{p['title']!r} overlaps another panel"
        taken |= cells


def test_every_query_uses_the_prometheus_datasource():
    for p in PANELS:
        for t in p.get("targets", []):
            assert t["datasource"]["uid"] == "prometheus"


def test_every_data_panel_explains_itself():
    for p in PANELS:
        if p["type"] not in ("row", "text"):
            assert len(p.get("description", "")) > 20, p["title"]


def test_every_metric_in_a_query_is_exported():
    # Collect the metric names the exporter really produces. Two sweeps, because the
    # block interval needs two heads to be measured.
    tracker, registry = Tracker(), CollectorRegistry()
    exporter = Exporter([up("a", 100).endpoint], registry)
    for i, number in enumerate((100, 111)):
        results = [up("a", number)]
        exporter.record(results, tracker.update(results, now=T0 + 5 * i), 0.1)
    exported = set(re.findall(r"^(bsc_\w+)", generate_latest(registry).decode(), re.M))

    used = {name for expr in all_exprs() for name in re.findall(r"\bbsc_\w+", expr)}
    assert used, "no bsc_ metrics found in the dashboard"
    assert used - exported == set()


def test_network_queries_are_filtered_by_the_variable():
    per_network = [e for e in all_exprs() if "bsc_" in e]
    assert all('network="$network"' in e for e in per_network)
