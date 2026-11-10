"""A release a running service still uses is never pruned.

systemd resolves WorkingDirectory=.../current/... when a service starts, so a
service left running across upgrades lives in an older releases/<version>/.
Suite 1.23.0 pruned such a folder and the Online Annotator served blank pages
until restarted. These tests drive the real shell code against a fake /proc.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "deploy" / "lib" / "common.sh"


def _bash(script: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["bash", "-c", f'source "{COMMON}"\n{script}'],
        capture_output=True, text=True, env={**os.environ, **env}, check=True,
    )
    return result.stdout


def _fake_proc(tmp_path: Path) -> tuple[Path, Path]:
    releases = tmp_path / "ml_platform" / "releases"
    for version in ("1.16.0", "1.19.0", "1.20.0", "1.23.0"):
        (releases / version / "apps" / "svc").mkdir(parents=True)
    proc = tmp_path / "proc"
    # 101: started under 1.16.0, whose folder has since been deleted.
    (proc / "101").mkdir(parents=True)
    os.symlink(f"{releases}/1.16.0/apps/OnlineAnnotator (deleted)", proc / "101" / "cwd")
    # 102: started under 1.19.0, still present; also maps a file from it.
    (proc / "102").mkdir()
    os.symlink(f"{releases}/1.19.0/apps/svc", proc / "102" / "cwd")
    (proc / "102" / "maps").write_text(
        f"7f00-7f01 r--p 00000000 08:01 42   {releases}/1.19.0/apps/svc/native.so\n"
        "7f01-7f02 r--p 00000000 08:01 43   /usr/lib/libc.so.6\n",
        encoding="utf-8",
    )
    # 103: restarted by the last update, in the current release.
    (proc / "103").mkdir()
    os.symlink(f"{releases}/1.23.0/apps/svc", proc / "103" / "cwd")
    return releases, proc


def test_the_release_each_process_uses_and_whether_it_still_exists(tmp_path: Path) -> None:
    releases, proc = _fake_proc(tmp_path)
    env = {"ML_PROC_ROOT": str(proc)}
    used = lambda pid: _bash(f'releases_used_by_pid {pid} "{releases}"', env).split("\n")  # noqa: E731
    assert used(101)[0] == "1.16.0\tdeleted"
    assert used(102)[0] == "1.19.0\tpresent" and len([x for x in used(102) if x]) == 1
    assert used(103)[0] == "1.23.0\tpresent"
    assert _bash(f'releases_used_by_pid 999 "{releases}"', env) == ""


def test_releases_in_use_lists_every_running_unit(tmp_path: Path) -> None:
    releases, proc = _fake_proc(tmp_path)
    stubs = '''
service_ids() { printf 'annotator\\ncalculator\\nportal\\npdf_tools\\n'; }
svc() {
    case "$1:$2" in
        pdf_tools:in_process) echo true ;;
        *:in_process) echo false ;;
        annotator:unit) echo ml-platform-annotator.service ;;
        calculator:unit) echo ml-platform-calculator.service ;;
        portal:unit) echo ml-platform-portal.service ;;
    esac
}
unit_main_pid() { case "$1" in *annotator*) echo 101 ;; *calculator*) echo 102 ;; *portal*) echo 103 ;; esac; }
'''
    out = _bash(stubs + f'releases_in_use "{releases}"', {"ML_PROC_ROOT": str(proc)})
    assert out.splitlines() == [
        "1.16.0\tml-platform-annotator.service\t101\tdeleted",
        "1.19.0\tml-platform-calculator.service\t102\tpresent",
        "1.23.0\tml-platform-portal.service\t103\tpresent",
    ]


def _prune_block() -> str:
    text = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    match = re.search(r"# --- B11\. prune old releases.*?(?=# --- B12\.)", text, re.DOTALL)
    assert match, "update.sh no longer has a B11 prune section"
    return match.group(0)


def test_update_prunes_only_releases_nothing_uses(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    for version in ("1.16.0", "1.18.0", "1.19.0", "1.20.0", "1.22.0", "1.23.0"):
        (releases / version).mkdir(parents=True)
    script = f'''
log() {{ echo "LOG $*"; }}
RELEASES_DIR="{releases}"
ML_RELEASE_KEEP=3
TARGET_VERSION=1.23.0
PREVIOUS_VERSION=1.20.0
TARGET_RELEASE="{releases}/1.23.0"
releases_in_use() {{ printf '1.16.0\\tml-platform-annotator.service\\t101\\tpresent\\n'; }}
{_prune_block()}
'''
    out = _bash(script, {})
    left = sorted(p.name for p in releases.iterdir())
    assert left == ["1.16.0", "1.20.0", "1.22.0", "1.23.0"], out
    assert "kept old release 1.16.0: still in use by ml-platform-annotator.service (pid 101)" in out
    assert "pruned old release 1.18.0" in out and "pruned old release 1.19.0" in out
