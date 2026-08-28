#!/usr/bin/env bash
# run.sh -- full deployment rehearsal on Ubuntu.
#
# Builds a disposable "office server" under ~/rehearsal and drives the real
# deployment scripts against it, with real systemd --user units, with GitHub
# genuinely unreachable, and with the same failure cases that would otherwise
# only be discovered in the office.
#
#   ./tests/rehearsal/run.sh                # every scenario
#   ./tests/rehearsal/run.sh fresh_install  # just one
#   ./tests/rehearsal/run.sh --list
#
# Nothing outside ${REHEARSAL_HOME} (default ~/rehearsal) is touched. Fixture
# units are suffixed -rehearsal and fixture ports are offset by 2000, so a
# rehearsal cannot collide with a real deployment on the same machine.
#
# That isolation is load-bearing, not decorative. An earlier version of this
# harness installed units under the real names and removed every
# ml-platform-*.service on the host between scenarios; running it alongside a
# real deployment destroyed that deployment's units.

set -Eeuo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HARNESS_DIR}/../.." && pwd)"

REHEARSAL_HOME="${REHEARSAL_HOME:-${HOME}/rehearsal}"
FIXTURES="${REHEARSAL_HOME}/fixtures"
DIST="${REHEARSAL_HOME}/dist"
SHIMS="${REHEARSAL_HOME}/shims"
WORK="${REHEARSAL_HOME}/work"
# The rehearsal ships its own requirements directory rather than whatever the
# repository happens to contain. A release build writes the real 105-package
# resolved.txt into requirements/, and a fixture archive built afterwards
# inherited it, then tried to install torch with the network deliberately
# blocked. An empty resolved.txt still exercises update.sh's install path and
# needs no network to do it.
FIXTURE_REQS="${REHEARSAL_HOME}/fixture-requirements"

# Must match UNIT_SUFFIX / PORT_OFFSET in make_fixtures.py.
UNIT_SUFFIX="-rehearsal"
PORT_OFFSET=2000

PASSED=0
FAILED=0
FAILED_NAMES=()
KEEP_GOING="${KEEP_GOING:-1}"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

if [[ -t 1 ]]; then C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_CYA=$'\033[36m'; C_OFF=$'\033[0m'
else C_RED=""; C_GRN=""; C_YEL=""; C_CYA=""; C_OFF=""; fi

info()  { printf '%s\n' "    $*"; }
note()  { printf '%s%s%s\n' "$C_CYA" "    $*" "$C_OFF"; }
pass()  { printf '%s%s%s\n' "$C_GRN" "    PASS  $*" "$C_OFF"; }
fail()  { printf '%s%s%s\n' "$C_RED" "    FAIL  $*" "$C_OFF"; }
warn2() { printf '%s%s%s\n' "$C_YEL" "    WARN  $*" "$C_OFF"; }

banner() {
    printf '\n%s\n' "==================================================================="
    printf ' %s\n' "$*"
    printf '%s\n' "==================================================================="
}

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

ASSERT_FAILURES=0

assert() {
    local description="$1"; shift
    if "$@"; then
        pass "$description"
    else
        fail "$description  (command: $*)"
        ASSERT_FAILURES=$(( ASSERT_FAILURES + 1 ))
    fi
}

assert_eq() {
    local description="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$description"
    else
        fail "$description"
        info "  expected: ${expected}"
        info "  actual:   ${actual}"
        ASSERT_FAILURES=$(( ASSERT_FAILURES + 1 ))
    fi
}

assert_contains() {
    local description="$1" haystack="$2" needle="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        pass "$description"
    else
        fail "$description"
        info "  expected to find: ${needle}"
        info "  in: $(printf '%s' "$haystack" | tail -5)"
        ASSERT_FAILURES=$(( ASSERT_FAILURES + 1 ))
    fi
}

assert_fails() {
    local description="$1"; shift
    if "$@" >/dev/null 2>&1; then
        fail "$description  (command unexpectedly succeeded: $*)"
        ASSERT_FAILURES=$(( ASSERT_FAILURES + 1 ))
    else
        pass "$description"
    fi
}

# ---------------------------------------------------------------------------
# Offline simulation.
#
# The office server cannot reach GitHub. Rather than trust that the scripts
# never try, the rehearsal makes the attempt actually fail: a dead proxy for
# every outbound HTTP client, and a git that refuses to run. Loopback is
# exempted so the health checks still work.
#
# This needs no root, which matters because the same harness has to run on a CI
# runner and on a developer's WSL install.
# ---------------------------------------------------------------------------

setup_offline() {
    mkdir -p "$SHIMS"
    cat >"${SHIMS}/git" <<'SHIM'
#!/usr/bin/env bash
echo "git: blocked by the rehearsal harness (the office server has no GitHub access)" >&2
exit 128
SHIM
    chmod +x "${SHIMS}/git"
    export PATH="${SHIMS}:${PATH}"
    export http_proxy="http://127.0.0.1:9"
    export https_proxy="http://127.0.0.1:9"
    export HTTP_PROXY="$http_proxy"
    export HTTPS_PROXY="$https_proxy"
    export no_proxy="127.0.0.1,localhost,::1"
    export NO_PROXY="$no_proxy"
}

