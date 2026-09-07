#!/usr/bin/env bash
# health_check.sh -- read-only verification that the deployed suite is serving.
#
# Safe to run at any time, from cron, or by hand.  Changes nothing.
# Exit status is 0 only when every configured check passes.
#
#   ./health_check.sh                    # check the active release
#   ./health_check.sh --wait 60          # give services up to 60s to come up
#   ./health_check.sh --release <path>   # check a specific release tree
#   ./health_check.sh --intranet-host 10.20.30.40   # assert catalog links use it
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
INTRANET_HOST_ARG=""
# Set by update.sh when the operator passed --allow-missing-seeds. See
# waived_severity() for exactly what it relaxes, and what it does not.
SEEDS_WAIVED=0

while (( $# )); do
    case "$1" in
        --wait)    WAIT_SECONDS="$2"; shift 2 ;;
        --release) RELEASE="$2"; shift 2 ;;
        --root)    ROOT_ARG="$2"; shift 2 ;;
        --systemd-scope) SCOPE_ARG="$2"; shift 2 ;;
        --host)    HOST="$2"; shift 2 ;;
        --intranet-host) INTRANET_HOST_ARG="$2"; shift 2 ;;
        --seeds-waived) SEEDS_WAIVED=1; shift ;;
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

# The address other machines use to reach this host. Only the catalog assertion
# needs it, and that assertion is skipped rather than guessed at when it cannot
# be determined -- see check_catalog_urls.
INTRANET_HOST="$(detect_intranet_host "$INTRANET_HOST_ARG")"

# results: one "id|kind|target|status|detail" line per check
RESULTS=()
FAILURES=0

# The severity a seeded-state assertion should carry.
#
# `fail` normally. `warn` when the operator passed --allow-missing-seeds, which
# is a statement that they know this data is absent and want the deployment
# anyway; asserting its absence back at them as a failure would make the flag
# they were offered impossible to use.
waived_severity() {
    if (( SEEDS_WAIVED )); then
        printf 'warn'
    else
        printf '%s' "${1:-fail}"
    fi
}

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
# 6. The units say what they are supposed to say.
#
# A service that declares an environment file in the manifest and whose
# INSTALLED unit does not load it starts with none of those variables set. That
# is invisible from outside -- the process is up, every health path answers 200
# -- and it is exactly how v1.4.0 shipped a portal serving loopback links.
# ---------------------------------------------------------------------------

