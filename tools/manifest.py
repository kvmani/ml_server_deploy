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
VALID_SEED_KINDS = {"file", "tree"}
# Generators update.sh knows how to run when no source for a seed exists.
VALID_GENERATORS = {"", "service_urls"}
VALID_SEVERITIES = {"fail", "warn"}
ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
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
    seen_url_vars: dict[str, str] = {}
    declared_env_file = str(runtime.get("env_file") or "")

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

            # An environment file is rendered into the unit as a strict
            # EnvironmentFile=, so a service that cannot start without it must
            # point at the one place update.sh actually seeds. Two services
            # quietly loading two different files is the sort of drift that is
            # only ever discovered from a support call.
            env_file = service.get("env_file")
            if env_file is not None:
                check(
                    isinstance(env_file, str) and bool(env_file),
                    f"{where}.env_file must be a non-empty path",
                )
                if isinstance(env_file, str):
                    check(
                        not env_file.startswith("/") or env_file == declared_env_file,
                        f"{where}.env_file {env_file!r} is an absolute path outside the deployment root",
                    )
                    check(
                        env_file.startswith("/") or env_file.startswith("shared/"),
                        f"{where}.env_file {env_file!r} must live under shared/ so it survives upgrades",
                    )
                    if declared_env_file:
                        check(
                            env_file == declared_env_file,
                            f"{where}.env_file {env_file!r} disagrees with runtime.env_file "
                            f"{declared_env_file!r}",
                        )

            url_var = service.get("public_url_env")
            if url_var is not None:
                check(
                    isinstance(url_var, str) and bool(ENV_VAR_RE.match(str(url_var))),
                    f"{where}.public_url_env {url_var!r} is not an UPPER_SNAKE environment variable name",
                )
                if url_var in seen_url_vars:
                    problems.append(
                        f"{where}.public_url_env {url_var!r} collides with services.{seen_url_vars[str(url_var)]}"
                    )
                else:
                    seen_url_vars[str(url_var)] = name

        health = str(service.get("health", ""))
        check(bool(health), f"{where}.health is required")
        check(health.startswith("/"), f"{where}.health {health!r} must be a path beginning with /")

    check("gateway" in services, "a 'gateway' service is required; it is the common portal")

    problems.extend(_validate_seeds(document))
    problems.extend(_validate_verify(document, services))
    problems.extend(_validate_docs_build(document, services))

    # Anything mounted in-process must be reachable through the gateway, so the
    # gateway must actually be a standalone service that can serve it.
    for name, service in services.items():
        if isinstance(service, dict) and service.get("via") == "gateway":
            check("gateway" in services, f"services.{name} routes via the gateway, which is not defined")

    return problems


def _validate_docs_build(document: dict[str, Any], services: dict[str, Any]) -> list[str]:
    """Check every `docs_build` block.

    The build runs on the office server, after a deployment has already
    succeeded, so a mistake here surfaces at the worst possible moment: on a
    host with no network, in a step nobody is watching because the rollout has
    been declared done. Every property that can be checked from the manifest is
    therefore checked here instead.
    """

    problems: list[str] = []
    shared_dirs = set(document.get("shared_dirs") or [])

    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        spec = service.get("docs_build")
        if spec is None:
            continue
        where = f"services.{name}.docs_build"

        if not isinstance(spec, dict):
            problems.append(f"{where} must be a mapping")
            continue

        target = str(spec.get("target") or "")
        if not target:
            problems.append(f"{where}.target is required")
        elif not target.startswith("shared/"):
            # The whole point is that the build outlives the release that
            # produced it. A target inside one would be rebuilt on every
            # deployment and lost on every prune.
            problems.append(f"{where}.target {target!r} must be under shared/")
        elif target.split("/")[1] not in shared_dirs:
            problems.append(
                f"{where}.target {target!r} is not under any directory in shared_dirs; "
                f"nothing would create it"
            )

        command = str(spec.get("command") or "")
        if not command:
            problems.append(f"{where}.command is required")
        elif "{target}" not in command:
            # Without it the command would write wherever it defaults to, which
            # for PyTex is inside the release tree -- the exact thing target
            # exists to avoid, and it would appear to work.
            problems.append(f"{where}.command must pass the build its destination via {{target}}")

        if not str(spec.get("marker") or ""):
            problems.append(
                f"{where}.marker is required; without a file that proves the build "
                f"finished, an interrupted one is indistinguishable from a good one"
            )

        timeout = spec.get("timeout_seconds", 0)
        if not isinstance(timeout, int) or timeout <= 0:
            problems.append(f"{where}.timeout_seconds must be a positive integer")

        requirements = spec.get("requirements") or []
        if not isinstance(requirements, list) or not all(isinstance(i, str) for i in requirements):
            problems.append(f"{where}.requirements must be a list of pip arguments")

        if not str(spec.get("why") or "").strip():
            problems.append(f"{where} must explain itself in `why`")

        # The service has to be able to find what was built, or the build is
        # tens of minutes of work nothing reads.
        environment = service.get("environment") or {}
        pointer = "{root}/" + target
        if pointer not in environment.values():
            problems.append(
                f"{where}.target is not referenced by any environment variable of "
                f"services.{name}; the service would never find the build. Expected "
                f"one variable set to {pointer!r}."
            )

    return problems