verify_offline() {
    # curl prints 000 on a connection failure and also exits non-zero, so a
    # `|| echo 000` fallback appends a second 000 and the comparison never
    # matches. Capture the output, then decide.
    local code
    if ! code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://github.com 2>/dev/null)"; then
        code="000"
    fi
    if [[ "$code" == "000" ]]; then
        pass "GitHub is unreachable, as it is in the office"
    else
        fail "GitHub is still reachable (HTTP ${code}); the offline simulation is not working"
        ASSERT_FAILURES=$(( ASSERT_FAILURES + 1 ))
    fi
    if git --version >/dev/null 2>&1; then
        fail "git is still usable; the shim is not on PATH"
        ASSERT_FAILURES=$(( ASSERT_FAILURES + 1 ))
    else
        pass "git is blocked"
    fi
}

# ---------------------------------------------------------------------------
# Building fixture archives
# ---------------------------------------------------------------------------

SOURCE_DIRS=(
    "gateway=ml_server"
    "pytex=pytex"
    "calculator=scientific_calculator"
    "converter=unit_converter"
    "pdf_tools=pdf_tools"
    "tabular_ml=tabular_ml"
    "hydride=HydrideSegmentation"
)

build_archive() {
    # build_archive <version> [python-mutator-file]
    local version="$1" mutator="${2:-}"
    local fixture_dir="${FIXTURES}/${version}"

    python3 "${HARNESS_DIR}/make_fixtures.py" --out "$fixture_dir" --version "$version" >/dev/null 2>&1 \
        || { fail "could not build fixtures for ${version}"; return 1; }

    if [[ -n "$mutator" ]]; then
        python3 "$mutator" "${fixture_dir}/manifest.fixture.yml" "$fixture_dir" \
            || { fail "mutator ${mutator} failed"; return 1; }
    fi

    local args=()
    local pair id dir
    for pair in "${SOURCE_DIRS[@]}"; do
        id="${pair%%=*}"; dir="${pair##*=}"
        args+=(--source "${id}=${fixture_dir}/sources/${dir}")
    done

    ( cd "$REPO_ROOT" && python3 tools/build_suite.py \
        --manifest "${fixture_dir}/manifest.fixture.yml" \
        --requirements "$FIXTURE_REQS" \
        --out "$DIST" --allow-unresolved "${args[@]}" ) >/dev/null 2>&1 \
        || { fail "could not build archive ${version}"; return 1; }

    echo "${DIST}/ml-server-suite-v${version}.tar.gz"
}

# ---------------------------------------------------------------------------
# A disposable office server
# ---------------------------------------------------------------------------

new_root() {
    # new_root <scenario-name>  -> echoes a fresh empty root
    local name="$1"
    local root="${WORK}/${name}"
    stop_units
    rm -rf "$root"
    mkdir -p "$root"
    echo "$root"
}

# Only ever touches units carrying the rehearsal suffix. The glob below must
# stay narrow: a wider one removed a real deployment's units once already.
stop_units() {
    local unit
    while read -r unit; do
        [[ -n "$unit" ]] || continue
        systemctl --user stop "$unit" >/dev/null 2>&1 || true
        systemctl --user disable "$unit" >/dev/null 2>&1 || true
    done < <(systemctl --user list-unit-files "ml-platform-*${UNIT_SUFFIX}.service" --no-legend 2>/dev/null | awk '{print $1}')
    rm -f "${HOME}/.config/systemd/user/ml-platform-"*"${UNIT_SUFFIX}.service" 2>/dev/null || true
    systemctl --user daemon-reload >/dev/null 2>&1 || true
}

# Names and ports as the fixture manifest defines them.
runit() { printf 'ml-platform-%s%s.service' "$1" "$UNIT_SUFFIX"; }
rport() { printf '%d' $(( $1 + PORT_OFFSET )); }

update() {
    # update <root> <archive> [extra args...]  -- returns the script's exit code
    local root="$1" archive="$2"; shift 2
    "${REPO_ROOT}/deploy/update.sh" "$archive" --root "$root" --systemd-scope user "$@"
}

fingerprint() {
    local root="$1"
    if [[ ! -d "$root" ]]; then echo ABSENT; return; fi
    find "$root" -printf '%p\t%s\t%y\t%m\n' 2>/dev/null | LC_ALL=C sort | sha256sum | cut -d' ' -f1
}

active_version() {
    cat "${1}/current/VERSION" 2>/dev/null | tr -d '[:space:]' || echo ""
}

unit_pid() {
    systemctl --user show -p MainPID --value "$1" 2>/dev/null || echo 0
}

# ===========================================================================
# Scenarios
# ===========================================================================

