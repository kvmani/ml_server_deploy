#!/usr/bin/env python3
"""Validate the authored suite manifest and resolve its component pins.

Three jobs, all of them gates rather than conveniences:

``--validate``
    Structural checks that catch a bad edit before it is pushed.  Runs on
    Windows under pytest, so a malformed manifest never reaches CI.

``--resolve``
    Turns every ``ref`` into an immutable commit SHA via the GitHub API and
    fails loudly if a tag does not exist.  This is what makes a suite release
    reproducible; without it a release is only as stable as a moving branch.

``--json``
    Emits ``manifest.resolved.json``.  The office-side shell scripts read only
    this file, because PyYAML cannot be assumed present on a freshly
    provisioned host while ``json`` is in the standard library.

Usage:
    python tools/manifest.py --validate manifest.yml
    python tools/manifest.py --resolve  manifest.yml --out manifest.resolved.yml
    python tools/manifest.py --json     manifest.resolved.yml --out manifest.resolved.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "ml-suite.manifest.v1"
GITHUB_API = "https://api.github.com"

VALID_ENVS = {"shared", "isolated"}
VALID_SCOPES = {"auto", "user", "system", "none"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
# Reserved ports and the loopback-only range the suite is allowed to use.
PORT_MIN, PORT_MAX = 1024, 65535


class ManifestError(Exception):
    """Raised for any manifest problem that must stop a release."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(document: dict[str, Any], *, require_commits: bool = False) -> list[str]:
    """Return a list of problems; an empty list means the manifest is sound.

    ``require_commits`` is off for the authored manifest and on for the
    resolved one, which is exactly the difference between "a human may edit
    this" and "this describes one immutable release".
    """
    problems: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    check(document.get("schema") == SCHEMA, f"schema must be {SCHEMA!r}, got {document.get('schema')!r}")

    version = str(document.get("suite_version", ""))
    check(bool(VERSION_RE.match(version)), f"suite_version {version!r} is not a semantic version")

    platform = document.get("platform") or {}
    check(platform.get("os_id") == "ubuntu", "platform.os_id must be 'ubuntu'")
    check(bool(platform.get("python_min")), "platform.python_min is required")

    runtime = document.get("runtime") or {}
    check(bool(runtime.get("user")), "runtime.user is required")
    check(
        runtime.get("systemd_scope", "auto") in VALID_SCOPES,
        f"runtime.systemd_scope must be one of {sorted(VALID_SCOPES)}",
    )
    check(
        isinstance(runtime.get("root_candidates"), list) and bool(runtime["root_candidates"]),
        "runtime.root_candidates must be a non-empty list",
    )

    services = document.get("services") or {}
    check(bool(services), "at least one service must be defined")

    seen_ports: dict[int, str] = {}
    seen_units: dict[str, str] = {}

    for name, service in services.items():
        where = f"services.{name}"
        if not isinstance(service, dict):
            problems.append(f"{where} must be a mapping")
            continue

        repo = service.get("repo", "")
        check(bool(REPO_RE.match(str(repo))), f"{where}.repo {repo!r} is not owner/name")
        check(bool(service.get("ref")), f"{where}.ref is required")

        commit = str(service.get("commit") or "")
        if require_commits:
            check(
                bool(SHA_RE.match(commit)),
                f"{where}.commit must be a full 40-character SHA in a resolved manifest, got {commit!r}",
            )
        elif commit:
            check(bool(SHA_RE.match(commit)), f"{where}.commit {commit!r} is not a full 40-character SHA")

        check(service.get("env", "shared") in VALID_ENVS, f"{where}.env must be one of {sorted(VALID_ENVS)}")

        in_process = bool(service.get("in_process"))
        if in_process:
            # Mounted inside the gateway: it must say where, and must not
            # claim a port or a unit it does not own.
            check(bool(service.get("mount")), f"{where} is in_process so it must declare a mount path")
            check("port" not in service, f"{where} is in_process and must not declare a port")
            check("unit" not in service, f"{where} is in_process and must not declare a systemd unit")
        else:
            port = service.get("port")
            check(isinstance(port, int), f"{where}.port is required for a standalone service")
            if isinstance(port, int):
                check(PORT_MIN <= port <= PORT_MAX, f"{where}.port {port} is outside {PORT_MIN}-{PORT_MAX}")
                if port in seen_ports:
                    problems.append(f"{where}.port {port} collides with services.{seen_ports[port]}")
                else:
                    seen_ports[port] = name

            unit = service.get("unit", "")
            check(bool(unit), f"{where}.unit is required for a standalone service")
            check(str(unit).endswith(".service"), f"{where}.unit {unit!r} must end in .service")
            if unit in seen_units:
                problems.append(f"{where}.unit {unit!r} collides with services.{seen_units[unit]}")
            else:
                seen_units[str(unit)] = name

            check(bool(service.get("start")), f"{where}.start is required for a standalone service")
            check(bool(service.get("workdir")), f"{where}.workdir is required for a standalone service")

        health = str(service.get("health", ""))
        check(bool(health), f"{where}.health is required")
        check(health.startswith("/"), f"{where}.health {health!r} must be a path beginning with /")

    check("gateway" in services, "a 'gateway' service is required; it is the common portal")

    # Anything mounted in-process must be reachable through the gateway, so the
    # gateway must actually be a standalone service that can serve it.
    for name, service in services.items():
        if isinstance(service, dict) and service.get("via") == "gateway":
            check("gateway" in services, f"services.{name} routes via the gateway, which is not defined")

    return problems


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _github_get(path: str, token: str | None) -> Any:
    request = urllib.request.Request(f"{GITHUB_API}{path}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "ml-server-deploy-manifest")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ManifestError(
                f"GitHub returned 404 for {path}. Either the ref does not exist, or the "
                f"repository is private and GITHUB_TOKEN lacks read access to it."
            ) from error
        if error.code in (401, 403):
            raise ManifestError(
                f"GitHub returned {error.code} for {path}. Check GITHUB_TOKEN scope, or rate limiting."
            ) from error
        raise ManifestError(f"GitHub returned {error.code} for {path}: {error.reason}") from error
    except urllib.error.URLError as error:
        raise ManifestError(f"cannot reach GitHub for {path}: {error.reason}") from error


