#!/usr/bin/env python3
"""Build the shared environment, prove it is coherent, and test the components in it.

This is the gate that decides whether a set of component pins can actually be
deployed together. It exists as a script rather than as shell embedded in a
workflow so that it can be run locally, before spending a CI cycle:

    python tools/dependency_gate.py --manifest manifest.resolved.yml \\
        --components ../components --venv /tmp/depcheck

Order matters here. An earlier version of the release workflow ran each
component's test suite *before* installing anything, so every suite failed at
import. Dependencies are resolved and installed first, and the components are
then tested against the exact environment that will be deployed -- which is a
more honest test than running them against a developer's machine.

Outputs, both consumed by the office server:

  requirements/resolved.txt     every package pinned, what update.sh installs
  requirements/mirror_audit.txt bare names, to hand to IT before release day
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# torch and friends must come from the CPU index. The default wheels bundle the
# CUDA runtime, which adds gigabytes and pulls GPU libraries onto an office
# machine that has no GPU.
CPU_INDEX = "https://download.pytorch.org/whl/cpu"


class GateError(Exception):
    """Raised when the pinned component set cannot be deployed as it stands."""


def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    print(f"    $ {' '.join(str(part) for part in command)}", flush=True)
    return subprocess.run(command, **kwargs)


def annotate(title: str, message: str) -> None:
    """Surface a failure reason in the GitHub Actions run summary.

    Without this a failed gate shows only "Process completed with exit code 1",
    and the reason is buried in a log that needs repository admin rights to
    download. An annotation is visible on the run itself and through the API,
    which is the difference between diagnosing a failed release in a minute and
    guessing at it.
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    # Annotations are single-line; newlines are encoded.
    encoded = message.replace("\r", "").replace("\n", "%0A")
    print(f"::error title={title}::{encoded}", flush=True)


def pyproject_dependencies(directory: Path) -> list[str]:
    """Read `[project] dependencies` from a component's pyproject.toml.

    Several components declare their dependencies only in pyproject.toml and
    have no requirements.txt at all. Reading the requirements files alone left
    pandas, WTForms and others out of the deployed environment entirely -- the
    suite installed cleanly and then failed at runtime, which is the worst way
    to find out. The package itself is deliberately not installed; only what it
    depends on, because application code runs from the release tree.
    """
    path = directory / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        return []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return []
    return list((data.get("project") or {}).get("dependencies") or [])


def _test_extra(directory: Path, extras: list[str]) -> list[str]:
    """Return the packages in the named pyproject extras of a component.

    These are installed only to run the component's test suite and are never
    written to resolved.txt, so they never reach the office server. Which
    extras a component needs is declared in the manifest (`test_extras`),
    because guessing is how you end up installing a documentation toolchain.
    """
    path = directory / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        return []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return []
    optional = (data.get("project") or {}).get("optional-dependencies") or {}
    packages: list[str] = []
    for key in extras:
        packages.extend(optional.get(key) or [])
    return [item for item in packages if "github.com" not in item.lower()]


