"""Load the list of RPC endpoints to watch from config/endpoints.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "endpoints.yaml"


class ConfigError(ValueError):
    """The endpoints file is missing something or contains a mistake."""


@dataclass(frozen=True)
class Endpoint:
    network: str  # "mainnet" or "chapel"
    chain_id: int  # the chain ID this endpoint is supposed to serve
    url: str

    @property
    def name(self) -> str:
        """Short label for tables and metric labels: host, plus port if the URL has one."""
        return urlparse(self.url).netloc or self.url


def parse_endpoints(raw: object) -> list[Endpoint]:
    """Turn the parsed YAML into Endpoint objects, failing loudly on mistakes.

    A typo in this file should stop the program at startup with a clear message,
    not show up later as an endpoint that silently never gets probed.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("networks"), dict):
        raise ConfigError("config must have a top-level 'networks' mapping")

    endpoints: list[Endpoint] = []
    seen_urls: set[str] = set()
    for network, settings in raw["networks"].items():
        if not isinstance(settings, dict):
            raise ConfigError(f"network '{network}' must be a mapping")
        chain_id = settings.get("chain_id")
        if not isinstance(chain_id, int):
            raise ConfigError(f"network '{network}' needs an integer chain_id")
        urls = settings.get("endpoints")
        if not isinstance(urls, list) or not urls:
            raise ConfigError(f"network '{network}' needs a non-empty endpoints list")
        for url in urls:
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise ConfigError(f"network '{network}': not an http(s) URL: {url!r}")
            if url in seen_urls:
                raise ConfigError(f"endpoint listed twice: {url}")
            seen_urls.add(url)
            endpoints.append(Endpoint(network=network, chain_id=chain_id, url=url))
    return endpoints


def load_endpoints(path: str | Path | None = None) -> list[Endpoint]:
    """Read endpoints from `path`, or $BSC_PULSE_CONFIG, or config/endpoints.yaml.

    Several files can be given separated by commas, e.g. the real networks plus the
    demo network: "config/endpoints.yaml,config/endpoints.demo.yaml". A network may
    appear in only one of them.
    """
    spec = str(path or os.environ.get("BSC_PULSE_CONFIG") or DEFAULT_CONFIG_PATH)
    networks: dict = {}
    for part in spec.split(","):
        with open(part.strip()) as handle:
            raw = yaml.safe_load(handle)
        if not isinstance(raw, dict) or not isinstance(raw.get("networks"), dict):
            raise ConfigError(f"{part}: config must have a top-level 'networks' mapping")
        for name, settings in raw["networks"].items():
            if name in networks:
                raise ConfigError(f"{part}: network '{name}' is defined in more than one file")
            networks[name] = settings
    return parse_endpoints({"networks": networks})
