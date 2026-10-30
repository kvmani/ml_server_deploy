#!/usr/bin/env python3
"""Nightly, verified backups of the ML Server suite to several destinations.

What one backup is
------------------
One compressed archive of everything needed to put this server back together,
except what can be rebuilt from the release archive and the package mirror:

* ``~/ml_platform`` -- data, models, uploads, configuration and secrets,
  logs and deployment history (``shared/``), the code that is running
  (the ``current`` release and its symlink), the update checkpoints
  (``backups/``) -- minus what the configuration excludes (``.venv``, the
  rebuilt ``/docs`` site, temporary files, older releases by default);
* every SQLite database copied with SQLite's online backup API, so the copy is
  consistent even if a service writes to it at the moment of the backup, and
  checked with ``PRAGMA integrity_check`` before it is trusted;
* the systemd user units, the pip configuration and the crontab, and a
  ``_meta/`` folder describing the machine (OS, time zone, installed Python
  packages, what the suite reported about itself).

Beside each archive sit ``<name>.sha256`` and ``<name>.json``: the checksum,
and a manifest listing every file with its own checksum.

How a run goes
--------------
1. Take the lock (two runs never overlap).
2. Build the archive in a staging folder, hashing every file as it is read.
3. Verify the archive: re-read it end to end and compare the checksum.
4. For each destination: if the content is identical to the last copy stored
   there, store nothing ("unchanged"); otherwise copy it, verify the copy at the
   destination, and only then delete copies beyond the newest ``keep``.
   A failed destination is never pruned, so a bad night never eats history.
5. Write a human-readable log, a JSON record of the run, and a status file.

Destinations
------------
``type = "path"`` writes to a directory: another folder on this machine, or a
network share that is already mounted. ``require_mountpoint`` refuses to write
into an empty mount point (which would silently fill the local disk), and
``mount_command`` may (re)mount it first.

``type = "smb"`` uploads to a Windows/SMB share with ``smbclient``, which needs
no mount and no root. Credentials live in a file readable only by this user,
outside ``~/ml_platform`` so they are never inside a backup, and are changed with
``backup.sh set-credentials <destination>``. Rejected, expired or locked
credentials are recognised and reported as such, with the command that fixes
them.

Usage (through ``backup.sh``)::

    backup.sh init-config              write shared/config/backup.toml to edit
    backup.sh test-destinations        check every destination now
    backup.sh run [--dry-run]          take a backup now
    backup.sh status [--json]          the last run, and credential ages
    backup.sh list                     backups held at every destination
    backup.sh set-credentials NAME     store or change a share's credentials
    backup.sh verify ARCHIVE           check an archive end to end
    backup.sh extract ARCHIVE --to DIR verify, then unpack for a restore
    backup.sh install-timer            run every night at the configured time
    backup.sh remove-timer             stop the nightly runs

Only the Python standard library is used: a backup must not depend on the
environment it may be needed to rebuild.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import errno
import fcntl
import fnmatch
import getpass
import hashlib
import io
import json
import logging
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

TOOL_VERSION = "1.0.0"
RUN_SCHEMA = "ml-platform.backup-run/1"
MANIFEST_SCHEMA = "ml-platform.backup-manifest/1"
ARCHIVE_PREFIX = "mlplatform"
SQLITE_MAGIC = b"SQLite format 3\x00"
TIMER_NAME = "ml-backup"

HOME = Path.home()
DEFAULT_ROOT = HOME / "ml_platform"
DEFAULT_CONFIG = DEFAULT_ROOT / "shared" / "config" / "backup.toml"
CREDENTIAL_DIR = HOME / ".config" / "ml_platform_backup" / "credentials"

#: Paths (relative to the archive) that change on every run by design and so
#: must not make two otherwise identical backups look different.
FINGERPRINT_IGNORE = ("_meta/*", "shared/logs/backup/*", "shared/state/backup-*", "shared/state/backup.lock")

EXIT_OK, EXIT_FAILED, EXIT_PARTIAL, EXIT_CONFIG, EXIT_LOCKED = 0, 1, 2, 3, 75

#: Files up to this size are read whole before archiving (see build_archive).
SMALL_FILE_BYTES = 64 << 20

log = logging.getLogger("ml-backup")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """The configuration cannot be used; the message says which key."""


@dataclasses.dataclass
class Destination:
    name: str
    type: str
    path: str = ""
    require_mountpoint: str = ""
    mount_command: str = ""
    share: str = ""
    remote_dir: str = ""
    credentials: str = ""
    verify_after_upload: bool = True
    timeout_seconds: int = 30


@dataclasses.dataclass
class Config:
    path: Path
    source_root: Path
    keep: int
    staging_dir: Path
    exclude: list[str]
    include_current_release: bool
    include_extra: list[Path]
    compress_level: int
    log_keep_runs: int
    schedule: str
    destinations: list[Destination]
    enabled: bool


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def load_config(path: Path) -> Config:
    """Read and validate the TOML configuration."""

    if tomllib is None:
        raise ConfigError("Python 3.11 or newer is required to read the TOML configuration.")
    if not path.is_file():
        raise ConfigError(
            f"no configuration at {path}. Create it with `backup.sh init-config` and edit it."
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error

    section = data.get("backup", {})
    keep = int(section.get("keep", 10))
    if keep < 1:
        raise ConfigError("backup.keep must be at least 1.")
    level = int(section.get("compress_level", 6))
    if not 1 <= level <= 9:
        raise ConfigError("backup.compress_level must be between 1 and 9.")

    destinations: list[Destination] = []
    names: set[str] = set()
    for index, raw in enumerate(data.get("destination", [])):
        where = f"destination #{index + 1}"
        name = str(raw.get("name", "")).strip()
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ConfigError(f"{where}: name must be letters, digits, '-' or '_'.")
        if name in names:
            raise ConfigError(f"{where}: the name {name!r} is used twice.")
        names.add(name)
        kind = str(raw.get("type", "path"))
        destination = Destination(
            name=name,
            type=kind,
            path=str(raw.get("path", "")),
            require_mountpoint=str(raw.get("require_mountpoint", "")),
            mount_command=str(raw.get("mount_command", "")),
            share=str(raw.get("share", "")),
            remote_dir=str(raw.get("remote_dir", "")).strip("/"),
            credentials=str(raw.get("credentials", name)),
            verify_after_upload=bool(raw.get("verify_after_upload", True)),
            timeout_seconds=int(raw.get("timeout_seconds", 30)),
        )
        if kind == "path":
            if not destination.path:
                raise ConfigError(f"{where} ({name}): type 'path' needs path = \"...\".")
        elif kind == "smb":
            if not destination.share.startswith("//"):
                raise ConfigError(
                    f"{where} ({name}): type 'smb' needs share = \"//server/share\"."
                )
        else:
            raise ConfigError(f"{where} ({name}): type must be 'path' or 'smb', not {kind!r}.")
        destinations.append(destination)
    if not destinations:
        raise ConfigError("at least one [[destination]] is required.")

    return Config(
        path=path,
        source_root=_expand(str(section.get("source_root", str(DEFAULT_ROOT)))),
        keep=keep,
        staging_dir=_expand(str(section.get("staging_dir", "~/ml_platform_backup_staging"))),
        exclude=[str(item).strip("/") for item in section.get("exclude", [])],
        include_current_release=bool(section.get("include_current_release", True)),
        include_extra=[_expand(str(item)) for item in section.get("include_extra", [])],
        compress_level=level,
        log_keep_runs=int(section.get("log_keep_runs", 90)),
        schedule=str(section.get("schedule", "*-*-* 00:00:00")),
        destinations=destinations,
        enabled=bool(section.get("enabled", True)),
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def human_bytes(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if count < 1024 or unit == "TiB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024
    return f"{count:.1f} TiB"  # pragma: no cover


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def matches(relative: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """Whether ``relative`` or any of its parent folders matches a pattern."""

    parts = relative.split("/")
    for depth in range(1, len(parts) + 1):
        candidate = "/".join(parts[:depth])
        if any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns):
            return True
    return False


def run_command(
    command: list[str], *, timeout: int, input_text: str | None = None
) -> tuple[int, str]:
    """Run a command; return (exit code, combined output). Never raises on failure."""

    try:
        completed = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{command[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(command[:2])}: no answer within {timeout} s"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


# ---------------------------------------------------------------------------
# Failure classification: say what is wrong and what fixes it
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Failure(Exception):
    reason: str
    detail: str
    hint: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}"


SMB_STATUS: dict[str, tuple[str, str]] = {
    "NT_STATUS_LOGON_FAILURE": (
        "credentials_rejected",
        "The share refused the user name or password. If the password was changed "
        "recently, store the new one with: backup.sh set-credentials {name}",
    ),
    "NT_STATUS_PASSWORD_EXPIRED": (
        "credentials_expired",
        "The password has expired. Change it (on any office PC), then store it with: "
        "backup.sh set-credentials {name}",
    ),
    "NT_STATUS_PASSWORD_MUST_CHANGE": (
        "credentials_expired",
        "The password must be changed before it can be used. Change it, then: "
        "backup.sh set-credentials {name}",
    ),
    "NT_STATUS_ACCOUNT_LOCKED_OUT": (
        "account_locked",
        "The account is locked, often after repeated attempts with an old password. "
        "Ask IT to unlock it, then: backup.sh set-credentials {name}",
    ),
    "NT_STATUS_ACCOUNT_DISABLED": (
        "account_disabled",
        "The account is disabled. Ask IT, or use another account with set-credentials.",
    ),
    "NT_STATUS_ACCESS_DENIED": (
        "permission_denied",
        "Signed in, but this account may not write to that folder. Ask IT for write access.",
    ),
    "NT_STATUS_BAD_NETWORK_NAME": (
        "share_not_found",
        "The server has no share by that name. Check 'share' in backup.toml.",
    ),
    "NT_STATUS_OBJECT_PATH_NOT_FOUND": (
        "path_not_found",
        "The folder does not exist on the share and could not be created.",
    ),
    "NT_STATUS_OBJECT_NAME_NOT_FOUND": (
        "path_not_found",
        "The folder or file does not exist on the share.",
    ),
    "NT_STATUS_OBJECT_NAME_COLLISION": ("already_exists", "It already exists."),
    "NT_STATUS_HOST_UNREACHABLE": ("unreachable", "The file server cannot be reached."),
    "NT_STATUS_IO_TIMEOUT": ("unreachable", "The file server did not answer in time."),
    "NT_STATUS_CONNECTION_REFUSED": ("unreachable", "The file server refused the connection."),
    "NT_STATUS_CONNECTION_RESET": ("unreachable", "The connection to the file server dropped."),
    "NT_STATUS_DISK_FULL": ("disk_full", "The share is full. Ask IT for space."),
    "NT_STATUS_QUOTA_EXCEEDED": ("disk_full", "The account's quota on the share is used up."),
}


def classify_smb(output: str, name: str) -> Failure:
    """Turn smbclient output into a reason code and the step that fixes it."""

    for status, (reason, hint) in SMB_STATUS.items():
        if status in output:
            return Failure(reason, status, hint.format(name=name))
    lowered = output.lower()
    if "command not found" in lowered:
        return Failure(
            "smbclient_missing",
            "smbclient is not installed",
            "Install it from the internal apt mirror: sudo apt-get install -y smbclient",
        )
    if "no answer within" in lowered:
        return Failure("unreachable", output.strip(), "The file server did not answer.")
    if "connection to" in lowered and "failed" in lowered:
        return Failure("unreachable", output.strip(), "The file server cannot be reached.")
    statuses = [line.strip() for line in output.splitlines() if "NT_STATUS_" in line]
    detail = statuses[0] if statuses else (output.strip().splitlines()[-1] if output.strip() else "no output")
    return Failure("smb_error", detail, "See the log for smbclient's full output.")


def classify_mount(output: str, name: str) -> Failure:
    """Turn the output of a failed mount command into a reason and a fix."""

    text = output.lower()
    if "error(13)" in text or "permission denied" in text:
        return Failure(
            "credentials_rejected",
            output.strip(),
            "The share refused the credentials used to mount it. Store the new ones with "
            f"`backup.sh set-credentials {name}` (if the mount reads that file), then retry.",
        )
    if "error(112)" in text or "error(113)" in text or "host is down" in text:
        return Failure("unreachable", output.strip(), "The file server cannot be reached.")
    if "could not resolve" in text:
        return Failure("unreachable", output.strip(), "The file server's name does not resolve.")
    if "only root" in text or "must be superuser" in text:
        return Failure(
            "mount_not_permitted",
            output.strip(),
            "This user may not mount the share. Ask IT for a 'user' fstab entry, or use "
            "type = \"smb\", which needs no mount.",
        )
    return Failure("mount_failed", output.strip() or "mount failed", "See the log.")


def classify_oserror(error: OSError, name: str) -> Failure:
    """Turn an operating-system error at a destination into a reason and a fix."""

    code = error.errno
    detail = f"{error.strerror or error} ({error.filename or ''})".strip()
    if code in (errno.EACCES, errno.EPERM):
        return Failure(
            "permission_denied",
            detail,
            "Writing was refused. On a mounted network share this is usually expired "
            f"credentials: run `backup.sh set-credentials {name}` and remount.",
        )
    if code in (errno.ENOSPC, errno.EDQUOT):
        return Failure("disk_full", detail, "The destination is full.")
    if code in (errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.ENOTCONN):
        return Failure("unreachable", detail, "The network share is not reachable.")
    if code == errno.ESTALE:
        return Failure("stale_mount", detail, "The network mount is stale; remount it.")
    if code == errno.EROFS:
        return Failure("read_only", detail, "The destination is mounted read-only.")
    if code == errno.ENOENT:
        return Failure("path_not_found", detail, "A folder on the path does not exist.")
    return Failure("io_error", detail, "See the log.")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def credential_path(name: str) -> Path:
    return CREDENTIAL_DIR / f"{name}.cred"


def credential_age_days(name: str) -> float | None:
    meta = CREDENTIAL_DIR / f"{name}.meta.json"
    try:
        updated = json.loads(meta.read_text(encoding="utf-8"))["updated_utc"]
    except (OSError, ValueError, KeyError):
        return None
    moment = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
    return (utc_now() - moment).total_seconds() / 86400.0


def store_credentials(name: str, username: str, password: str, domain: str) -> Path:
    """Write an smbclient/mount.cifs credentials file, readable only by this user."""

    CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CREDENTIAL_DIR, 0o700)
    os.chmod(CREDENTIAL_DIR.parent, 0o700)
    lines = [f"username={username}", f"password={password}"]
    if domain:
        lines.append(f"domain={domain}")
    target = credential_path(name)
    descriptor, temporary = tempfile.mkstemp(dir=CREDENTIAL_DIR, prefix=".cred-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    write_json_atomic(
        CREDENTIAL_DIR / f"{name}.meta.json",
        {"destination": name, "username": username, "domain": domain, "updated_utc": iso(utc_now())},
    )
    return target


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


def archive_files(name: str) -> list[str]:
    return [name, f"{name}.sha256", f"{name}.json"]


def is_archive_name(name: str, host: str | None = None) -> bool:
    """Whether ``name`` is one of this host's archives (never another machine's)."""

    host = host or socket.gethostname()
    return name.startswith(f"{ARCHIVE_PREFIX}-{host}-") and name.endswith(".tar.gz")


class PathTarget:
    """A directory: on this machine, or a network share that is already mounted."""

    def __init__(self, spec: Destination) -> None:
        self.spec = spec
        self.root = _expand(spec.path)

    @property
    def description(self) -> str:
        return str(self.root)

    def _probe(self) -> None:
        """Prove the folder is there and writable, without hanging on a dead mount.

        A network mount whose server has gone away can block any process that
        touches it. The probe runs in a child process with a time limit, so a
        dead share costs ``timeout_seconds`` and a clear message rather than a
        backup that never finishes.
        """

        script = (
            "import os,sys,pathlib;"
            "p=pathlib.Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=True);"
            "t=p/'.ml-backup-probe';t.write_text('ok');t.unlink()"
        )
        code, output = run_command(
            [sys.executable, "-c", script, str(self.root)], timeout=self.spec.timeout_seconds
        )
        if code == 124:
            raise Failure(
                "unreachable",
                f"{self.root} did not respond within {self.spec.timeout_seconds} s",
                "The share is mounted but not answering (server down or network lost).",
            )
        if code != 0:
            raise self._oserror_from_output(output)

    def _oserror_from_output(self, output: str) -> Failure:
        for name in ("PermissionError", "Errno 13", "Errno 1]"):
            if name in output:
                return classify_oserror(OSError(errno.EACCES, "Permission denied", str(self.root)), self.spec.name)
        for marker, number in (
            ("Errno 28", errno.ENOSPC),
            ("Errno 122", errno.EDQUOT),
            ("Errno 112", errno.EHOSTDOWN),
            ("Errno 113", errno.EHOSTUNREACH),
            ("Errno 110", errno.ETIMEDOUT),
            ("Errno 107", errno.ENOTCONN),
            ("Errno 116", errno.ESTALE),
            ("Errno 30", errno.EROFS),
        ):
            if marker in output:
                return classify_oserror(OSError(number, os.strerror(number), str(self.root)), self.spec.name)
        tail = output.strip().splitlines()[-1] if output.strip() else "unknown error"
        return Failure("io_error", tail, "See the log.")

    def check(self) -> None:
        mountpoint = self.spec.require_mountpoint
        if mountpoint:
            point = _expand(mountpoint)
            if not os.path.ismount(point):
                if self.spec.mount_command:
                    log.info("[%s] %s is not mounted; running: %s", self.spec.name, point, self.spec.mount_command)
                    code, output = run_command(
                        ["/bin/sh", "-c", self.spec.mount_command], timeout=max(60, self.spec.timeout_seconds)
                    )
                    if code != 0 or not os.path.ismount(point):
                        raise classify_mount(output, self.spec.name)
                else:
                    raise Failure(
                        "not_mounted",
                        f"{point} is not mounted",
                        "Nothing is written into an empty mount point, because that would "
                        "quietly fill the local disk instead. Mount the share, or set "
                        "mount_command in backup.toml.",
                    )
        self._probe()

    def free_bytes(self) -> int | None:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return None

    def list(self) -> list[str]:
        try:
            return sorted(entry.name for entry in self.root.iterdir() if is_archive_name(entry.name))
        except OSError as error:
            raise classify_oserror(error, self.spec.name) from error

    def read_manifest(self, name: str) -> dict[str, Any] | None:
        try:
            return json.loads((self.root / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, staged: dict[str, Path], expected_sha: str) -> None:
        """Copy as .partial, verify the copy against the checksum, then rename."""

        try:
            # A copy cut off by a power or network loss leaves a .partial behind;
            # it is never a backup, so it is cleared before the next copy.
            for stale in self.root.glob(f"{ARCHIVE_PREFIX}-{socket.gethostname()}-*.partial"):
                stale.unlink()
                log.info("[%s] removed an interrupted copy: %s", self.spec.name, stale.name)
            for final_name, source in staged.items():
                partial = self.root / f"{final_name}.partial"
                with source.open("rb") as reader, partial.open("wb") as writer:
                    shutil.copyfileobj(reader, writer, 1 << 20)
                    writer.flush()
                    os.fsync(writer.fileno())
            archive_name = next(name for name in staged if name.endswith(".tar.gz"))
            copied = sha256_file(self.root / f"{archive_name}.partial")
            if copied != expected_sha:
                for final_name in staged:
                    with contextlib.suppress(OSError):
                        (self.root / f"{final_name}.partial").unlink()
                raise Failure(
                    "verify_failed",
                    f"checksum of the copy is {copied}, expected {expected_sha}",
                    "The copy was corrupted in transit; it was deleted. The next run retries.",
                )
            # The archive last: a listing never shows an archive without its sidecars.
            for final_name in sorted(staged, key=lambda item: item.endswith(".tar.gz")):
                os.replace(self.root / f"{final_name}.partial", self.root / final_name)
        except OSError as error:
            raise classify_oserror(error, self.spec.name) from error

    def delete(self, name: str) -> None:
        for item in archive_files(name):
            with contextlib.suppress(FileNotFoundError):
                (self.root / item).unlink()


class SmbTarget:
    """A Windows/SMB share reached with smbclient: no mount, no root."""

    def __init__(self, spec: Destination) -> None:
        self.spec = spec

    @property
    def description(self) -> str:
        return f"{self.spec.share}/{self.spec.remote_dir}".rstrip("/")

    def _client(
        self, commands: list[str], *, directory: str | None = None, timeout: int | None = None
    ) -> str:
        """Run smbclient commands, inside ``directory`` when given.

        The folder is entered with ``-D``, never with a ``cd`` in the command
        list: smbclient carries on after a failed ``cd`` and would put the file
        in the top of the share instead (and report success), whereas ``-D``
        stops before running anything.
        """

        credentials = credential_path(self.spec.credentials)
        if not credentials.is_file():
            raise Failure(
                "credentials_missing",
                f"no credentials stored for {self.spec.credentials!r}",
                f"Store them with: backup.sh set-credentials {self.spec.credentials}",
            )
        command = [
            "smbclient",
            self.spec.share,
            "-A",
            str(credentials),
            "-t",
            str(max(self.spec.timeout_seconds, 10)),
        ]
        if directory:
            command += ["-D", directory]
        command += ["-c", "; ".join(commands)]
        code, output = run_command(command, timeout=timeout or max(120, self.spec.timeout_seconds * 4))
        if code != 0 or "NT_STATUS_" in output:
            log.info("[%s] smbclient said: %s", self.spec.name, " | ".join(output.strip().splitlines()[-6:]))
            raise classify_smb(output, self.spec.credentials)
        return output

    @property
    def folder(self) -> str | None:
        return self.spec.remote_dir or None

    def check(self) -> None:
        """Sign in, and make sure the folder exists (creating it if it does not)."""

        try:
            self._client(["ls"], directory=self.folder)
            return
        except Failure as failure:
            if failure.reason != "path_not_found":
                raise
        parts = self.spec.remote_dir.split("/")
        for depth in range(1, len(parts) + 1):
            parent = "/".join(parts[: depth - 1]) or None
            try:
                self._client([f'mkdir "{parts[depth - 1]}"'], directory=parent)
                log.info("[%s] created folder %s on the share", self.spec.name, "/".join(parts[:depth]))
            except Failure as failure:
                if failure.reason != "already_exists":
                    raise
        self._client(["ls"], directory=self.folder)

    def free_bytes(self) -> int | None:
        return None

    def list(self) -> list[str]:
        output = self._client(["ls"], directory=self.folder)
        names = []
        for line in output.splitlines():
            token = line.strip().split(" ")[0] if line.strip() else ""
            if is_archive_name(token):
                names.append(token)
        return sorted(set(names))

    def read_manifest(self, name: str) -> dict[str, Any] | None:
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder) / "manifest.json"
            try:
                self._client([f'get "{name}.json" "{local}"'], directory=self.folder)
                return json.loads(local.read_text(encoding="utf-8"))
            except (Failure, OSError, ValueError):
                return None

    def put(self, staged: dict[str, Path], expected_sha: str) -> None:
        archive_name = next(name for name in staged if name.endswith(".tar.gz"))
        size = staged[archive_name].stat().st_size
        # Generous: 1 MiB/s plus ten minutes, for a slow office link.
        upload_timeout = 600 + size // (1 << 20)
        # Clear what an interrupted upload left behind; none is a failure.
        with contextlib.suppress(Failure):
            self._client([f'del "{ARCHIVE_PREFIX}-{socket.gethostname()}-*.partial"'], directory=self.folder)
        commands: list[str] = []
        for final_name, source in sorted(staged.items(), key=lambda item: item[0].endswith(".tar.gz")):
            commands.append(f'put "{source}" "{final_name}.partial"')
        self._client(commands, directory=self.folder, timeout=upload_timeout)
        if self.spec.verify_after_upload:
            with tempfile.TemporaryDirectory(dir=staged[archive_name].parent) as folder:
                back = Path(folder) / "readback.tar.gz"
                self._client([f'get "{archive_name}.partial" "{back}"'], directory=self.folder, timeout=upload_timeout)
                copied = sha256_file(back)
            if copied != expected_sha:
                with contextlib.suppress(Failure):
                    self._client([f'del "{n}.partial"' for n in staged], directory=self.folder)
                raise Failure(
                    "verify_failed",
                    f"the copy on the share has checksum {copied}, expected {expected_sha}",
                    "The upload was corrupted; it was deleted. The next run retries.",
                )
        # Names carry a timestamp, so the final name never exists yet: a plain
        # rename is atomic on the server, and the archive goes last so a listing
        # never shows an archive without its checksum and manifest.
        ordered = sorted(staged, key=lambda item: item.endswith(".tar.gz"))
        self._client([f'rename "{n}.partial" "{n}"' for n in ordered], directory=self.folder)

    def delete(self, name: str) -> None:
        for item in archive_files(name):
            with contextlib.suppress(Failure):
                self._client([f'del "{item}"'], directory=self.folder)


def make_target(spec: Destination) -> PathTarget | SmbTarget:
    return PathTarget(spec) if spec.type == "path" else SmbTarget(spec)


# ---------------------------------------------------------------------------
# Building the archive
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Entry:
    source: Path
    arcname: str


def _current_release(root: Path) -> str | None:
    link = root / "current"
    try:
        target = os.readlink(link)
    except OSError:
        return None
    resolved = (link.parent / target).resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def collect(config: Config) -> tuple[list[Entry], list[str]]:
    """Every file, symlink and folder to archive, and the warnings met finding them."""

    root = config.source_root
    entries: list[Entry] = []
    warnings: list[str] = []
    keep_release = _current_release(root) if config.include_current_release else None

    def excluded(relative: str) -> bool:
        if keep_release and (relative == keep_release or relative.startswith(keep_release + "/")):
            return False
        if keep_release and keep_release.startswith(relative + "/"):
            return False  # a parent of the kept release (e.g. releases/) must be walked
        return matches(relative, config.exclude)

    for folder, directories, files in os.walk(root, followlinks=False):
        here = Path(folder)
        relative_folder = here.relative_to(root).as_posix()
        if relative_folder != "." and excluded(relative_folder):
            directories[:] = []
            continue
        kept_dirs = []
        for name in sorted(directories):
            path = here / name
            relative = path.relative_to(root).as_posix()
            if excluded(relative):
                continue
            if path.is_symlink():
                entries.append(Entry(path, relative))
            else:
                kept_dirs.append(name)
                entries.append(Entry(path, relative))
        directories[:] = kept_dirs
        for name in sorted(files):
            path = here / name
            relative = path.relative_to(root).as_posix()
            if excluded(relative):
                continue
            if path.is_symlink() or path.is_file():
                entries.append(Entry(path, relative))
    for extra in config.include_extra:
        if not extra.exists():
            warnings.append(f"extra path {extra} does not exist; skipped")
            continue
        base = f"_extra/{extra.as_posix().lstrip('/')}"
        if extra.is_dir():
            for folder, _directories, files in os.walk(extra):
                for name in sorted(files):
                    path = Path(folder) / name
                    entries.append(Entry(path, f"_extra/{path.as_posix().lstrip('/')}"))
        else:
            entries.append(Entry(extra, base))
    return entries, warnings


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def snapshot_sqlite(source: Path, target: Path) -> str:
    """Copy a live database consistently; return the integrity check's answer."""

    source_db = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=60)
    try:
        target_db = sqlite3.connect(target)
        try:
            source_db.backup(target_db)
            answer = target_db.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target_db.close()
    finally:
        source_db.close()
    return str(answer)


