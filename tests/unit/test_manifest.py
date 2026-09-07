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


# ---------------------------------------------------------------------------
# Seeded persistent state, the intranet address, and the post-deployment
# assertions.
#
# Every test below corresponds to a way the v1.4.0 deployment came up broken
# while reporting success. A manifest that cannot express these, or expresses
# them wrongly, puts the deployment straight back where it was.
# ---------------------------------------------------------------------------


def _seeds(document: dict) -> dict[str, dict]:
    return {entry["id"]: entry for entry in document.get("seeds") or []}


def test_shipped_manifest_seeds_everything_that_broke(authored: dict) -> None:
    seeds = _seeds(authored)
    assert "portal-env" in seeds, "the portal's environment file must be seeded"
    assert "server-config" in seeds, "config.intranet.json must be seeded"
    assert "hydride-models" in seeds, "the checkpoints must be seeded"
    assert "hydride-test-library" in seeds, "the sample micrographs must be seeded"


def test_every_seed_target_is_persistent(authored: dict) -> None:
    # A target inside a release directory is replaced by the next upgrade, so
    # seeding one would quietly lose the data on the following deployment.
    for seed_id, seed in _seeds(authored).items():
        assert seed["target"].startswith("shared/"), f"{seed_id} would not survive an upgrade"


def test_the_environment_file_can_always_be_produced(authored: dict) -> None:
    # A fresh host has no legacy install to copy one from, and the portal
    # cannot render a usable link without it, so it must be generatable.
    assert _seeds(authored)["portal-env"]["generate"] == "service_urls"


def test_the_gateway_loads_the_seeded_environment_file(authored: dict) -> None:
    # The whole of root cause 1: the unit did not load this file, so systemd
    # started the portal with none of the URL variables set.
    gateway = authored["services"]["gateway"]
    assert gateway.get("env_file") == authored["runtime"]["env_file"]
    assert gateway["env_file"] == _seeds(authored)["portal-env"]["target"]


def test_every_routed_service_supplies_a_url_variable(authored: dict) -> None:
    # These four are what the portal reads to build the catalog. A service with
    # a unit and no variable is one the portal will link to on loopback.
    variables = {
        name: service.get("public_url_env")
        for name, service in authored["services"].items()
        if service.get("unit") and name != "gateway"
    }
    assert variables == {
        "pytex": "PYTEX_URL",
        "calculator": "SCIENTIFIC_CALCULATOR_URL",
        "converter": "UNIT_CONVERTER_URL",
        "hydride": "HYDRIDE_SEGMENTATION_URL",
    }


def test_the_hydride_test_library_is_linked_to_shared_state(authored: dict) -> None:
    links = authored["services"]["hydride"]["shared_links"]
    assert links.get("test_library", "").startswith("shared/")


def test_the_journal_assertions_cover_the_warm_load_failure(authored: dict) -> None:
    entries = (authored.get("verify") or {}).get("journal") or []
    fatal = [entry for entry in entries if entry.get("severity", "fail") == "fail"]
    assert fatal, "a warm-load failure must be able to fail a deployment"
    forbidden = " ".join(str(item) for entry in entries for item in entry.get("forbid", []))
    assert "Warm load failed" in forbidden
    assert "Image library is unavailable" in forbidden


def test_the_catalog_assertion_rejects_loopback(authored: dict) -> None:
    catalog = (authored.get("verify") or {}).get("catalog") or {}
    assert catalog.get("path") == "/api/catalog"
    assert "127.0.0.1" in catalog.get("reject", [])


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda d: d["seeds"][0].update(target="config/ml-platform.env"), "must be under shared/"),
        (lambda d: d["seeds"][0].update(target="shared/nowhere/x.env"), "not under any directory in shared_dirs"),
        (lambda d: d["seeds"][0].update(sources=[], generate=""), "can never be satisfied"),
        (lambda d: d["seeds"][0].update(generate="magic"), "generate must be one of"),
        (lambda d: d["seeds"][0].update(kind="symlink"), "kind must be one of"),
        (lambda d: d["seeds"][0].update(id=d["seeds"][1]["id"]), "duplicates"),
        (lambda d: d["seeds"][2].update(why=""), "must explain itself"),
        (lambda d: d["seeds"][0].update(marker="x"), "applies to a tree, not to a file"),
    ],
)
def test_rejects_a_bad_seed(authored: dict, mutation, expected: str) -> None:
    document = copy.deepcopy(authored)
    mutation(document)
    problems = manifest_tool.validate(document)
    assert any(expected in problem for problem in problems), f"expected {expected!r} in {problems}"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda d: d["verify"]["journal"][0].update(service="not_a_service"),
            "is not a service in this manifest",
        ),
        (
            lambda d: d["verify"]["journal"][0].update(service="pdf_tools"),
            "runs in-process and has no journal",
        ),
        (lambda d: d["verify"]["journal"][0].update(severity="explode"), "severity must be one of"),
        (lambda d: d["verify"]["journal"][0].update(forbid=[]), "must list at least one line"),
        (lambda d: d["verify"]["journal"][0].update(forbid=["a|b"]), "may not contain"),
        (lambda d: d["verify"]["journal"][0].update(lines=0), "must be a positive integer"),
        (lambda d: d["verify"]["catalog"].update(path="api/catalog"), "must be a path beginning with /"),
        (lambda d: d["verify"]["catalog"].update(reject=[]), "must be a non-empty list"),
    ],
)
def test_rejects_a_bad_verify_block(authored: dict, mutation, expected: str) -> None:
    document = copy.deepcopy(authored)
    mutation(document)
    problems = manifest_tool.validate(document)
    assert any(expected in problem for problem in problems), f"expected {expected!r} in {problems}"


