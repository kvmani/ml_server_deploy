#!/usr/bin/env bash
# rollback.sh -- return the suite to a previously deployed, health-verified release.
#
#   ./rollback.sh                  # back to the last known-good release
#   ./rollback.sh --to 1.3.0       # back to a specific release
#   ./rollback.sh --list           # show what can be rolled back to
#
# Rollback is a symlink swap and a restart. It needs no network, no pip mirror
# and no archive, which matters because a rollback happens exactly when things
# are already going wrong.
#
# It NEVER touches ${ROOT}/shared. Scientific data, models, uploads and
# configuration are deliberately outside every release directory, so going back
# to an older release cannot revert or destroy them. Restoring a data backup is
# a separate, explicit operation (--restore-data) and is almost never wanted.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

TO_VERSION=""
ROOT_ARG=""
SCOPE_ARG="auto"
AUTO=0
LIST=0
RESTORE_DATA=0
RESTORE_DEPS=0
HEALTH_WAIT=90

usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while (( $# )); do
    case "$1" in
        --to)            TO_VERSION="$2"; shift 2 ;;
        --root)          ROOT_ARG="$2"; shift 2 ;;
        --systemd-scope) SCOPE_ARG="$2"; shift 2 ;;
        --health-wait)   HEALTH_WAIT="$2"; shift 2 ;;
        --auto)          AUTO=1; shift ;;
        --list)          LIST=1; shift ;;
        --restore-data)  RESTORE_DATA=1; shift ;;
        --restore-deps)  RESTORE_DEPS=1; shift ;;
        -h|--help)       usage ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

detect_layout "$ROOT_ARG" "$SCOPE_ARG" 1 || die "cannot determine the deployment layout; pass --root"
log_init "${ML_ROOT}/shared/logs" "rollback"

RELEASES_DIR="${ML_ROOT}/releases"
CURRENT_LINK="${ML_ROOT}/current"
CURRENT_VERSION="$(current_version)"

[[ -d "$RELEASES_DIR" ]] || die "no releases directory at ${RELEASES_DIR}; nothing to roll back to"

# ---------------------------------------------------------------------------
# What is available?
# ---------------------------------------------------------------------------