class HashingReader(io.RawIOBase):
    """Hash exactly the bytes tarfile reads, so the manifest matches the archive."""

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.digest.update(data)
        return data


def system_metadata(config: Config) -> dict[str, bytes]:
    """The _meta/ folder: what a rebuild on new hardware needs to know."""

    root = config.source_root
    meta: dict[str, bytes] = {}
    os_release = ""
    with contextlib.suppress(OSError):
        os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    code, timezone = run_command(["timedatectl", "show", "-p", "Timezone", "--value"], timeout=10)
    info = {
        "host": socket.gethostname(),
        "fqdn": socket.getfqdn(),
        "kernel": platform.release(),
        "os_release": os_release,
        "python": sys.version,
        "timezone": timezone.strip() if code == 0 else None,
        "user": getpass.getuser(),
        "source_root": str(root),
    }
    meta["system.json"] = (json.dumps(info, indent=2) + "\n").encode()
    venv_python = root / ".venv" / "bin" / "python"
    if venv_python.exists():
        code, freeze = run_command([str(venv_python), "-m", "pip", "freeze", "--all"], timeout=120)
        meta["pip-freeze.txt"] = freeze.encode() if code == 0 else f"pip freeze failed:\n{freeze}".encode()
    code, crontab = run_command(["crontab", "-l"], timeout=10)
    meta["crontab.txt"] = crontab.encode() if code == 0 else b"(no crontab)\n"
    status_script = root / "current" / "deploy" / "status.sh"
    if status_script.exists():
        code, status = run_command([str(status_script), "--json"], timeout=120)
        meta["suite-status.json"] = status.encode()
    code, units = run_command(["systemctl", "--user", "list-unit-files", "ml-*", "--no-legend"], timeout=20)
    meta["systemd-user-units.txt"] = units.encode()
    return meta