def test_rejects_an_environment_file_inside_a_release(authored: dict) -> None:
    # It would be replaced by the next upgrade, taking the site's settings with
    # it -- the failure this whole mechanism exists to prevent.
    document = _mutate(authored, "services.gateway.env_file", "apps/ml_server/config/ml-platform.env")
    problems = manifest_tool.validate(document)
    assert any("must live under shared/" in problem for problem in problems)


def test_rejects_two_services_loading_different_environment_files(authored: dict) -> None:
    document = copy.deepcopy(authored)
    document["services"]["pytex"]["env_file"] = "shared/config/other.env"
    problems = manifest_tool.validate(document)
    assert any("disagrees with runtime.env_file" in problem for problem in problems)


def test_rejects_two_services_claiming_one_url_variable(authored: dict) -> None:
    # Both would be advertised at the same address and one of them would be
    # wrong, which is not something a user could ever diagnose.
    document = _mutate(authored, "services.pytex.public_url_env", "HYDRIDE_SEGMENTATION_URL")
    problems = manifest_tool.validate(document)
    assert any("collides with" in problem for problem in problems)


def test_rejects_a_malformed_url_variable(authored: dict) -> None:
    document = _mutate(authored, "services.pytex.public_url_env", "pytex-url")
    problems = manifest_tool.validate(document)
    assert any("UPPER_SNAKE" in problem for problem in problems)


# ---------------------------------------------------------------------------
# Documentation built on the server.
#
# The build runs after a deployment has already been declared successful, on a
# host with no network, in a step nobody is watching. Everything about it that
# can be checked from the manifest is checked here instead.
# ---------------------------------------------------------------------------


def _docs_build(document: dict) -> dict:
    return document["services"]["pytex"]["docs_build"]


def test_pytex_builds_its_documentation_on_the_server(authored: dict) -> None:
    # /docs answered 404 on the office host because PyTex ships its site inside
    # the installed package and this suite runs application code from source.
    spec = _docs_build(authored)
    assert spec["target"] == "shared/docs/pytex"
    assert "{target}" in spec["command"]


def test_the_documentation_target_survives_an_upgrade(authored: dict) -> None:
    # Inside a release it would be rebuilt on every deployment and removed by
    # the next prune, which for a build measured in tens of minutes is the
    # difference between usable and not.
    target = _docs_build(authored)["target"]
    assert target.startswith("shared/")
    assert target.split("/")[1] in set(authored["shared_dirs"])


def test_the_service_is_told_where_its_documentation_went(authored: dict) -> None:
    # A build nothing can find is tens of minutes of work for no route.
    service = authored["services"]["pytex"]
    pointer = "{root}/" + service["docs_build"]["target"]
    assert service["environment"]["PYTEX_DOCS_ROOT"] == pointer


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda d: d["services"]["pytex"]["docs_build"].update(target="apps/pytex/docs"),
            "must be under shared/",
        ),
        (
            lambda d: d["services"]["pytex"]["docs_build"].update(target="shared/nowhere/docs"),
            "not under any directory in shared_dirs",
        ),
        (
            lambda d: d["services"]["pytex"]["docs_build"].update(
                command="python scripts/build_docs_bundle.py"
            ),
            "must pass the build its destination",
        ),
        (lambda d: d["services"]["pytex"]["docs_build"].update(marker=""), "marker is required"),
        (
            lambda d: d["services"]["pytex"]["docs_build"].update(timeout_seconds=0),
            "timeout_seconds must be a positive integer",
        ),
        (
            lambda d: d["services"]["pytex"]["docs_build"].update(why=""),
            "must explain itself",
        ),
        (
            lambda d: d["services"]["pytex"]["environment"].pop("PYTEX_DOCS_ROOT"),
            "the service would never find the build",
        ),
    ],
)
def test_rejects_a_bad_documentation_build(authored: dict, mutation, expected: str) -> None:
    document = copy.deepcopy(authored)
    mutation(document)
    problems = manifest_tool.validate(document)
    assert any(expected in problem for problem in problems), f"expected {expected!r} in {problems}"
