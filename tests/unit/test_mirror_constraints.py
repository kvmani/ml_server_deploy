"""The office pip mirror lags PyPI, and a release must never pin past it.

Suite v1.12.0 resolved threadpoolctl 3.7.0, which the air-gapped mirror does not
carry, and failed to deploy until the pin was edited by hand to 3.6.0. These
tests keep that cap in place and prove the dependency gate enforces it.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import dependency_gate  # noqa: E402

CONSTRAINTS = REPO_ROOT / "requirements" / "constraints.txt"


def test_threadpoolctl_is_capped_to_the_office_mirror() -> None:
    pins = dependency_gate.read_constraints(CONSTRAINTS)
    assert pins.get("threadpoolctl") == "3.6.0"


def test_the_gate_uses_the_tracked_constraints_file() -> None:
    assert dependency_gate.CONSTRAINTS_FILE == CONSTRAINTS


def test_constraints_file_is_not_git_ignored() -> None:
    # resolved.txt and mirror_audit.txt are generated and ignored; the
    # constraints file is source and must reach CI and the archive.
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "requirements/constraints.txt" not in ignored
    assert "requirements/" not in ignored


def test_a_resolve_past_the_mirror_is_rejected() -> None:
    frozen = "numpy==2.3.1\nscikit-learn==1.7.2\nthreadpoolctl==3.7.0\n"
    problems = dependency_gate.constraint_violations(frozen, {"threadpoolctl": "3.6.0"})
    assert len(problems) == 1
    assert "threadpoolctl==3.7.0" in problems[0]


def test_a_resolve_at_the_cap_is_accepted() -> None:
    frozen = "numpy==2.3.1\nscikit-learn==1.7.2\nthreadpoolctl==3.6.0\n"
    assert dependency_gate.constraint_violations(frozen, {"threadpoolctl": "3.6.0"}) == []


def test_names_are_compared_the_way_pip_compares_them(tmp_path: Path) -> None:
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("# comment\nThreadPool_Ctl==3.6.0  # trailing\n", encoding="utf-8")
    pins = dependency_gate.read_constraints(constraints)
    assert pins == {"threadpool-ctl": "3.6.0"}
    assert dependency_gate.constraint_violations("threadpool.ctl==3.7.0\n", pins)
