"""The docs must not drift from the code: commands and files they mention must exist."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
MAKE_TARGETS = set(re.findall(r"^([a-z][a-z0-9-]*):", (ROOT / "Makefile").read_text(), re.M))
FILE_PATTERN = re.compile(r"`([\w./-]+\.(?:py|yml|yaml|json|tf|md|j2|txt|toml))`")
REPO_FILES = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_make_target_mentioned_exists(doc):
    mentioned = set(re.findall(r"\bmake ([a-z][a-z0-9-]*)", doc.read_text()))
    assert mentioned - MAKE_TARGETS == set()


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_file_mentioned_exists(doc):
    missing = []
    for name in FILE_PATTERN.findall(doc.read_text()):
        if "/" in name:
            if not (ROOT / name).exists() and not (doc.parent / name).exists():
                missing.append(name)
        elif not any(p.name == name for p in REPO_FILES):
            missing.append(name)
    assert missing == []


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(doc):
    links = re.findall(r"\]\((?!https?://|#)([^)#]+)", doc.read_text())
    assert [link for link in links if not (doc.parent / link).exists()] == []


def test_every_alert_rule_is_documented_in_the_readme():
    import yaml

    rules = yaml.safe_load((ROOT / "deploy/prometheus/rules/bsc.yml").read_text())
    names = {r["alert"] for g in rules["groups"] for r in g["rules"]}
    readme = (ROOT / "README.md").read_text()
    assert {n for n in names if f"`{n}`" not in readme} == set()