check_unit_environment_files() {
    local id unit env_file installed
    local dir
    dir="$(unit_dir)"
    for id in $(service_ids); do
        [[ "$(svc "$id" in_process false)" == "true" ]] && continue
        env_file="$(svc "$id" env_file '')"
        [[ -n "$env_file" ]] || continue
        [[ "$env_file" == /* ]] || env_file="${ML_ROOT}/${env_file}"
        unit="$(svc "$id" unit)"
        installed="${dir}/${unit}"

        if [[ ! -f "$installed" ]]; then
            record "$id" envfile "$unit" fail "unit file not installed at ${installed}"
            continue
        fi
        if ! grep -qF "EnvironmentFile=${env_file}" "$installed"; then
            record "$id" envfile "$unit" fail "unit does not load ${env_file}"
            continue
        fi
        if [[ ! -s "$env_file" ]]; then
            record "$id" envfile "$unit" fail "${env_file} is missing or empty"
            continue
        fi
        record "$id" envfile "$unit" ok "loads ${env_file}"
    done
}

# ---------------------------------------------------------------------------
# 7. The catalog advertises addresses other machines can reach.
#
# The portal renders the links everyone else clicks. If it falls back to
# loopback, every one of those links is dead from every desk but this one,
# while every check above still passes. This is that check.
# ---------------------------------------------------------------------------

check_catalog_urls() {
    local path port url body token found=""
    path="$(mf_or 'verify.catalog.path' '')"
    [[ -n "$path" ]] || return 0
    port="$(svc gateway port)"
    [[ -n "$port" ]] || return 0
    url="http://${HOST}:${port}${path}"

    # Skipped, not guessed at: without a known non-loopback address for this
    # host there is no way to tell a wrong answer from a correct one, and a
    # laptop-bound test deployment answering 127.0.0.1 is answering correctly.
    if host_is_loopback "$INTRANET_HOST"; then
        record "gateway" catalog "$path" warn "intranet address unknown; cannot assert link addresses"
        return 0
    fi

    body="$(curl -s --max-time 15 "$url" 2>/dev/null || true)"
    if [[ -z "$body" ]]; then
        record "gateway" catalog "$path" fail "no response from ${url}"
        return 0
    fi

    while read -r token; do
        [[ -n "$token" ]] || continue
        if [[ "$body" == *"$token"* ]]; then
            found="${found:+${found}, }${token}"
        fi
    done < <(mf 'verify.catalog.reject' 2>/dev/null || true)

    if [[ -n "$found" ]]; then
        record "gateway" catalog "$path" fail \
            "advertises ${found} instead of ${INTRANET_HOST} -- links are dead off this host"
    elif [[ "$body" == *"$INTRANET_HOST"* ]]; then
        record "gateway" catalog "$path" ok "advertises ${INTRANET_HOST}"
    else
        # No loopback, but no absolute URLs either: the portal is serving
        # relative links, which work from anywhere and are not a fault.
        record "gateway" catalog "$path" ok "no loopback addresses advertised"
    fi
}

# ---------------------------------------------------------------------------
# 8. What the services said about themselves as they started.
#
# A model that failed to warm-load leaves the service healthy, listening and
# useless. The only place that is visible is the journal, so the manifest names
# the lines that must not be there, and this reads the current run of each unit
# looking for them.
# ---------------------------------------------------------------------------

check_journal_assertions() {
    local id severity lines require forbid unit text token hit=""
    # U+001F: `require` is empty for some entries, and an empty field between
    # two tabs would shift the forbid list into it.
    while IFS=$'\x1f' read -r id severity lines require forbid; do
        [[ -n "$id" ]] || continue
        unit="$(svc "$id" unit '')"
        [[ -n "$unit" ]] || continue
        if ! unit_is_active "$unit"; then
            # The unit check has already failed for this; saying it twice adds
            # noise and no information.
            continue
        fi

        text="$(unit_journal "$unit" "${lines:-200}")"
        if [[ -z "$text" ]]; then
            record "$id" journal "$unit" warn "journal is empty or unreadable; nothing asserted"
            continue
        fi

        hit=""
        local old_ifs="$IFS"
        IFS='|'
        for token in $forbid; do
            [[ -n "$token" ]] || continue
            if [[ "$text" == *"$token"* ]]; then
                hit="$token"
                break
            fi
        done
        IFS="$old_ifs"

        if [[ -n "$hit" ]]; then
            record "$id" journal "$unit" "$(waived_severity "${severity:-fail}")" "journal reports: ${hit}"
            continue
        fi
        if [[ -n "$require" && "$text" != *"$require"* ]]; then
            # Only ever a warning: the wording belongs to the component, and a
            # service that was not restarted has nothing recent to say.
            record "$id" journal "$unit" warn "did not see \"${require}\" in this run"
            continue
        fi
        record "$id" journal "$unit" ok "clean${require:+, saw \"${require}\"}"
    done < <(python3 - "$ML_MANIFEST" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)

for entry in (document.get("verify") or {}).get("journal") or []:
    forbid = entry.get("forbid") or []
    if isinstance(forbid, str):
        forbid = [forbid]
    print("\x1f".join([
        str(entry.get("service", "")),
        str(entry.get("severity", "fail")),
        str(entry.get("lines", 200)),
        str(entry.get("require", "")),
        "|".join(str(item) for item in forbid),
    ]))
PYEOF
)
}

# ---------------------------------------------------------------------------
# 9. Seeded state is really there, and really reachable from the release.
#
# check_shared_state above asserts the directories exist. These are the things
# that have to be INSIDE them, and the link that carries them into the release
# the services actually run from.
# ---------------------------------------------------------------------------

check_seeded_state() {
    local id kind target marker required _rest abs status
    while IFS=$'\x1f' read -r id kind target marker required _rest; do
        [[ -n "$id" ]] || continue
        abs="${ML_ROOT}/${target}"
        if seed_is_populated "$kind" "$abs" "$marker"; then
            record "seed" "$id" "$target" ok "populated"
        else
            [[ "$required" == "true" ]] && status="$(waived_severity fail)" || status=warn
            record "seed" "$id" "$target" "$status" "empty${marker:+ (no ${marker})}"
        fi
    done < <(seed_specs)

    # The link, not just the directory. The service reads the checkpoints
    # through apps/<component>/<link-name>, so a shared/models full of weights
    # and a broken link into it is the same outage as no weights at all. The
    # link name comes from shared_links rather than being spelled here, so
    # renaming it in the manifest cannot leave this check asserting on a path
    # that no longer exists.
    local app_dir registry link through
    for id in $(service_ids); do
        registry="$(svc "$id" model_registry '')"
        [[ -n "$registry" ]] || continue
        app_dir="$(svc "$id" dir "$id")"
        while IFS=$'\t' read -r link target; do
            [[ -n "$link" ]] || continue
            [[ "$target" == shared/models/* ]] || continue
            through="${RELEASE}/apps/${app_dir}/${link}/${registry}"
            if [[ -e "$through" ]]; then
                record "$id" registry "apps/${app_dir}/${link}/${registry}" ok "resolves"
            else
                record "$id" registry "apps/${app_dir}/${link}/${registry}" "$(waived_severity fail)" \
                    "does not resolve; the service cannot look a model up"
            fi
        done < <(python3 - "$ML_MANIFEST" "$id" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
links = (document["services"][sys.argv[2]].get("shared_links") or {})
for name, target in links.items():
    print(f"{name}\t{target}")
PYEOF
)
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
check_unit_environment_files
check_catalog_urls
check_journal_assertions
check_seeded_state

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
