#!/usr/bin/env bash
# Shared library for the ml_server suite deployment scripts.
#
# Sourced by inventory.sh, update.sh, rollback.sh, status.sh and health_check.sh.
# Nothing here mutates the deployment except the helpers named for what they
# change, which are called only from update.sh and rollback.sh.
#
# Target: Ubuntu 22.04/24.04, bash 5, python3 >= 3.12, GNU coreutils.

[[ -n "${ML_COMMON_SOURCED:-}" ]] && return 0
ML_COMMON_SOURCED=1

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Constants and detected state
# ---------------------------------------------------------------------------

ML_MANIFEST_SCHEMA="ml-suite.manifest.v1"
ML_UNIT_PREFIX="ml-platform-"
ML_BACKUP_KEEP="${ML_BACKUP_KEEP:-5}"
ML_RELEASE_KEEP="${ML_RELEASE_KEEP:-4}"

ML_ROOT=""
ML_SYSTEMD_SCOPE=""
ML_LAYOUT=""          # platform | legacy | new
ML_VENV=""

# ---------------------------------------------------------------------------
# Logging.  Logs go to stderr and the log file; results go to stdout, so that
# `status.sh --json | jq` stays parseable.
# ---------------------------------------------------------------------------

ML_LOG_FILE=""
ML_COLOR=""
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
    ML_COLOR=1
fi

_ml_stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

_ml_emit() {
    local colour="$1" level="$2"
    shift 2
    local line
    line="[$(_ml_stamp)] [${level}] $*"
    if [[ -n "$ML_COLOR" && -n "$colour" ]]; then
        printf '\033[%sm%s\033[0m\n' "$colour" "$line" >&2
    else
        printf '%s\n' "$line" >&2
    fi
    if [[ -n "$ML_LOG_FILE" ]]; then
        printf '%s\n' "$line" >>"$ML_LOG_FILE"
    fi
    return 0
}

log()  { _ml_emit ''   'INFO'  "$@"; }
ok()   { _ml_emit '32' 'OK'    "$@"; }
warn() { _ml_emit '33' 'WARN'  "$@"; }
err()  { _ml_emit '31' 'ERROR' "$@"; }
step() { _ml_emit '36' 'STEP'  "$@"; }

die() { err "$@"; exit 1; }

say() { printf '%s\n' "$*"; }

