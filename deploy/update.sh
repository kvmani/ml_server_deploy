#!/usr/bin/env bash
# update.sh -- install or upgrade the ML Server suite from a transferred archive.
#
# This never contacts GitHub. Application code comes entirely from the archive;
# third-party dependencies come from the office's configured pip mirror.
#
#   ./update.sh /path/to/ml-server-suite-v1.4.0.tar.gz
#   ./update.sh <archive> --dry-run          # preflight and plan only, no changes
#   ./update.sh <archive> --root /home/kvmani/ml_platform_staging --port-offset 100
#
# The script is in two halves. Phase A verifies everything and prints a plan
# while changing NOTHING, so it is safe to run against production at any time.
# Phase B mutates, in an order chosen so that every step before the atomic
# symlink swap is reversible by doing nothing at all.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

ARCHIVE=""
ROOT_ARG=""
SCOPE_ARG="auto"
PORT_OFFSET=0
DRY_RUN=0
FORCE=0
ADOPT_LEGACY=0
SKIP_CHECKSUM=0
NO_RESTART=0
NO_DEPS=0
HEALTH_WAIT=90
EXTRA_INDEX_URLS=()
EXTRA_INDEX_SET=0

usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while (( $# )); do
    case "$1" in
        --root)          ROOT_ARG="$2"; shift 2 ;;
        --systemd-scope) SCOPE_ARG="$2"; shift 2 ;;
        --port-offset)   PORT_OFFSET="$2"; shift 2 ;;
        --health-wait)   HEALTH_WAIT="$2"; shift 2 ;;
        --extra-index-url) EXTRA_INDEX_URLS+=("$2"); EXTRA_INDEX_SET=1; shift 2 ;;
        --dry-run)       DRY_RUN=1; shift ;;
        --force)         FORCE=1; shift ;;
        --adopt-legacy)  ADOPT_LEGACY=1; shift ;;
        --no-restart)    NO_RESTART=1; shift ;;
        --no-deps)       NO_DEPS=1; shift ;;
        --i-know-the-checksum-is-unverified) SKIP_CHECKSUM=1; shift ;;
        -h|--help)       usage ;;
        -*)              die "unknown option: $1 (try --help)" ;;
        *)               [[ -z "$ARCHIVE" ]] || die "more than one archive given"; ARCHIVE="$1"; shift ;;
    esac
done

[[ -n "$ARCHIVE" ]] || die "no archive given. Usage: ./update.sh <archive.tar.gz>"

require_cmd tar sha256sum python3 find sed awk flock curl

# ===========================================================================
# PHASE A -- preflight. Nothing below this line changes the system.
# ===========================================================================

step "Phase A: preflight (no changes will be made)"

# --- A1. the archive itself ------------------------------------------------

[[ -f "$ARCHIVE" ]] || die "archive not found: ${ARCHIVE}"
ARCHIVE="$(cd "$(dirname "$ARCHIVE")" && pwd)/$(basename "$ARCHIVE")"
ARCHIVE_SIZE="$(stat -c %s "$ARCHIVE")"
log "archive: ${ARCHIVE} ($(human_bytes "$ARCHIVE_SIZE"))"

ARCHIVE_SHA="$(sha256sum "$ARCHIVE" | cut -d' ' -f1)"
log "archive sha256: ${ARCHIVE_SHA}"

if (( SKIP_CHECKSUM )); then
    warn "checksum verification SKIPPED at operator request"
else
    SIDECAR=""
    for candidate in "${ARCHIVE}.sha256" "${ARCHIVE%.tar.gz}.sha256"; do
        [[ -f "$candidate" ]] && { SIDECAR="$candidate"; break; }
    done
    [[ -n "$SIDECAR" ]] || die "no checksum file beside the archive (expected ${ARCHIVE}.sha256).
Transfer it along with the archive, or re-run with --i-know-the-checksum-is-unverified."
    EXPECTED_SHA="$(awk '{print $1}' "$SIDECAR" | head -1)"
    if [[ "$EXPECTED_SHA" != "$ARCHIVE_SHA" ]]; then
        err "checksum MISMATCH -- the archive is corrupt or was tampered with"
        err "  expected ${EXPECTED_SHA}"
        err "  actual   ${ARCHIVE_SHA}"
        die "refusing to deploy a corrupt archive"
    fi
    ok "checksum verified against ${SIDECAR##*/}"
fi

# --- A2. archive structure and path-traversal safety -----------------------

# A tar member with an absolute path or a `..` component can write outside the
# release directory. Nothing legitimate produces one, so any occurrence is
# treated as fatal rather than filtered out.
log "inspecting archive members"
MEMBERS="$(tar -tzf "$ARCHIVE")" || die "archive is not a readable gzip tar (truncated download?)"
[[ -n "$MEMBERS" ]] || die "archive is empty"

