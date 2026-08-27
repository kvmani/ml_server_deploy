#!/usr/bin/env bash
# health_check.sh -- read-only verification that the deployed suite is serving.
#
# Safe to run at any time, from cron, or by hand.  Changes nothing.
# Exit status is 0 only when every configured check passes.
#
#   ./health_check.sh                    # check the active release
#   ./health_check.sh --wait 60          # give services up to 60s to come up
#   ./health_check.sh --release <path>   # check a specific release tree
#   ./health_check.sh --json

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

WAIT_SECONDS=0
RELEASE=""
ROOT_ARG=""
SCOPE_ARG="auto"
JSON_OUT=0
HOST="127.0.0.1"

while (( $# )); do
    case "$1" in
        --wait)    WAIT_SECONDS="$2"; shift 2 ;;
        --release) RELEASE="$2"; shift 2 ;;
        --root)    ROOT_ARG="$2"; shift 2 ;;
        --systemd-scope) SCOPE_ARG="$2"; shift 2 ;;
        --host)    HOST="$2"; shift 2 ;;
        --json)    JSON_OUT=1; shift ;;
        -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

detect_layout "$ROOT_ARG" "$SCOPE_ARG" 1 || die "cannot determine the deployment layout; pass --root"
[[ -n "$RELEASE" ]] || RELEASE="${ML_ROOT}/current"
[[ -d "$RELEASE" ]] || die "no release to check at ${RELEASE}"

manifest_load "${RELEASE}/manifest.resolved.json"

SUITE_VERSION="$(mf '.suite_version')"

# results: one "id|kind|target|status|detail" line per check
RESULTS=()
FAILURES=0

record() {
    local id="$1" kind="$2" target="$3" status="$4" detail="${5:-}"
    RESULTS+=("${id}|${kind}|${target}|${status}|${detail}")
    [[ "$status" == "ok" ]] || FAILURES=$(( FAILURES + 1 ))
}

# ---------------------------------------------------------------------------
# 1. systemd units
# ---------------------------------------------------------------------------

check_units() {
    local id unit
    for id in $(service_ids); do
        [[ "$(svc "$id" in_process false)" == "true" ]] && continue
        unit="$(svc "$id" unit)"
        [[ -n "$unit" ]] || continue
        if unit_is_active "$unit"; then
            record "$id" unit "$unit" ok "active"
        else
            local state
            state="$(sctl is-active "$unit" 2>/dev/null || echo unknown)"
            record "$id" unit "$unit" fail "state=${state}"
        fi
    done
}

# ---------------------------------------------------------------------------
# 2. HTTP health endpoints
# ---------------------------------------------------------------------------

check_health_endpoints() {
    local id port path url code
    for id in $(service_ids); do
        path="$(svc "$id" health)"
        [[ -n "$path" ]] || continue
        if [[ "$(svc "$id" in_process false)" == "true" ]]; then
            # Mounted inside the gateway, so it answers on the gateway's port.
            port="$(svc gateway port)"
        else
            port="$(svc "$id" port)"
        fi
        [[ -n "$port" ]] || continue
        url="http://${HOST}:${port}${path}"

        if (( WAIT_SECONDS > 0 )); then
            wait_for_http "$url" "$WAIT_SECONDS" || true
        fi
        code="$(http_status "$url" 10)"
        if [[ "$code" =~ ^[23] ]]; then
            record "$id" health "$url" ok "HTTP ${code}"
        else
            record "$id" health "$url" fail "HTTP ${code}"
        fi
    done
}

# ---------------------------------------------------------------------------
# 3. The gateway must actually reach everything it advertises.
#
# A suite where every service is individually healthy but the portal cannot
# route to them is still broken from a user's point of view, which is why this
# check exists separately from the per-service ones above.
# ---------------------------------------------------------------------------

check_gateway_routes() {
    local port path url code
    port="$(svc gateway port)"
    [[ -n "$port" ]] || return 0
    while read -r path; do
        [[ -n "$path" ]] || continue
        url="http://${HOST}:${port}${path}"
        code="$(http_status "$url" 15)"
        if [[ "$code" =~ ^[23] ]]; then
            record "gateway" route "$path" ok "HTTP ${code}"
        else
            record "gateway" route "$path" fail "HTTP ${code}"
        fi
    done < <(mf 'services.gateway.gateway_checks' 2>/dev/null || true)
}

