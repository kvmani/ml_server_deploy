"""Tests for tools/manifest.py.

These run on the Windows development machine so that a malformed manifest is
caught before it is pushed, long before a release build would fail on it.
Every test here is a mistake that is genuinely easy to make while editing pins
by hand.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import manifest as manifest_tool  # noqa: E402


@pytest.fixture(scope="module")
def authored() -> dict:
    """The real manifest.yml, so these tests also guard the shipped file."""
    with (REPO_ROOT / "manifest.yml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_shipped_manifest_is_valid(authored: dict) -> None:
    assert manifest_tool.validate(authored) == []


def test_shipped_manifest_has_no_commits_yet(authored: dict) -> None:
    # The authored file must not carry SHAs; the workflow fills them in. A SHA
    # committed here by hand would quietly pin a release to a stale tree.
    for name, service in authored["services"].items():
        assert not service.get("commit"), f"{name} should have an empty commit in the authored manifest"


def test_resolved_manifest_requires_full_shas(authored: dict) -> None:
    problems = manifest_tool.validate(authored, require_commits=True)
    assert problems, "an unresolved manifest must fail the resolved-manifest check"
    assert all("commit must be a full 40-character SHA" in p for p in problems)


def _mutate(base: dict, path: str, value: object) -> dict:
    document = copy.deepcopy(base)
    node = document
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    if value is manifest_tool:  # sentinel meaning "delete"
        del node[parts[-1]]
    else:
        node[parts[-1]] = value
    return document


DELETE = manifest_tool


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        ("schema", "ml-suite.manifest.v99", "schema must be"),
        ("suite_version", "1.0", "not a semantic version"),
        ("suite_version", "v1.0.0", "not a semantic version"),
        ("platform.os_id", "debian", "os_id must be 'ubuntu'"),
        ("runtime.user", "", "runtime.user is required"),
        ("runtime.systemd_scope", "sysv", "systemd_scope must be one of"),
        ("services.gateway.repo", "not-a-repo", "is not owner/name"),
        ("services.gateway.commit", "abc123", "not a full 40-character SHA"),
        ("services.gateway.env", "venv", "env must be one of"),
        ("services.gateway.health", "health/live", "must be a path beginning with /"),
        ("services.pytex.port", 80, "outside 1024-65535"),
        ("services.pytex.start", "", "start is required"),
    ],
)
def test_rejects_bad_field(authored: dict, path: str, value: object, expected: str) -> None:
    problems = manifest_tool.validate(_mutate(authored, path, value))
    assert any(expected in problem for problem in problems), f"expected {expected!r} in {problems}"


def test_rejects_duplicate_port(authored: dict) -> None:
    # The single most likely hand-edit mistake: copying a service block and
    # forgetting to change the port. Two services would then fight over it and
    # one would fail to start, minutes after a "successful" deployment.
    document = _mutate(authored, "services.pytex.port", authored["services"]["calculator"]["port"])
    problems = manifest_tool.validate(document)
    assert any("collides with" in problem for problem in problems)


def test_rejects_duplicate_unit(authored: dict) -> None:
    document = _mutate(authored, "services.pytex.unit", authored["services"]["calculator"]["unit"])
    problems = manifest_tool.validate(document)
    assert any("collides with" in problem for problem in problems)


def test_rejects_in_process_service_claiming_a_port(authored: dict) -> None:
    document = _mutate(authored, "services.pdf_tools.port", 5045)
    problems = manifest_tool.validate(document)
    assert any("in_process and must not declare a port" in problem for problem in problems)


def test_requires_a_gateway(authored: dict) -> None:
    document = copy.deepcopy(authored)
    del document["services"]["gateway"]
    problems = manifest_tool.validate(document)
    assert any("'gateway' service is required" in problem for problem in problems)


def test_standalone_service_needs_a_unit(authored: dict) -> None:
    document = copy.deepcopy(authored)
    del document["services"]["pytex"]["unit"]
    problems = manifest_tool.validate(document)
    assert any("unit is required" in problem for problem in problems)


def test_json_output_preserves_service_order(authored: dict, tmp_path: Path) -> None:
    # Manifest order is start order. If the JSON emitter sorted keys, the
    # gateway would start after the calculator and the catalog would come up
    # against services that are not listening yet.
    out = tmp_path / "resolved.json"
    rc = manifest_tool.main([str(REPO_ROOT / "manifest.yml"), "--json", "--out", str(out)])
    assert rc == 0
    emitted = json.loads(out.read_text(encoding="utf-8"))
    assert list(emitted["services"]) == list(authored["services"])
    assert list(emitted["services"])[0] == "gateway"


def test_resolve_ref_passes_through_a_sha() -> None:
    sha = "a" * 40
    assert manifest_tool.resolve_ref("owner/repo", sha, None) == sha


def test_hydride_models_are_never_expected_in_the_archive(authored: dict) -> None:
    # The checkpoints are not in git, so they must be declared as shared state.
    hydride = authored["services"]["hydride"]
    assert hydride.get("requires_models") is True
    assert "frozen_checkpoints" in hydride.get("shared_links", {})
    assert hydride["shared_links"]["frozen_checkpoints"].startswith("shared/")
