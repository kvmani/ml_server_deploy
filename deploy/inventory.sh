#!/usr/bin/env bash
# inventory.sh -- read-only survey of an office server.
#
# This script changes NOTHING.  It opens no sockets outward, writes only the
# report file you name, and never touches a service.  Run it first on the office
# machine, review the output, and use it to fill in manifest.yml with facts
# rather than assumptions.
#
#   ./inventory.sh                       # report to stdout and ./inventory-<host>-<date>.txt
#   ./inventory.sh --out /tmp/report.txt
#   ./inventory.sh --stdout              # no file written at all

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

OUT_FILE=""
WRITE_FILE=1

usage() {
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

while (( $# )); do
    case "$1" in
        --out)    OUT_FILE="$2"; shift 2 ;;
        --stdout) WRITE_FILE=0; shift ;;
        -h|--help) usage ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

if (( WRITE_FILE )) && [[ -z "$OUT_FILE" ]]; then
    OUT_FILE="inventory-$(hostname -s 2>/dev/null || echo host)-$(date -u '+%Y%m%dT%H%M%SZ').txt"
fi

# ---------------------------------------------------------------------------
# Report helpers.  Everything is printed to stdout; the caller tees it to a file
# so that a failure mid-report still leaves the operator with what was gathered.
# ---------------------------------------------------------------------------

section() {
    printf '\n==============================================================\n'
    printf '  %s\n' "$1"
    printf -- '--------------------------------------------------------------\n'
}

field() { printf '  %-28s %s\n' "$1" "$2"; }

# run <label> <command...> -- prints the command output indented, or a clear
# "not available" line.  Never aborts the report.
run() {
    local label="$1"; shift
    printf '\n  %s\n' "$label"
    if "$@" 2>&1 | sed 's/^/    /'; then
        return 0
    fi
    printf '    (command failed or produced nothing)\n'
    return 0
}