while IFS= read -r member; do
    case "$member" in
        /*) die "unsafe archive: member has an absolute path: ${member}" ;;
    esac
    # Wrapping in slashes lets one pattern catch a leading `../`, an embedded
    # `/../` and a trailing `/..`, while still accepting an ordinary filename
    # that merely contains two dots, such as `notes..txt`.
    case "/${member}/" in
        */../*) die "unsafe archive: member escapes the archive root: ${member}" ;;
    esac
done <<<"$MEMBERS"

# Extracted with parameter expansion rather than `printf ... | head -1 | cut`.
# A real suite archive holds several thousand members, and `head -1` closes the
# pipe as soon as it has the first line, killing printf with SIGPIPE (exit 141).
# Under `set -o pipefail` that aborts the deployment. The fixture archives used
# in the rehearsal were small enough that printf finished first, so this only
# appeared against a full-size release.
FIRST_MEMBER="${MEMBERS%%$'\n'*}"
ARCHIVE_PREFIX="${FIRST_MEMBER%%/*}"
[[ -n "$ARCHIVE_PREFIX" ]] || die "archive has no top-level directory"
STRAY="$(printf '%s\n' "$MEMBERS" | cut -d/ -f1 | sort -u | grep -v "^${ARCHIVE_PREFIX}$" || true)"
[[ -z "$STRAY" ]] || die "archive has more than one top-level directory: ${ARCHIVE_PREFIX} and ${STRAY}"
ok "archive structure is safe (single root: ${ARCHIVE_PREFIX})"

# --- A3. manifest, read straight out of the archive ------------------------

STAGE_MANIFEST="$(mktemp)"
on_cleanup "rm -f '${STAGE_MANIFEST}'"
tar -xzOf "$ARCHIVE" "${ARCHIVE_PREFIX}/manifest.resolved.json" >"$STAGE_MANIFEST" 2>/dev/null \
    || die "archive contains no ${ARCHIVE_PREFIX}/manifest.resolved.json -- is this an ML Server suite archive?"

manifest_load "$STAGE_MANIFEST"
TARGET_VERSION="$(mf '.suite_version')"
[[ -n "$TARGET_VERSION" ]] || die "manifest has no suite_version"
ok "manifest schema ${ML_MANIFEST_SCHEMA}, suite version ${TARGET_VERSION}"

ARCHIVE_VERSION="$(tar -xzOf "$ARCHIVE" "${ARCHIVE_PREFIX}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
if [[ -n "$ARCHIVE_VERSION" && "$ARCHIVE_VERSION" != "$TARGET_VERSION" ]]; then
    die "archive VERSION (${ARCHIVE_VERSION}) disagrees with manifest suite_version (${TARGET_VERSION})"
fi

# Every service must carry a real commit SHA; a release built without pinning
# is not reproducible and must not be installed.
while read -r id; do
    commit="$(svc "$id" commit)"
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || die "services.${id} has no valid commit SHA in the released manifest"
done < <(service_ids)
ok "all components carry immutable commit SHAs"

# --- A4. platform ----------------------------------------------------------

WANT_OS="$(mf_or 'platform.os_id' ubuntu)"
WANT_OS_MIN="$(mf_or 'platform.os_version_min' 22.04)"
WANT_PY_MIN="$(mf_or 'platform.python_min' 3.12)"

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "$WANT_OS" ]] || die "this host is '${ID:-unknown}' but the suite requires ${WANT_OS}"
    if [[ "$(version_cmp "${VERSION_ID:-0}" "$WANT_OS_MIN")" == "-1" ]]; then
        die "this host is ${WANT_OS} ${VERSION_ID}, but the suite requires ${WANT_OS_MIN} or newer"
    fi
    ok "platform: ${PRETTY_NAME:-${ID} ${VERSION_ID}}"
else
    warn "/etc/os-release is unreadable; cannot verify the platform"
fi

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3; do
    if have_cmd "$candidate"; then
        candidate_version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0)"
        if [[ "$(version_cmp "$candidate_version" "$WANT_PY_MIN")" != "-1" ]]; then
            PYTHON_BIN="$(command -v "$candidate")"
            ok "python: ${PYTHON_BIN} (${candidate_version} >= ${WANT_PY_MIN})"
            break
        fi
    fi
done
[[ -n "$PYTHON_BIN" ]] || die "no python >= ${WANT_PY_MIN} found; install python${WANT_PY_MIN} from the office apt mirror"

# --- A5. layout ------------------------------------------------------------

detect_layout "$ROOT_ARG" "$SCOPE_ARG" "$ADOPT_LEGACY" || exit $?
print_layout

RELEASES_DIR="${ML_ROOT}/releases"
TARGET_RELEASE="${RELEASES_DIR}/${TARGET_VERSION}"
CURRENT_LINK="${ML_ROOT}/current"
CURRENT_VERSION="$(current_version)"

# Preflight logs to a temporary file. Creating ${ROOT}/shared/logs here would
# mean that --dry-run, and every refused deployment, had already modified the
# server -- which would make "Phase A changes nothing" untrue and unprovable.
# The log is promoted into shared/logs only once Phase B begins.
ML_LOG_FILE="$(mktemp -t ml-update-XXXXXXXX.log)"
PREFLIGHT_LOG="$ML_LOG_FILE"
on_cleanup "rm -f '${PREFLIGHT_LOG}'"

promote_log() {
    local dir="${ML_ROOT}/shared/logs"
    mkdir -p "$dir" || return 0
    local target
    target="${dir}/update-${TARGET_VERSION}-$(date -u '+%Y%m%dT%H%M%SZ').log"
    cp "$PREFLIGHT_LOG" "$target" 2>/dev/null || return 0
    ML_LOG_FILE="$target"
    log "log file: ${ML_LOG_FILE}"
}

if [[ -n "$CURRENT_VERSION" ]]; then
    log "currently active suite version: ${CURRENT_VERSION}"
else
    log "no suite is currently active; this will be a fresh install"
fi

# --- A6. idempotency -------------------------------------------------------

REDEPLOY_SAME=0
if [[ -n "$CURRENT_VERSION" ]] && version_eq "$CURRENT_VERSION" "$TARGET_VERSION"; then
    RECORDED_SHA="$(cat "${TARGET_RELEASE}/.archive-sha256" 2>/dev/null || echo "")"
    if [[ "$RECORDED_SHA" == "$ARCHIVE_SHA" ]]; then
        REDEPLOY_SAME=1
        log "version ${TARGET_VERSION} is already active and was installed from this exact archive"
        log "this run will re-verify and re-check health without reinstalling anything"
    elif (( ! FORCE )); then
        die "version ${TARGET_VERSION} is already active but was installed from a DIFFERENT archive
  installed from: ${RECORDED_SHA:-unknown}
  this archive:   ${ARCHIVE_SHA}
Re-run with --force to overwrite it, after confirming which build you want."
    fi
elif [[ -n "$CURRENT_VERSION" ]] && version_gt "$CURRENT_VERSION" "$TARGET_VERSION" && (( ! FORCE )); then
    die "refusing to downgrade from ${CURRENT_VERSION} to ${TARGET_VERSION}; use rollback.sh, or --force if you mean it"
fi

# --- A7. disk --------------------------------------------------------------

# Three times the archive: the extracted tree, the archive being extracted, and
# headroom for the dependency install.
if (( ! REDEPLOY_SAME )); then
    disk_free_at_least "$ML_ROOT" $(( ARCHIVE_SIZE * 3 )) || die "free some space and retry"
fi

# --- A8. ports -------------------------------------------------------------

PORT_PROBLEMS=0
while read -r id; do
    [[ "$(svc "$id" in_process false)" == "true" ]] && continue
    port="$(svc "$id" port)"
    [[ -n "$port" ]] || continue
    port=$(( port + PORT_OFFSET ))
    listener="$(port_listener "$port")"
    if [[ -z "$listener" ]]; then
        continue
    fi
    unit="$(svc "$id" unit)"
    if unit_is_active "$unit" 2>/dev/null; then
        log "port ${port} is held by our own ${unit}, which will be restarted"
    else
        err "port ${port} (service ${id}) is in use by something that is not our service:"
        err "    ${listener}"
        PORT_PROBLEMS=$(( PORT_PROBLEMS + 1 ))
    fi
done < <(service_ids)
(( PORT_PROBLEMS == 0 )) || die "${PORT_PROBLEMS} port conflict(s); free them or use --port-offset"
ok "no port conflicts"

# --- A9. systemd session ---------------------------------------------------

if [[ "$ML_SYSTEMD_SCOPE" == "user" ]]; then
    ensure_user_bus || die "the user systemd session is unavailable, so services cannot be managed.
Run:  loginctl enable-linger $(id -un)
then log out and back in, or start the session with:  systemctl --user start default.target"
    if ! linger_enabled; then
        warn "linger is NOT enabled for $(id -un); services will stop when you log out"
        warn "enable it with: loginctl enable-linger $(id -un)"
    fi
elif [[ "$ML_SYSTEMD_SCOPE" == "system" ]]; then
    have_sudo || die "systemd scope is 'system' but passwordless sudo is unavailable"
fi

# --- A9b. prerequisites this script will not install for you ---------------
#
# Everything that needs a manual install is detected here, in preflight, and
# reported together. A deployment that stops one missing package at a time,
# during a maintenance window, is a bad experience; this collects the system
# packages and the hand-installed python packages in one pass and prints a
# single list with the commands to fix all of them.

step "A9b. checking prerequisites"

check_system_requirements

PREINSTALLED=()
while read -r package; do
    [[ -n "$package" ]] && PREINSTALLED+=("$package")
done < <(mf 'pip.preinstalled' 2>/dev/null || true)

if (( ${#PREINSTALLED[@]} )); then
    if [[ -x "${ML_VENV}/bin/python" ]]; then
        # The release's own pinned list, read out of the archive for the
        # version-comparison note only.
        STAGE_RESOLVED="$(mktemp)"
        on_cleanup "rm -f '${STAGE_RESOLVED}'"
        tar -xzOf "$ARCHIVE" "${ARCHIVE_PREFIX}/requirements/resolved.txt" >"$STAGE_RESOLVED" 2>/dev/null || true
        check_preinstalled "${ML_VENV}/bin/python" "$STAGE_RESOLVED" "${PREINSTALLED[@]}"
    else
        warn "the environment ${ML_VENV} does not exist yet and will be created"
        warn "these package(s) must then be installed into it by hand: ${PREINSTALLED[*]}"
        warn "the deployment will stop and say so if they are still missing"
    fi
fi

report_missing_prerequisites "${ML_VENV}/bin/python" \
    || die "cannot proceed until the prerequisites above are installed"
ok "all prerequisites present"

# --- A9c. units already owned by another deployment ------------------------
#
# Unit names come from the manifest, so deploying a second root on the same host
# claims the same units. That is permitted, but it stops the other deployment's
# services, so it is announced here rather than happening silently.

ALL_UNITS=()
while read -r id; do
    [[ "$(svc "$id" in_process false)" == "true" ]] && continue
    unit="$(svc "$id" unit)"
    [[ -n "$unit" ]] && ALL_UNITS+=("$unit")
done < <(service_ids)

if (( ${#ALL_UNITS[@]} )); then
    check_unit_takeover "$(unit_dir)" "$ML_ROOT" "${ALL_UNITS[@]}"
fi

# --- A10. the plan ---------------------------------------------------------

CHANGED_SERVICES=()
OLD_MANIFEST="${CURRENT_LINK}/manifest.resolved.json"

service_commit_changed() {
    local id="$1" old_commit new_commit
    [[ -f "$OLD_MANIFEST" ]] || return 0    # fresh install: everything is new
    new_commit="$(svc "$id" commit)"
    old_commit="$(ML_MANIFEST="$OLD_MANIFEST" mf "services.${id}.commit" 2>/dev/null || echo "")"
    [[ "$old_commit" != "$new_commit" ]]
}

say ""
say "==================================================================="
say " Deployment plan"
say "==================================================================="
printf '  root            %s  (%s layout, systemd --%s)\n' "$ML_ROOT" "$ML_LAYOUT" "$ML_SYSTEMD_SCOPE"
printf '  suite version   %s -> %s\n' "${CURRENT_VERSION:-<none>}" "$TARGET_VERSION"
printf '  archive         %s\n' "${ARCHIVE##*/}"
(( PORT_OFFSET )) && printf '  port offset     +%s\n' "$PORT_OFFSET"
say ""
printf '  %-12s %-10s %-42s %s\n' "COMPONENT" "STATUS" "COMMIT" "ACTION"
printf '  %-12s %-10s %-42s %s\n' "---------" "------" "------" "------"