scenario_fresh_install() {
    local root archive
    root="$(new_root fresh_install)"
    archive="$(build_archive 1.0.0)" || return 1

    assert "update.sh succeeds on an empty root" update "$root" "$archive"
    assert_eq "active version is 1.0.0" "1.0.0" "$(active_version "$root")"
    assert "current is a symlink" test -L "${root}/current"
    assert "release directory exists" test -d "${root}/releases/1.0.0"
    assert "shared/data was created" test -d "${root}/shared/data"
    assert "deployment history was written" test -s "${root}/shared/state/history.jsonl"

    local unit
    for unit in portal pytex calculator converter hydride; do
        assert "$(runit "$unit") is active" systemctl --user is-active --quiet "$(runit "$unit")"
    done

    assert "the portal answers on its port" bash -c "curl -sf --max-time 5 http://127.0.0.1:$(rport 5000)/health/live >/dev/null"
    assert "health_check.sh passes" "${REPO_ROOT}/deploy/health_check.sh" --root "$root" --systemd-scope user
}

scenario_upgrade_all() {
    local root a1 a2
    root="$(new_root upgrade_all)"
    a1="$(build_archive 1.0.0)" || return 1
    a2="$(build_archive 1.1.0)" || return 1

    update "$root" "$a1" >/dev/null 2>&1 || { fail "baseline install failed"; return 1; }
    assert_eq "baseline is 1.0.0" "1.0.0" "$(active_version "$root")"

    assert "upgrade to 1.1.0 succeeds" update "$root" "$a2"
    assert_eq "active version is now 1.1.0" "1.1.0" "$(active_version "$root")"
    assert "the old release is retained for rollback" test -d "${root}/releases/1.0.0"
    assert_eq "marker file came from the new release" "gateway 1.1.0" \
        "$(cat "${root}/current/apps/ml_server/src/MARKER" | tr -d '\n')"
}

scenario_single_component() {
    # The headline requirement: change one repository, rebuild, deploy, and
    # confirm the other applications were not restarted.
    local root a1 a2
    root="$(new_root single_component)"
    a1="$(build_archive 1.0.0)" || return 1

    update "$root" "$a1" >/dev/null 2>&1 || { fail "baseline install failed"; return 1; }

    local -A pid_before
    local unit
    for unit in portal pytex calculator converter hydride; do
        pid_before[$unit]="$(unit_pid "$(runit "$unit")")"
    done

    # Build 1.0.1 where only pytex's commit differs from 1.0.0.
    cat >"${WORK}/only_pytex.py" <<'MUTATOR'
import sys
import hashlib
import yaml

manifest_path = sys.argv[1]
with open(manifest_path, encoding="utf-8") as handle:
    document = yaml.safe_load(handle)

# Pin every component to the SHA it had at 1.0.0 so that nothing but pytex is
# seen as changed; give pytex a genuinely new one.
for name, service in document["services"].items():
    service["commit"] = hashlib.sha1(f"rehearsal:{name}:1.0.0".encode()).hexdigest()
document["services"]["pytex"]["commit"] = hashlib.sha1(b"rehearsal:pytex:changed").hexdigest()

with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
    yaml.safe_dump(document, handle, sort_keys=False, default_flow_style=False)
MUTATOR

    a2="$(build_archive 1.0.1 "${WORK}/only_pytex.py")" || return 1

    local output
    output="$(update "$root" "$a2" 2>&1)" || { fail "single-component upgrade failed"; info "$output"; return 1; }
    assert_eq "active version is 1.0.1" "1.0.1" "$(active_version "$root")"

    local restarted=0 untouched=0
    if [[ "$(unit_pid "$(runit pytex)")" != "${pid_before[pytex]}" ]]; then
        restarted=1
    fi
    assert_eq "pytex was restarted" "1" "$restarted"

    for unit in calculator converter hydride; do
        if [[ "$(unit_pid "$(runit "$unit")")" == "${pid_before[$unit]}" ]]; then
            untouched=$(( untouched + 1 ))
        else
            fail "${unit} was restarted but its component did not change"
            ASSERT_FAILURES=$(( ASSERT_FAILURES + 1 ))
        fi
    done
    assert_eq "the three unchanged services kept running untouched" "3" "$untouched"
    assert "health still passes" "${REPO_ROOT}/deploy/health_check.sh" --root "$root" --systemd-scope user
}

scenario_idempotent() {
    local root archive before after
    root="$(new_root idempotent)"
    archive="$(build_archive 1.0.0)" || return 1

    update "$root" "$archive" >/dev/null 2>&1 || { fail "first install failed"; return 1; }
    local pid_before; pid_before="$(unit_pid "$(runit portal)")"
    before="$(fingerprint "${root}/releases")"

    local output
    output="$(update "$root" "$archive" 2>&1)" || { fail "re-running the same archive failed"; info "$output"; return 1; }
    pass "re-running the same archive exits 0"

    after="$(fingerprint "${root}/releases")"
    assert_eq "the release tree is unchanged by the second run" "$before" "$after"
    assert_eq "the portal was not restarted" "$pid_before" "$(unit_pid "$(runit portal)")"
    assert_contains "the run reported that it was already deployed" "$output" "already active"
}