# ---------------------------------------------------------------------------
# 4. Offline guarantees.
#
# The portal vendors MathJax, Bootstrap and Font Awesome precisely so that an
# air-gapped host renders equations correctly.  A dependency bump that
# reintroduces a CDN <script> tag is invisible on a connected development
# machine and fatal in the office, so it is asserted here on every deployment.
# ---------------------------------------------------------------------------

check_offline_assets() {
    local port url code count
    port="$(svc gateway port)"
    [[ -n "$port" ]] || return 0

    url="http://${HOST}:${port}/static/vendor/mathjax/tex-chtml-full.js"
    code="$(http_status "$url" 15)"
    if [[ "$code" == "200" ]]; then
        record "offline" asset "vendored MathJax bundle" ok "HTTP 200"
    else
        record "offline" asset "vendored MathJax bundle" fail "HTTP ${code}"
    fi

    # No served help page may reference a public CDN.
    count="$(curl -s --max-time 15 "http://${HOST}:${port}/tools/pytex/help" 2>/dev/null \
        | grep -c -E 'cdn\.|jsdelivr|googleapis|unpkg\.com' || true)"
    count="${count:-0}"
    if [[ "$count" == "0" ]]; then
        record "offline" cdn "no CDN references in help pages" ok "0 matches"
    else
        record "offline" cdn "no CDN references in help pages" fail "${count} CDN reference(s) found"
    fi
}

# ---------------------------------------------------------------------------
# 5. Persistent state is present and outside the release tree.
# ---------------------------------------------------------------------------

check_shared_state() {
    local dir
    while read -r dir; do
        [[ -n "$dir" ]] || continue
        if [[ -d "${ML_ROOT}/shared/${dir}" ]]; then
            record "shared" dir "shared/${dir}" ok "present"
        else
            record "shared" dir "shared/${dir}" fail "missing"
        fi
    done < <(mf '.shared_dirs' 2>/dev/null || true)

    # A release directory that physically contains persistent data means an
    # upgrade would delete it.
    local id
    for id in $(service_ids); do
        if [[ "$(svc "$id" requires_models false)" == "true" ]]; then
            local models="${ML_ROOT}/shared/models"
            if [[ -d "$models" ]] && [[ -n "$(ls -A "$models" 2>/dev/null)" ]]; then
                record "$id" models "shared/models" ok "populated"
            else
                record "$id" models "shared/models" warn "empty -- checkpoints are not in git and must be supplied once, by hand"
            fi
        fi
    done
}

# ---------------------------------------------------------------------------
# Run and report
# ---------------------------------------------------------------------------

check_units
check_health_endpoints
check_gateway_routes
check_offline_assets
check_shared_state

# A `warn` result is informational and must not fail the deployment.
FAILURES=0
for line in "${RESULTS[@]}"; do
    IFS='|' read -r _ _ _ status _ <<<"$line"
    [[ "$status" == "fail" ]] && FAILURES=$(( FAILURES + 1 ))
done

if (( JSON_OUT )); then
    {
        printf '{\n  "suite_version": "%s",\n  "release": "%s",\n' "$SUITE_VERSION" "$RELEASE"
        printf '  "failures": %d,\n  "checks": [\n' "$FAILURES"
        local_first=1
        for line in "${RESULTS[@]}"; do
            IFS='|' read -r id kind target status detail <<<"$line"
            (( local_first )) || printf ',\n'
            local_first=0
            printf '    {"service": "%s", "kind": "%s", "target": "%s", "status": "%s", "detail": "%s"}' \
                "$id" "$kind" "$target" "$status" "$detail"
        done
        printf '\n  ]\n}\n'
    }
else
    say ""
    say "Health of suite ${SUITE_VERSION}  (${RELEASE})"
    say "-------------------------------------------------------------------"
    for line in "${RESULTS[@]}"; do
        IFS='|' read -r id kind target status detail <<<"$line"
        case "$status" in
            ok)   marker="  ok  " ;;
            warn) marker=" warn " ;;
            *)    marker=" FAIL " ;;
        esac
        printf '[%s] %-12s %-8s %-46s %s\n' "$marker" "$id" "$kind" "$target" "$detail"
    done
    say "-------------------------------------------------------------------"
    if (( FAILURES == 0 )); then
        say "All checks passed."
    else
        say "${FAILURES} check(s) FAILED."
    fi
fi

exit $(( FAILURES > 0 ? 1 : 0 ))
