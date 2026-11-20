"""Security checks on docker-compose.yml: nothing is published on a public interface."""

from pathlib import Path

import yaml

COMPOSE = yaml.safe_load((Path(__file__).parent.parent / "docker-compose.yml").read_text())


def test_every_published_port_is_bound_to_localhost():
    # On the server the firewall only allows SSH, but this is the second line of
    # defence: even with the firewall off, nothing listens on a public address.
    for name, service in COMPOSE["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} publicly"


def test_no_image_uses_latest():
    for name, service in COMPOSE["services"].items():
        image = service.get("image", "")
        assert ":" in image and not image.endswith(":latest"), f"{name}: pin {image!r}"


def test_grafana_has_no_default_password():
    env = COMPOSE["services"]["grafana"]["environment"]
    assert env["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD:?")