scenario_data_survives() {
    local root a1 a2
    root="$(new_root data_survives)"
    a1="$(build_archive 1.0.0)" || return 1
    a2="$(build_archive 1.1.0)" || return 1

    update "$root" "$a1" >/dev/null 2>&1 || { fail "baseline install failed"; return 1; }

    # Persistent state of each kind the suite actually keeps.
    mkdir -p "${root}/shared/data" "${root}/shared/uploads" "${root}/shared/models/hydride" "${root}/shared/config"
    printf 'scientific-results-must-survive\n' >"${root}/shared/data/engagement.sqlite3"
    printf 'user-upload\n'                     >"${root}/shared/uploads/sample.tif"
    head -c 4096 /dev/urandom                  >"${root}/shared/models/hydride/checkpoint.pt"
    printf '{"admin_token": "secret"}\n'       >"${root}/shared/config/config.intranet.json"

    # Compare the state that belongs to users and to science. shared/logs and
    # shared/state are the deployment's own bookkeeping and are expected to grow
    # on every run; treating them as user data would make this assertion fail
    # for a reason that has nothing to do with data safety.
    user_data_sums() {
        find "${root}/shared/data" "${root}/shared/uploads" \
             "${root}/shared/models" "${root}/shared/config" \
             -type f -exec sha256sum {} \; 2>/dev/null | LC_ALL=C sort
    }

    local sums_before
    sums_before="$(user_data_sums)"

    update "$root" "$a2" >/dev/null 2>&1 || { fail "upgrade failed"; return 1; }
    assert_eq "upgrade completed" "1.1.0" "$(active_version "$root")"

    local sums_after
    sums_after="$(user_data_sums)"
    assert_eq "every file under shared/ is byte-identical after the upgrade" "$sums_before" "$sums_after"

    # And the model directory must be reachable from inside the new release.
    assert "hydride checkpoints are visible in the new release" \
        test -f "${root}/current/apps/HydrideSegmentation/frozen_checkpoints/checkpoint.pt"
}

scenario_rollback_bad_release() {
    local root a1 a2
    root="$(new_root rollback_bad_release)"
    a1="$(build_archive 1.0.0)" || return 1

    update "$root" "$a1" >/dev/null 2>&1 || { fail "baseline install failed"; return 1; }
    printf 'precious\n' >"${root}/shared/data/engagement.sqlite3"

    # 1.2.0 is built normally, but the gateway is told to fail its health check,
    # which is what a genuinely broken release looks like from outside.
    a2="$(build_archive 1.2.0)" || return 1
    cat >"${WORK}/break_gateway.py" <<'BREAK'
import sys
from pathlib import Path

# Make the gateway stub report unhealthy by baking the scenario hook into it.
stub = Path(sys.argv[2]) / "sources" / "ml_server" / "app_stub.py"
text = stub.read_text(encoding="utf-8")
text = text.replace(
    'FAIL_HEALTH = os.environ.get("ML_FIXTURE_FAIL_HEALTH") == SERVICE',
    'FAIL_HEALTH = True',
)
stub.write_text(text, encoding="utf-8", newline="\n")
BREAK
    a2="$(build_archive 1.2.0 "${WORK}/break_gateway.py")" || return 1

    local output rc=0
    output="$(update "$root" "$a2" --health-wait 15 2>&1)" || rc=$?
    assert "update.sh fails on a release that does not pass health checks" test "$rc" -ne 0
    assert_contains "it reports rolling back" "$output" "rolling back"
    assert_eq "the previous release is active again" "1.0.0" "$(active_version "$root")"
    assert "the portal is serving again" bash -c "curl -sf --max-time 5 http://127.0.0.1:$(rport 5000)/health/live >/dev/null"
    assert_eq "persistent data was not touched by the failure" "precious" \
        "$(cat "${root}/shared/data/engagement.sqlite3" | tr -d '\n')"
    assert "the failed release is kept for diagnosis" test -d "${root}/releases/1.2.0"
}

scenario_manual_rollback() {
    local root a1 a2
    root="$(new_root manual_rollback)"
    a1="$(build_archive 1.0.0)" || return 1
    a2="$(build_archive 1.1.0)" || return 1

    update "$root" "$a1" >/dev/null 2>&1 || { fail "baseline install failed"; return 1; }
    update "$root" "$a2" >/dev/null 2>&1 || { fail "upgrade failed"; return 1; }
    assert_eq "on 1.1.0 before rollback" "1.1.0" "$(active_version "$root")"

    assert "rollback.sh succeeds" "${REPO_ROOT}/deploy/rollback.sh" --auto --root "$root" --systemd-scope user
    assert_eq "back on 1.0.0" "1.0.0" "$(active_version "$root")"
    assert "services are healthy after rollback" \
        "${REPO_ROOT}/deploy/health_check.sh" --root "$root" --systemd-scope user
}