log_init() {
    # log_init <log-dir> <script-name>
    local dir="$1" name="$2"
    if [[ ! -d "$dir" ]] && ! mkdir -p "$dir" 2>/dev/null; then
        warn "cannot create log dir ${dir}; logging to stderr only"
        return 0
    fi
    local candidate
    candidate="${dir}/${name}-$(date -u '+%Y%m%dT%H%M%SZ').log"
    if : >"$candidate" 2>/dev/null; then
        ML_LOG_FILE="$candidate"
        log "log file: ${ML_LOG_FILE}"
    else
        warn "cannot write ${candidate}; logging to stderr only"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Error trap and cleanup hooks
# ---------------------------------------------------------------------------

ML_CLEANUP_HOOKS=()
on_cleanup() { ML_CLEANUP_HOOKS+=("$1"); }

_ml_run_cleanup() {
    local i
    for (( i=${#ML_CLEANUP_HOOKS[@]}-1; i>=0; i-- )); do
        eval "${ML_CLEANUP_HOOKS[$i]}" || true
    done
    ML_CLEANUP_HOOKS=()
}

_ml_on_err() {
    local rc="$1" line="$2" cmd="$3"
    err "failed at line ${line}: ${cmd} (exit ${rc})"
    if [[ -n "$ML_LOG_FILE" ]]; then
        err "full log: ${ML_LOG_FILE}"
    fi
    return 0
}

trap '_ml_on_err "$?" "$LINENO" "$BASH_COMMAND"' ERR
trap '_ml_run_cleanup' EXIT

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

have_cmd() { command -v "$1" >/dev/null 2>&1; }

require_cmd() {
    local missing=() c
    for c in "$@"; do
        have_cmd "$c" || missing+=("$c")
    done
    if (( ${#missing[@]} )); then
        die "required command(s) not found: ${missing[*]}"
    fi
}

have_sudo() {
    # Passwordless only.  An interactive password prompt in the middle of an
    # unattended update is worse than a clean refusal up front.
    have_cmd sudo && sudo -n true 2>/dev/null
}

require_not_root() {
    if [[ "$(id -u)" == "0" ]]; then
        die "refusing to run as root; run as the service user named in manifest runtime.user"
    fi
}

human_bytes() {
    awk -v b="${1:-0}" 'BEGIN{
        split("B KiB MiB GiB TiB", u, " ")
        i = 1
        while (b >= 1024 && i < 5) { b /= 1024; i++ }
        if (i == 1) printf "%d %s", b, u[i]
        else printf "%.1f %s", b, u[i]
    }'
}

disk_free_at_least() {
    # disk_free_at_least <path> <bytes>
    local path="$1" need="$2" probe="$1" avail
    while [[ ! -d "$probe" && "$probe" != "/" ]]; do
        probe="$(dirname "$probe")"
    done
    avail="$(df -PB1 "$probe" | awk 'NR==2 {print $4}')"
    if (( avail < need )); then
        err "insufficient disk space at ${probe}: need $(human_bytes "$need"), have $(human_bytes "$avail")"
        return 1
    fi
    log "disk at ${probe}: $(human_bytes "$avail") free, need $(human_bytes "$need")"
    return 0
}

port_listener() {
    # Prints the listening socket line, empty when the port is free.
    local port="$1"
    if have_cmd ss; then
        ss -H -ltnp "sport = :${port}" 2>/dev/null | sed -n '1p'
    elif have_cmd lsof; then
        lsof -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | sed -n '2p'
    else
        warn "neither ss nor lsof is present; cannot check port ${port}"
        printf ''
    fi
}

port_is_free() { [[ -z "$(port_listener "$1")" ]]; }

# ---------------------------------------------------------------------------
# Version comparison: numeric dot-separated core, optional -prerelease suffix.
# ---------------------------------------------------------------------------

version_cmp() {
    # echoes -1, 0 or 1 for A vs B
    local a="${1%%-*}" b="${2%%-*}"
    if [[ "$a" == "$b" ]]; then
        local sa="${1#"$a"}" sb="${2#"$b"}"
        if   [[ "$sa" == "$sb" ]]; then echo 0
        elif [[ -z "$sa" ]];       then echo 1
        elif [[ -z "$sb" ]];       then echo -1
        elif [[ "$sa" < "$sb" ]];  then echo -1
        else echo 1
        fi
        return 0
    fi
    local -a A B
    IFS=. read -r -a A <<<"$a"
    IFS=. read -r -a B <<<"$b"
    local i n="${#A[@]}"
    if (( ${#B[@]} > n )); then n="${#B[@]}"; fi
    for (( i=0; i<n; i++ )); do
        local x="${A[i]:-0}" y="${B[i]:-0}"
        x="${x//[^0-9]/}"; y="${y//[^0-9]/}"
        x="${x:-0}"; y="${y:-0}"
        if (( 10#$x > 10#$y )); then echo 1; return 0; fi
        if (( 10#$x < 10#$y )); then echo -1; return 0; fi
    done
    echo 0
}

version_gt() { [[ "$(version_cmp "$1" "$2")" == "1" ]]; }
version_eq() { [[ "$(version_cmp "$1" "$2")" == "0" ]]; }

# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------

# A non-login shell -- `ssh host ./update.sh`, cron, or WSL -- frequently has no
# XDG_RUNTIME_DIR, and `systemctl --user` then fails with "Failed to connect to
# bus".  Exporting these two variables is the entire fix, and it belongs before
# the first --user call rather than being discovered on the office machine.
ensure_user_bus() {
    local uid
    uid="$(id -u)"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${uid}}"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
    if [[ ! -d "$XDG_RUNTIME_DIR" ]]; then
        warn "XDG_RUNTIME_DIR ${XDG_RUNTIME_DIR} does not exist; the user systemd session is not running"
        warn "enable it with: loginctl enable-linger $(id -un)"
        return 1
    fi
    return 0
}

sctl() {
    case "${ML_SYSTEMD_SCOPE}" in
        user)
            ensure_user_bus || return 1
            systemctl --user "$@"
            ;;
        system)
            if have_sudo; then
                sudo systemctl "$@"
            else
                die "systemd scope is 'system' but passwordless sudo is unavailable"
            fi
            ;;
        none)
            log "[systemd-scope=none] would run: systemctl $*"
            return 0
            ;;
        *)
            die "systemd scope not detected; call detect_layout first"
            ;;
    esac
}

unit_dir() {
    case "${ML_SYSTEMD_SCOPE}" in
        user)   echo "${HOME}/.config/systemd/user" ;;
        system) echo "/etc/systemd/system" ;;
        none)   echo "${ML_ROOT}/shared/state/fake-systemd" ;;
        *)      die "systemd scope not detected" ;;
    esac
}

unit_is_active()  { sctl is-active  --quiet "$1" 2>/dev/null; }
unit_is_enabled() { sctl is-enabled --quiet "$1" 2>/dev/null; }

linger_enabled() {
    have_cmd loginctl || return 1
    [[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" == "yes" ]]
}

# ---------------------------------------------------------------------------
# Layout detection.
#
# The office layout is not known with certainty: the suite specification calls
# for /home/<user>/ml_platform with `systemd --user`, while the ml_server repo's
# own documentation describes a legacy /opt/ml_server install with system units.
# This function reports what it found and why.  It never chooses between two
# plausible candidates silently -- an ambiguous or legacy result demands an
# explicit --root or --adopt-legacy, because guessing wrong here is the one
# mistake that could damage a working installation.
# ---------------------------------------------------------------------------

ML_LAYOUT_NOTES=()

detect_layout() {
    # detect_layout [want_root] [want_scope=auto] [adopt_legacy=0]
    local want_root="${1:-}" want_scope="${2:-auto}" adopt_legacy="${3:-0}"
    ML_LAYOUT_NOTES=()

    local platform_root="${ML_PLATFORM_ROOT:-${HOME}/ml_platform}"
    local legacy_root="${ML_LEGACY_ROOT:-/opt/ml_server}"

    local platform_dir=0 legacy_dir=0 platform_live=0 legacy_live=0
    [[ -d "$platform_root" ]] && platform_dir=1
    [[ -d "$legacy_root"   ]] && legacy_dir=1
    [[ -e "${platform_root}/current" ]] && platform_live=1
    if [[ -d "${legacy_root}/env" || -f "${legacy_root}/requirements.txt" ]]; then
        legacy_live=1
    fi

    ML_LAYOUT_NOTES+=("candidate ${platform_root}: exists=${platform_dir} active=${platform_live}")
    ML_LAYOUT_NOTES+=("candidate ${legacy_root}: exists=${legacy_dir} active=${legacy_live}")

    if [[ -n "$want_root" ]]; then
        ML_ROOT="$want_root"
        if [[ "$want_root" == "$legacy_root" ]]; then ML_LAYOUT="legacy"; else ML_LAYOUT="platform"; fi
        ML_LAYOUT_NOTES+=("root supplied explicitly: ${ML_ROOT}")
    elif (( platform_live )); then
        ML_ROOT="$platform_root"; ML_LAYOUT="platform"
        ML_LAYOUT_NOTES+=("selected versioned layout: found ${platform_root}/current")
    elif (( legacy_live )); then
        if (( ! adopt_legacy )); then
            err "found a legacy installation at ${legacy_root} and no versioned layout at ${platform_root}"
            err "this script will not convert a working legacy install by accident"
            err "re-run with --adopt-legacy to migrate it, or --root <path> to install alongside it"
            return 2
        fi
        ML_ROOT="$legacy_root"; ML_LAYOUT="legacy"
        ML_LAYOUT_NOTES+=("adopting legacy layout at ${legacy_root} because --adopt-legacy was given")
    elif (( platform_dir )); then
        ML_ROOT="$platform_root"; ML_LAYOUT="platform"
        ML_LAYOUT_NOTES+=("platform root exists but has no active release; treating as a partial install")
    else
        ML_ROOT="$platform_root"; ML_LAYOUT="new"
        ML_LAYOUT_NOTES+=("no installation found; ${platform_root} would be a fresh install")
    fi

    if [[ "$want_scope" != "auto" ]]; then
        ML_SYSTEMD_SCOPE="$want_scope"
        ML_LAYOUT_NOTES+=("systemd scope forced to '${want_scope}'")
    else
        local user_units=0 system_units=0
        if ensure_user_bus 2>/dev/null; then
            user_units="$(systemctl --user list-unit-files "${ML_UNIT_PREFIX}*" --no-legend 2>/dev/null | wc -l)"
        fi
        if have_cmd systemctl; then
            system_units="$(systemctl list-unit-files 'ml_server*' 'microseg*' "${ML_UNIT_PREFIX}*" --no-legend 2>/dev/null | wc -l)"
        fi
        ML_LAYOUT_NOTES+=("unit files found: user=${user_units} system=${system_units}")
        if   (( user_units > 0 ));            then ML_SYSTEMD_SCOPE="user"
        elif (( system_units > 0 ));          then ML_SYSTEMD_SCOPE="system"
        elif [[ "$ML_LAYOUT" == "legacy" ]];  then ML_SYSTEMD_SCOPE="system"
        else                                       ML_SYSTEMD_SCOPE="user"
        fi
        ML_LAYOUT_NOTES+=("systemd scope detected as '${ML_SYSTEMD_SCOPE}'")
    fi

    ML_VENV="${ML_ROOT}/.venv"
    if [[ "$ML_LAYOUT" == "legacy" && -d "${ML_ROOT}/env" ]]; then
        ML_VENV="${ML_ROOT}/env"
    fi
    return 0
}

print_layout() {
    local n
    for n in "${ML_LAYOUT_NOTES[@]}"; do
        log "layout: ${n}"
    done
    log "layout: ROOT=${ML_ROOT} LAYOUT=${ML_LAYOUT} SCOPE=${ML_SYSTEMD_SCOPE} VENV=${ML_VENV}"
}

# ---------------------------------------------------------------------------
# Manifest access.
#
# The archive ships manifest.resolved.json beside the YAML precisely so that
# office-side scripts need nothing beyond python3's standard library: PyYAML
# cannot be assumed present on a freshly provisioned host.
# ---------------------------------------------------------------------------

ML_MANIFEST=""

manifest_load() {
    local path="$1"
    [[ -f "$path" ]] || die "manifest not found: ${path}"
    ML_MANIFEST="$path"
    local schema
    schema="$(mf '.schema' 2>/dev/null || true)"
    if [[ "$schema" != "$ML_MANIFEST_SCHEMA" ]]; then
        die "manifest schema is '${schema:-<unreadable>}', expected '${ML_MANIFEST_SCHEMA}'"
    fi
}

mf() {
    # mf <dotted.path>  -- '.' returns the whole document
    python3 - "$ML_MANIFEST" "$1" <<'PYEOF'
import json
import sys

path, query = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    node = json.load(handle)
for part in [p for p in query.strip(".").split(".") if p]:
    node = node[int(part)] if isinstance(node, list) else node[part]

if isinstance(node, list) and all(isinstance(item, str) for item in node):
    print("\n".join(node))
elif isinstance(node, (dict, list)):
    print(json.dumps(node, indent=2, sort_keys=True))
elif isinstance(node, bool):
    print("true" if node else "false")
elif node is None:
    print("")
else:
    print(node)
PYEOF
}

mf_or() {
    local value
    if value="$(mf "$1" 2>/dev/null)" && [[ -n "$value" ]]; then
        printf '%s\n' "$value"
    else
        printf '%s\n' "$2"
    fi
}

service_ids() {
    python3 - "$ML_MANIFEST" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
for name in document.get("services", {}):
    print(name)
PYEOF
}

svc() {
    # svc <service-id> <field> [default]
    mf_or "services.${1}.${2}" "${3:-}"
}

# ---------------------------------------------------------------------------
# Deployment history: shared/state/history.jsonl
# ---------------------------------------------------------------------------

history_file() { echo "${ML_ROOT}/shared/state/history.jsonl"; }

history_append() {
    local file
    file="$(history_file)"
    mkdir -p "$(dirname "$file")"
    printf '%s\n' "$1" >>"$file"
}

history_last_good() {
    # history_last_good [version-to-exclude]
    #
    # The most recently deployed version whose LATEST recorded outcome is ok.
    #
    # Taking simply "the newest entry that says ok" is wrong: a version can be
    # deployed successfully, deployed again later and fail, and the older ok
    # entry would still select it. The last thing known about a release is what
    # counts, so records are collapsed per version first.
    local exclude="${1:-}" file
    file="$(history_file)"
    [[ -f "$file" ]] || return 1
    python3 - "$file" "$exclude" <<'PYEOF'
import json
import sys

path, exclude = sys.argv[1], sys.argv[2]

latest_per_version: dict[str, str] = {}
order: list[str] = []
with open(path, encoding="utf-8") as handle:
    for line in handle:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        version = record.get("suite_version")
        if not version:
            continue
        latest_per_version[version] = record.get("health", "")
        if version in order:
            order.remove(version)
        order.append(version)

choice = ""
for version in order:
    if exclude and version == exclude:
        continue
    if latest_per_version.get(version) == "ok":
        choice = version
print(choice, end="")
PYEOF
}

current_version() {
    local link="${ML_ROOT}/current"
    if [[ -e "${link}/VERSION" ]]; then
        tr -d '[:space:]' <"${link}/VERSION"
    else
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

ML_LOCK_FD=""

acquire_lock() {
    local lockfile="${ML_ROOT}/shared/state/deploy.lock"
    mkdir -p "$(dirname "$lockfile")"
    exec {ML_LOCK_FD}>"$lockfile"
    if ! flock -n "$ML_LOCK_FD"; then
        die "another deployment is already running on ${ML_ROOT} (lock: ${lockfile})"
    fi
    printf 'pid=%s script=%s started=%s\n' "$$" "${0##*/}" "$(_ml_stamp)" >&"$ML_LOCK_FD"
    on_cleanup "release_lock"
}

release_lock() {
    [[ -n "$ML_LOCK_FD" ]] || return 0
    flock -u "$ML_LOCK_FD" 2>/dev/null || true
    eval "exec ${ML_LOCK_FD}>&-" 2>/dev/null || true
    ML_LOCK_FD=""
}

# ---------------------------------------------------------------------------
# Mutation audit -- the rehearsal harness uses this to prove that a refused
# deployment changed nothing whatsoever.
# ---------------------------------------------------------------------------

tree_fingerprint() {
    local root="$1"
    if [[ ! -d "$root" ]]; then
        echo "ABSENT"
        return 0
    fi
    find "$root" -printf '%p\t%s\t%y\t%m\n' 2>/dev/null | LC_ALL=C sort | sha256sum | cut -d' ' -f1
}

# ---------------------------------------------------------------------------
# HTTP probes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pre-installed packages
#
# Some dependencies are put into the environment by hand, offline, because their
# wheels are large and not available from the normal mirror. torch is the reason
# this exists. They are verified rather than installed.
# ---------------------------------------------------------------------------

# installed_version <venv-python> <distribution> -- prints the version, or empty
installed_version() {
    local python_bin="$1" distribution="$2"
    "$python_bin" - "$distribution" <<'PYEOF' 2>/dev/null || true
import sys

try:
    from importlib.metadata import PackageNotFoundError, version
    print(version(sys.argv[1]))
except Exception:
    print("")
PYEOF
}

# Prerequisites are accumulated across every check and reported together. Being
# told about one missing package, installing it, re-running and being told about
# the next is a miserable way to bring up a server that you can only reach
# during a maintenance window.
ML_MISSING_APT=()        # apt package names
ML_MISSING_PYTHON=()     # python distribution names
ML_MISSING_NOTES=()      # human-readable "name -- why" lines

reset_prerequisite_state() {
    ML_MISSING_APT=()
    ML_MISSING_PYTHON=()
    ML_MISSING_NOTES=()
}

# check_system_requirements — reads system_requirements from the manifest.
check_system_requirements() {
    local command_name package reason
    while IFS=$'\t' read -r command_name package reason; do
        [[ -n "$command_name" ]] || continue
        if have_cmd "$command_name"; then
            log "system requirement ${command_name} present"
        else
            ML_MISSING_APT+=("$package")
            ML_MISSING_NOTES+=("${command_name} (apt: ${package}) -- ${reason}")
        fi
    done < <(python3 - "$ML_MANIFEST" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
for entry in document.get("system_requirements") or []:
    reason = " ".join((entry.get("why") or "").split())
    print(f"{entry.get('command', '')}\t{entry.get('package', '')}\t{reason}")
PYEOF
)
}

# check_preinstalled <venv-python> <resolved-requirements-or-empty> <pkg...>
check_preinstalled() {
    local python_bin="$1" resolved="$2"
    shift 2
    local package found pinned

    for package in "$@"; do
        [[ -n "$package" ]] || continue
        found="$(installed_version "$python_bin" "$package")"
        if [[ -z "$found" ]]; then
            ML_MISSING_PYTHON+=("$package")
            ML_MISSING_NOTES+=("${package} (python package, into ${python_bin%/bin/python}) -- installed by hand because its wheels are large and not on the normal mirror")
            continue
        fi
        ok "pre-installed ${package} ${found} found in the environment"
        # The release was built and tested against a specific version. A
        # different one is allowed -- it is the operator's deliberate choice --
        # but silently differing from what was tested is worth saying out loud.
        if [[ -n "$resolved" && -f "$resolved" ]]; then
            pinned="$(sed -n "s/^${package}==\(.*\)$/\1/Ip" "$resolved" | head -1)"
            if [[ -n "$pinned" && "$pinned" != "$found" ]]; then
                warn "  this release was tested with ${package}==${pinned}; the host has ${found}"
                warn "  the installed one will be kept and left untouched"
            fi
        fi
    done
}

# report_missing_prerequisites <venv-python>
# Returns 1 if anything is missing, after printing one consolidated report.
report_missing_prerequisites() {
    local python_bin="$1"
    local total=$(( ${#ML_MISSING_APT[@]} + ${#ML_MISSING_PYTHON[@]} ))
    (( total == 0 )) && return 0

    err ""
    err "This host is missing ${total} prerequisite(s). NOTHING HAS BEEN CHANGED."
    err ""
    err "These are never installed automatically: system packages need root and"
    err "come from your own apt mirror, and the python packages listed here were"
    err "installed by hand for good reasons. Install them, then re-run this update."
    err ""
    local note
    for note in "${ML_MISSING_NOTES[@]}"; do
        err "  * ${note}"
    done
    err ""
    if (( ${#ML_MISSING_APT[@]} )); then
        err "Install the system packages from your internal apt mirror:"
        err "    sudo apt-get install -y ${ML_MISSING_APT[*]}"
        err ""
    fi
    if (( ${#ML_MISSING_PYTHON[@]} )); then
        err "Install the python packages offline into the deployment environment:"
        err "    ${python_bin} -m pip install --no-index \\"
        err "        --find-links /path/to/wheels ${ML_MISSING_PYTHON[*]}"
        err ""
    fi
    err "Then run this same command again."
    return 1
}

# ---------------------------------------------------------------------------
# Unit ownership
#
# Unit names come from the manifest, so two deployments on the same host -- a
# staging root alongside production, say -- want the same unit files. Installing
# one takes the units away from the other. That is allowed, but it must never be
# a surprise, so it is detected and announced before anything is written.
# ---------------------------------------------------------------------------

ML_TAKEOVER_UNITS=()   # "unit<TAB>other-root" for units belonging elsewhere

# unit_deployment_root <unit-file> -- prints the root the unit currently serves
unit_deployment_root() {
    local unit_file="$1" workdir
    [[ -f "$unit_file" ]] || return 0
    workdir="$(sed -n 's/^WorkingDirectory=\(.*\)$/\1/p' "$unit_file" | head -1)"
    [[ -n "$workdir" ]] || return 0

    # A unit this tool wrote has WorkingDirectory=<root>/current/apps/<component>.
    if [[ "$workdir" == */current/* ]]; then
        printf '%s' "${workdir%%/current/*}"
        return 0
    fi

    # Anything else was written by hand or by an older arrangement -- the office
    # deployment, for instance, runs from <root>/src/<component>/<component>-main
    # with no `current` symlink at all. Reporting the full working directory
    # there would be misleading, so the deployment root is inferred by trimming
    # the component path, and the whole path is shown when that is not possible.
    if [[ "$workdir" == */src/* ]]; then
        printf '%s' "${workdir%%/src/*}"
        return 0
    fi
    printf '%s' "$workdir"
}

# check_unit_takeover <unit-dir> <this-root> <unit...>
check_unit_takeover() {
    local unit_dir="$1" this_root="$2"
    shift 2
    ML_TAKEOVER_UNITS=()
    local unit other

    for unit in "$@"; do
        [[ -n "$unit" ]] || continue
        other="$(unit_deployment_root "${unit_dir}/${unit}")"
        if [[ -n "$other" && "$other" != "$this_root" ]]; then
            ML_TAKEOVER_UNITS+=("${unit}"$'\t'"${other}")
        fi
    done

    (( ${#ML_TAKEOVER_UNITS[@]} )) || return 0

    local entry name root
    warn ""
    warn "TAKING OVER ${#ML_TAKEOVER_UNITS[@]} SERVICE(S) FROM ANOTHER DEPLOYMENT"
    warn ""
    for entry in "${ML_TAKEOVER_UNITS[@]}"; do
        name="${entry%%$'\t'*}"
        root="${entry##*$'\t'}"
        warn "    ${name}"
        warn "        currently serving: ${root}"
    done
    warn ""
    warn "These units will be stopped, their unit files overwritten, and restarted"
    warn "against this deployment (${this_root}). The other deployment's files stay"
    warn "on disk but will no longer be served by systemd."
    warn ""
    warn "If that is not what you want, re-run with --root ${root} to update the"
    warn "existing deployment in place instead."
    warn ""
    return 0
}

http_status() {
    # http_status <url> [timeout-seconds] -- prints 000 when unreachable.
    # curl already prints 000 on a connection failure AND exits non-zero, so the
    # fallback has to replace that output rather than be appended to it.
    local code
    if code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-5}" "$1" 2>/dev/null)"; then
        printf '%s' "${code:-000}"
    else
        printf '000'
    fi
}

wait_for_http() {
    # wait_for_http <url> <deadline-seconds>
    local url="$1" deadline="${2:-60}" waited=0 code
    while (( waited < deadline )); do
        code="$(http_status "$url" 3)"
        if [[ "$code" =~ ^[23] ]]; then
            return 0
        fi
        sleep 2
        waited=$(( waited + 2 ))
    done
    return 1
}

# ---------------------------------------------------------------------------
# The intranet host.
#
# bind_host is what the sockets listen on; this is the address other machines
# use to reach them, and therefore what the portal must put in the links it
# renders. Conflating the two is what made every catalog link in v1.4.0 point
# at 127.0.0.1: correct from the server itself, useless from every other desk.
#
# Prints the empty string when it cannot be determined, so every caller has to
# decide what to do about that rather than silently inheriting a wrong answer.
# ---------------------------------------------------------------------------

detect_intranet_host() {
    # detect_intranet_host [explicit-override]
    local want="${1:-}"
    if [[ -z "$want" ]]; then
        want="${ML_INTRANET_HOST:-}"
    fi
    if [[ -z "$want" ]]; then
        want="$(mf_or 'runtime.intranet_host' auto)"
    fi
    if [[ -n "$want" && "$want" != "auto" ]]; then
        printf '%s' "$want"
        return 0
    fi

    # The address on the interface holding the default route. That is the one
    # the rest of the intranet reaches, which is the question being asked --
    # `hostname -I` would answer with whichever NIC happens to be listed first.
    local address=""
    if have_cmd ip; then
        address="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.*[[:space:]]src[[:space:]]\([0-9.]\+\).*/\1/p' | head -1)"
    fi
    if [[ -z "$address" ]] && have_cmd hostname; then
        address="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^127\.' | grep -v '^$' | head -1)"
    fi
    printf '%s' "$address"
}

host_is_loopback() {
    case "${1:-}" in
        127.*|localhost|localhost.*|::1|0.0.0.0|"") return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Journal access.
#
# Reads only the CURRENT run of a unit -- everything since its
# ActiveEnterTimestamp -- so an assertion about a fault this very deployment
# fixed cannot be failed by a stale entry from before the restart.
# ---------------------------------------------------------------------------

unit_journal() {
    # unit_journal <unit> [lines]
    local unit="$1" lines="${2:-200}" since=""
    have_cmd journalctl || return 0
    since="$(sctl show "$unit" -p ActiveEnterTimestamp --value 2>/dev/null || true)"

    local -a args=(--no-pager --output=cat -n "$lines" -u "$unit")
    # An empty or epoch-zero timestamp means the unit has never been active, so
    # there is no current run to bound the query with.
    if [[ -n "$since" && "$since" != "0" ]]; then
        args+=(--since "$since")
    fi

    case "${ML_SYSTEMD_SCOPE}" in
        user)
            ensure_user_bus 2>/dev/null || return 0
            journalctl --user "${args[@]}" 2>/dev/null || true
            ;;
        system)
            if have_sudo; then
                sudo journalctl "${args[@]}" 2>/dev/null || true
            else
                journalctl "${args[@]}" 2>/dev/null || true
            fi
            ;;
        *)
            return 0
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Seeding persistent state.
#
# shared_dirs creates empty directories; these helpers put into them the things
# no release archive can carry -- the environment file, the site config, the
# trained checkpoints, the sample micrographs.
#
# Two rules make this safe to run on every deployment, forever:
#
#   1. A target that is already populated is never touched. An operator's
#      hand-edited env file survives every future upgrade byte for byte.
#   2. Sources are read, never moved. Adopting the checkpoints out of
#      /opt/microseg leaves /opt/microseg exactly as it was.
#
# The seed_* functions that copy or write are named for it and are called only
# from update.sh, after preflight has already reported what they will do.
# ---------------------------------------------------------------------------

# Paths substituted into a seed's `sources`. update.sh sets these before it
# calls anything below; the fallback makes an unset token expand to a path that
# cannot exist rather than to a bare "/".
ML_SEED_RELEASE=""
ML_SEED_PREVIOUS=""
ML_SEED_BACKUP=""

seed_specs() {
    # One record per seed, fields separated by U+001F (the unit separator):
    #   id  kind  target  marker  required  generate  sources(|-separated)  why
    #
    # Not TAB. Tab is an IFS whitespace character, so `read` collapses a run of
    # them and an empty field -- three of these four seeds have one -- shifts
    # every field after it one place to the left. That parsed silently and
    # wrongly, which is the worst way for a deployment to be wrong.
    python3 - "$ML_MANIFEST" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)

for entry in document.get("seeds") or []:
    sources = "|".join(str(item) for item in (entry.get("sources") or []))
    why = " ".join((entry.get("why") or "").split())
    print("\x1f".join([
        str(entry.get("id", "")),
        str(entry.get("kind", "file")),
        str(entry.get("target", "")),
        str(entry.get("marker", "")),
        "true" if entry.get("required") else "false",
        str(entry.get("generate", "")),
        sources,
        why,
    ]))
PYEOF
}

seed_expand() {
    # seed_expand <path-with-tokens>
    local text="$1"
    text="${text//\{root\}/$ML_ROOT}"
    text="${text//\{release\}/${ML_SEED_RELEASE:-/nonexistent}}"
    text="${text//\{previous\}/${ML_SEED_PREVIOUS:-/nonexistent}}"
    text="${text//\{backup\}/${ML_SEED_BACKUP:-/nonexistent}}"
    text="${text//\{home\}/$HOME}"
    printf '%s' "$text"
}

seed_is_populated() {
    # seed_is_populated <kind> <path> <marker>
    #
    # "Populated", not "exists". An empty frozen_checkpoints/ directory exists
    # and satisfies nothing, and that distinction is the entire point of the
    # `marker` field.
    local kind="$1" path="$2" marker="$3"
    case "$kind" in
        file)
            [[ -s "$path" ]]
            ;;
        tree)
            [[ -d "$path" ]] || return 1
            if [[ -n "$marker" ]]; then
                [[ -e "${path}/${marker}" ]]
            else
                [[ -n "$(find "$path" -mindepth 1 -type f -print -quit 2>/dev/null)" ]]
            fi
            ;;
        *)
            return 1
            ;;
    esac
}

seed_pick_source() {
    # seed_pick_source <kind> <marker> <sources-pipe-separated>
    # Prints the first source that exists and is itself populated.
    local kind="$1" marker="$2" sources="$3" candidate expanded
    [[ -n "$sources" ]] || return 1
    local IFS='|'
    for candidate in $sources; do
        [[ -n "$candidate" ]] || continue
        expanded="$(seed_expand "$candidate")"
        if seed_is_populated "$kind" "$expanded" "$marker"; then
            printf '%s' "$expanded"
            return 0
        fi
    done
    return 1
}

seed_copy() {
    # seed_copy <kind> <source> <target>   -- MUTATES
    local kind="$1" source="$2" target="$3"
    case "$kind" in
        file)
            mkdir -p "$(dirname "$target")" || return 1
            cp -p "$source" "$target" || return 1
            ;;
        tree)
            mkdir -p "$target" || return 1
            # The trailing /. copies the CONTENTS, so an existing (empty)
            # target directory is filled rather than nested inside itself.
            cp -a "${source}/." "${target}/" || return 1
            ;;
        *)
            return 1
            ;;
    esac
    return 0
}

# service_url_vars <host> [port-offset]
#
# "KEY<TAB>http://host:port" for every service declaring public_url_env. The
# offset is applied so a staging root deployed with --port-offset advertises
# the ports it actually listens on rather than production's.
service_url_vars() {
    local host="$1" offset="${2:-0}" id key port
    while read -r id; do
        key="$(svc "$id" public_url_env '')"
        [[ -n "$key" ]] || continue
        port="$(svc "$id" port '')"
        [[ -n "$port" ]] || continue
        printf '%s\thttp://%s:%s\n' "$key" "$host" "$(( port + offset ))"
    done < <(service_ids)
}

seed_generate_service_urls() {
    # seed_generate_service_urls <target> <host> [port-offset]   -- MUTATES
    local target="$1" host="$2" offset="${3:-0}"
    [[ -n "$host" ]] || return 1
    mkdir -p "$(dirname "$target")" || return 1
    {
        printf '# ml-platform.env -- generated by deploy/update.sh on %s\n' "$(_ml_stamp)"
        printf '#\n'
        printf '# Loaded by systemd through EnvironmentFile= for the portal. These are the\n'
        printf '# URLs the portal puts in the catalog, so they have to be addresses other\n'
        printf '# machines on the intranet can reach -- not loopback.\n'
        printf '#\n'
        printf '# Safe to edit by hand. Nothing regenerates or overwrites this file once it\n'
        printf '# exists: a later deployment only APPENDS a variable that is missing\n'
        printf '# entirely, and never changes a value already in it.\n'
        printf '#\n'
        printf '# Host detected as: %s\n' "$host"
        printf '\n'
        service_url_vars "$host" "$offset" | while IFS=$'\t' read -r key value; do
            printf '%s=%s\n' "$key" "$value"
        done
    } >"$target" || return 1
    return 0
}

# seed_env_topup <file> <host> [port-offset]   -- MUTATES
#
# An env file adopted from the pre-suite install predates every variable added
# since. Values already present are never touched -- they may have been set by
# hand for good reasons -- but a variable absent ENTIRELY is appended, because
# the alternative is the portal silently falling back to loopback for that one
# service. Prints the names it added, one per line.
seed_env_topup() {
    local file="$1" host="$2" offset="${3:-0}" key value
    local added=()
    [[ -f "$file" && -n "$host" ]] || return 0
    while IFS=$'\t' read -r key value; do
        [[ -n "$key" ]] || continue
        if ! grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file"; then
            added+=("$key")
            printf '%s=%s\n' "$key" "$value" >>"$file"
        fi
    done < <(service_url_vars "$host" "$offset")
    (( ${#added[@]} )) || return 0
    printf '%s\n' "${added[@]}"
    return 0
}

# ---------------------------------------------------------------------------
# Documentation built on this server.
#
# A component may render its own documentation here rather than shipping it.
# PyTex is the case this exists for: it renders its Sphinx site into the
# installed package, which works for a wheel and cannot work for this suite,
# because application code is run from source over PYTHONPATH -- deliberately,
# since that is what makes a rollback a symlink swap needing no network. The
# built site is tens of megabytes of generated output and is git-ignored like
# any other build product, so it reaches neither the component checkout nor the
# release archive, and /docs answered 404 on this host for exactly that reason.
#
# Three properties make this safe to run on a production rollout, and all three
# are load-bearing:
#
#   1. It runs after the deployment has succeeded, so nothing waits on it and a
#      slow build costs no downtime.
#   2. It cannot fail a deployment. Every failure path below warns and returns
#      0; the worst outcome is the 404 the host had before.
#   3. It is keyed to the component's commit, so a suite release that does not
#      move the component reuses the existing build.
#
# The result lives in shared/ and is pointed at by an environment variable on
# the service (PYTEX_DOCS_ROOT for PyTex), so it survives upgrades, rollbacks
# and pruning.
# ---------------------------------------------------------------------------

docs_build_one() {
    # docs_build_one <service-id> <release-dir>   -- MUTATES shared/, never the release
    local id="$1" release="$2"
    local target marker command timeout commit abs stamp built workdir tmp rendered src item
    local -a req_args=()

    target="$(mf_or "services.${id}.docs_build.target" '')"
    command="$(mf_or "services.${id}.docs_build.command" '')"
    [[ -n "$target" && -n "$command" ]] || return 0

    marker="$(mf_or "services.${id}.docs_build.marker" 'index.html')"
    timeout="$(mf_or "services.${id}.docs_build.timeout_seconds" '3600')"
    commit="$(svc "$id" commit '')"
    abs="${ML_ROOT}/${target}"
    stamp="${abs}/.built-from"
    workdir="${release}/apps/$(svc "$id" dir "$id")"
    src="$(svc "$id" src '.')"

    built=""
    [[ -f "$stamp" ]] && built="$(<"$stamp")"
    # The stamp, not merely the marker: a marker alone would go on serving the
    # previous version's pages after an upgrade that did move the component.
    if [[ -f "${abs}/${marker}" && -n "$commit" && "$built" == "$commit" ]]; then
        ok "docs ${id}: already built from ${commit:0:12}"
        return 0
    fi

    if [[ ! -d "$workdir" ]]; then
        warn "docs ${id}: ${workdir} is not in this release; skipped"
        return 0
    fi

    step "building documentation for ${id}"
    log "this can take tens of minutes; the suite is already serving"

    while read -r item; do
        [[ -n "$item" ]] || continue
        req_args+=("$item")
    done < <(mf "services.${id}.docs_build.requirements" 2>/dev/null || true)

    # Missing build dependencies are the ordinary case on a host whose mirror
    # does not carry them, and they are not a deployment problem. Say so once,
    # name what is needed, and leave.
    if (( ${#req_args[@]} )); then
        log "installing the documentation build dependencies"
        # PIP_INDEX_ARGS belongs to update.sh; build_docs.sh sources only this
        # library, so it is expanded in the form that tolerates being unset.
        if ! ( cd "$workdir" && "${ML_VENV}/bin/python" -m pip install \
                --disable-pip-version-check ${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"} "${req_args[@]}" \
                >>"${ML_LOG_FILE:-/dev/null}" 2>&1 ); then
            warn "docs ${id}: the build dependencies could not be installed"
            warn "  the mirror needs: ${req_args[*]}"
            warn "  /docs answers 404 exactly as it did before; nothing else is affected"
            warn "  install them and run deploy/build_docs.sh to finish the job"
            return 0
        fi
    fi

    # Rendered beside the target and swapped in, so a reader never meets a
    # half-written tree and an interrupted build cannot destroy a good one.
    tmp="${abs}.new"
    rm -rf "$tmp"
    mkdir -p "$(dirname "$abs")"
    rendered="${command//\{target\}/$tmp}"
    log "\$ ${rendered}"

    if ! ( cd "$workdir" \
            && PATH="${ML_VENV}/bin:${PATH}" \
               PYTHONPATH="${workdir}/${src}" \
               timeout "${timeout}s" bash -c "$rendered" \
               >>"${ML_LOG_FILE:-/dev/null}" 2>&1 ); then
        warn "docs ${id}: the build failed or exceeded ${timeout}s"
        warn "  /docs keeps whatever it had; the deployment is unaffected"
        [[ -n "${ML_LOG_FILE:-}" ]] && warn "  see ${ML_LOG_FILE}"
        rm -rf "$tmp"
        return 0
    fi

    if [[ ! -f "${tmp}/${marker}" ]]; then
        warn "docs ${id}: the build produced no ${marker}, so it did not finish; discarded"
        rm -rf "$tmp"
        return 0
    fi

    printf '%s\n' "$commit" >"${tmp}/.built-from"
    rm -rf "${abs}.old"
    [[ -d "$abs" ]] && mv "$abs" "${abs}.old"
    mv "$tmp" "$abs"
    rm -rf "${abs}.old"
    ok "docs ${id}: built into ${target} ($(du -sh "$abs" 2>/dev/null | cut -f1 || echo '?'))"
    return 0
}

# ---------------------------------------------------------------------------
# The portal's shared configuration
# ---------------------------------------------------------------------------
#
# ~/ml_platform/shared/config/config.intranet.json is the one file that holds
# this site's settings and secrets. It is seeded once and hand-edited
# afterwards, so every release that follows meets a file an operator wrote for
# an older release. Three rules follow from that, and they are the whole reason
# this section exists:
#
#   * it is NEVER blindly overwritten -- the values in it cannot be regenerated;
#   * it is validated against the NEW release's schema BEFORE `current` moves,
#     so an incompatible config is a preflight refusal rather than a portal that
#     will not start after the swap;
#   * a migration copies the original beside it first, so an operator can always
#     put back exactly what was there.
#
# The work itself is done by ml_server.config_cli in the release being
# installed, not by shell: the schema belongs to the application, and a copy of
# it in bash would drift from it within one release.

#: Path of the live shared configuration, relative to the deployment root.
ML_PORTAL_CONFIG_REL="shared/config/config.intranet.json"

portal_config_path() {
    printf '%s\n' "${ML_ROOT}/${ML_PORTAL_CONFIG_REL}"
}

# config_tool <release-dir> <action> [args...]
#
# Run the config CLI out of <release-dir>, using the deployment venv's
# interpreter but the release's own source tree. PYTHONPATH rather than an
# install, so this works in preflight -- before the new release's dependencies
# have been installed and long before `current` points at it.
#
# Exit codes come straight from ml_server.config_cli:
#   0 usable   1 invalid   2 ambiguous, a human must decide   3 unreadable
# and 4 is added here for "this release has no config tool", which is what an
# older release looks like and must not be treated as a failure.
config_tool() {
    local release="$1"; shift
    local dir src python
    # From the manifest when one is loaded, and from the office layout's own
    # names otherwise -- status.sh and health_check.sh always have a manifest,
    # while update.sh calls this against a staging directory in preflight.
    dir="$(svc gateway dir ml_server 2>/dev/null || echo ml_server)"
    src="${release}/apps/${dir:-ml_server}/$(svc gateway src src 2>/dev/null || echo src)"
    python="${ML_VENV}/bin/python"

    [[ -f "${src}/ml_server/config_cli.py" ]] || return 4
    [[ -x "$python" ]] || python="$(command -v python3 || true)"
    [[ -n "$python" ]] || return 4

    PYTHONPATH="$src" "$python" -m ml_server.config_cli "$@"
}

# config_preflight <release-dir>
#
# Phase A. Reports what migrating the live config would do, and refuses the
# deployment when that would be a guess or the result would not validate.
# Changes nothing.
config_preflight() {
    local release="$1"
    local config output status
    config="$(portal_config_path)"

    if [[ ! -f "$config" ]]; then
        log "  no shared config at ${config} yet; it will be seeded from the release"
        return 0
    fi

    set +e
    output="$(config_tool "$release" plan "$config" 2>&1)"
    status=$?
    set -e

    case "$status" in
        0)
            printf '%s\n' "$output" | while IFS= read -r line; do log "  ${line}"; done
            ok "  shared config is usable by this release"
            return 0
            ;;
        4)
            warn "  this release ships no config validator; the shared config is not checked"
            return 0
            ;;
        1)
            err "  the shared config is not valid for this release:"
            ;;
        2)
            err "  the shared config cannot be migrated without a decision:"
            ;;
        3)
            err "  the shared config cannot be read:"
            ;;
        *)
            err "  the config check failed unexpectedly (exit ${status}):"
            ;;
    esac
    printf '%s\n' "$output" | while IFS= read -r line; do err "    ${line}"; done
    err "  file: ${config}"
    err "  NOTHING HAS BEEN CHANGED. Fix the file and run this command again."
    return 1
}

# config_migrate_apply <release-dir> <stamp>
#
# Phase B, before the symlink swap. Migrates in place with a backup. A failure
# here is a hard failure: the caller must not activate a release whose
# configuration it could not prepare.
config_migrate_apply() {
    local release="$1" stamp="$2"
    local config output status
    config="$(portal_config_path)"

    if [[ ! -f "$config" ]]; then
        warn "  no shared config at ${config}; the portal will fall back to its defaults"
        return 0
    fi

    set +e
    output="$(config_tool "$release" migrate "$config" --stamp "$stamp" 2>&1)"
    status=$?
    set -e

    if (( status == 4 )); then
        warn "  this release ships no config migrator; ${ML_PORTAL_CONFIG_REL} left untouched"
        return 0
    fi
    printf '%s\n' "$output" | while IFS= read -r line; do log "  ${line}"; done
    if (( status != 0 )); then
        err "  migrating ${ML_PORTAL_CONFIG_REL} failed (exit ${status})"
        return 1
    fi
    ok "  shared config validated and migrated; original kept alongside it"
    return 0
}

# config_summary_json <release-dir>
#
# The secret-free description of the live configuration, as JSON, for status.sh
# and health_check.sh. Prints nothing and returns non-zero when unavailable.
config_summary_json() {
    local release="$1"
    local config
    config="$(portal_config_path)"
    [[ -f "$config" ]] || return 1
    config_tool "$release" summary "$config" --json 2>/dev/null
}

# config_summary_field <json> <key>
config_summary_field() {
    python3 -c '
import json, sys
try:
    print(json.loads(sys.argv[1]).get(sys.argv[2], ""))
except Exception:
    print("")
' "$1" "$2" 2>/dev/null || printf '\n'
}