def suite_identity(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "current" / "manifest.resolved.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"suite_version": None, "components": {}}
    components = {
        name: {"repo": service.get("repo"), "ref": service.get("ref"), "commit": service.get("commit")}
        for name, service in (data.get("services") or {}).items()
    }
    return {"suite_version": data.get("suite_version"), "components": components}


@dataclasses.dataclass
class Built:
    archive: Path
    sha256: str
    fingerprint: str
    manifest: dict[str, Any]
    files: int
    source_bytes: int


def build_archive(config: Config, name: str, staging: Path, run_id: str) -> Built:
    """Write the archive, hashing every member, with consistent database copies."""

    entries, warnings = collect(config)
    database_dir = staging / "databases"
    database_dir.mkdir(parents=True, exist_ok=True)
    archive_path = staging / name

    database_paths = {entry.source for entry in entries if entry.source.is_file() and not entry.source.is_symlink() and is_sqlite(entry.source)}
    companions = {Path(f"{db}{suffix}") for db in database_paths for suffix in ("-wal", "-shm", "-journal")}

    files_manifest: list[dict[str, Any]] = []
    databases: list[dict[str, Any]] = []
    total = 0
    with tarfile.open(archive_path, "w:gz", compresslevel=config.compress_level) as archive:
        for entry in entries:
            source = entry.source
            if source in companions:
                continue  # folded into the database's consistent copy
            if source.is_symlink():
                info = archive.gettarinfo(str(source), arcname=entry.arcname)
                archive.addfile(info)
                files_manifest.append({"path": entry.arcname, "type": "symlink", "target": os.readlink(source)})
                continue
            if source.is_dir():
                info = archive.gettarinfo(str(source), arcname=entry.arcname)
                archive.addfile(info)
                continue
            read_from = source
            if source in database_paths:
                copy = database_dir / f"{len(databases):04d}.sqlite3"
                try:
                    integrity = snapshot_sqlite(source, copy)
                    method = "sqlite-online-backup"
                    read_from = copy
                except sqlite3.Error as error:
                    integrity = f"not checked: {error}"
                    method = "raw-copy"
                    warnings.append(
                        f"{entry.arcname}: SQLite online backup failed ({error}); copied the file as it is"
                    )
                if integrity != "ok":
                    warnings.append(f"{entry.arcname}: integrity check says {integrity!r}")
                databases.append({"path": entry.arcname, "method": method, "integrity": integrity})
            try:
                info = archive.gettarinfo(str(read_from), arcname=entry.arcname)
                if read_from != source:
                    source_stat = source.stat()
                    info.mode = source_stat.st_mode
                    info.mtime = source_stat.st_mtime
                if info.size <= SMALL_FILE_BYTES:
                    # Read whole, then written: a log appended to (or truncated)
                    # mid-read is captured as one consistent snapshot, and the tar
                    # stream can never be left short of the size it promised.
                    data = read_from.read_bytes()
                    info.size = len(data)
                    digest = hashlib.sha256(data).hexdigest()
                    archive.addfile(info, io.BytesIO(data))
                else:
                    with read_from.open("rb") as handle:
                        reader = HashingReader(handle)
                        archive.addfile(info, reader)  # type: ignore[arg-type]
                    digest = reader.digest.hexdigest()
            except OSError as error:
                warnings.append(f"{entry.arcname}: could not be read ({error}); left out")
                continue
            total += info.size
            files_manifest.append(
                {
                    "path": entry.arcname,
                    "type": "file",
                    "size": info.size,
                    "sha256": digest,
                    "mode": oct(info.mode & 0o7777),
                    "mtime": iso(dt.datetime.fromtimestamp(info.mtime, dt.UTC)),
                }
            )
        for meta_name, payload in system_metadata(config).items():
            info = tarfile.TarInfo(f"_meta/{meta_name}")
            info.size = len(payload)
            info.mtime = int(utc_now().timestamp())
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))

    fingerprint_lines = sorted(
        f"{item['path']}\0{item.get('sha256') or item.get('target')}\0{item.get('mode', '')}"
        for item in files_manifest
        if not matches(item["path"], FINGERPRINT_IGNORE)
    )
    fingerprint = hashlib.sha256("\n".join(fingerprint_lines).encode()).hexdigest()
    archive_sha = sha256_file(archive_path)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "tool_version": TOOL_VERSION,
        "run_id": run_id,
        "archive": name,
        "created_utc": iso(utc_now()),
        "host": socket.gethostname(),
        "source_root": str(config.source_root),
        "suite": suite_identity(config.source_root),
        "sha256": archive_sha,
        "bytes": archive_path.stat().st_size,
        "fingerprint": fingerprint,
        "excluded": config.exclude,
        "included_extra": [str(path) for path in config.include_extra],
        "databases": databases,
        "warnings": warnings,
        "files": files_manifest,
    }
    shutil.rmtree(database_dir, ignore_errors=True)
    return Built(archive_path, archive_sha, fingerprint, manifest, len(files_manifest), total)