report() {

# Errexit and pipefail are deliberately relaxed for the body of the report.
#
# This script is the first thing run on an unfamiliar office server and its only
# job is to describe what is there. A diagnostic that aborts two thirds of the
# way through because a `find | head -20` took SIGPIPE, or because one probe of
# an absent directory returned non-zero, is far worse than one that prints an
# odd line and carries on. Nothing here modifies anything, so there is no
# half-completed state to protect against.
set +e
set +o pipefail

printf 'ML Server suite -- server inventory\n'
printf 'generated  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf 'host       %s\n' "$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo unknown)"
printf 'invoked by %s\n' "$(id -un)"

section "1. Operating system"
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    field "distribution" "${PRETTY_NAME:-unknown}"
    field "id / version"  "${ID:-?} / ${VERSION_ID:-?}"
fi
field "kernel"        "$(uname -sr)"
field "architecture"  "$(uname -m)"
field "virtualisation" "$(systemd-detect-virt 2>/dev/null || echo unknown)"
field "uptime"        "$(uptime -p 2>/dev/null || echo unknown)"

section "2. Identity, sudo and systemd session"
field "user / uid"     "$(id -un) / $(id -u)"
field "home"           "${HOME}"
field "groups"         "$(id -Gn)"
if have_sudo; then
    field "passwordless sudo" "yes"
elif have_cmd sudo; then
    field "passwordless sudo" "no (sudo present, would prompt)"
else
    field "passwordless sudo" "no (sudo not installed)"
fi
if linger_enabled; then
    field "linger" "enabled"
else
    field "linger" "DISABLED -- user services will stop at logout"
    field ""       "enable with: loginctl enable-linger $(id -un)"
fi
if ensure_user_bus 2>/dev/null; then
    field "user systemd bus" "reachable (${XDG_RUNTIME_DIR})"
    field "user manager"     "$(systemctl --user is-system-running 2>/dev/null || echo unknown)"
else
    field "user systemd bus" "NOT reachable -- systemctl --user will fail"
fi
field "system manager" "$(systemctl is-system-running 2>/dev/null || echo unknown)"

section "3. Interpreters and required tools"
local_py=""
for candidate in python3 python3.13 python3.12 python3.11; do
    if have_cmd "$candidate"; then
        field "$candidate" "$("$candidate" --version 2>&1)"
        [[ -z "$local_py" ]] && local_py="$candidate"
    fi
done
[[ -z "$local_py" ]] && field "python3" "NOT FOUND -- the suite cannot run"
for candidate in pip3 node npm git curl tar sha256sum flock ss sqlite3 nginx redis-server gunicorn pdftoppm; do
    if have_cmd "$candidate"; then
        field "$candidate" "$(command -v "$candidate")"
    else
        field "$candidate" "absent"
    fi
done

section "4. Candidate deployment roots"
for root in "${HOME}/ml_platform" /opt/ml_server /opt/microseg "${HOME}/ml_platform_staging"; do
    if [[ -d "$root" ]]; then
        field "$root" "EXISTS ($(du -sh "$root" 2>/dev/null | cut -f1) )"
        ls -la "$root" 2>/dev/null | sed 's/^/      /'
        if [[ -L "${root}/current" ]]; then
            field "  current ->" "$(readlink -f "${root}/current" 2>/dev/null)"
        fi
        if [[ -f "${root}/VERSION" ]]; then
            field "  VERSION" "$(tr -d '[:space:]' <"${root}/VERSION")"
        fi
        if [[ -f "${root}/current/VERSION" ]]; then
            field "  current VERSION" "$(tr -d '[:space:]' <"${root}/current/VERSION")"
        fi
    else
        field "$root" "absent"
    fi
done

printf '\n  What detect_layout() would conclude right now:\n'
if detect_layout "" auto 1 2>/dev/null; then
    for note in "${ML_LAYOUT_NOTES[@]}"; do printf '    %s\n' "$note"; done
    printf '    => ROOT=%s LAYOUT=%s SCOPE=%s VENV=%s\n' "$ML_ROOT" "$ML_LAYOUT" "$ML_SYSTEMD_SCOPE" "$ML_VENV"
else
    printf '    detection was inconclusive; --root will be required\n'
fi

section "5. systemd units"
printf '\n  User units matching ml*/microseg*:\n'
if ensure_user_bus 2>/dev/null; then
    systemctl --user list-units --all --no-legend --no-pager 'ml*' 'microseg*' 2>/dev/null | sed 's/^/    /' || true
    systemctl --user list-unit-files --no-legend --no-pager 'ml*' 'microseg*' 2>/dev/null | sed 's/^/    [file] /' || true
else
    printf '    (user bus unreachable)\n'
fi
printf '\n  System units matching ml*/microseg*:\n'
systemctl list-units --all --no-legend --no-pager 'ml*' 'microseg*' 2>/dev/null | sed 's/^/    /' || true
systemctl list-unit-files --no-legend --no-pager 'ml*' 'microseg*' 2>/dev/null | sed 's/^/    [file] /' || true

printf '\n  Unit file contents (these define the real start commands):\n'
for dir in "${HOME}/.config/systemd/user" /etc/systemd/system; do
    for unit in "${dir}"/ml*.service "${dir}"/microseg*.service; do
        [[ -f "$unit" ]] || continue
        printf '\n    --- %s\n' "$unit"
        sed 's/^/      /' "$unit"
    done
done

section "6. Listening TCP ports"
if have_cmd ss; then
    ss -ltnp 2>/dev/null | sed 's/^/  /'
elif have_cmd netstat; then
    netstat -ltnp 2>/dev/null | sed 's/^/  /'
else
    printf '  neither ss nor netstat available\n'
fi
printf '\n  Ports the suite expects:\n'
for port in 5000 5005 5045 5055 5065 5070 8765 6379 80; do
    listener="$(port_listener "$port")"
    if [[ -n "$listener" ]]; then
        field "  ${port}" "IN USE: ${listener}"
    else
        field "  ${port}" "free"
    fi
done

section "7. Python environments"
for venv in "${HOME}/ml_platform/.venv" /opt/ml_server/env /opt/microseg/HydrideSegmentation/.venv "${HOME}/ml_platform/.venv-hydride"; do
    if [[ -x "${venv}/bin/python" ]]; then
        printf '\n  %s\n' "$venv"
        printf '    version: %s\n' "$("${venv}/bin/python" --version 2>&1)"
        printf '    packages (%s installed):\n' "$("${venv}/bin/python" -m pip list --format=freeze 2>/dev/null | wc -l)"
        "${venv}/bin/python" -m pip list --format=freeze 2>/dev/null | sed 's/^/      /'
        printf '    pip check: '
        "${venv}/bin/python" -m pip check 2>&1 | head -5 | sed 's/^/      /'
    fi
done

section "8. Package mirror configuration"
printf '\n  pip:\n'
for conf in /etc/pip.conf "${HOME}/.pip/pip.conf" "${HOME}/.config/pip/pip.conf"; do
    if [[ -f "$conf" ]]; then
        printf '    --- %s\n' "$conf"
        sed 's/^/      /' "$conf"
    else
        printf '    %s: absent\n' "$conf"
    fi
done
field "  PIP_INDEX_URL" "${PIP_INDEX_URL:-<unset>}"
field "  PIP_CONFIG_FILE" "${PIP_CONFIG_FILE:-<unset>}"

printf '\n  npm:\n'
if have_cmd npm; then
    printf '    registry: %s\n' "$(npm config get registry 2>/dev/null || echo unknown)"
    printf '    strict-ssl: %s\n' "$(npm config get strict-ssl 2>/dev/null || echo unknown)"
else
    printf '    npm not installed\n'
fi

printf '\n  apt sources:\n'
grep -rhE '^[[:space:]]*(deb|deb-src|URIs:)' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null | sed 's/^/    /' | head -30 || printf '    (unreadable)\n'

printf '\n  git url rewrites (an insteadOf rule here means git traffic is redirected):\n'
git config --global --get-regexp 'url\..*\.insteadof' 2>/dev/null | sed 's/^/    /' || printf '    none configured\n'

section "9. Reachability -- confirming the air gap"
printf '\n  These SHOULD fail on an air-gapped office host.  A success here means\n'
printf '  the host can reach GitHub, which contradicts the deployment assumption.\n\n'
for target in https://github.com https://pypi.org; do
    # curl already prints 000 on a connection failure AND exits non-zero, so a
    # `|| echo 000` fallback appends a second 000 and the comparison never
    # matches. That made this report an air-gapped host as REACHABLE, which is
    # the opposite of the truth and exactly the sort of thing this section
    # exists to establish. Capture first, then decide.
    if ! code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$target" 2>/dev/null)"; then
        code="000"
    fi
    if [[ "$code" == "000" || -z "$code" ]]; then
        field "  $target" "unreachable (expected on an air-gapped host)"
    else
        field "  $target" "REACHABLE (HTTP ${code}) -- unexpected, note this"
    fi
done

section "10. Persistent data, models, configuration"
for path in "${HOME}/ml_platform/shared" /opt/ml_server/data /opt/ml_server/logs \
            /opt/microseg/HydrideSegmentation/frozen_checkpoints "${HOME}/ml_platform/shared/models"; do
    if [[ -e "$path" ]]; then
        field "$path" "$(du -sh "$path" 2>/dev/null | cut -f1)"
        find "$path" -maxdepth 2 -type f -printf '      %10s  %p\n' 2>/dev/null | head -20
    else
        field "$path" "absent"
    fi
done
printf '\n  sqlite databases found under candidate roots:\n'
find "${HOME}/ml_platform" /opt/ml_server -name '*.sqlite3' -o -name '*.db' 2>/dev/null \
    | head -10 | sed 's/^/    /' || printf '    none\n'

section "11. Reverse proxy"
if [[ -d /etc/nginx ]]; then
    printf '\n  enabled sites:\n'
    ls -la /etc/nginx/sites-enabled/ 2>/dev/null | sed 's/^/    /'
    for site in /etc/nginx/sites-enabled/*; do
        [[ -f "$site" || -L "$site" ]] || continue
        printf '\n    --- %s\n' "$site"
        sed 's/^/      /' "$site" 2>/dev/null | head -60
    done
else
    printf '\n  nginx is not installed\n'
fi

section "12. Resources"
printf '\n  disk:\n'
df -h 2>/dev/null | sed 's/^/    /'
printf '\n  memory:\n'
free -h 2>/dev/null | sed 's/^/    /'
printf '\n  cpu: %s core(s)\n' "$(nproc 2>/dev/null || echo unknown)"

section "End of inventory"
printf '\n  Nothing on this host was modified.\n'
printf '  Send this file back to the development machine to populate manifest.yml.\n\n'

}

if (( WRITE_FILE )); then
    report | tee "$OUT_FILE"
    printf '\nReport written to: %s\n' "$OUT_FILE"
else
    report
fi