while read -r id; do
    new_commit="$(svc "$id" commit)"
    if service_commit_changed "$id"; then
        status="changed"
        CHANGED_SERVICES+=("$id")
    else
        status="unchanged"
    fi
    if [[ "$(svc "$id" in_process false)" == "true" ]]; then
        action="in-process (gateway)"
    elif [[ "$status" == "changed" ]]; then
        action="restart $(svc "$id" unit)"
    else
        action="leave running"
    fi
    printf '  %-12s %-10s %-42s %s\n' "$id" "$status" "$new_commit" "$action"
done < <(service_ids)

# A change to anything mounted in-process is a change to the gateway process.
for id in "${CHANGED_SERVICES[@]}"; do
    if [[ "$(svc "$id" via '')" == "gateway" ]]; then
        if [[ " ${CHANGED_SERVICES[*]} " != *" gateway "* ]]; then
            CHANGED_SERVICES+=("gateway")
            log "gateway will also restart because in-process component '${id}' changed"
        fi
    fi
done

say ""
if (( REDEPLOY_SAME )); then
    say "  This archive is already deployed. The run will verify and re-check health only."
elif (( ${#CHANGED_SERVICES[@]} == 0 )); then
    say "  No component changed. Units will be re-rendered and health re-checked."
else
    say "  Services to restart: ${CHANGED_SERVICES[*]}"
fi
if (( ${#ML_TAKEOVER_UNITS[@]} )); then
    say ""
    say "  !! TAKING OVER ${#ML_TAKEOVER_UNITS[@]} service(s) from another deployment:"
    for entry in "${ML_TAKEOVER_UNITS[@]}"; do
        say "       ${entry%%$'\t'*}   (currently serving ${entry##*$'\t'})"
    done
    say "     They will be stopped and restarted against this release."
fi

say ""
say "  Persistent data in ${ML_ROOT}/shared is NOT touched by this update."
say "==================================================================="
say ""

if (( DRY_RUN )); then
    ok "--dry-run: preflight passed, nothing was changed"
    exit 0
fi

# ===========================================================================
# PHASE B -- mutate.
# ===========================================================================

step "Phase B: applying the update"

mkdir -p "$ML_ROOT"
promote_log
acquire_lock

ROLLBACK_NEEDED=0
PREVIOUS_VERSION="$CURRENT_VERSION"

fail_and_rollback() {
    local reason="$1"
    err "$reason"
    if (( ROLLBACK_NEEDED )) && [[ -n "$PREVIOUS_VERSION" ]]; then
        warn "rolling back to ${PREVIOUS_VERSION}"
        release_lock
        if "${SCRIPT_DIR}/rollback.sh" --auto --root "$ML_ROOT" --systemd-scope "$ML_SYSTEMD_SCOPE" --to "$PREVIOUS_VERSION"; then
            err "rolled back to ${PREVIOUS_VERSION}; the failed release remains at ${TARGET_RELEASE} for diagnosis"
        else
            err "AUTOMATIC ROLLBACK FAILED -- manual intervention required"
            err "  previous release: ${RELEASES_DIR}/${PREVIOUS_VERSION}"
            err "  restore with: ln -sfn '${RELEASES_DIR}/${PREVIOUS_VERSION}' '${ML_ROOT}/current.tmp' && mv -Tf '${ML_ROOT}/current.tmp' '${ML_ROOT}/current'"
        fi
    else
        err "no activation happened, so the previous state is intact"
    fi
    [[ -n "$ML_LOG_FILE" ]] && err "log: ${ML_LOG_FILE}"
    exit 1
}

# --- B1. directory skeleton ------------------------------------------------

mkdir -p "$RELEASES_DIR" "${ML_ROOT}/backups" "${ML_ROOT}/deployment_inbox"
while read -r dir; do
    [[ -n "$dir" ]] || continue
    mkdir -p "${ML_ROOT}/shared/${dir}"
done < <(mf '.shared_dirs')
ok "directory skeleton present"

# --- B2. checkpoint --------------------------------------------------------

STAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
BACKUP_DIR="${ML_ROOT}/backups/${STAMP}"

if (( ! REDEPLOY_SAME )); then
    step "B2. checkpoint -> backups/${STAMP}"
    mkdir -p "$BACKUP_DIR"
    printf '%s\n' "${CURRENT_VERSION:-none}" >"${BACKUP_DIR}/previous-version"
    [[ -f "$OLD_MANIFEST" ]] && cp -p "$OLD_MANIFEST" "${BACKUP_DIR}/manifest.resolved.json"

    if [[ -x "${ML_VENV}/bin/python" ]]; then
        "${ML_VENV}/bin/python" -m pip freeze >"${BACKUP_DIR}/freeze.txt" 2>/dev/null || true
        log "recorded $(wc -l <"${BACKUP_DIR}/freeze.txt" 2>/dev/null || echo 0) installed package(s)"
    fi

    UNIT_DIR="$(unit_dir)"
    if [[ -d "$UNIT_DIR" ]]; then
        mkdir -p "${BACKUP_DIR}/units"
        find "$UNIT_DIR" -maxdepth 1 -name "${ML_UNIT_PREFIX}*.service" -exec cp -p {} "${BACKUP_DIR}/units/" \; 2>/dev/null || true
    fi

    # sqlite must be copied with .backup, not cp: copying a live database with a
    # write-ahead log yields a torn file that looks fine until it is opened.
    shopt -s nullglob
    for db in "${ML_ROOT}"/shared/data/*.sqlite3 "${ML_ROOT}"/shared/data/*.db; do
        target="${BACKUP_DIR}/$(basename "$db")"
        if have_cmd sqlite3; then
            sqlite3 "$db" ".backup '${target}'" 2>/dev/null && log "backed up $(basename "$db") via sqlite3 .backup" \
                || { cp -p "$db" "$target"; warn "sqlite3 .backup failed for $(basename "$db"); used cp"; }
        else
            cp -p "$db" "$target"
            warn "sqlite3 is not installed; $(basename "$db") copied with cp, which is not safe against a live writer"
            warn "install it from the apt mirror: sudo apt-get install -y sqlite3"
        fi
    done
    shopt -u nullglob
    ok "checkpoint written"

    # Prune old checkpoints.
    mapfile -t OLD_BACKUPS < <(find "${ML_ROOT}/backups" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | sort -r | tail -n +$(( ML_BACKUP_KEEP + 1 )))
    for old in "${OLD_BACKUPS[@]}"; do
        rm -rf "${ML_ROOT}/backups/${old}"
        log "pruned old checkpoint ${old}"
    done
fi

# --- B3. extract -----------------------------------------------------------

if (( REDEPLOY_SAME )); then
    log "B3. skipping extraction; ${TARGET_RELEASE} already holds this archive"
else
    step "B3. extracting release ${TARGET_VERSION}"
    INCOMING="${RELEASES_DIR}/${TARGET_VERSION}.incoming"

    # A previous run killed mid-extract leaves this behind. It is always safe to
    # discard: nothing is ever activated from .incoming.
    if [[ -d "$INCOMING" ]]; then
        warn "removing stale ${INCOMING} from an interrupted run"
        rm -rf "$INCOMING"
    fi
    mkdir -p "$INCOMING"
    on_cleanup "rm -rf '${INCOMING}' 2>/dev/null || true"

    tar -xzf "$ARCHIVE" -C "$INCOMING" --strip-components=1 \
        || fail_and_rollback "extraction failed"

    [[ -f "${INCOMING}/manifest.resolved.json" ]] || fail_and_rollback "extracted tree has no manifest"
    printf '%s\n' "$ARCHIVE_SHA" >"${INCOMING}/.archive-sha256"
    printf '%s\n' "$TARGET_VERSION" >"${INCOMING}/VERSION"

    # Existing target (a --force overwrite) is moved aside, never deleted while
    # it might still be the running release.
    if [[ -d "$TARGET_RELEASE" ]]; then
        warn "replacing existing ${TARGET_RELEASE}"
        rm -rf "${TARGET_RELEASE}.superseded"
        mv "$TARGET_RELEASE" "${TARGET_RELEASE}.superseded"
    fi
    mv "$INCOMING" "$TARGET_RELEASE"
    ok "release tree at ${TARGET_RELEASE}"
fi

manifest_load "${TARGET_RELEASE}/manifest.resolved.json"

# --- B4. shared state links ------------------------------------------------

step "B4. linking persistent state into the release"
while read -r id; do
    links="$(mf "services.${id}.shared_links" 2>/dev/null || true)"
    [[ -n "$links" && "$links" != "null" ]] || continue
    app_dir="$(svc "$id" dir "$id")"
    while IFS=$'\t' read -r name target; do
        [[ -n "$name" ]] || continue
        src_path="${TARGET_RELEASE}/apps/${app_dir}/${name}"
        dst_path="${ML_ROOT}/${target}"
        mkdir -p "$dst_path"
        # Replace whatever the archive shipped at that path with a link to the
        # persistent location, so an upgrade physically cannot delete data.
        rm -rf "$src_path"
        mkdir -p "$(dirname "$src_path")"
        ln -sfn "$dst_path" "$src_path"
        log "  ${id}: apps/${app_dir}/${name} -> ${target}"
    done < <(python3 -c "
import json,sys
doc = json.load(open(sys.argv[1], encoding='utf-8'))
for key, value in (doc['services'][sys.argv[2]].get('shared_links') or {}).items():
    print(f'{key}\t{value}')
" "${TARGET_RELEASE}/manifest.resolved.json" "$id")
done < <(service_ids)
ok "persistent state linked"

# --- B5. dependencies ------------------------------------------------------

if (( NO_DEPS )); then
    warn "B5. --no-deps: skipping dependency installation"
elif (( REDEPLOY_SAME )); then
    log "B5. skipping dependency installation; nothing changed"
else
    step "B5. installing dependencies from the configured mirror"

    if [[ ! -x "${ML_VENV}/bin/python" ]]; then
        log "creating virtual environment at ${ML_VENV}"
        "$PYTHON_BIN" -m venv "$ML_VENV" || fail_and_rollback "could not create the virtual environment"
    fi

    # Extra package indexes.
    #
    # torch resolves to a local version such as 2.13.0+cpu, and that wheel exists
    # only on a PyTorch CPU index -- never on PyPI. Installing resolved.txt
    # without one fails on torch alone, which is exactly what happened the first
    # time the real archive was installed.
    #
    # Precedence: --extra-index-url, then ML_PIP_EXTRA_INDEX_URL (which may be
    # set to the empty string to use only the mirror in /etc/pip.conf), then
    # whatever the manifest declares.
    PIP_INDEX_ARGS=()
    if (( EXTRA_INDEX_SET )); then
        for url in "${EXTRA_INDEX_URLS[@]}"; do
            PIP_INDEX_ARGS+=(--extra-index-url "$url")
        done
    elif [[ -n "${ML_PIP_EXTRA_INDEX_URL+x}" ]]; then
        if [[ -n "$ML_PIP_EXTRA_INDEX_URL" ]]; then
            PIP_INDEX_ARGS+=(--extra-index-url "$ML_PIP_EXTRA_INDEX_URL")
        fi
    else
        while read -r url; do
            [[ -n "$url" ]] || continue
            PIP_INDEX_ARGS+=(--extra-index-url "$url")
        done < <(mf 'pip.extra_index_urls' 2>/dev/null || true)
    fi
    if (( ${#PIP_INDEX_ARGS[@]} )); then
        log "extra package index(es) in use: ${PIP_INDEX_ARGS[*]}"
    else
        log "no extra package index configured; using only the mirror in pip.conf"
    fi

    # A freshly created environment still has to have the hand-installed
    # packages put into it before anything can run. Say so now, clearly, rather
    # than letting the services fail to start later.
    if (( ${#PREINSTALLED[@]} )); then
        # Re-checked against the environment as it now exists: preflight may
        # have run before the venv was created.
        reset_prerequisite_state
        check_preinstalled "${ML_VENV}/bin/python" "${TARGET_RELEASE}/requirements/resolved.txt" \
            "${PREINSTALLED[@]}"
        report_missing_prerequisites "${ML_VENV}/bin/python" \
            || fail_and_rollback "required pre-installed package(s) are missing from ${ML_VENV}"
    fi

    RESOLVED_REQ="${TARGET_RELEASE}/requirements/resolved.txt"
    if [[ -f "$RESOLVED_REQ" ]]; then
        # Pre-installed packages are filtered out of the list pip is given, so
        # that a version pinned by the release build can never override the one
        # deliberately installed on this host -- which pip would otherwise try
        # to download, and fail to, on an air-gapped machine.
        INSTALL_REQ="$RESOLVED_REQ"
        if (( ${#PREINSTALLED[@]} )); then
            INSTALL_REQ="$(mktemp)"
            on_cleanup "rm -f '${INSTALL_REQ}'"
            python3 - "$RESOLVED_REQ" "$INSTALL_REQ" "${PREINSTALLED[@]}" <<'PYEOF'
import re
import sys

source, destination = sys.argv[1], sys.argv[2]
skip = {name.lower().replace("_", "-") for name in sys.argv[3:]}
kept, dropped = [], []
with open(source, encoding="utf-8") as handle:
    for line in handle:
        stripped = line.strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*==", stripped)
        if match and match.group(1).lower().replace("_", "-") in skip:
            dropped.append(stripped)
        else:
            kept.append(line.rstrip("\n"))
with open(destination, "w", encoding="utf-8", newline="\n") as handle:
    handle.write("\n".join(kept) + "\n")
for item in dropped:
    print(f"    holding back {item} (already installed on this host)", file=sys.stderr)
PYEOF
        fi
        log "installing from requirements/resolved.txt (fully pinned by the release build)"
        if ! "${ML_VENV}/bin/python" -m pip install --disable-pip-version-check \
                "${PIP_INDEX_ARGS[@]}" -r "$INSTALL_REQ"; then
            fail_and_rollback "dependency installation failed. The office pip mirror may be unreachable, or a
pinned package may be missing from it. requirements/mirror_audit.txt in this release
lists everything the mirror must carry."
        fi
    else
        warn "no requirements/resolved.txt in the release; falling back to per-component requirements"
        while read -r id; do
            app_dir="$(svc "$id" dir "$id")"
            while read -r req; do
                [[ -n "$req" ]] || continue
                req_path="${TARGET_RELEASE}/apps/${app_dir}/${req}"
                [[ -f "$req_path" ]] || { warn "  ${id}: ${req} not found, skipping"; continue; }
                log "  ${id}: pip install -r ${req}"
                "${ML_VENV}/bin/python" -m pip install --disable-pip-version-check \
                    "${PIP_INDEX_ARGS[@]}" -r "$req_path" \
                    || fail_and_rollback "dependency installation failed for ${id}"
            done < <(mf "services.${id}.requirements" 2>/dev/null || true)
        done < <(service_ids)
    fi

    # pip check is the gate that catches an incompatible dependency set before
    # anything is activated. It runs while the old release is still serving.
    step "B5b. verifying the environment is internally consistent (pip check)"
    if ! "${ML_VENV}/bin/python" -m pip check; then
        fail_and_rollback "pip check FAILED: the installed packages are mutually incompatible.
Nothing has been activated; the previous release is still serving.
If this is the shared-environment conflict between hydride (torch) and the portal,
set 'env: isolated' on that service in manifest.yml and cut a new release."
    fi
    ok "pip check passed"
fi

# --- B6. systemd units -----------------------------------------------------

step "B6. rendering systemd units"

UNIT_DIR="$(unit_dir)"
mkdir -p "$UNIT_DIR"
TEMPLATE="${TARGET_RELEASE}/systemd/service.template"
[[ -f "$TEMPLATE" ]] || TEMPLATE="${SCRIPT_DIR}/../systemd/service.template"
[[ -f "$TEMPLATE" ]] || fail_and_rollback "systemd/service.template is missing from the release"

UNITS_CHANGED=0
RENDERED_UNITS=()

# The suite target. Units are WantedBy and PartOf it, so it has to exist or
# `systemctl --user enable` fails. The office deployment already has one; a
# fresh host will not, so it is created when the manifest names one.
SUITE_TARGET="$(mf_or 'runtime.systemd_target' '')"
if [[ -n "$SUITE_TARGET" ]]; then
    TARGET_TEMPLATE="${TARGET_RELEASE}/systemd/target.template"
    [[ -f "$TARGET_TEMPLATE" ]] || TARGET_TEMPLATE="${SCRIPT_DIR}/../systemd/target.template"
    if [[ -f "$TARGET_TEMPLATE" ]]; then
        target_text="$(<"$TARGET_TEMPLATE")"
        target_text="${target_text//@TARGET_NAME@/$SUITE_TARGET}"
        target_text="${target_text//@DESCRIPTION@/Unified Scientific ML Platform}"
        target_text="${target_text//@ROOT@/$ML_ROOT}"
        target_tmp="$(mktemp)"
        printf '%s\n' "$target_text" >"$target_tmp"
        if [[ -f "${UNIT_DIR}/${SUITE_TARGET}" ]] && cmp -s "$target_tmp" "${UNIT_DIR}/${SUITE_TARGET}"; then
            log "  ${SUITE_TARGET}: unchanged"
        else
            cp "$target_tmp" "${UNIT_DIR}/${SUITE_TARGET}"
            UNITS_CHANGED=1
            log "  ${SUITE_TARGET}: installed"
        fi
        rm -f "$target_tmp"
    fi
fi

# The address services listen on. 0.0.0.0 by default because the target server
# has no reverse proxy and is reached directly across the intranet; binding to
# loopback there would take the site down while local health checks still pass.
ML_BIND_HOST="$(mf_or 'runtime.bind_host' '0.0.0.0')"
log "services will bind ${ML_BIND_HOST}"

# Expand the manifest's own {tokens} in a value. Pure bash substitution: no sed
# delimiters to collide with paths, and no shell re-parsing of the result.
expand_tokens() {
    local text="$1" port="$2"
    text="${text//\{bind\}/$ML_BIND_HOST}"
    text="${text//\{venv\}/$ML_VENV}"
    text="${text//\{release\}/$TARGET_RELEASE}"
    text="${text//\{current\}/${ML_ROOT}/current}"
    text="${text//\{root\}/$ML_ROOT}"
    text="${text//\{port\}/$port}"
    printf '%s' "$text"
}

render_unit() {
    local id="$1" out="$2"
    local unit port start workdir description wanted_by target commit
    unit="$(svc "$id" unit)"
    port="$(svc "$id" port)"
    port=$(( port + PORT_OFFSET ))
    start="$(expand_tokens "$(svc "$id" start)" "$port")"
    workdir="$(expand_tokens "$(svc "$id" workdir)" "$port")"
    commit="$(svc "$id" commit)"
    # No suite version in the description: it would change every release.
    description="ML Platform -- ${id}"

    # Group units under the manifest's target when it names one, so that an
    # existing `systemctl --user start ml-platform.target` keeps bringing the
    # whole suite up. Fall back to the scope default when it does not.
    local default_target
    if [[ "$ML_SYSTEMD_SCOPE" == "user" ]]; then
        default_target="default.target"
    else
        default_target="multi-user.target"
    fi
    target="$(mf_or 'runtime.systemd_target' "$default_target")"
    wanted_by="$target"

    # Ordering: the gateway must come up after the services it routes to, which
    # is how the existing deployment is arranged and worth preserving.
    local after_list wants_list after_line="" wants_line=""
    after_list="$(mf "services.${id}.after" 2>/dev/null | tr '\n' ' ' || true)"
    wants_list="$(mf "services.${id}.wants" 2>/dev/null | tr '\n' ' ' || true)"
    if [[ -n "${after_list// /}" ]]; then
        after_line=" ${after_list% }"
    fi
    if [[ -n "${wants_list// /}" ]]; then
        wants_line=$'\n'"Wants=${wants_list% }"
    fi

    # Environment= lines from the manifest.
    local env_lines="" key value
    while IFS=$'\t' read -r key value; do
        [[ -n "$key" ]] || continue
        env_lines+="Environment=\"${key}=$(expand_tokens "$value" "$port")\""$'\n'
    done < <(python3 -c "
import json,sys
doc = json.load(open(sys.argv[1], encoding='utf-8'))
for key, value in (doc['services'][sys.argv[2]].get('environment') or {}).items():
    print(f'{key}\t{value}')
" "${TARGET_RELEASE}/manifest.resolved.json" "$id")
    env_lines="${env_lines%$'\n'}"

    local text
    text="$(<"$TEMPLATE")"
    text="${text//@SERVICE_ID@/$id}"
    text="${text//@COMMIT@/$commit}"
    text="${text//@DESCRIPTION@/$description}"
    text="${text//@ROOT@/$ML_ROOT}"
    text="${text//@TARGET@/$target}"
    text="${text//@AFTER@/$after_line}"
    text="${text//@WANTS@/$wants_line}"
    text="${text//@WORKDIR@/$workdir}"
    text="${text//@ENVIRONMENT@/$env_lines}"
    text="${text//@EXECSTART@/$start}"
    text="${text//@WANTED_BY@/$wanted_by}"
    printf '%s\n' "$text" >"$out"
}

while read -r id; do
    [[ "$(svc "$id" in_process false)" == "true" ]] && continue
    unit="$(svc "$id" unit)"
    [[ -n "$unit" ]] || continue
    RENDERED_UNITS+=("$unit")

    tmp_unit="$(mktemp)"
    render_unit "$id" "$tmp_unit" || fail_and_rollback "could not render unit for ${id}"

    installed="${UNIT_DIR}/${unit}"
    if [[ -f "$installed" ]] && cmp -s "$tmp_unit" "$installed"; then
        log "  ${unit}: unchanged"
        rm -f "$tmp_unit"
    else
        cp "$tmp_unit" "$installed"
        rm -f "$tmp_unit"
        UNITS_CHANGED=1
        log "  ${unit}: installed"
        # A unit whose text changed must restart even if its code did not.
        if [[ " ${CHANGED_SERVICES[*]} " != *" ${id} "* ]]; then
            CHANGED_SERVICES+=("$id")
        fi
    fi
done < <(service_ids)

if (( UNITS_CHANGED )); then
    sctl daemon-reload || fail_and_rollback "systemctl daemon-reload failed"
    if [[ -n "$SUITE_TARGET" ]]; then
        sctl enable "$SUITE_TARGET" >/dev/null 2>&1 || warn "could not enable ${SUITE_TARGET}"
    fi
    ok "unit files updated and daemon reloaded"
else
    ok "unit files already current; no daemon-reload needed"
fi

# --- B7. activate ----------------------------------------------------------

step "B7. activating release ${TARGET_VERSION}"

# ln -sfn onto an existing symlink is not atomic; creating a new link and
# renaming it over the old one is. A reader either sees the old release or the
# new one, never a missing `current`.
ln -sfn "$TARGET_RELEASE" "${ML_ROOT}/current.tmp"
mv -Tf "${ML_ROOT}/current.tmp" "$CURRENT_LINK"
ROLLBACK_NEEDED=1
ok "current -> releases/${TARGET_VERSION}"

# --- B8. restart -----------------------------------------------------------

if (( NO_RESTART )); then
    warn "B8. --no-restart: services were not restarted; the new release is active but not running"
else
    step "B8. restarting affected services"
    if (( ${#CHANGED_SERVICES[@]} == 0 )); then
        log "no service needed a restart"
    fi
    for id in "${CHANGED_SERVICES[@]}"; do
        [[ "$(svc "$id" in_process false)" == "true" ]] && continue
        unit="$(svc "$id" unit)"
        [[ -n "$unit" ]] || continue
        log "  enabling and restarting ${unit}"
        sctl enable "$unit" >/dev/null 2>&1 || warn "could not enable ${unit}"
        if ! sctl restart "$unit"; then
            fail_and_rollback "failed to restart ${unit}"
        fi
    done

    # Give units a moment, then confirm they are actually up rather than
    # crash-looping, which `restart` alone will not tell us.
    sleep 3
    for unit in "${RENDERED_UNITS[@]}"; do
        if ! unit_is_active "$unit"; then
            err "unit ${unit} is not active after restart:"
            sctl status "$unit" --no-pager --lines=20 2>&1 | sed 's/^/    /' >&2 || true
            fail_and_rollback "service ${unit} did not stay up"
        fi
    done
    ok "all units active"
fi

# --- B9. health ------------------------------------------------------------

step "B9. health checks"
HEALTH_STATUS="failed"
if (( NO_RESTART )); then
    warn "skipping health checks because --no-restart was given"
    HEALTH_STATUS="skipped"
elif "${SCRIPT_DIR}/health_check.sh" --root "$ML_ROOT" --systemd-scope "$ML_SYSTEMD_SCOPE" --wait "$HEALTH_WAIT"; then
    HEALTH_STATUS="ok"
    ok "health checks passed"
else
    fail_and_rollback "health checks failed for suite ${TARGET_VERSION}"
fi

# --- B10. record -----------------------------------------------------------

COMPONENTS_JSON="$(python3 -c "
import json,sys
doc = json.load(open(sys.argv[1], encoding='utf-8'))
print(json.dumps({name: service.get('commit', '') for name, service in doc['services'].items()}))
" "${TARGET_RELEASE}/manifest.resolved.json")"

history_append "$(printf '{"suite_version": "%s", "deployed_utc": "%s", "archive_sha256": "%s", "archive": "%s", "previous_version": "%s", "health": "%s", "components": %s}' \
    "$TARGET_VERSION" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$ARCHIVE_SHA" "${ARCHIVE##*/}" "${PREVIOUS_VERSION:-}" "$HEALTH_STATUS" "$COMPONENTS_JSON")"

# --- B11. prune old releases -----------------------------------------------

mapfile -t ALL_RELEASES < <(find "$RELEASES_DIR" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | grep -v '\.\(incoming\|superseded\)$' | sort -V -r)
KEPT=0
for rel in "${ALL_RELEASES[@]}"; do
    KEPT=$(( KEPT + 1 ))
    (( KEPT <= ML_RELEASE_KEEP )) && continue
    [[ "$rel" == "$TARGET_VERSION" || "$rel" == "$PREVIOUS_VERSION" ]] && continue
    rm -rf "${RELEASES_DIR:?}/${rel}"
    log "pruned old release ${rel}"
done
rm -rf "${TARGET_RELEASE}.superseded" 2>/dev/null || true

# --- done ------------------------------------------------------------------

say ""
ok "suite ${TARGET_VERSION} deployed successfully"
say ""
"${SCRIPT_DIR}/status.sh" --root "$ML_ROOT" --systemd-scope "$ML_SYSTEMD_SCOPE" || true