def verify_archive(path: Path, expected_sha: str | None = None, manifest: dict[str, Any] | None = None) -> list[str]:
    """Re-read an archive end to end; return a list of problems (empty means sound)."""

    problems: list[str] = []
    if expected_sha is not None:
        actual = sha256_file(path)
        if actual != expected_sha:
            problems.append(f"archive checksum {actual} does not match {expected_sha}")
            return problems
    expected = {item["path"]: item for item in (manifest or {}).get("files", []) if item.get("type") == "file"}
    seen = set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    problems.append(f"unsafe path in archive: {member.name}")
                    continue
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                digest = hashlib.sha256()
                assert handle is not None
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
                want = expected.get(member.name)
                if want is not None:
                    seen.add(member.name)
                    if want["sha256"] != digest.hexdigest():
                        problems.append(f"{member.name}: content differs from the manifest")
    except (tarfile.TarError, OSError, EOFError) as error:
        problems.append(f"archive cannot be read: {error}")
    missing = set(expected) - seen
    if missing:
        problems.append(f"{len(missing)} file(s) in the manifest are missing from the archive")
    return problems


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------


def state_dir(config: Config) -> Path:
    return config.source_root / "shared" / "state"


def log_dir(config: Config) -> Path:
    return config.source_root / "shared" / "logs" / "backup"


