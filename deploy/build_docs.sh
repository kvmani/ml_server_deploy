#!/usr/bin/env bash
# build_docs.sh -- build a component's documentation against the ACTIVE release.
#
# The deployment builds documentation itself, as its last step. This script is
# for the two cases where that step did not produce anything:
#
#   * the office mirror had none of the build dependencies at rollout time, and
#     they have since been installed;
#   * the rollout was run with --skip-docs to get the suite back quickly.
#
# It changes no release, no unit and no service. It reads the active release for
# the source tree, writes only into shared/, and swaps the result in when it is
# complete -- so it is safe to run on a live server, and safe to interrupt.
#
#   ./deploy/build_docs.sh                 # every component that declares one
#   ./deploy/build_docs.sh pytex           # just this one
#   ./deploy/build_docs.sh --force pytex   # rebuild even if the stamp matches
#
# Nothing here can take the site down: the running services are not restarted,
# and the workbench picks up the new directory on the next request.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

ROOT_ARG=""
SCOPE_ARG="auto"
FORCE=0
WANTED=()

usage() {
    sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while (( $# )); do
    case "$1" in
        --root) ROOT_ARG="$2"; shift 2 ;;
        --systemd-scope) SCOPE_ARG="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage 0 ;;
        -*) die "unknown option: $1" ;;
        *) WANTED+=("$1"); shift ;;
    esac
done

detect_layout "$ROOT_ARG" "$SCOPE_ARG" 1 || die "cannot determine the deployment layout; pass --root"

CURRENT_LINK="${ML_ROOT}/current"
[[ -L "$CURRENT_LINK" ]] || die "no active release at ${CURRENT_LINK}; deploy the suite first"
ACTIVE_RELEASE="$(readlink -f "$CURRENT_LINK")"
[[ -d "$ACTIVE_RELEASE" ]] || die "the current symlink does not resolve to a directory"

manifest_load "${ACTIVE_RELEASE}/manifest.resolved.json"

log "root:    ${ML_ROOT}"
log "release: ${ACTIVE_RELEASE}"

# The build writes into the shared log when there is one; on this path there is
# not, so give it a file of its own rather than discarding the output an
# operator is running this script precisely in order to read.
mkdir -p "${ML_ROOT}/shared/logs"
ML_LOG_FILE="${ML_ROOT}/shared/logs/build-docs-$(date -u '+%Y%m%dT%H%M%SZ').log"
export ML_LOG_FILE
log "log:     ${ML_LOG_FILE}"

BUILT=0
while read -r id; do
    [[ -n "$id" ]] || continue
    [[ -n "$(mf_or "services.${id}.docs_build.target" '')" ]] || continue
    if (( ${#WANTED[@]} )) && ! printf '%s\n' "${WANTED[@]}" | grep -qx "$id"; then
        continue
    fi
    if (( FORCE )); then
        # Removing the stamp is how a rebuild is requested: docs_build_one
        # decides for itself, and there is no second code path that skips its
        # checks.
        rm -f "${ML_ROOT}/$(mf_or "services.${id}.docs_build.target" '')/.built-from"
    fi
    docs_build_one "$id" "$ACTIVE_RELEASE" || true
    BUILT=$(( BUILT + 1 ))
done < <(service_ids)

if (( BUILT == 0 )); then
    if (( ${#WANTED[@]} )); then
        die "no component named ${WANTED[*]} declares a documentation build"
    fi
    warn "no component in this release declares a documentation build"
fi

say ""
ok "done; the workbench serves the new pages on the next request, with no restart"
