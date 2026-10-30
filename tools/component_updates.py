"""What could go into the next suite release: each component's pin against its repository.

For every service in the manifest this prints the ref it is pinned to, the
newest vX.Y.Z tag in its repository, and whether the default branch has moved
past the pinned commit. That answers "is there anything new to deploy?" without
cloning anything, and it is the first step of RELEASING.md.

Only `git ls-remote` is used, so it works wherever git can reach GitHub: a
cloud agent's session (for the repositories that session may read), a
workstation, or CI. A repository it cannot reach is reported, not fatal.

    python tools/component_updates.py              # table
    python tools/component_updates.py --json       # for scripts and agents

Exit status: 0 when every pin is the newest tag and nothing is unreleased,
1 when something newer exists, 2 when a repository could not be read.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manifest import load

VERSION_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def ls_remote(repo: str, timeout: float = 60) -> dict[str, str]:
    """Every ref of a GitHub repository, mapped to the commit it names (tags peeled)."""

    result = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repo}.git"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "git ls-remote failed")
    refs: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in result.stdout.splitlines():
        commit, _, name = line.partition("\t")
        if name.endswith("^{}"):
            peeled[name[:-3]] = commit
        else:
            refs[name] = commit
    refs.update(peeled)
    return refs


def newest_tag(refs: dict[str, str]) -> str | None:
    versions = []
    for name in refs:
        if name.startswith("refs/tags/"):
            match = VERSION_TAG.match(name.removeprefix("refs/tags/"))
            if match:
                versions.append((tuple(int(part) for part in match.groups()), match.group(0)))
    return max(versions)[1] if versions else None


def pinned_commit(refs: dict[str, str], ref: str) -> str | None:
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref
    for candidate in (f"refs/tags/{ref}", f"refs/heads/{ref}"):
        if candidate in refs:
            return refs[candidate]
    return None


def survey(document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name, service in (document.get("services") or {}).items():
        repo, ref = service.get("repo"), str(service.get("ref", ""))
        row: dict[str, Any] = {"service": name, "repo": repo, "pinned": ref}
        if not repo:
            continue
        try:
            refs = ls_remote(repo)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            row["error"] = str(error)
            rows.append(row)
            continue
        head = refs.get("HEAD")
        pin = pinned_commit(refs, ref)
        latest = newest_tag(refs)
        row.update(
            {
                "pinned_commit": pin,
                "newest_tag": latest,
                "newer_tag": bool(latest and _key(latest) > _key(ref)),
                "default_branch_commit": head,
                "unreleased_commits": bool(head and latest and head != refs.get(f"refs/tags/{latest}")),
                "default_branch_moved": bool(head and pin and head != pin),
            }
        )
        rows.append(row)
    return rows


def _key(tag: str) -> tuple[int, ...]:
    match = VERSION_TAG.match(tag)
    return tuple(int(part) for part in match.groups()) if match else (-1,)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parents[1] / "manifest.yml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = survey(load(args.manifest))
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'service':<12} {'pinned':<10} {'newest tag':<11} state")
        for row in rows:
            if "error" in row:
                state = f"cannot read {row['repo']}: {row['error']}"
            else:
                notes = []
                if row["newer_tag"]:
                    notes.append(f"newer tag {row['newest_tag']} to take")
                if row["unreleased_commits"]:
                    notes.append("default branch has commits after the newest tag (release the component first)")
                state = "; ".join(notes) or "up to date"
            print(f"{row['service']:<12} {row['pinned']:<10} {row.get('newest_tag')!s:<11} {state}")
    if any("error" in row for row in rows):
        return 2
    if any(row.get("newer_tag") or row.get("unreleased_commits") for row in rows):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