def last_stored_path(config: Config) -> Path:
    return state_dir(config) / "backup-last-stored.json"


@contextlib.contextmanager
def run_lock(config: Config) -> Iterator[bool]:
    state_dir(config).mkdir(parents=True, exist_ok=True)
    with (state_dir(config) / "backup.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield True


def setup_logging(config: Config | None, stamp: str, *, to_file: bool) -> Path | None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    log.addHandler(console)
    if not (to_file and config):
        return None
    folder = log_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"backup-{stamp}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter)
    log.addHandler(handler)
    return path


def prune_logs(config: Config) -> None:
    logs = sorted(log_dir(config).glob("backup-*.log"))
    for old in logs[: max(0, len(logs) - config.log_keep_runs)]:
        with contextlib.suppress(OSError):
            old.unlink()


def cmd_run(config: Config, *, dry_run: bool, trigger: str) -> int:
    started = utc_now()
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    log_path = setup_logging(config, stamp, to_file=not dry_run)
    host = socket.gethostname()
    suite = suite_identity(config.source_root)
    version = suite.get("suite_version") or "unknown"
    name = f"{ARCHIVE_PREFIX}-{host}-{stamp}-suite{version}.tar.gz"
    run_id = f"{host}-{stamp}"
    record: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "tool_version": TOOL_VERSION,
        "run_id": run_id,
        "trigger": trigger,
        "dry_run": dry_run,
        "host": host,
        "started_utc": iso(started),
        "started_local": started.astimezone().isoformat(timespec="seconds"),
        "config_path": str(config.path),
        "log_path": str(log_path) if log_path else None,
        "suite": suite,
        "source": {"root": str(config.source_root), "exclude": config.exclude},
        "archive": None,
        "databases": [],
        "destinations": [],
        "warnings": [],
        "errors": [],
    }

    if not config.enabled:
        log.warning("backups are disabled in %s (enabled = false); nothing done", config.path)
        return EXIT_OK

    with run_lock(config) as acquired:
        if not acquired:
            log.error("another backup is already running; this one stops without doing anything")
            return EXIT_LOCKED
        log.info("backup %s starting (trigger=%s%s)", run_id, trigger, ", DRY RUN" if dry_run else "")
        log.info("suite %s on %s; source %s", version, host, config.source_root)

        targets = [(spec, make_target(spec)) for spec in config.destinations]
        if dry_run:
            entries, warnings = collect(config)
            size = sum(e.source.stat().st_size for e in entries if e.source.is_file() and not e.source.is_symlink())
            log.info("would archive %d entries, %s before compression", len(entries), human_bytes(size))
            for warning in warnings:
                log.warning(warning)
            worst = EXIT_OK
            for spec, target in targets:
                try:
                    target.check()
                    log.info("[%s] %s: reachable and writable", spec.name, target.description)
                except Failure as failure:
                    log.error("[%s] %s: %s -- %s", spec.name, target.description, failure, failure.hint)
                    worst = EXIT_PARTIAL
            log.info("dry run finished; nothing was written")
            return worst

        staging = config.staging_dir / f"run-{stamp}"
        exit_code = EXIT_FAILED
        try:
            staging.mkdir(parents=True, exist_ok=False)
            built = build_archive(config, name, staging, run_id)
            log.info(
                "archive %s: %d files, %s of data -> %s compressed, sha256 %s",
                name, built.files, human_bytes(built.source_bytes), human_bytes(built.manifest["bytes"]), built.sha256,
            )
            for database in built.manifest["databases"]:
                log.info("database %s: %s, integrity %s", database["path"], database["method"], database["integrity"])
            for warning in built.manifest["warnings"]:
                log.warning(warning)
            problems = verify_archive(built.archive, built.sha256, built.manifest)
            if problems:
                for problem in problems:
                    log.error("verification: %s", problem)
                raise Failure("verify_failed", "; ".join(problems), "The archive was not stored anywhere.")
            log.info("archive verified: re-read end to end, every file matches its checksum")
            (staging / f"{name}.sha256").write_text(f"{built.sha256}  {name}\n", encoding="utf-8")
            write_json_atomic(staging / f"{name}.json", built.manifest)
            record["archive"] = {
                "name": name,
                "bytes": built.manifest["bytes"],
                "sha256": built.sha256,
                "fingerprint": built.fingerprint,
                "files": built.files,
                "source_bytes": built.source_bytes,
            }
            record["databases"] = built.manifest["databases"]
            record["warnings"].extend(built.manifest["warnings"])
            staged = {
                name: built.archive,
                f"{name}.sha256": staging / f"{name}.sha256",
                f"{name}.json": staging / f"{name}.json",
            }

            last_stored = {}
            with contextlib.suppress(OSError, ValueError):
                last_stored = json.loads(last_stored_path(config).read_text(encoding="utf-8"))

            results = []
            for spec, target in targets:
                results.append(store_one(config, spec, target, staged, built, last_stored))
            record["destinations"] = results
            write_json_atomic(last_stored_path(config), last_stored)

            good = [r for r in results if r["outcome"] in ("stored", "unchanged")]
            if len(good) == len(results):
                exit_code = EXIT_OK
            elif good:
                exit_code = EXIT_PARTIAL
            else:
                exit_code = EXIT_FAILED
        except Failure as failure:
            record["errors"].append({"reason": failure.reason, "detail": failure.detail, "hint": failure.hint})
            log.error("%s -- %s", failure, failure.hint)
        except Exception as error:  # noqa: BLE001 - the run must always leave a record
            record["errors"].append({"reason": "unexpected", "detail": repr(error), "hint": "See the log."})
            log.exception("unexpected error")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            finished = utc_now()
            record["finished_utc"] = iso(finished)
            record["duration_seconds"] = round((finished - started).total_seconds(), 1)
            record["outcome"] = {EXIT_OK: "ok", EXIT_PARTIAL: "partial", EXIT_FAILED: "failed"}[exit_code]
            record["exit_code"] = exit_code
            write_json_atomic(state_dir(config) / "backup-last.json", record)
            with (state_dir(config) / "backup-history.jsonl").open("a", encoding="utf-8") as history:
                history.write(json.dumps(record, sort_keys=True) + "\n")
            prune_logs(config)
            log.info(
                "backup %s finished: %s in %.0f s",
                run_id, record["outcome"].upper(), record["duration_seconds"],
            )
    return exit_code