# --- refusal scenarios: each must change absolutely nothing ----------------

assert_no_mutation() {
    local description="$1" root="$2" before="$3"
    assert_eq "$description" "$before" "$(fingerprint "$root")"
}

scenario_corrupt_archive() {
    local root archive corrupt before
    root="$(new_root corrupt_archive)"
    archive="$(build_archive 1.0.0)" || return 1
    update "$root" "$archive" >/dev/null 2>&1 || { fail "baseline install failed"; return 1; }

    corrupt="${WORK}/corrupt.tar.gz"
    cp "$archive" "$corrupt"
    cp "${archive}.sha256" "${corrupt}.sha256"
    sed -i "s/ml-server-suite-v1.0.0.tar.gz/corrupt.tar.gz/" "${corrupt}.sha256"
    # Flip a byte in the middle of the payload.
    printf 'X' | dd of="$corrupt" bs=1 seek=$(( $(stat -c %s "$corrupt") / 2 )) conv=notrunc 2>/dev/null

    before="$(fingerprint "$root")"
    local output rc=0
    output="$(update "$root" "$corrupt" 2>&1)" || rc=$?
    assert "a corrupt archive is refused" test "$rc" -ne 0
    assert_contains "the reason given is a checksum mismatch" "$output" "checksum MISMATCH"
    assert_no_mutation "nothing on the server changed" "$root" "$before"
}

scenario_missing_checksum() {
    local root archive lonely before
    root="$(new_root missing_checksum)"
    archive="$(build_archive 1.0.0)" || return 1

    lonely="${WORK}/lonely.tar.gz"
    cp "$archive" "$lonely"   # deliberately no .sha256 beside it

    before="$(fingerprint "$root")"
    local output rc=0
    output="$(update "$root" "$lonely" 2>&1)" || rc=$?
    assert "an archive with no checksum file is refused" test "$rc" -ne 0
    assert_contains "the message explains what to transfer" "$output" "no checksum file"
    assert_no_mutation "nothing on the server changed" "$root" "$before"
}

scenario_truncated_archive() {
    local root archive truncated before
    root="$(new_root truncated_archive)"
    archive="$(build_archive 1.0.0)" || return 1

    truncated="${WORK}/truncated.tar.gz"
    head -c $(( $(stat -c %s "$archive") / 3 )) "$archive" >"$truncated"
    sha256sum "$truncated" | sed "s|${WORK}/||" >"${truncated}.sha256"

    before="$(fingerprint "$root")"
    local rc=0
    update "$root" "$truncated" >/dev/null 2>&1 || rc=$?
    assert "a truncated archive is refused" test "$rc" -ne 0
    assert_no_mutation "nothing on the server changed" "$root" "$before"
}

scenario_path_traversal() {
    # A tar member that writes outside the release directory. Nothing benign
    # produces one, so it must be refused rather than sanitised.
    local root evil before
    root="$(new_root path_traversal)"
    mkdir -p "${WORK}/evil/ml-server-suite-v9.9.9"
    ( cd "${WORK}/evil" && \
      echo 'pwned' > ml-server-suite-v9.9.9/harmless.txt && \
      mkdir -p ml-server-suite-v9.9.9 && \
      tar -czf "${WORK}/evil.tar.gz" ml-server-suite-v9.9.9 --transform 's|harmless.txt|../../../../tmp/pwned.txt|' 2>/dev/null ) || true
    evil="${WORK}/evil.tar.gz"
    if [[ ! -f "$evil" ]]; then
        warn2 "could not construct a traversal archive on this tar; skipping"
        return 0
    fi
    sha256sum "$evil" | awk '{print $1"  evil.tar.gz"}' >"${evil}.sha256"

    before="$(fingerprint "$root")"
    local output rc=0
    output="$(update "$root" "$evil" 2>&1)" || rc=$?
    assert "an archive that escapes its root is refused" test "$rc" -ne 0
    assert_contains "the reason given names the escape" "$output" "escapes the archive root"
    assert_no_mutation "nothing on the server changed" "$root" "$before"
}

scenario_not_a_suite_archive() {
    local root other before
    root="$(new_root not_a_suite)"
    mkdir -p "${WORK}/other/some-project"
    echo hello >"${WORK}/other/some-project/file.txt"
    ( cd "${WORK}/other" && tar -czf "${WORK}/other.tar.gz" some-project )
    other="${WORK}/other.tar.gz"
    sha256sum "$other" | awk '{print $1"  other.tar.gz"}' >"${other}.sha256"

    before="$(fingerprint "$root")"
    local output rc=0
    output="$(update "$root" "$other" 2>&1)" || rc=$?
    assert "an unrelated tarball is refused" test "$rc" -ne 0
    assert_contains "the reason mentions the missing manifest" "$output" "manifest.resolved.json"
    assert_no_mutation "nothing on the server changed" "$root" "$before"
}

