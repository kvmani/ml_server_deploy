#!/usr/bin/env python3
"""Guard against Windows text artifacts reaching the Ubuntu server.

Development happens on Windows; deployment is Ubuntu.  A shell script that
arrives with CRLF line endings fails there with

    bash: ./update.sh: /usr/bin/env: bad interpreter: No such file or directory

which is one of the least obvious error messages in Unix, and would cost a trip
to the office to diagnose.  Editors, IDEs and some tooling silently rewrite
files to CRLF, so this is not a hypothetical: it happened during development of
this very repository.

Checks, all of them blocking:

* no CR anywhere in a text file that ships;
* no UTF-8 BOM;
* shell scripts start with a shebang;
* no two paths differing only in case (breaks a case-insensitive checkout);
* no absolute Windows paths or developer home paths left in shipped files.

    python tools/check_text_hygiene.py            # check, exit 1 on any problem
    python tools/check_text_hygiene.py --fix      # rewrite CRLF to LF, strip BOMs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TEXT_SUFFIXES = {
    ".sh", ".bats", ".py", ".yml", ".yaml", ".md", ".txt", ".json",
    ".service", ".in", ".template", ".cfg", ".toml", ".gitattributes",
}
TEXT_NAMES = {"VERSION", ".gitattributes", ".gitignore"}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", "dist", "build"}

BOM = b"\xef\xbb\xbf"

# What build_suite.py actually copies into a release archive. Only these travel
# to the office server, so only these are held to the no-developer-paths rule.
SHIPPED_PREFIXES = ("deploy/", "systemd/", "requirements/", "RUNBOOK.md", "manifest.yml")

# Patterns that must never survive into a shipped file.
FORBIDDEN_SUBSTRINGS = [
    (b"C:\\\\", "an absolute Windows path"),
    (b"C:/Users/", "an absolute Windows path"),
    (b"/mnt/c/Users/", "a WSL-mounted development path"),
]


def shipped_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            files.append(path)
    return sorted(files)


def check(fix: bool = False) -> int:
    problems: list[str] = []
    fixed: list[str] = []

    for path in shipped_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        data = path.read_bytes()
        changed = False

        if b"\r" in data:
            if fix:
                data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                changed = True
            else:
                count = data.count(b"\r")
                problems.append(f"{relative}: {count} CR byte(s) -- must be LF only")

        if data.startswith(BOM):
            if fix:
                data = data[len(BOM):]
                changed = True
            else:
                problems.append(f"{relative}: starts with a UTF-8 BOM")

        if path.suffix in {".sh", ".bats"} and not data.startswith(b"#!"):
            problems.append(f"{relative}: shell script has no shebang line")

        # A developer-side path is only a problem in a file that actually
        # travels to the office server. The rehearsal harness and the README
        # legitimately name the Windows mount point; build_suite.py copies only
        # the prefixes in SHIPPED_PREFIXES into a release.
        if any(relative == item or relative.startswith(item) for item in SHIPPED_PREFIXES):
            for needle, description in FORBIDDEN_SUBSTRINGS:
                if needle in data:
                    problems.append(f"{relative}: contains {description} ({needle.decode()!r})")

        if changed:
            path.write_bytes(data)
            fixed.append(relative)

    # Case-collision check: fatal on a case-insensitive checkout.
    lowered: dict[str, str] = {}
    for path in shipped_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        key = relative.lower()
        if key in lowered and lowered[key] != relative:
            problems.append(f"{relative}: differs only in case from {lowered[key]}")
        lowered[key] = relative

    if fixed:
        print(f"normalised {len(fixed)} file(s):")
        for name in fixed:
            print(f"  {name}")

    if problems:
        print(f"\ntext hygiene: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"text hygiene: {len(shipped_files())} file(s) checked, all clean")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fix", action="store_true", help="rewrite CRLF to LF and strip BOMs in place")
    args = parser.parse_args()
    return check(fix=args.fix)


if __name__ == "__main__":
    sys.exit(main())