def store_one(
    config: Config,
    spec: Destination,
    target: PathTarget | SmbTarget,
    staged: dict[str, Path],
    built: Built,
    last_stored: dict[str, Any],
) -> dict[str, Any]:
    """Store the archive at one destination, then apply retention there."""

    began = utc_now()
    result: dict[str, Any] = {
        "name": spec.name,
        "type": spec.type,
        "target": target.description,
        "outcome": "failed",
        "reason": None,
        "detail": None,
        "hint": None,
        "stored_as": None,
        "kept": [],
        "pruned": [],
    }
    try:
        target.check()
        existing = target.list()
        previous = last_stored.get(spec.name) or {}
        if previous.get("fingerprint") == built.fingerprint and previous.get("name") in existing:
            result["outcome"] = "unchanged"
            result["stored_as"] = previous["name"]
            log.info(
                "[%s] content identical to %s, already held there; nothing new stored",
                spec.name, previous["name"],
            )
        else:
            free = target.free_bytes()
            need = int(built.manifest["bytes"] * 1.1) + (1 << 20)
            if free is not None and free < need:
                raise Failure(
                    "disk_full",
                    f"{human_bytes(free)} free, {human_bytes(need)} needed",
                    "Free space at the destination, or lower 'keep'.",
                )
            target.put(staged, built.sha256)
            result["outcome"] = "stored"
            result["stored_as"] = next(n for n in staged if n.endswith(".tar.gz"))
            last_stored[spec.name] = {"name": result["stored_as"], "fingerprint": built.fingerprint, "stored_utc": iso(utc_now())}
            log.info("[%s] stored and verified at %s", spec.name, target.description)
            existing = target.list()
        # Retention only after a verified store (or a confirmed unchanged copy).
        ordered = sorted(existing)
        surplus = ordered[: max(0, len(ordered) - config.keep)]
        for old in surplus:
            target.delete(old)
            result["pruned"].append(old)
            log.info("[%s] removed old backup %s (keeping the newest %d)", spec.name, old, config.keep)
        result["kept"] = [item for item in ordered if item not in surplus]
    except Failure as failure:
        result.update(reason=failure.reason, detail=failure.detail, hint=failure.hint)
        log.error("[%s] FAILED at %s: %s -- %s", spec.name, target.description, failure, failure.hint)
    result["seconds"] = round((utc_now() - began).total_seconds(), 1)
    return result