scenario_wrong_platform() {
    local root archive before
    root="$(new_root wrong_platform)"
    cat >"${WORK}/future_ubuntu.py" <<'MUTATOR'
import sys
import yaml

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    document = yaml.safe_load(handle)
document["platform"]["os_version_min"] = "99.04"
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    yaml.safe_dump(document, handle, sort_keys=False, default_flow_style=False)
MUTATOR
    archive="$(build_archive 2.0.0 "${WORK}/future_ubuntu.py")" || return 1

    before="$(fingerprint "$root")"
    local output rc=0
    output="$(update "$root" "$archive" 2>&1)" || rc=$?
    assert "a release requiring a newer Ubuntu is refused" test "$rc" -ne 0
    assert_contains "the reason names the version requirement" "$output" "99.04"
    assert_no_mutation "nothing on the server changed" "$root" "$before"
}

scenario_port_conflict() {
    local root archive before squatter
    root="$(new_root port_conflict)"
    archive="$(build_archive 1.0.0)" || return 1

    # Squat the portal's port with something that is not one of our units.
    python3 -c "
import socket, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', $(rport 5000)))
s.listen(1)
time.sleep(120)
" &
    squatter=$!
    sleep 1

    before="$(fingerprint "$root")"
    local output rc=0
    output="$(update "$root" "$archive" 2>&1)" || rc=$?
    kill "$squatter" 2>/dev/null || true
    wait "$squatter" 2>/dev/null || true

    assert "a port held by a foreign process is refused" test "$rc" -ne 0
    assert_contains "the reason names the port conflict" "$output" "port conflict"
    assert_no_mutation "nothing on the server changed" "$root" "$before"
}

scenario_dependency_failure() {
    # A release whose pinned dependencies cannot be installed must abort while
    # the previous release is still serving, never half-way through.
    local root a1 a2
    root="$(new_root dependency_failure)"
    a1="$(build_archive 1.0.0)" || return 1
    update "$root" "$a1" >/dev/null 2>&1 || { fail "baseline install failed"; return 1; }

    cat >"${WORK}/bad_requirement.py" <<'MUTATOR'
import sys
from pathlib import Path

# The release build normally writes requirements/resolved.txt; here it names a
# distribution that no mirror will ever have.
requirements = Path(sys.argv[2]).parent.parent / "requirements"
MUTATOR
    a2="$(build_archive 1.3.0)" || return 1

    # Inject an unsatisfiable pin directly into the built archive.
    local staging="${WORK}/inject"
    rm -rf "$staging"; mkdir -p "$staging"
    tar -xzf "$a2" -C "$staging"
    mkdir -p "${staging}/ml-server-suite-v1.3.0/requirements"
    printf 'this-package-does-not-exist-anywhere==9.9.9\n' \
        >"${staging}/ml-server-suite-v1.3.0/requirements/resolved.txt"
    ( cd "$staging" && tar -czf "${WORK}/bad-deps.tar.gz" ml-server-suite-v1.3.0 )
    sha256sum "${WORK}/bad-deps.tar.gz" | awk '{print $1"  bad-deps.tar.gz"}' >"${WORK}/bad-deps.tar.gz.sha256"

    local output rc=0
    output="$(update "$root" "${WORK}/bad-deps.tar.gz" 2>&1)" || rc=$?
    assert "a release with an uninstallable dependency is refused" test "$rc" -ne 0
    assert_contains "the message points at the mirror audit" "$output" "mirror_audit.txt"
    assert_eq "the previous release is still active" "1.0.0" "$(active_version "$root")"
    assert "the portal never stopped serving" bash -c "curl -sf --max-time 5 http://127.0.0.1:$(rport 5000)/health/live >/dev/null"
}

scenario_interrupted_update() {
    # A previous run killed mid-extract leaves a .incoming directory behind.
    # The next run must clean it up and succeed rather than trip over it.
    local root archive
    root="$(new_root interrupted_update)"
    archive="$(build_archive 1.0.0)" || return 1

    mkdir -p "${root}/releases/1.0.0.incoming/apps/ml_server"
    echo "half-extracted junk" >"${root}/releases/1.0.0.incoming/apps/ml_server/partial.txt"

    local output
    output="$(update "$root" "$archive" 2>&1)" || { fail "update failed after an interrupted run"; info "$output"; return 1; }
    pass "update succeeds after an interrupted previous run"
    assert_contains "the stale directory was reported and removed" "$output" "stale"
    assert "no .incoming directory is left behind" bash -c "! test -d '${root}/releases/1.0.0.incoming'"
    assert_eq "the release installed correctly" "1.0.0" "$(active_version "$root")"
}

scenario_concurrent_update() {
    local root archive
    root="$(new_root concurrent_update)"
    archive="$(build_archive 1.0.0)" || return 1
    update "$root" "$archive" >/dev/null 2>&1 || { fail "baseline install failed"; return 1; }

    # Hold the lock the way a running update would, then try to start another.
    mkdir -p "${root}/shared/state"
    ( flock 9; sleep 20 ) 9>"${root}/shared/state/deploy.lock" &
    local holder=$!
    sleep 1

    local a2 output rc=0
    a2="$(build_archive 1.1.0)" || return 1
    output="$(update "$root" "$a2" 2>&1)" || rc=$?
    kill "$holder" 2>/dev/null || true
    wait "$holder" 2>/dev/null || true

    assert "a second concurrent update is refused" test "$rc" -ne 0
    assert_contains "the reason names the lock" "$output" "already running"
    assert_eq "the running release was left alone" "1.0.0" "$(active_version "$root")"
}

