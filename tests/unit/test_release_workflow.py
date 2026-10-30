"""The release can be run end to end by whoever may start a workflow.

A cloud agent can start a workflow but cannot push a tag, so a release started
by hand must create its own tag -- and only after every gate has passed, so a
failed build never leaves a tag on an unbuildable commit. These checks keep
that shape from being lost in a later edit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from component_updates import newest_tag, pinned_commit


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1).
    return document.get("on", document.get(True)) or {}


def _steps(document: dict) -> list[dict]:
    (job,) = document["jobs"].values()
    return job["steps"]


def _index(steps: list[dict], prefix: str) -> int:
    return next(i for i, step in enumerate(steps) if str(step.get("name", "")).startswith(prefix))


def test_the_suite_release_starts_only_by_hand_or_by_a_pushed_tag() -> None:
    triggers = _triggers(_workflow(ROOT / ".github" / "workflows" / "release.yml"))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert set(triggers["push"]) == {"tags"}
    publish = triggers["workflow_dispatch"]["inputs"]["publish"]
    assert publish["type"] == "boolean" and publish["default"] is False


def test_the_suite_is_tagged_only_after_every_gate_and_published_under_that_tag() -> None:
    steps = _steps(_workflow(ROOT / ".github" / "workflows" / "release.yml"))
    tag = _index(steps, "Create the tag")
    for gate in ("Decide the tag", "Unit tests", "Dependency gate", "Assemble the suite archive"):
        assert _index(steps, gate) < tag, f"{gate} must run before the tag is created"
    assert steps[tag]["if"] == "steps.tag.outputs.create == 'true'"
    publish = steps[_index(steps, "Publish the release")]
    assert publish["if"] == "steps.tag.outputs.publish == 'true'"
    assert publish["with"]["tag_name"] == "${{ steps.tag.outputs.tag }}"
    assert "github.ref_name" not in yaml.safe_dump(publish)


def test_a_rehearsal_keeps_its_archive_and_publishes_nothing() -> None:
    steps = _steps(_workflow(ROOT / ".github" / "workflows" / "release.yml"))
    keep = steps[_index(steps, "Keep the rehearsal archive")]
    assert keep["if"] == "steps.tag.outputs.publish != 'true'"


def test_the_component_template_can_tag_and_names_what_to_change() -> None:
    path = ROOT / "tools" / "templates" / "component-release.yml"
    document = _workflow(path)
    triggers = _triggers(document)
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert set(triggers["push"]) == {"tags"}
    assert "version" in triggers["workflow_dispatch"]["inputs"]
    assert document["env"] == {"PRODUCT": "__PRODUCT__", "VERSION_FILE": "__VERSION_FILE__"}
    steps = _steps(document)
    assert _index(steps, "Decide the tag") < _index(steps, "Take the release notes") < _index(steps, "Create the tag")
    assert _index(steps, "Create the tag") < _index(steps, "Publish the release")


def test_no_workflow_runs_on_a_schedule_or_a_branch_push() -> None:
    for path in [*(ROOT / ".github" / "workflows").glob("*.yml"), ROOT / "tools" / "templates" / "component-release.yml"]:
        triggers = _triggers(_workflow(path))
        assert not set(triggers) & {"schedule", "pull_request", "pull_request_target"}, path.name
        assert set(triggers.get("push") or {}) <= {"tags"}, path.name


def test_newest_tag_orders_versions_numerically_and_ignores_other_tags() -> None:
    refs = {
        "refs/tags/v0.9.0": "a" * 40,
        "refs/tags/v0.10.0": "b" * 40,
        "refs/tags/v0.10.0-rc1": "c" * 40,
        "refs/tags/latest": "d" * 40,
        "refs/heads/main": "e" * 40,
    }
    assert newest_tag(refs) == "v0.10.0"
    assert newest_tag({"refs/heads/main": "e" * 40}) is None


def test_pinned_commit_resolves_tags_branches_and_bare_commits() -> None:
    refs = {"refs/tags/v1.0.0": "a" * 40, "refs/heads/main": "b" * 40}
    assert pinned_commit(refs, "v1.0.0") == "a" * 40
    assert pinned_commit(refs, "main") == "b" * 40
    assert pinned_commit(refs, "c" * 40) == "c" * 40
    assert pinned_commit(refs, "v9.9.9") is None