def _validate_seeds(document: dict[str, Any]) -> list[str]:
    """Check the `seeds` section.

    These entries are the difference between a deployment that works and one
    that comes up empty, and a typo in a target path fails silently -- update.sh
    would happily seed a directory nothing reads.  So the shape is checked here,
    on the development machine, rather than discovered in a maintenance window.
    """
    problems: list[str] = []
    seeds = document.get("seeds") or []
    if not isinstance(seeds, list):
        return ["seeds must be a list"]

    shared_dirs = {str(item) for item in (document.get("shared_dirs") or [])}
    seen: dict[str, int] = {}

    for index, entry in enumerate(seeds):
        where = f"seeds[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping")
            continue

        seed_id = str(entry.get("id") or "")
        if not seed_id:
            problems.append(f"{where}.id is required")
        elif seed_id in seen:
            problems.append(f"{where}.id {seed_id!r} duplicates seeds[{seen[seed_id]}]")
        else:
            seen[seed_id] = index

        kind = str(entry.get("kind") or "file")
        if kind not in VALID_SEED_KINDS:
            problems.append(f"{where}.kind must be one of {sorted(VALID_SEED_KINDS)}, got {kind!r}")

        target = str(entry.get("target") or "")
        if not target:
            problems.append(f"{where}.target is required")
        elif not target.startswith("shared/"):
            # Anywhere else is inside a release directory, which the next
            # upgrade replaces -- so seeding it would silently lose the data.
            problems.append(f"{where}.target {target!r} must be under shared/")
        elif target.split("/")[1] not in shared_dirs:
            problems.append(
                f"{where}.target {target!r} is not under any directory in shared_dirs; "
                f"nothing would create it"
            )

        if kind == "file" and entry.get("marker"):
            problems.append(f"{where}.marker applies to a tree, not to a file")

        generate = str(entry.get("generate") or "")
        if generate not in VALID_GENERATORS:
            problems.append(f"{where}.generate must be one of {sorted(VALID_GENERATORS - {''})}, got {generate!r}")

        sources = entry.get("sources") or []
        if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
            problems.append(f"{where}.sources must be a list of paths")
            sources = []
        if not sources and not generate:
            problems.append(f"{where} has neither sources nor a generator, so it can never be satisfied")

        # A required seed with no generator can only ever be satisfied from a
        # source, and preflight refuses the whole deployment when it is not
        # there.  Declaring one with no `why` leaves the operator holding a
        # refusal and no idea what to put where.
        if entry.get("required") and not str(entry.get("why") or "").strip():
            problems.append(f"{where} is required, so it must explain itself in `why`")

    return problems


def _validate_verify(document: dict[str, Any], services: dict[str, Any]) -> list[str]:
    """Check the `verify` section: the post-deployment assertions."""
    problems: list[str] = []
    verify = document.get("verify") or {}
    if not isinstance(verify, dict):
        return ["verify must be a mapping"]

    catalog = verify.get("catalog") or {}
    if catalog:
        if not isinstance(catalog, dict):
            problems.append("verify.catalog must be a mapping")
        else:
            path = str(catalog.get("path") or "")
            if not path.startswith("/"):
                problems.append(f"verify.catalog.path {path!r} must be a path beginning with /")
            reject = catalog.get("reject") or []
            if not isinstance(reject, list) or not reject:
                problems.append("verify.catalog.reject must be a non-empty list")

    journal = verify.get("journal") or []
    if not isinstance(journal, list):
        return problems + ["verify.journal must be a list"]

    for index, entry in enumerate(journal):
        where = f"verify.journal[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} must be a mapping")
            continue

        service = str(entry.get("service") or "")
        if service not in services:
            problems.append(f"{where}.service {service!r} is not a service in this manifest")
        elif services[service].get("in_process"):
            # No unit of its own, so no journal of its own.
            problems.append(f"{where}.service {service!r} runs in-process and has no journal")

        severity = str(entry.get("severity") or "fail")
        if severity not in VALID_SEVERITIES:
            problems.append(f"{where}.severity must be one of {sorted(VALID_SEVERITIES)}, got {severity!r}")

        forbid = entry.get("forbid") or []
        if isinstance(forbid, str):
            forbid = [forbid]
        if not isinstance(forbid, list) or not forbid:
            problems.append(f"{where}.forbid must list at least one line that must not appear")
        elif any("|" in str(item) for item in forbid):
            # The shell side joins these with a pipe to cross the process
            # boundary, so one inside a pattern would split it in half.
            problems.append(f"{where}.forbid may not contain '|'")

        lines = entry.get("lines", 200)
        if not isinstance(lines, int) or lines <= 0:
            problems.append(f"{where}.lines must be a positive integer")

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