scenario_legacy_layout() {
    # A legacy /opt-style install must never be silently converted.
    local fake_legacy before
    fake_legacy="${WORK}/legacy/opt_ml_server"
    rm -rf "${WORK}/legacy"
    mkdir -p "${fake_legacy}/env/bin" "${fake_legacy}/data"
    printf 'Flask==3.0.2\n' >"${fake_legacy}/requirements.txt"
    printf 'live-production-data\n' >"${fake_legacy}/data/engagement.sqlite3"

    local archive
    archive="$(build_archive 1.0.0)" || return 1

    before="$(fingerprint "$fake_legacy")"

    # With the legacy root as the only installation present, and no --root or
    # --adopt-legacy, detection must refuse rather than guess.
    local output rc=0
    output="$( ML_PLATFORM_ROOT="${WORK}/legacy/nonexistent" ML_LEGACY_ROOT="$fake_legacy" \
        "${REPO_ROOT}/deploy/update.sh" "$archive" --systemd-scope user 2>&1 )" || rc=$?

    assert "a legacy install is not adopted without being asked" test "$rc" -ne 0
    assert_contains "the message explains the two ways forward" "$output" "--adopt-legacy"
    assert_no_mutation "the legacy installation was not touched" "$fake_legacy" "$before"
}

scenario_unit_takeover() {
    # Two deployments on one host want the same unit names. Deploying the second
    # takes the services away from the first. That is allowed -- but it must be
    # announced, not silent, because it stops the other deployment.
    local first second archive
    archive="$(build_archive 1.0.0)" || return 1

    first="${WORK}/takeover_a"
    second="${WORK}/takeover_b"
    stop_units
    rm -rf "$first" "$second"
    mkdir -p "$first" "$second"

    update "$first" "$archive" >/dev/null 2>&1 || { fail "first deployment failed"; return 1; }
    assert_eq "the first deployment is serving" "1.0.0" "$(active_version "$first")"

    local output
    output="$(update "$second" "$archive" 2>&1)" || { fail "second deployment failed"; info "$output"; return 1; }

    assert_contains "the takeover is announced" "$output" "TAKING OVER"
    assert_contains "it names the deployment being taken over" "$output" "$first"
    assert_contains "it names a unit being taken" "$output" "$(runit portal)"

    # The units must now serve the second deployment.
    local workdir
    workdir="$(sed -n 's/^WorkingDirectory=\(.*\)$/\1/p' \
        "${HOME}/.config/systemd/user/$(runit portal)" | head -1)"
    assert_contains "the units now point at the second deployment" "$workdir" "$second"
    assert "the portal is serving again after the takeover" \
        bash -c "curl -sf --max-time 5 http://127.0.0.1:$(rport 5000)/health/live >/dev/null"

    # The first deployment's files are untouched -- only systemd moved on.
    assert "the first deployment's release tree is left on disk" test -d "${first}/releases/1.0.0"
    assert "the first deployment's data is left on disk" test -d "${first}/shared/data"
}

scenario_missing_prerequisite() {
    # A prerequisite the operator must install by hand must stop the deployment
    # in preflight, name what is missing and how to install it, and change
    # nothing at all -- so the fix can be applied and the same command re-run.
    local root archive before
    root="$(new_root missing_prerequisite)"

    cat >"${WORK}/needs_tool.py" <<'MUTATOR'
import sys
import yaml

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    document = yaml.safe_load(handle)
document["system_requirements"] = [
    {
        "command": "definitely-not-installed-anywhere",
        "package": "some-office-package",
        "why": "a tool the deployment needs and will not install for you",
    }
]
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    yaml.safe_dump(document, handle, sort_keys=False, default_flow_style=False)
MUTATOR

    archive="$(build_archive 1.4.0 "${WORK}/needs_tool.py")" || return 1

    before="$(fingerprint "$root")"
    local output rc=0
    output="$(update "$root" "$archive" 2>&1)" || rc=$?

    assert "a missing prerequisite is refused" test "$rc" -ne 0
    assert_contains "it says nothing was changed" "$output" "NOTHING HAS BEEN CHANGED"
    assert_contains "it names the missing tool" "$output" "definitely-not-installed-anywhere"
    assert_contains "it gives the apt command to fix it" "$output" "sudo apt-get install -y some-office-package"
    assert_no_mutation "the server really is unchanged" "$root" "$before"

    # And once the requirement is satisfiable, the same archive deploys.
    mkdir -p "${SHIMS}"
    printf '#!/usr/bin/env bash\nexit 0\n' >"${SHIMS}/definitely-not-installed-anywhere"
    chmod +x "${SHIMS}/definitely-not-installed-anywhere"
    assert "the same archive deploys once the prerequisite is present" update "$root" "$archive"
    rm -f "${SHIMS}/definitely-not-installed-anywhere"
}