def resolve_ref(repo: str, ref: str, token: str | None) -> str:
    """Resolve a tag, branch or SHA to a full commit SHA."""
    if SHA_RE.match(ref):
        return ref
    payload = _github_get(f"/repos/{repo}/commits/{ref}", token)
    sha = payload.get("sha", "")
    if not SHA_RE.match(sha):
        raise ManifestError(f"{repo}@{ref} resolved to an unexpected value: {sha!r}")
    return sha


def resolve(document: dict[str, Any], token: str | None) -> dict[str, Any]:
    """Fill in every service's commit SHA, failing hard on any that cannot resolve."""
    resolved = json.loads(json.dumps(document))  # deep copy without aliasing surprises
    for name, service in resolved.get("services", {}).items():
        repo, ref = service["repo"], str(service["ref"])
        sha = resolve_ref(repo, ref, token)
        declared = str(service.get("commit") or "")
        if declared and declared != sha:
            # An authored commit that disagrees with its tag means the tag was
            # moved.  That is exactly the situation pinning exists to catch.
            raise ManifestError(
                f"services.{name}: manifest pins commit {declared} but {repo}@{ref} is now {sha}. "
                f"The tag has moved. Update the ref or the commit deliberately."
            )
        service["commit"] = sha
        print(f"  resolved {name:<12} {repo}@{ref} -> {sha}", file=sys.stderr)
    return resolved


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ManifestError(f"{path} does not contain a YAML mapping")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--validate", action="store_true", help="structural checks only")
    parser.add_argument("--resolve", action="store_true", help="resolve refs to commit SHAs via the GitHub API")
    parser.add_argument("--json", action="store_true", help="emit JSON for the office-side scripts")
    parser.add_argument("--require-commits", action="store_true", help="demand a full SHA for every service")
    parser.add_argument("--out", type=Path, help="output path (default: stdout)")
    args = parser.parse_args(argv)

    if not (args.validate or args.resolve or args.json):
        args.validate = True

    try:
        document = load(args.manifest)

        problems = validate(document, require_commits=args.require_commits)
        if problems:
            print(f"manifest {args.manifest} has {len(problems)} problem(s):", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print(f"manifest {args.manifest}: schema and structure OK", file=sys.stderr)

        if args.resolve:
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if not token:
                print("  note: no GITHUB_TOKEN set; public repos only, and rate limits are low", file=sys.stderr)
            document = resolve(document, token)
            problems = validate(document, require_commits=True)
            if problems:
                print("resolved manifest failed re-validation:", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
                return 1

        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            if args.json:
                # Keys are NOT sorted: the order services appear in the manifest
                # is the order they are started in, and sorting would silently
                # reshuffle that (gateway would fall after calculator).
                args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            else:
                args.out.write_text(
                    yaml.safe_dump(document, sort_keys=False, default_flow_style=False),
                    encoding="utf-8",
                )
            print(f"wrote {args.out}", file=sys.stderr)
        elif args.json:
            print(json.dumps(document, indent=2))

    except ManifestError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
