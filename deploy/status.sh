#!/usr/bin/env bash
# status.sh -- what is deployed, from what, and is it working.
#
# Read-only. Answers the question you actually have in front of a server you
# have not touched for three months: which suite version is live, which commit
# of each application it contains, and whether anything is down.
#
#   ./status.sh
#   ./status.sh --json
#   ./status.sh --root /home/kvmani/ml_platform_staging

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

ROOT_ARG=""
SCOPE_ARG="auto"
JSON_OUT=0
NO_HEALTH=0

while (( $# )); do
    case "$1" in
        --root)          ROOT_ARG="$2"; shift 2 ;;
        --systemd-scope) SCOPE_ARG="$2"; shift 2 ;;
        --json)          JSON_OUT=1; shift ;;
        --no-health)     NO_HEALTH=1; shift ;;
        -h|--help)       sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

detect_layout "$ROOT_ARG" "$SCOPE_ARG" 1 || die "cannot determine the deployment layout; pass --root"

CURRENT_LINK="${ML_ROOT}/current"

if [[ ! -e "$CURRENT_LINK" ]]; then
    if (( JSON_OUT )); then
        printf '{"root": "%s", "installed": false}\n' "$ML_ROOT"
    else
        say ""
        say "No suite is installed at ${ML_ROOT}."
        say "Deploy one with:  ./update.sh /path/to/ml-server-suite-vX.Y.Z.tar.gz"
        say ""
    fi
    exit 0
fi

manifest_load "${CURRENT_LINK}/manifest.resolved.json"

SUITE_VERSION="$(mf '.suite_version')"
RELEASE_PATH="$(readlink -f "$CURRENT_LINK")"
ARCHIVE_SHA="$(cat "${RELEASE_PATH}/.archive-sha256" 2>/dev/null || echo unknown)"

LAST_RECORD="$(tail -1 "$(history_file)" 2>/dev/null || echo "")"
DEPLOYED_AT="$(printf '%s' "$LAST_RECORD" | sed -n 's/.*"deployed_utc": "\([^"]*\)".*/\1/p')"

# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

if (( JSON_OUT )); then
    python3 - "$ML_MANIFEST" "$ML_ROOT" "$RELEASE_PATH" "$ARCHIVE_SHA" "${DEPLOYED_AT:-unknown}" <<'PYEOF'
import json
import subprocess
import sys

manifest_path, root, release, sha, deployed = sys.argv[1:6]
with open(manifest_path, encoding="utf-8") as handle:
    document = json.load(handle)

services = {}
for name, service in document.get("services", {}).items():
    entry = {
        "repo": service.get("repo"),
        "ref": service.get("ref"),
        "commit": service.get("commit"),
        "in_process": bool(service.get("in_process")),
        "port": service.get("port"),
        "unit": service.get("unit"),
    }
    if entry["unit"]:
        probe = subprocess.run(
            ["systemctl", "--user", "is-active", entry["unit"]],
            capture_output=True, text=True, check=False,
        )
        entry["active"] = probe.stdout.strip() or "unknown"
    services[name] = entry

print(json.dumps({
    "root": root,
    "installed": True,
    "suite_version": document.get("suite_version"),
    "release_path": release,
    "archive_sha256": sha,
    "deployed_utc": deployed,
    "services": services,
}, indent=2))
PYEOF
    exit 0
fi

# ---------------------------------------------------------------------------
# Human output
# ---------------------------------------------------------------------------

say ""
say "==================================================================="
say " ML Server suite ${SUITE_VERSION}"
say "==================================================================="
printf '  root             %s  (%s layout, systemd --%s)\n' "$ML_ROOT" "$ML_LAYOUT" "$ML_SYSTEMD_SCOPE"
printf '  active release   %s\n' "$RELEASE_PATH"
printf '  deployed         %s\n' "${DEPLOYED_AT:-unknown}"
printf '  archive sha256   %s\n' "$ARCHIVE_SHA"
printf '  python env       %s\n' "$ML_VENV"
if linger_enabled; then
    printf '  linger           enabled\n'
else
    printf '  linger           DISABLED -- services stop at logout (loginctl enable-linger %s)\n' "$(id -un)"
fi

say ""
say "  Components"
say "  ---------------------------------------------------------------"
printf '  %-12s %-34s %-10s %s\n' "NAME" "REPO@REF" "COMMIT" "STATE"
while read -r id; do
    repo="$(svc "$id" repo)"
    ref="$(svc "$id" ref)"
    commit="$(svc "$id" commit)"
    short="${commit:0:10}"
    if [[ "$(svc "$id" in_process false)" == "true" ]]; then
        state="in-process (gateway)"
    else
        unit="$(svc "$id" unit)"
        if unit_is_active "$unit"; then
            state="active"
        else
            state="INACTIVE ($(sctl is-active "$unit" 2>/dev/null || echo unknown))"
        fi
        port="$(svc "$id" port)"
        state="${state}  :${port}"
    fi
    printf '  %-12s %-34s %-10s %s\n' "$id" "${repo}@${ref}" "$short" "$state"
done < <(service_ids)

say ""
say "  Persistent state (survives every upgrade and rollback)"
say "  ---------------------------------------------------------------"
while read -r dir; do
    [[ -n "$dir" ]] || continue
    path="${ML_ROOT}/shared/${dir}"
    if [[ -d "$path" ]]; then
        size="$(du -sh "$path" 2>/dev/null | cut -f1)"
        count="$(find "$path" -type f 2>/dev/null | wc -l)"
        printf '  %-12s %-8s %s file(s)\n' "$dir" "$size" "$count"
    else
        printf '  %-12s %s\n' "$dir" "missing"
    fi
done < <(mf '.shared_dirs')

say ""
say "  Releases present"
say "  ---------------------------------------------------------------"
if [[ -d "${ML_ROOT}/releases" ]]; then
    while read -r version; do
        marker="  "
        [[ "$version" == "$SUITE_VERSION" ]] && marker="=>"
        size="$(du -sh "${ML_ROOT}/releases/${version}" 2>/dev/null | cut -f1)"
        printf '  %s %-12s %s\n' "$marker" "$version" "$size"
    done < <(find "${ML_ROOT}/releases" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' \
             | grep -v '\.\(incoming\|superseded\)$' | sort -V -r)
fi

say ""
say "  Recent deployments"
say "  ---------------------------------------------------------------"
if [[ -f "$(history_file)" ]]; then
    tail -5 "$(history_file)" | python3 -c "
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        continue
    print('  {:<22} {:<10} {:<10} from {}'.format(
        record.get('deployed_utc', '?'),
        record.get('suite_version', '?'),
        record.get('health', '?'),
        record.get('archive', '?'),
    ))
"
else
    say "  (no deployment history yet)"
fi

if (( ! NO_HEALTH )); then
    say ""
    "${SCRIPT_DIR}/health_check.sh" --root "$ML_ROOT" --systemd-scope "$ML_SYSTEMD_SCOPE" || true
fi

say ""
