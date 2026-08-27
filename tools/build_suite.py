#!/usr/bin/env python3
"""Assemble a deployable suite archive from resolved component checkouts.

Called by the release workflow after every component has been checked out at
its exact pinned commit.  It can also be run locally against working copies to
produce a rehearsal archive, which is how the deployment scripts get tested
without going near GitHub.

    python tools/build_suite.py \\
        --manifest manifest.resolved.yml \\
        --source gateway=/checkout/ml_server \\
        --source pytex=/checkout/pytex \\
        ... \\
        --out dist/

What it guarantees, in order of how much trouble each one saves:

1.  No component in the assembled tree resolves a dependency from GitHub.  The
    gateway's requirements.txt pins pdf_tools and tabular_ml to GitHub archive
    URLs; on an air-gapped host that fails on the first pip invocation, so those
    lines are rewritten to the local paths inside the archive and the result is
    re-scanned to prove none survived.
2.  Nothing that must not ship, ships: .git, node_modules, virtualenvs, caches,
    and the model checkpoints that belong in shared/models instead.
3.  The tar is byte-for-byte reproducible, so building the same commits twice
    gives the same checksum and "is this the archive I tested?" is answerable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories and files that must never reach the office server.
EXCLUDE_DIRS = {
    ".git", ".github", "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "venv", "env", ".mypy_cache", ".ruff_cache", ".tox",
    "htmlcov", ".idea", ".vscode", "playwright-report", ".playwright-cli",
    "build", "dist", "logs", "output", "outputs", "tmp",
}
EXCLUDE_SUFFIXES = {
    ".pyc", ".pyo", ".log", ".coverage",
    # Trained model weights are not in git and belong in shared/models.
    ".pt", ".pth", ".ckpt", ".h5", ".hdf5", ".pkl", ".onnx", ".safetensors",
}
EXCLUDE_NAMES = {".coverage", ".DS_Store", "Thumbs.db"}

GITHUB_PATTERN = re.compile(rb"https?://(?:[\w.-]+\.)?github(?:usercontent)?\.com", re.IGNORECASE)

# Text files worth scanning for a stray GitHub dependency reference.
SCAN_SUFFIXES = {".txt", ".toml", ".cfg", ".json", ".yml", ".yaml", ".in"}


class BuildError(Exception):
    """Raised when the assembled tree would not be safe to deploy."""


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


def _ignore(directory: str, entries: list[str]) -> set[str]:
    ignored: set[str] = set()
    for entry in entries:
        if entry in EXCLUDE_DIRS or entry in EXCLUDE_NAMES:
            ignored.add(entry)
            continue
        if any(entry.endswith(suffix) for suffix in EXCLUDE_SUFFIXES):
            ignored.add(entry)
        if entry.endswith(".egg-info"):
            ignored.add(entry)
    return ignored


def copy_component(source: Path, destination: Path) -> int:
    if not source.is_dir():
        raise BuildError(f"component source is not a directory: {source}")
    shutil.copytree(source, destination, ignore=_ignore, symlinks=False)
    return sum(1 for path in destination.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------
# The GitHub-dependency rewrite
# ---------------------------------------------------------------------------


def rewrite_github_requirements(staging: Path, manifest: dict[str, Any]) -> list[str]:
    """Replace GitHub archive pins with paths inside the archive.

    ``pdf-tools-service @ https://github.com/kvmani/pdf_tools/archive/...zip``
    becomes ``./apps/pdf_tools``.  Without this the very first pip command on
    the office server tries to reach GitHub and the install stops dead.
    """
    services = manifest.get("services", {})
    # Map the distribution name each component publishes to its directory.
    dist_to_dir = {
        "pdf-tools-service": services.get("pdf_tools", {}).get("dir", "pdf_tools"),
        "pdf_tools_service": services.get("pdf_tools", {}).get("dir", "pdf_tools"),
        "tabular-ml-service": services.get("tabular_ml", {}).get("dir", "tabular_ml"),
        "tabular_ml_service": services.get("tabular_ml", {}).get("dir", "tabular_ml"),
    }

    notes: list[str] = []
    for requirements_path in staging.glob("apps/*/requirements*.txt"):
        original = requirements_path.read_text(encoding="utf-8")
        rewritten_lines: list[str] = []
        for line in original.splitlines():
            stripped = line.strip()
            match = re.match(r"^([A-Za-z0-9_.-]+)\s*@\s*(https?://\S+)", stripped)
            if match and "github" in match.group(2).lower():
                distribution = match.group(1)
                target_dir = dist_to_dir.get(distribution.lower().replace("_", "-"))
                if target_dir is None:
                    raise BuildError(
                        f"{requirements_path.relative_to(staging)} pins {distribution!r} to a GitHub URL "
                        f"but no component in the manifest provides it. The office server cannot fetch it. "
                        f"Add the component to the manifest, or vendor the package on the internal mirror."
                    )
                replacement = f"./apps/{target_dir}"
                rewritten_lines.append(replacement)
                notes.append(
                    f"{requirements_path.relative_to(staging)}: {distribution} -> {replacement}"
                )
            else:
                rewritten_lines.append(line)
        new_text = "\n".join(rewritten_lines) + "\n"
        if new_text != original:
            requirements_path.write_text(new_text, encoding="utf-8", newline="\n")
    return notes


def assert_no_github_dependencies(staging: Path) -> list[str]:
    """Fail if anything pip reads at deploy time resolves from GitHub.

    Scope matters here. An earlier version scanned every text file for a GitHub
    URL and failed the build on npm sponsorship links in a package-lock, on the
    provenance metadata recording where a pretrained model architecture came
    from, and on a dataset citation. None of those are ever fetched; flagging
    them made the check noise rather than a safeguard.

    What actually matters is the files pip consumes: requirements files, which
    update.sh installs from. A GitHub URL there stops the office install dead.

    pyproject.toml is reported but not fatal. The deployment never runs
    `pip install .` -- application code runs from the release tree via
    PYTHONPATH -- so a GitHub pin there is inert, though still worth knowing
    about because it would bite anyone who did try to install the package.
    """
    fatal: list[str] = []
    warnings: list[str] = []

    for path in staging.rglob("requirements*.txt"):
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if GITHUB_PATTERN.search(stripped.encode()):
                fatal.append(f"{path.relative_to(staging)}:{number}: {stripped}")

    for path in staging.rglob("pyproject.toml"):
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if "@" in stripped and GITHUB_PATTERN.search(stripped.encode()):
                warnings.append(f"{path.relative_to(staging)}:{number}: {stripped}")

    if fatal:
        raise BuildError(
            "the assembled tree still resolves dependencies from GitHub in files pip reads, "
            "which an air-gapped office server cannot reach:\n  " + "\n  ".join(fatal)
        )
    return warnings


# ---------------------------------------------------------------------------
# Deterministic tar
# ---------------------------------------------------------------------------


def build_archive(staging: Path, prefix: str, out_path: Path, mtime: int) -> str:
    """Write a reproducible .tar.gz and return its sha256.

    Reproducibility is not pedantry here: it is what lets you prove the archive
    on the office server is the one that passed the tests, by comparing a single
    checksum.
    """
    entries: list[tuple[str, Path]] = []
    for path in sorted(staging.rglob("*"), key=lambda item: item.relative_to(staging).as_posix()):
        entries.append((f"{prefix}/{path.relative_to(staging).as_posix()}", path))

    def normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = mtime
        # Executable bits survive, everything else is normalised, so that a
        # different umask on the build machine cannot change the checksum.
        if info.isdir():
            info.mode = 0o755
        elif info.mode & stat.S_IXUSR:
            info.mode = 0o755
        else:
            info.mode = 0o644
        return info

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # gzip with mtime=0 so the container header is stable too.
    import gzip

    raw = out_path.with_suffix(".tar.tmp")
    with tarfile.open(raw, "w", format=tarfile.GNU_FORMAT) as archive:
        for name, path in entries:
            info = archive.gettarinfo(str(path), arcname=name)
            info = normalise(info)
            if path.is_file():
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            else:
                archive.addfile(info)

    with raw.open("rb") as source, out_path.open("wb") as destination:
        with gzip.GzipFile(fileobj=destination, mode="wb", mtime=0, compresslevel=9) as gz:
            shutil.copyfileobj(source, gz)
    raw.unlink()

    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    return digest


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble(manifest: dict[str, Any], sources: dict[str, Path], staging: Path,
             deploy_root: Path) -> list[str]:
    staging.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    services = manifest.get("services", {})
    missing = [name for name in services if name not in sources]
    if missing:
        raise BuildError(
            f"no --source given for component(s): {', '.join(sorted(missing))}. "
            f"Every service in the manifest must be supplied."
        )

    apps = staging / "apps"
    apps.mkdir()
    for name, service in services.items():
        directory = service.get("dir", name)
        count = copy_component(sources[name], apps / directory)
        notes.append(f"apps/{directory}: {count} file(s) from {sources[name]}")

    # Deployment scripts, unit template and requirements travel with the release
    # so that the archive is self-sufficient and the scripts always match the
    # manifest they were built against.
    shutil.copytree(deploy_root / "deploy", staging / "deploy", ignore=_ignore)
    shutil.copytree(deploy_root / "systemd", staging / "systemd", ignore=_ignore)
    if (deploy_root / "requirements").is_dir():
        shutil.copytree(deploy_root / "requirements", staging / "requirements", ignore=_ignore)
    else:
        (staging / "requirements").mkdir()

    for script in (staging / "deploy").glob("*.sh"):
        script.chmod(0o755)

    runbook = deploy_root / "RUNBOOK.md"
    if runbook.is_file():
        shutil.copy2(runbook, staging / "RUNBOOK.md")

    notes.extend(rewrite_github_requirements(staging, manifest))
    for warning in assert_no_github_dependencies(staging):
        notes.append(f"note (not fatal, pip never reads this at deploy time): {warning}")

    version = manifest["suite_version"]
    (staging / "VERSION").write_text(f"{version}\n", encoding="utf-8", newline="\n")
    (staging / "manifest.resolved.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (staging / "manifest.resolved.yml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False),
        encoding="utf-8", newline="\n",
    )
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source", action="append", default=[], metavar="ID=PATH",
                        help="component source directory, repeatable")
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--staging", type=Path, help="keep the staging tree here for inspection")
    parser.add_argument("--allow-unresolved", action="store_true",
                        help="permit empty commit SHAs (rehearsal builds only)")
    parser.add_argument("--mtime", type=int, default=0, help="fixed mtime for reproducibility")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import manifest as manifest_tool

    try:
        with args.manifest.open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle)

        problems = manifest_tool.validate(document, require_commits=not args.allow_unresolved)
        if problems:
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            raise BuildError(f"{args.manifest} is not valid")

        if args.allow_unresolved:
            # A rehearsal archive still needs a value in every commit field so
            # that update.sh's own checks exercise the same code path.
            for name, service in document["services"].items():
                if not service.get("commit"):
                    service["commit"] = hashlib.sha1(
                        f"rehearsal:{name}:{document['suite_version']}".encode()
                    ).hexdigest()

        sources: dict[str, Path] = {}
        for item in args.source:
            if "=" not in item:
                raise BuildError(f"--source must be ID=PATH, got {item!r}")
            key, _, value = item.partition("=")
            path = Path(value).expanduser().resolve()
            if not path.is_dir():
                raise BuildError(f"--source {key}: {path} is not a directory")
            sources[key] = path

        version = document["suite_version"]
        prefix = f"ml-server-suite-v{version}"

        staging_root = args.staging or (args.out / f".staging-{version}")
        if staging_root.exists():
            shutil.rmtree(staging_root)

        print(f"assembling suite {version}", file=sys.stderr)
        notes = assemble(document, sources, staging_root, REPO_ROOT)
        for note in notes:
            print(f"  {note}", file=sys.stderr)

        archive_path = args.out / f"{prefix}.tar.gz"
        digest = build_archive(staging_root, prefix, archive_path, args.mtime)

        checksum_path = Path(f"{archive_path}.sha256")
        checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8", newline="\n")

        size = archive_path.stat().st_size
        print(f"\nwrote {archive_path} ({size / 1024 / 1024:.1f} MiB)", file=sys.stderr)
        print(f"wrote {checksum_path}", file=sys.stderr)
        print(f"sha256 {digest}", file=sys.stderr)

        if args.staging is None:
            shutil.rmtree(staging_root, ignore_errors=True)

        print(digest)

    except BuildError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
