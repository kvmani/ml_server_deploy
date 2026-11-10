"""What the suite packager leaves out of a component, and what it must not.

Up to suite 1.21.0, `build`, `output` and similar names were excluded at every
depth. PDF.js keeps its modules in vendor/pdfjs/build/ and MathJax its fonts in
vendor/mathjax/output/, so both silently vanished from the office server and
the PDF Editor could not start. These names are now excluded only at the top of
a component, and a tracked file that would still go missing fails the build.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from build_suite import BuildError, copy_component, tracked_files_lost


def _write(root: Path, relative: str, text: str = "x") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _component(tmp_path: Path) -> Path:
    source = tmp_path / "pdf_tools"
    for relative in (
        "src/app/static/vendor/pdfjs/build/pdf.mjs",
        "src/app/static/vendor/pdfjs/legacy/build/pdf.mjs",
        "src/app/static/vendor/mathjax/output/chtml/fonts/a.woff",
        "src/app/dist/kept.js",
        "build/lib/leftover.py",
        "dist/pkg.whl",
        "logs/run.log.txt",
        "tmp/scratch.txt",
        "node_modules/x/index.js",
        "src/app/__pycache__/m.cpython-312.pyc",
        "src/app/main.py",
    ):
        _write(source, relative)
    return source


def test_vendored_build_and_output_folders_are_kept(tmp_path: Path) -> None:
    source = _component(tmp_path)
    copy_component(source, tmp_path / "out")
    out = tmp_path / "out"
    for kept in (
        "src/app/static/vendor/pdfjs/build/pdf.mjs",
        "src/app/static/vendor/pdfjs/legacy/build/pdf.mjs",
        "src/app/static/vendor/mathjax/output/chtml/fonts/a.woff",
        "src/app/dist/kept.js",
        "src/app/main.py",
    ):
        assert (out / kept).is_file(), kept


def test_top_level_leftovers_and_caches_are_still_left_out(tmp_path: Path) -> None:
    source = _component(tmp_path)
    copy_component(source, tmp_path / "out")
    out = tmp_path / "out"
    for dropped in ("build", "dist", "logs", "tmp", "node_modules", "src/app/__pycache__"):
        assert not (out / dropped).exists(), dropped


def _git(source: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)


def test_the_guard_reports_a_tracked_file_missing_from_the_copy(tmp_path: Path) -> None:
    source = _component(tmp_path)
    _git(source, "init", "-q")
    _git(source, "add", "-f", ".")
    out = tmp_path / "out"
    copy_component(source, out)
    (out / "src/app/static/vendor/pdfjs/legacy/build/pdf.mjs").unlink()
    assert tracked_files_lost(source, out) == ["src/app/static/vendor/pdfjs/legacy/build/pdf.mjs"]


def test_a_rule_widened_by_mistake_fails_the_build(tmp_path: Path, monkeypatch) -> None:
    """Re-adding `build` to the every-depth list must not pass silently again."""
    import build_suite

    source = _component(tmp_path)
    _git(source, "init", "-q")
    _git(source, "add", "-f", ".")
    monkeypatch.setattr(build_suite, "EXCLUDE_DIRS", build_suite.EXCLUDE_DIRS | {"build"})
    with pytest.raises(BuildError, match="pdfjs/build/pdf.mjs"):
        build_suite.copy_component(source, tmp_path / "out")


def test_tracked_files_under_kept_names_pass_the_guard(tmp_path: Path) -> None:
    source = _component(tmp_path)
    _git(source, "init", "-q")
    _git(source, "add", "-f", ".")
    copy_component(source, tmp_path / "out")  # raises if anything tracked went missing