mapfile -t AVAILABLE < <(find "$RELEASES_DIR" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' \
    | grep -v '\.\(incoming\|superseded\)$' | sort -V -r)

if (( LIST )); then
    say ""
    say "Releases present under ${RELEASES_DIR}:"
    say ""
    printf '  %-12s %-10s %-22s %s\n' "VERSION" "ACTIVE" "DEPLOYED" "LAST HEALTH"
    printf '  %-12s %-10s %-22s %s\n' "-------" "------" "--------" "-----------"
    for version in "${AVAILABLE[@]}"; do
        active=""
        [[ "$version" == "$CURRENT_VERSION" ]] && active="<== active"
        record="$(grep -F "\"suite_version\": \"${version}\"" "$(history_file)" 2>/dev/null | tail -1 || true)"
        deployed="$(printf '%s' "$record" | sed -n 's/.*"deployed_utc": "\([^"]*\)".*/\1/p')"
        health="$(printf '%s' "$record" | sed -n 's/.*"health": "\([^"]*\)".*/\1/p')"
        printf '  %-12s %-10s %-22s %s\n' "$version" "$active" "${deployed:-unknown}" "${health:-unknown}"
    done
    say ""
    exit 0
fi

# ---------------------------------------------------------------------------
# Choose a target
# ---------------------------------------------------------------------------

if [[ -z "$TO_VERSION" ]]; then
    # Prefer the newest release the history records as healthy, excluding the
    # one we are rolling away from. Rolling back onto a release that was never
    # verified would simply move the outage.
    TO_VERSION="$(history_last_good "$CURRENT_VERSION" || true)"
    if [[ -n "$TO_VERSION" ]]; then
        log "last known-good release from the deployment history: ${TO_VERSION}"
    else
        warn "no health-verified release in the deployment history"
        for version in "${AVAILABLE[@]}"; do
            if [[ "$version" != "$CURRENT_VERSION" ]]; then
                TO_VERSION="$version"
                warn "falling back to the newest other release present: ${TO_VERSION}"
                break
            fi
        done
    fi
fi

[[ -n "$TO_VERSION" ]] || die "no release to roll back to. Present: ${AVAILABLE[*]:-none}"

TARGET_RELEASE="${RELEASES_DIR}/${TO_VERSION}"
[[ -d "$TARGET_RELEASE" ]] || die "release ${TO_VERSION} is not present at ${TARGET_RELEASE}.
Available: ${AVAILABLE[*]:-none}"
[[ -f "${TARGET_RELEASE}/manifest.resolved.json" ]] || die "release ${TO_VERSION} has no manifest; it is not usable"

if [[ "$TO_VERSION" == "$CURRENT_VERSION" ]]; then
    die "release ${TO_VERSION} is already active; nothing to do"
fi

# ---------------------------------------------------------------------------
# Confirm, unless this is an automatic rollback from a failed update
# ---------------------------------------------------------------------------

say ""
say "==================================================================="
say " Rollback plan"
say "==================================================================="
printf '  root            %s\n' "$ML_ROOT"
printf '  suite version   %s -> %s\n' "${CURRENT_VERSION:-<none>}" "$TO_VERSION"
printf '  persistent data %s (NOT touched)\n' "${ML_ROOT}/shared"
if (( RESTORE_DATA )); then
    printf '  data restore    REQUESTED -- shared data WILL be overwritten from backup\n'
fi
say "==================================================================="
say ""

if (( ! AUTO )) && [[ -t 0 ]]; then
    read -r -p "Proceed with rollback? [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]] || die "cancelled"
fi

acquire_lock

# ---------------------------------------------------------------------------
# Swap
# ---------------------------------------------------------------------------

manifest_load "${TARGET_RELEASE}/manifest.resolved.json"

step "restoring unit files recorded with release ${TO_VERSION}"

UNIT_DIR="$(unit_dir)"
mkdir -p "$UNIT_DIR"

# The unit files that belong to the target release were captured in the
# checkpoint taken just before it was replaced. Prefer those; if they are gone,
# leave the current units alone rather than guessing, and say so.
UNITS_RESTORED=0
RESTORE_FROM="$(find "${ML_ROOT}/backups" -maxdepth 2 -name previous-version -print0 2>/dev/null \
    | xargs -0 -r grep -l "^${TO_VERSION}$" 2>/dev/null | head -1 || true)"
if [[ -n "$RESTORE_FROM" ]]; then
    backup_units="$(dirname "$RESTORE_FROM")/units"
    if [[ -d "$backup_units" ]]; then
        shopt -s nullglob
        for unit_file in "${backup_units}"/*.service; do
            if ! cmp -s "$unit_file" "${UNIT_DIR}/$(basename "$unit_file")"; then
                cp "$unit_file" "${UNIT_DIR}/"
                log "  restored $(basename "$unit_file")"
                UNITS_RESTORED=1
            fi
        done
        shopt -u nullglob
    fi
fi

if (( UNITS_RESTORED )); then
    sctl daemon-reload || warn "daemon-reload failed; continuing"
else
    log "  unit files already match the target release"
fi

step "activating release ${TO_VERSION}"
ln -sfn "$TARGET_RELEASE" "${ML_ROOT}/current.tmp"
mv -Tf "${ML_ROOT}/current.tmp" "$CURRENT_LINK"
ok "current -> releases/${TO_VERSION}"

# ---------------------------------------------------------------------------
# Optional dependency restore.
#
# Not the default: reinstalling packages needs the pip mirror to be reachable,
# and a rollback often happens when something else is already broken. Rolling
# back the code alone is almost always enough, because a newer dependency set is
# normally still compatible with the older application code.
# ---------------------------------------------------------------------------

if (( RESTORE_DEPS )); then
    if [[ -n "$RESTORE_FROM" && -f "$(dirname "$RESTORE_FROM")/freeze.txt" ]]; then
        freeze="$(dirname "$RESTORE_FROM")/freeze.txt"
        step "restoring the recorded dependency set from ${freeze}"
        if "${ML_VENV}/bin/python" -m pip install --disable-pip-version-check -r "$freeze"; then
            ok "dependencies restored"
        else
            warn "dependency restore failed; the code rollback still stands"
        fi
    else
        warn "--restore-deps was given but no freeze.txt was recorded for ${TO_VERSION}"
    fi
fi

# ---------------------------------------------------------------------------
# Optional data restore -- deliberately awkward to reach.
# ---------------------------------------------------------------------------

if (( RESTORE_DATA )); then
    if [[ -z "$RESTORE_FROM" ]]; then
        warn "--restore-data was given but no checkpoint matches ${TO_VERSION}; data left untouched"
    else
        backup_dir="$(dirname "$RESTORE_FROM")"
        warn "restoring shared data from ${backup_dir} -- this overwrites current data"
        shopt -s nullglob
        for db in "${backup_dir}"/*.sqlite3 "${backup_dir}"/*.db; do
            target="${ML_ROOT}/shared/data/$(basename "$db")"
            cp -p "$target" "${target}.before-restore-$(date -u '+%Y%m%dT%H%M%SZ')" 2>/dev/null || true
            cp -p "$db" "$target"
            warn "  restored $(basename "$db")"
        done
        shopt -u nullglob
    fi
fi

# ---------------------------------------------------------------------------
# Restart and verify
# ---------------------------------------------------------------------------

step "restarting services"
RESTART_FAILED=0
while read -r id; do
    [[ "$(svc "$id" in_process false)" == "true" ]] && continue
    unit="$(svc "$id" unit)"
    [[ -n "$unit" ]] || continue
    if ! sctl restart "$unit"; then
        err "failed to restart ${unit}"
        RESTART_FAILED=1
    fi
done < <(service_ids)

sleep 3

step "verifying health after rollback"
if "${SCRIPT_DIR}/health_check.sh" --root "$ML_ROOT" --systemd-scope "$ML_SYSTEMD_SCOPE" --wait "$HEALTH_WAIT"; then
    HEALTH_STATUS="ok"
    ok "rollback to ${TO_VERSION} complete and healthy"
else
    HEALTH_STATUS="failed"
    err "rollback to ${TO_VERSION} completed but health checks FAILED"
    err "the previous release is active; investigate with:"
    err "    ${SCRIPT_DIR}/status.sh --root ${ML_ROOT}"
    err "    journalctl --user -u '${ML_UNIT_PREFIX}*' -n 100"
fi

history_append "$(printf '{"suite_version": "%s", "deployed_utc": "%s", "archive_sha256": "", "archive": "rollback", "previous_version": "%s", "health": "%s", "components": {}}' \
    "$TO_VERSION" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${CURRENT_VERSION:-}" "$HEALTH_STATUS")"

if [[ "$HEALTH_STATUS" != "ok" ]] || (( RESTART_FAILED )); then
    exit 1
fi

say ""
"${SCRIPT_DIR}/status.sh" --root "$ML_ROOT" --systemd-scope "$ML_SYSTEMD_SCOPE" || true