scenario_dry_run() {
    local root archive before
    root="$(new_root dry_run)"
    archive="$(build_archive 1.0.0)" || return 1

    before="$(fingerprint "$root")"
    local output
    output="$(update "$root" "$archive" --dry-run 2>&1)" || { fail "--dry-run failed"; info "$output"; return 1; }
    pass "--dry-run exits 0"
    assert_contains "it prints a deployment plan" "$output" "Deployment plan"
    assert_contains "it says nothing was changed" "$output" "nothing was changed"
    assert_no_mutation "--dry-run really changed nothing" "$root" "$before"
}

scenario_offline_enforced() {
    # The whole point: a full deployment with GitHub genuinely unreachable.
    local root archive
    root="$(new_root offline_enforced)"
    archive="$(build_archive 1.0.0)" || return 1

    verify_offline
    assert "a full install succeeds with GitHub unreachable" update "$root" "$archive"
    assert_eq "the suite is active" "1.0.0" "$(active_version "$root")"

    # And no shipped file asks for GitHub at deploy time.
    local hits
    hits="$(grep -rIl 'github.com' "${root}/current/apps" 2>/dev/null | grep -v manifest || true)"
    assert_eq "no application in the release resolves anything from GitHub" "" "$hits"
}

# ===========================================================================
# Driver
# ===========================================================================

SCENARIOS=(
    fresh_install
    upgrade_all
    single_component
    idempotent
    data_survives
    rollback_bad_release
    manual_rollback
    unit_takeover
    missing_prerequisite
    dry_run
    corrupt_archive
    missing_checksum
    truncated_archive
    path_traversal
    not_a_suite_archive
    wrong_platform
    port_conflict
    dependency_failure
    interrupted_update
    concurrent_update
    legacy_layout
    offline_enforced
)

run_scenario() {
    local name="$1"
    banner "scenario: ${name}"
    ASSERT_FAILURES=0
    local start; start="$(date +%s)"

    if ! "scenario_${name}"; then
        ASSERT_FAILURES=$(( ASSERT_FAILURES + 1 ))
    fi

    local elapsed=$(( $(date +%s) - start ))
    if (( ASSERT_FAILURES == 0 )); then
        printf '%s\n' "${C_GRN}  => ${name} PASSED (${elapsed}s)${C_OFF}"
        PASSED=$(( PASSED + 1 ))
    else
        printf '%s\n' "${C_RED}  => ${name} FAILED with ${ASSERT_FAILURES} problem(s) (${elapsed}s)${C_OFF}"
        FAILED=$(( FAILED + 1 ))
        FAILED_NAMES+=("$name")
    fi
}

main() {
    if [[ "${1:-}" == "--list" ]]; then
        printf '%s\n' "${SCENARIOS[@]}"
        exit 0
    fi

    command -v systemctl >/dev/null || { echo "systemctl is required" >&2; exit 1; }
    if ! systemctl --user is-system-running >/dev/null 2>&1; then
        echo "the user systemd session is not running; this harness needs it" >&2
        echo "try: loginctl enable-linger \$(id -un)" >&2
        exit 1
    fi

    mkdir -p "$FIXTURES" "$DIST" "$WORK" "$SHIMS" "$FIXTURE_REQS"
    printf '# Rehearsal fixture: the stub services need no third-party packages.\n' \
        >"${FIXTURE_REQS}/resolved.txt"
    printf '# Rehearsal fixture: nothing to mirror.\n' \
        >"${FIXTURE_REQS}/mirror_audit.txt"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

    banner "ML Server suite -- deployment rehearsal"
    info "repo        ${REPO_ROOT}"
    info "rehearsal   ${REHEARSAL_HOME}"
    info "ubuntu      $(. /etc/os-release; echo "$PRETTY_NAME")"
    info "python      $(python3 --version)"
    info "user        $(id -un)  linger=$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)"

    setup_offline
    info "offline     GitHub blocked via dead proxy; git shimmed"

    local requested=("$@")
    (( ${#requested[@]} )) || requested=("${SCENARIOS[@]}")

    local name
    for name in "${requested[@]}"; do
        if ! declare -F "scenario_${name}" >/dev/null; then
            echo "no such scenario: ${name}" >&2
            exit 1
        fi
        run_scenario "$name"
    done

    stop_units

    banner "rehearsal summary"
    printf '  passed  %d\n' "$PASSED"
    printf '  failed  %d\n' "$FAILED"
    if (( FAILED )); then
        printf '  failing scenarios: %s\n' "${FAILED_NAMES[*]}"
        exit 1
    fi
    printf '\n%s\n\n' "${C_GRN}  All scenarios passed.${C_OFF}"
}

main "$@"
