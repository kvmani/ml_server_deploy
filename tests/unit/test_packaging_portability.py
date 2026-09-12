"""The archive is built on Windows and unpacked on Ubuntu.

Everything here is about that crossing. Nothing in a release archive may depend
on which of the two machines produced it: not the line endings, not the file
modes, and not the paths recorded inside the tar.
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from build_suite import build_archive, wants_execute_bit  # noqa: E402


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    root = tmp_path / "staging"
    (root / "deploy" / "lib").mkdir(parents=True)
    (root / "apps" / "ml_server" / "src").mkdir(parents=True)

    (root / "deploy" / "update.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n"
    )
    (root / "deploy" / "lib" / "common.sh").write_text(
        "# sourced, not executed\n", encoding="utf-8", newline="\n"
    )
    (root / "tool").write_text(
        "#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8", newline="\n"
    )
    (root / "VERSION").write_text("1.9.0\n", encoding="utf-8", newline="\n")
    (root / "apps" / "ml_server" / "src" / "app.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    return root


def members(archive_path: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(archive_path, "r:gz") as archive:
        return {member.name: member for member in archive.getmembers()}


# ---------------------------------------------------------------------------
# Execute bits
# ---------------------------------------------------------------------------
def test_shell_scripts_are_executable_in_the_archive(staging, tmp_path):
    """NTFS has no execute bit; the archive must have one regardless.

    A release built on the Windows workstation once shipped update.sh as 0644,
    and the office server answered "Permission denied" to the one command the
    runbook is built around.
    """
    out = tmp_path / "suite.tar.gz"
    build_archive(staging, "suite-1.9.0", out, mtime=0)

    entries = members(out)
    assert entries["suite-1.9.0/deploy/update.sh"].mode == 0o755
    assert entries["suite-1.9.0/deploy/lib/common.sh"].mode == 0o755


def test_a_shebang_makes_a_file_executable_whatever_its_name(staging, tmp_path):
    out = tmp_path / "suite.tar.gz"
    build_archive(staging, "suite-1.9.0", out, mtime=0)

    assert members(out)["suite-1.9.0/tool"].mode == 0o755


def test_ordinary_files_are_not_executable(staging, tmp_path):
    out = tmp_path / "suite.tar.gz"
    build_archive(staging, "suite-1.9.0", out, mtime=0)

    entries = members(out)
    assert entries["suite-1.9.0/VERSION"].mode == 0o644
    assert entries["suite-1.9.0/apps/ml_server/src/app.py"].mode == 0o644


def test_directories_are_traversable(staging, tmp_path):
    out = tmp_path / "suite.tar.gz"
    build_archive(staging, "suite-1.9.0", out, mtime=0)

    assert members(out)["suite-1.9.0/deploy"].mode == 0o755


def test_wants_execute_bit_is_decided_by_content_not_by_the_host(tmp_path):
    script = tmp_path / "run"
    script.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    plain = tmp_path / "notes.md"
    plain.write_text("# notes\n", encoding="utf-8", newline="\n")

    assert wants_execute_bit(script)
    assert not wants_execute_bit(plain)


# ---------------------------------------------------------------------------
# Paths and reproducibility
# ---------------------------------------------------------------------------
def test_archive_paths_use_forward_slashes(staging, tmp_path):
    """A backslash in a tar member name is a filename on Ubuntu, not a directory."""
    out = tmp_path / "suite.tar.gz"
    build_archive(staging, "suite-1.9.0", out, mtime=0)

    for name in members(out):
        assert "\\" not in name, name
    assert "suite-1.9.0/deploy/lib/common.sh" in members(out)


def test_the_archive_is_reproducible(staging, tmp_path):
    first = build_archive(staging, "suite-1.9.0", tmp_path / "a.tar.gz", mtime=0)
    second = build_archive(staging, "suite-1.9.0", tmp_path / "b.tar.gz", mtime=0)

    assert first == second


def test_ownership_is_normalised(staging, tmp_path):
    """The build machine's user must not end up owning files on the server."""
    out = tmp_path / "suite.tar.gz"
    build_archive(staging, "suite-1.9.0", out, mtime=0)

    for member in members(out).values():
        assert member.uid == 0 and member.gid == 0
        assert member.uname == "" and member.gname == ""


# ---------------------------------------------------------------------------
# Line endings, at the source
# ---------------------------------------------------------------------------
def test_no_shipped_shell_script_has_crlf():
    """`bad interpreter: /bin/bash^M` is the failure this prevents."""
    offenders = []
    for path in sorted((REPO_ROOT / "deploy").rglob("*.sh")):
        if b"\r\n" in path.read_bytes():
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, offenders


def test_no_shipped_systemd_template_has_crlf():
    offenders = []
    for path in sorted((REPO_ROOT / "systemd").glob("*.template")):
        if b"\r\n" in path.read_bytes():
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, offenders