# ---------------------------------------------------------------------------
# Other commands
# ---------------------------------------------------------------------------


CONFIG_TEMPLATE = Path(__file__).with_name("backup.toml.example")


def cmd_init_config(path: Path) -> int:
    if path.exists():
        print(f"{path} already exists; not overwritten.")
        return EXIT_OK
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CONFIG_TEMPLATE, path)
    os.chmod(path, 0o600)
    print(f"wrote {path}\nEdit the destinations in it, then run: backup.sh test-destinations")
    return EXIT_OK


def cmd_test(config: Config) -> int:
    setup_logging(None, "", to_file=False)
    worst = EXIT_OK
    for spec in config.destinations:
        target = make_target(spec)
        try:
            target.check()
            held = target.list()
            print(f"[ ok ] {spec.name:<10} {target.description}  ({len(held)} backup(s) held)")
        except Failure as failure:
            worst = EXIT_PARTIAL
            print(f"[FAIL] {spec.name:<10} {target.description}\n       {failure}\n       {failure.hint}")
        if spec.type == "smb":
            age = credential_age_days(spec.credentials)
            if age is not None:
                print(f"       credentials stored {age:.0f} day(s) ago")
    return worst


def cmd_status(config: Config, *, as_json: bool) -> int:
    path = state_dir(config) / "backup-last.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    credentials = {
        spec.name: credential_age_days(spec.credentials) for spec in config.destinations if spec.type == "smb"
    }
    if as_json:
        print(json.dumps({"last_run": record, "credential_age_days": credentials}, indent=2))
        return EXIT_OK
    if record is None:
        print("No backup has run yet.")
        return EXIT_OK
    print(f"last backup   {record['run_id']}  {record['outcome'].upper()}")
    print(f"started       {record['started_local']}  ({record.get('duration_seconds', '?')} s)")
    if record.get("archive"):
        archive = record["archive"]
        print(f"archive       {archive['name']}  {human_bytes(archive['bytes'])}")
    for destination in record.get("destinations", []):
        line = f"  {destination['name']:<10} {destination['outcome']:<10} {destination['target']}"
        if destination["outcome"] == "failed":
            line += f"\n             {destination['reason']}: {destination['detail']}\n             {destination['hint']}"
        else:
            line += f"  ({len(destination.get('kept', []))} kept)"
        print(line)
    for error in record.get("errors", []):
        print(f"  error: {error['reason']}: {error['detail']}\n         {error['hint']}")
    for name, age in credentials.items():
        print(f"credentials   {name}: " + ("not stored" if age is None else f"updated {age:.0f} day(s) ago"))
    print(f"log           {record.get('log_path')}")
    return EXIT_OK


