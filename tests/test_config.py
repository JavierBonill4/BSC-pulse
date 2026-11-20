"""Tests for loading the endpoint list."""

import pytest

from bsc_pulse.config import ConfigError, load_endpoints, parse_endpoints

GOOD = {
    "networks": {
        "mainnet": {"chain_id": 56, "endpoints": ["https://a.example", "https://b.example"]},
        "chapel": {"chain_id": 97, "endpoints": ["https://c.example"]},
    }
}


def test_parse_endpoints_keeps_order_and_network():
    endpoints = parse_endpoints(GOOD)
    assert [e.name for e in endpoints] == ["a.example", "b.example", "c.example"]
    assert [e.chain_id for e in endpoints] == [56, 56, 97]
    assert endpoints[2].network == "chapel"


def test_name_keeps_the_port_so_local_endpoints_stay_distinct():
    raw = {"networks": {"local": {"chain_id": 56, "endpoints": ["http://127.0.0.1:8545"]}}}
    assert parse_endpoints(raw)[0].name == "127.0.0.1:8545"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"networks": {"mainnet": {"endpoints": ["https://a.example"]}}},  # no chain_id
        {"networks": {"mainnet": {"chain_id": "56", "endpoints": ["https://a.example"]}}},
        {"networks": {"mainnet": {"chain_id": 56, "endpoints": []}}},
        {"networks": {"mainnet": {"chain_id": 56, "endpoints": ["a.example"]}}},  # not a URL
        {"networks": {"mainnet": {"chain_id": 56, "endpoints": ["https://a", "https://a"]}}},
    ],
)
def test_parse_endpoints_rejects_mistakes(bad):
    with pytest.raises(ConfigError):
        parse_endpoints(bad)


def test_the_shipped_config_is_valid():
    endpoints = load_endpoints()
    by_network = {e.network: e.chain_id for e in endpoints}
    assert by_network == {"mainnet": 56, "chapel": 97}
    assert all(e.url.startswith("https://") for e in endpoints)
    # Comparing endpoints only works with at least two per network.
    for network in by_network:
        assert sum(e.network == network for e in endpoints) >= 2
