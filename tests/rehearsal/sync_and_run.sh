#!/usr/bin/env bash
# sync_and_run.sh -- copy the repository from the Windows working tree into the
# WSL filesystem and run the rehearsal there.
#
# The copy is not optional. Under /mnt/c the scripts cannot create symlinks,
# executable bits are not honoured, and file modes come back as 0777, so the
# atomic `current` symlink swap and the unit-file comparison would both behave
# differently from the office server. Running on the native ext4 filesystem is
# what makes the rehearsal representative.
#
#   wsl -d Ubuntu-24.04 -- bash /mnt/c/.../tests/rehearsal/sync_and_run.sh [scenario...]

set -Eeuo pipefail

WINDOWS_REPO="${WINDOWS_REPO:-/mnt/c/Users/kvman/PycharmProjects/ml_server_deploy}"
LINUX_REPO="${LINUX_REPO:-${HOME}/ml_server_deploy}"

[[ -d "$WINDOWS_REPO" ]] || { echo "cannot find the repository at ${WINDOWS_REPO}" >&2; exit 1; }

echo "syncing ${WINDOWS_REPO} -> ${LINUX_REPO}"
rm -rf "$LINUX_REPO"
mkdir -p "$LINUX_REPO"
# Copy only what the rehearsal needs, and never the Windows .git or caches.
tar -C "$WINDOWS_REPO" -cf - \
    --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='dist' --exclude='.venv' \
    . | tar -C "$LINUX_REPO" -xf -

chmod +x "${LINUX_REPO}/deploy/"*.sh "${LINUX_REPO}/tests/rehearsal/"*.sh

# The line endings must be LF or nothing will run; check rather than assume.
if grep -rlq $'\r' "${LINUX_REPO}/deploy/" 2>/dev/null; then
    echo "ERROR: CRLF line endings found in deploy/ after sync." >&2
    echo "Run: python tools/check_text_hygiene.py --fix" >&2
    exit 1
fi

exec "${LINUX_REPO}/tests/rehearsal/run.sh" "$@"