def cmd_list(config: Config) -> int:
    for spec in config.destinations:
        target = make_target(spec)
        print(f"{spec.name}  ({target.description})")
        try:
            for name in target.list():
                print(f"  {name}")
        except Failure as failure:
            print(f"  cannot list: {failure} -- {failure.hint}")
    return EXIT_OK


def cmd_set_credentials(config: Config, name: str) -> int:
    spec = next((d for d in config.destinations if d.name == name or d.credentials == name), None)
    credential_name = spec.credentials if spec else name
    print(f"Credentials for {credential_name!r} (stored in {credential_path(credential_name)}, mode 600).")
    username = input("user name: ").strip()
    domain = input("domain (empty if none): ").strip()
    password = getpass.getpass("password: ")
    again = getpass.getpass("password again: ")
    if not username or not password:
        print("user name and password are both required; nothing stored.")
        return EXIT_CONFIG
    if password != again:
        print("the two passwords differ; nothing stored.")
        return EXIT_CONFIG
    store_credentials(credential_name, username, password, domain)
    print("stored.")
    if spec is not None:
        try:
            make_target(spec).check()
            print(f"[ ok ] {spec.name} accepts the new credentials.")
        except Failure as failure:
            print(f"[FAIL] {spec.name}: {failure}\n       {failure.hint}")
            return EXIT_PARTIAL
    return EXIT_OK


def _sidecars(archive: Path) -> tuple[str | None, dict[str, Any] | None]:
    expected = None
    sha_file = Path(f"{archive}.sha256")
    if sha_file.is_file():
        expected = sha_file.read_text(encoding="utf-8").split()[0]
    manifest = None
    with contextlib.suppress(OSError, ValueError):
        manifest = json.loads(Path(f"{archive}.json").read_text(encoding="utf-8"))
    return expected, manifest


def cmd_verify(archive: Path) -> int:
    expected, manifest = _sidecars(archive)
    if expected is None:
        print(f"note: no {archive.name}.sha256 beside the archive; checking readability only")
    problems = verify_archive(archive, expected, manifest)
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}")
        return EXIT_FAILED
    print(f"[ ok ] {archive.name}: checksum and every file verified")
    return EXIT_OK


def cmd_extract(archive: Path, destination: Path) -> int:
    if destination.exists() and any(destination.iterdir()):
        print(f"{destination} is not empty; choose a new folder so nothing is overwritten.")
        return EXIT_CONFIG
    if cmd_verify(archive) != EXIT_OK:
        print("refusing to extract an archive that did not verify.")
        return EXIT_FAILED
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(destination, filter="data")
        else:  # pragma: no cover - Python < 3.12
            tar.extractall(destination)
    print(f"extracted to {destination}\nNext steps: RUNBOOK.md, section 'Restoring from a backup'.")
    return EXIT_OK


UNIT_DIR = HOME / ".config" / "systemd" / "user"


def cmd_install_timer(config: Config) -> int:
    script = config.source_root / "current" / "deploy" / "backup.py"
    service = f"""[Unit]
Description=ML Server suite backup (deploy/backup.py)
Documentation=file:{config.source_root}/current/RUNBOOK.md

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {script} run --trigger timer --config {config.path}
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=6h
"""
    timer = f"""[Unit]
Description=Nightly ML Server suite backup

[Timer]
OnCalendar={config.schedule}
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
"""
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / f"{TIMER_NAME}.service").write_text(service, encoding="utf-8")
    (UNIT_DIR / f"{TIMER_NAME}.timer").write_text(timer, encoding="utf-8")
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", f"{TIMER_NAME}.timer"],
    ):
        code, output = run_command(command, timeout=60)
        if code != 0:
            print(f"failed: {' '.join(command)}\n{output}")
            return EXIT_FAILED
    code, output = run_command(["systemctl", "--user", "list-timers", f"{TIMER_NAME}.timer", "--no-pager"], timeout=30)
    print(output)
    print(f"installed: {TIMER_NAME}.timer runs '{config.schedule}' (server local time).")
    return EXIT_OK


def cmd_remove_timer() -> int:
    run_command(["systemctl", "--user", "disable", "--now", f"{TIMER_NAME}.timer"], timeout=60)
    for suffix in ("timer", "service"):
        with contextlib.suppress(FileNotFoundError):
            (UNIT_DIR / f"{TIMER_NAME}.{suffix}").unlink()
    run_command(["systemctl", "--user", "daemon-reload"], timeout=60)
    print(f"removed {TIMER_NAME}.timer; no further nightly backups. Existing backups are untouched.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help=f"configuration file (default {DEFAULT_CONFIG})"
    )
    parser = argparse.ArgumentParser(prog="backup.sh", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", parents=[common], help="take a backup now")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--trigger", default="manual")
    sub.add_parser("init-config", parents=[common], help="write a configuration to edit")
    sub.add_parser("test-destinations", parents=[common], help="check every destination")
    status = sub.add_parser("status", parents=[common], help="the last run")
    status.add_argument("--json", action="store_true")
    sub.add_parser("list", parents=[common], help="backups held at every destination")
    creds = sub.add_parser("set-credentials", parents=[common], help="store or change a share's credentials")
    creds.add_argument("name")
    verify = sub.add_parser("verify", parents=[common], help="check an archive end to end")
    verify.add_argument("archive", type=Path)
    extract = sub.add_parser("extract", parents=[common], help="verify, then unpack an archive for a restore")
    extract.add_argument("archive", type=Path)
    extract.add_argument("--to", type=Path, required=True)
    sub.add_parser("install-timer", parents=[common], help="run every night")
    sub.add_parser("remove-timer", parents=[common], help="stop the nightly runs")
    args = parser.parse_args(argv)

    if args.command == "init-config":
        return cmd_init_config(args.config)
    if args.command == "verify":
        return cmd_verify(args.archive)
    if args.command == "extract":
        return cmd_extract(args.archive, args.to)
    if args.command == "remove-timer":
        return cmd_remove_timer()
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"configuration problem: {error}", file=sys.stderr)
        return EXIT_CONFIG
    if args.command == "run":
        return cmd_run(config, dry_run=args.dry_run, trigger=args.trigger)
    if args.command == "test-destinations":
        return cmd_test(config)
    if args.command == "status":
        return cmd_status(config, as_json=args.json)
    if args.command == "list":
        return cmd_list(config)
    if args.command == "set-credentials":
        return cmd_set_credentials(config, args.name)
    if args.command == "install-timer":
        return cmd_install_timer(config)
    return EXIT_CONFIG  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