def collect_requirements(document: dict, components: Path) -> tuple[list[str], list[str]]:
    """Gather every component's third-party requirements.

    GitHub archive pins are dropped: those components travel inside the release
    archive already, and an air-gapped host could never fetch them anyway.
    """
    requirements: list[str] = []
    dropped: list[str] = []

    for name, service in document["services"].items():
        directory = components / service.get("dir", name)

        for dependency in pyproject_dependencies(directory):
            if "github.com" in dependency.lower():
                dropped.append(dependency)
            else:
                requirements.append(dependency)

        for requirement_file in service.get("requirements") or []:
            path = directory / requirement_file
            if not path.is_file():
                print(f"    note: {name}: {requirement_file} not present, skipping")
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("-r "):
                    # Recursive includes are resolved by walking the file we were
                    # pointed at, relative to the file that referenced it.
                    nested = path.parent / stripped[3:].strip()
                    if nested.is_file():
                        for nested_line in nested.read_text(encoding="utf-8").splitlines():
                            nested_stripped = nested_line.strip()
                            if nested_stripped and not nested_stripped.startswith(("#", "-r ")):
                                if "github.com" in nested_stripped.lower():
                                    dropped.append(nested_stripped)
                                else:
                                    requirements.append(nested_stripped)
                    continue
                if "github.com" in stripped.lower():
                    dropped.append(stripped)
                    continue
                requirements.append(stripped)

    return sorted(set(requirements)), sorted(set(dropped))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("requirements"))
    parser.add_argument("--run-component-tests", action="store_true",
                        help="also run each component's own test suite (slow; not part of the release gate)")
    parser.add_argument("--skip-install", action="store_true", help="reuse an existing venv")
    args = parser.parse_args()

    try:
        with args.manifest.open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle)

        args.out.mkdir(parents=True, exist_ok=True)
        python = args.venv / "bin" / "python"

        # --- 1. the environment ------------------------------------------
        if not args.skip_install:
            print("== creating the shared environment")
            run([sys.executable, "-m", "venv", str(args.venv)], check=True)
            run([str(python), "-m", "pip", "install", "--upgrade", "pip", "wheel"],
                check=True, stdout=subprocess.DEVNULL)

        print("\n== collecting component requirements")
        requirements, dropped = collect_requirements(document, args.components)
        for item in dropped:
            print(f"    dropped GitHub pin (ships inside the archive): {item}")
        declared = args.out / "declared.txt"
        declared.write_text("\n".join(requirements) + "\n", encoding="utf-8", newline="\n")
        print(f"    {len(requirements)} requirement(s) -> {declared}")

        if not args.skip_install:
            print("\n== installing")
            # Captured rather than streamed so that the reason for a failure can
            # be turned into an annotation. Downloading a job log needs admin
            # rights on the repository, so a failure that only lives in the log
            # is effectively invisible.
            result = run([str(python), "-m", "pip", "install",
                          "--extra-index-url", CPU_INDEX,
                          "-r", str(declared)],
                         capture_output=True, text=True)
            print(result.stdout)
            if result.returncode != 0:
                print(result.stderr, file=sys.stderr)
                tail = "\n".join((result.stderr or result.stdout).strip().splitlines()[-25:])
                annotate("Dependency install failed", tail)
                raise GateError(
                    "the combined dependency set could not be installed. Either a pin is wrong, "
                    "or two components disagree about a shared package.\n" + tail
                )

        # --- 2. coherence -------------------------------------------------
        print("\n== pip check")
        result = run([str(python), "-m", "pip", "check"], capture_output=True, text=True)
        print(result.stdout)
        if result.returncode != 0:
            annotate("pip check failed", (result.stdout or result.stderr).strip())
            raise GateError(
                "pip check failed: the components cannot share one environment as pinned.\n"
                "If this is the torch/portal conflict, set `env: isolated` on the offending\n"
                "service in manifest.yml and the deployment scripts will give it its own venv."
            )

        # --- 3. outputs, captured BEFORE any test-only package is installed.
        # Freezing after the test step would put pytest, hypothesis and the
        # rest into resolved.txt, and the office server would then install
        # a test harness it has no use for.
        print("\n== writing the deployable requirement set")
        frozen = subprocess.run([str(python), "-m", "pip", "freeze", "--exclude-editable"],
                                capture_output=True, text=True, check=True).stdout
        resolved = args.out / "resolved.txt"
        resolved.write_text(frozen, encoding="utf-8", newline="\n")

        names = sorted({line.split("==")[0] for line in frozen.splitlines() if "==" in line})
        audit = args.out / "mirror_audit.txt"
        audit.write_text(
            "# Every distribution the office pip mirror must be able to serve.\n"
            "# Generated by tools/dependency_gate.py; hand this to IT before release day.\n"
            + "\n".join(names) + "\n",
            encoding="utf-8", newline="\n",
        )
        print(f"    {len(frozen.splitlines())} package(s) pinned -> {resolved}")
        print(f"    {len(names)} distribution(s) to mirror -> {audit}")

        # --- 4. every component must import in the shared environment ------
        #
        # This is the deployment-relevant question: can these components, at
        # these pins, actually coexist and load? It catches a missing
        # dependency, a version conflict pip check cannot see, and a component
        # that needs a package nobody declared -- which is exactly how
        # tabular_ml was found to be missing pandas.
        failures: list[str] = []
        print("\n== import checks in the shared environment")
        for name, service in document["services"].items():
            module = service.get("import_check")
            if not module:
                continue
            directory = args.components / service.get("dir", name)
            declared_path = ((service.get("environment") or {}).get("PYTHONPATH") or "")
            if declared_path:
                path_value = declared_path.replace("{current}/apps", str(args.components.resolve()))
            else:
                path_value = str((directory / service.get("src", "src")).resolve())

            import os

            probe = subprocess.run(
                [str(python), "-c", f"import {module}; print({module}.__name__)"],
                cwd=directory, env={**os.environ, "PYTHONPATH": path_value},
                capture_output=True, text=True,
            )
            if probe.returncode == 0:
                print(f"    ok       {name:<12} import {module}")
            else:
                last = (probe.stderr.strip().splitlines() or ["unknown error"])[-1]
                print(f"    FAILED   {name:<12} import {module}: {last}")
                annotate(f"{name}: import {module} failed",
                         (probe.stderr or "").strip()[-1500:])
                failures.append(f"{name}: `import {module}` failed: {last}")

        # --- 5. the components' own suites, only when explicitly asked -----
        if args.run_component_tests:
            print("\n== component test suites")
            run([str(python), "-m", "pip", "install", "pytest"], stdout=subprocess.DEVNULL)

            for name, service in document["services"].items():
                command = service.get("tests")
                if not command:
                    print(f"\n-- {name}: no test command declared, skipping")
                    continue
                directory = args.components / service.get("dir", name)
                if not directory.is_dir():
                    failures.append(f"{name}: component directory missing")
                    continue

                # Test-only dependencies are not part of what gets deployed, so
                # they are installed here and never written to resolved.txt.
                # resolved.txt is captured before this point for that reason.
                for extra in ("requirements-test.txt", "requirements-dev.txt"):
                    if (directory / extra).is_file():
                        run([str(python), "-m", "pip", "install", "-r", str(directory / extra)],
                            stdout=subprocess.DEVNULL)
                # Several projects declare their test dependencies as pyproject
                # extras rather than in a requirements file.
                extras = _test_extra(directory, service.get("test_extras") or ["test", "tests", "dev"])
                if extras:
                    print(f"   installing test-only extras: {' '.join(extras)}")
                    run([str(python), "-m", "pip", "install", *extras], stdout=subprocess.DEVNULL)

                print(f"\n-- {name}: {command}")
                import os

                # Use the PYTHONPATH the deployed unit will actually have,
                # translated from the manifest. The gateway imports pdf_tools
                # and tabular_ml in-process, so testing it with only its own
                # src/ on the path fails in a way production never would -- and
                # would have hidden whether the real path is correct.
                declared_path = ((service.get("environment") or {}).get("PYTHONPATH") or "")
                if declared_path:
                    resolved_path = declared_path.replace("{current}/apps", str(args.components.resolve()))
                else:
                    resolved_path = str((directory / service.get("src", "src")).resolve())

                merged = {**os.environ, "PYTHONPATH": resolved_path}
                print(f"   PYTHONPATH={resolved_path}")
                parts = command.split()
                if parts[:3] == ["python", "-m", "pytest"]:
                    parts = [str(python), "-m", "pytest", *parts[3:]]
                result = subprocess.run(parts, cwd=directory, env=merged)
                if result.returncode != 0:
                    failures.append(f"{name}: `{command}` exited {result.returncode}")

        if failures:
            print("\n== FAILURES")
            for failure in failures:
                print(f"    {failure}")
            annotate("Dependency gate failed", "\n".join(failures))
            raise GateError(f"{len(failures)} check(s) failed; this release will not be published")

        print("\n== dependency gate passed")

    except GateError as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"\nERROR: command failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
