"""Tests for deploy/backup.py, run against a throwaway ~/ml_platform tree.

Each test states a promise the backup makes to the office: what is in an
archive and what is not, that a live database is copied consistently, that an
unchanged night stores nothing, that only the newest copies are kept, that a
failing destination is never pruned, and that rejected or expired credentials
are reported as exactly that.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "deploy"))

import backup  # noqa: E402

HOST = socket.gethostname()


@pytest.fixture()
def platform_root(tmp_path: Path) -> Path:
    """A miniature ~/ml_platform with a live database and two releases."""

    root = tmp_path / "ml_platform"
    for folder in (
        "shared/data/online_annotator",
        "shared/config",
        "shared/logs",
        "shared/tmp",
        "shared/docs",
        "shared/state",
        "shared/models/hydride",
        "releases/1.18.0/deploy",
        "releases/1.19.0/deploy",
        ".venv/lib",
        "deployment_inbox",
        "backups/20261030T051400Z",
    ):
        (root / folder).mkdir(parents=True)
    (root / "current").symlink_to("releases/1.19.0")
    (root / "releases/1.19.0/manifest.resolved.json").write_text(
        json.dumps({"suite_version": "1.19.0", "services": {"pytex": {"repo": "kvmani/pytex", "ref": "v0.13.0", "commit": "7f41ac0"}}})
    )
    (root / "releases/1.19.0/deploy/update.sh").write_text("#!/bin/sh\n")
    (root / "releases/1.18.0/deploy/update.sh").write_text("#!/bin/sh\n")
    (root / ".venv/lib/torch.so").write_bytes(b"x" * 1000)
    (root / "shared/docs/index.html").write_text("<html/>")
    (root / "shared/tmp/scratch").write_text("scratch")
    (root / "deployment_inbox/suite.tar.gz").write_bytes(b"archive")
    (root / "shared/config/config.intranet.json").write_text('{"secret_key": "s"}')
    (root / "shared/models/hydride/model_registry.json").write_text("{}")
    (root / "shared/logs/portal.log").write_text("started\n")

    database = sqlite3.connect(root / "shared/data/engagement.sqlite3")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, page TEXT)")
    database.executemany("INSERT INTO visits (page) VALUES (?)", [("home",)] * 50)
    database.commit()
    database.close()
    return root


def make_config(root: Path, tmp_path: Path, *, keep: int = 10, network: dict | None = None) -> backup.Config:
    destinations = [backup.Destination(name="local", type="path", path=str(tmp_path / "local_backups"))]
    if network is not None:
        destinations.append(backup.Destination(**network))
    return backup.Config(
        path=root / "shared/config/backup.toml",
        source_root=root,
        keep=keep,
        staging_dir=tmp_path / "staging",
        exclude=[".venv", "shared/tmp", "shared/docs", "deployment_inbox", "releases/*"],
        include_current_release=True,
        include_extra=[],
        compress_level=1,
        log_keep_runs=5,
        schedule="*-*-* 00:00:00",
        destinations=destinations,
        enabled=True,
    )


@pytest.fixture(autouse=True)
def quiet_system(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No real systemctl, crontab or status.sh, and credentials in the sandbox."""

    def fake_run(command, *, timeout, input_text=None):  # noqa: ANN001
        if command[0] == sys.executable:
            return real_run(command, timeout=timeout, input_text=input_text)
        return 0, ""

    real_run = backup.run_command
    monkeypatch.setattr(backup, "run_command", fake_run)
    monkeypatch.setattr(backup, "CREDENTIAL_DIR", tmp_path / "credentials")


def archive_names(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as archive:
        return archive.getnames()


def last_record(root: Path) -> dict:
    return json.loads((root / "shared/state/backup-last.json").read_text())


# -- what goes in --------------------------------------------------------------


def test_the_archive_holds_data_config_and_the_running_release_only(platform_root: Path, tmp_path: Path) -> None:
    config = make_config(platform_root, tmp_path)
    assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_OK
    stored = sorted((tmp_path / "local_backups").glob("*.tar.gz"))
    assert len(stored) == 1
    names = archive_names(stored[0])
    assert "shared/data/engagement.sqlite3" in names
    assert "shared/config/config.intranet.json" in names
    assert "shared/models/hydride/model_registry.json" in names
    assert "current" in names
    assert "releases/1.19.0/deploy/update.sh" in names
    assert "_meta/system.json" in names
    for excluded in (".venv", "shared/tmp", "shared/docs", "deployment_inbox", "releases/1.18.0"):
        assert not any(name == excluded or name.startswith(excluded + "/") for name in names), excluded


def test_a_live_wal_database_is_copied_consistently(platform_root: Path, tmp_path: Path) -> None:
    # A write committed to the WAL but not yet checkpointed into the main file
    # must be in the backup: a plain file copy would lose it.
    live = sqlite3.connect(platform_root / "shared/data/engagement.sqlite3")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("INSERT INTO visits (page) VALUES ('only-in-the-wal')")
    live.commit()
    try:
        config = make_config(platform_root, tmp_path)
        assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_OK
    finally:
        live.close()
    archive = next((tmp_path / "local_backups").glob("*.tar.gz"))
    extracted = tmp_path / "extracted"
    with tarfile.open(archive) as tar:
        tar.extract("shared/data/engagement.sqlite3", extracted, filter="data")
        assert "shared/data/engagement.sqlite3-wal" not in tar.getnames()
    copy = sqlite3.connect(extracted / "shared/data/engagement.sqlite3")
    pages = [row[0] for row in copy.execute("SELECT page FROM visits")]
    assert "only-in-the-wal" in pages
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    record = last_record(platform_root)
    assert record["databases"][0]["method"] == "sqlite-online-backup"
    assert record["databases"][0]["integrity"] == "ok"


def test_every_archive_carries_a_checksum_and_a_manifest_that_verify(platform_root: Path, tmp_path: Path) -> None:
    config = make_config(platform_root, tmp_path)
    backup.cmd_run(config, dry_run=False, trigger="test")
    archive = next((tmp_path / "local_backups").glob("*.tar.gz"))
    assert archive.name.startswith(f"mlplatform-{HOST}-") and "suite1.19.0" in archive.name
    assert Path(f"{archive}.sha256").read_text().split()[0] == backup.sha256_file(archive)
    manifest = json.loads(Path(f"{archive}.json").read_text())
    assert manifest["schema"] == backup.MANIFEST_SCHEMA
    assert manifest["suite"]["components"]["pytex"]["ref"] == "v0.13.0"
    assert backup.cmd_verify(archive) == backup.EXIT_OK


def test_a_corrupted_archive_fails_verification(platform_root: Path, tmp_path: Path) -> None:
    config = make_config(platform_root, tmp_path)
    backup.cmd_run(config, dry_run=False, trigger="test")
    archive = next((tmp_path / "local_backups").glob("*.tar.gz"))
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(bytes(data))
    assert backup.cmd_verify(archive) == backup.EXIT_FAILED


# -- unique copies and retention ----------------------------------------------


def test_an_unchanged_night_stores_nothing_new(platform_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(platform_root, tmp_path)
    backup.cmd_run(config, dry_run=False, trigger="test")
    # The next night runs in a new process, so the lock file records a new PID;
    # the backup's own bookkeeping must not make the night look different.
    (platform_root / "shared/state/backup.lock").write_text("999999\n")
    monkeypatch.setattr(backup, "utc_now", lambda: backup.dt.datetime(2030, 1, 2, tzinfo=backup.dt.UTC))
    monkeypatch.setattr(backup.os, "getpid", lambda: 999999)
    assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_OK
    assert len(list((tmp_path / "local_backups").glob("*.tar.gz"))) == 1
    assert last_record(platform_root)["destinations"][0]["outcome"] == "unchanged"


def test_only_the_newest_copies_are_kept(platform_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(platform_root, tmp_path, keep=3)
    for day in range(1, 6):
        monkeypatch.setattr(backup, "utc_now", lambda day=day: backup.dt.datetime(2030, 1, day, tzinfo=backup.dt.UTC))
        (platform_root / "shared/data/notes.txt").write_text(f"day {day}")
        assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_OK
    kept = sorted(path.name for path in (tmp_path / "local_backups").glob("*.tar.gz"))
    assert len(kept) == 3
    assert [name.split("-")[2] for name in kept] == ["20300103T000000Z", "20300104T000000Z", "20300105T000000Z"]
    # Sidecars go with their archive.
    assert len(list((tmp_path / "local_backups").glob("*.json"))) == 3


def test_retention_never_touches_another_machines_backups(platform_root: Path, tmp_path: Path) -> None:
    folder = tmp_path / "local_backups"
    folder.mkdir()
    foreign = folder / "mlplatform-otherhost-20200101T000000Z-suite1.0.0.tar.gz"
    foreign.write_bytes(b"theirs")
    config = make_config(platform_root, tmp_path, keep=1)
    backup.cmd_run(config, dry_run=False, trigger="test")
    assert foreign.exists()


# -- failing destinations ------------------------------------------------------


def test_an_unmounted_share_is_refused_and_the_local_copy_still_made(platform_root: Path, tmp_path: Path) -> None:
    empty_mountpoint = tmp_path / "mnt" / "share"
    empty_mountpoint.mkdir(parents=True)
    network = {
        "name": "network",
        "type": "path",
        "path": str(empty_mountpoint / "ml_platform"),
        "require_mountpoint": str(empty_mountpoint),
    }
    config = make_config(platform_root, tmp_path, network=network)
    assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_PARTIAL
    record = last_record(platform_root)
    outcomes = {d["name"]: d for d in record["destinations"]}
    assert outcomes["local"]["outcome"] == "stored"
    assert outcomes["network"]["outcome"] == "failed"
    assert outcomes["network"]["reason"] == "not_mounted"
    # Nothing was written into the empty mount point.
    assert not any(empty_mountpoint.iterdir())
    assert record["outcome"] == "partial"


def test_a_failing_destination_is_never_pruned(platform_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(platform_root, tmp_path, keep=2)
    for day in (1, 2):
        monkeypatch.setattr(backup, "utc_now", lambda day=day: backup.dt.datetime(2030, 1, day, tzinfo=backup.dt.UTC))
        (platform_root / "shared/data/notes.txt").write_text(f"day {day}")
        backup.cmd_run(config, dry_run=False, trigger="test")

    def refuse(self, staged, expected_sha):  # noqa: ANN001
        raise backup.Failure("disk_full", "no space", "free some")

    monkeypatch.setattr(backup.PathTarget, "put", refuse)
    monkeypatch.setattr(backup, "utc_now", lambda: backup.dt.datetime(2030, 1, 3, tzinfo=backup.dt.UTC))
    (platform_root / "shared/data/notes.txt").write_text("day 3")
    assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_FAILED
    assert len(list((tmp_path / "local_backups").glob("*.tar.gz"))) == 2


def test_a_permission_error_on_a_share_points_at_the_credentials() -> None:
    failure = backup.classify_oserror(OSError(13, "Permission denied", "/mnt/share/x"), "network")
    assert failure.reason == "permission_denied"
    assert "set-credentials network" in failure.hint


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("session setup failed: NT_STATUS_LOGON_FAILURE", "credentials_rejected"),
        ("session setup failed: NT_STATUS_PASSWORD_EXPIRED", "credentials_expired"),
        ("session setup failed: NT_STATUS_PASSWORD_MUST_CHANGE", "credentials_expired"),
        ("session setup failed: NT_STATUS_ACCOUNT_LOCKED_OUT", "account_locked"),
        ("tree connect failed: NT_STATUS_BAD_NETWORK_NAME", "share_not_found"),
        ("Connection to fs01 failed (Error NT_STATUS_HOST_UNREACHABLE)", "unreachable"),
        ("NT_STATUS_DISK_FULL writing remote file", "disk_full"),
        ("smbclient: command not found", "smbclient_missing"),
    ],
)
def test_smb_failures_are_named_for_what_fixes_them(output: str, reason: str) -> None:
    failure = backup.classify_smb(output, "network")
    assert failure.reason == reason
    if reason.startswith("credentials"):
        assert "backup.sh set-credentials network" in failure.hint


def test_a_mount_refused_with_error_13_is_a_credentials_problem() -> None:
    failure = backup.classify_mount("mount error(13): Permission denied", "network")
    assert failure.reason == "credentials_rejected"


def test_smb_without_stored_credentials_says_how_to_store_them(tmp_path: Path) -> None:
    target = backup.SmbTarget(backup.Destination(name="network", type="smb", share="//fs/share", credentials="network"))
    with pytest.raises(backup.Failure) as raised:
        target.check()
    assert raised.value.reason == "credentials_missing"
    assert "set-credentials network" in raised.value.hint


def test_an_expired_password_on_smb_is_reported_and_the_run_is_partial(
    platform_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup.store_credentials("network", "svc_backup", "old-password", "OFFICE")
    real = backup.run_command

    def smbclient_refuses(command, *, timeout, input_text=None):  # noqa: ANN001
        if command[0] == "smbclient":
            return 1, "session setup failed: NT_STATUS_PASSWORD_EXPIRED"
        return real(command, timeout=timeout, input_text=input_text)

    monkeypatch.setattr(backup, "run_command", smbclient_refuses)
    network = {"name": "network", "type": "smb", "share": "//fs01/backups", "remote_dir": "ml/meghvm", "credentials": "network"}
    config = make_config(platform_root, tmp_path, network=network)
    assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_PARTIAL
    failed = {d["name"]: d for d in last_record(platform_root)["destinations"]}["network"]
    assert failed["reason"] == "credentials_expired"
    assert "set-credentials network" in failed["hint"]


# -- credentials, records, configuration ---------------------------------------


def test_credentials_are_private_and_never_inside_the_backup(platform_root: Path, tmp_path: Path) -> None:
    path = backup.store_credentials("network", "svc_backup", "s3cret pass", "OFFICE")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert "password=s3cret pass" in path.read_text()
    meta = json.loads((path.parent / "network.meta.json").read_text())
    assert "s3cret" not in json.dumps(meta)
    assert backup.credential_age_days("network") is not None
    config = make_config(platform_root, tmp_path)
    backup.cmd_run(config, dry_run=False, trigger="test")
    archive = next((tmp_path / "local_backups").glob("*.tar.gz"))
    assert not any("credentials" in name for name in archive_names(archive))


def test_every_run_leaves_a_log_a_status_and_a_history_line(platform_root: Path, tmp_path: Path) -> None:
    config = make_config(platform_root, tmp_path)
    backup.cmd_run(config, dry_run=False, trigger="timer")
    record = last_record(platform_root)
    assert record["schema"] == backup.RUN_SCHEMA
    assert record["trigger"] == "timer"
    assert record["outcome"] == "ok"
    assert record["archive"]["sha256"]
    assert Path(record["log_path"]).is_file()
    history = (platform_root / "shared/state/backup-history.jsonl").read_text().splitlines()
    assert json.loads(history[-1])["run_id"] == record["run_id"]
    # The staging folder is emptied.
    assert not any((tmp_path / "staging").iterdir())


def test_a_second_run_waits_for_the_first(platform_root: Path, tmp_path: Path) -> None:
    config = make_config(platform_root, tmp_path)
    with backup.run_lock(config) as held:
        assert held
        assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_LOCKED


def test_a_dry_run_writes_nothing(platform_root: Path, tmp_path: Path) -> None:
    config = make_config(platform_root, tmp_path)
    assert backup.cmd_run(config, dry_run=True, trigger="test") == backup.EXIT_OK
    assert not (tmp_path / "local_backups").exists() or not any((tmp_path / "local_backups").glob("*.tar.gz"))
    assert not (platform_root / "shared/state/backup-last.json").exists()


def test_the_shipped_template_is_a_valid_configuration(tmp_path: Path) -> None:
    config = backup.load_config(REPO_ROOT / "deploy" / "backup.toml.example")
    assert config.keep == 10
    assert ".venv" in config.exclude
    assert {d.name for d in config.destinations} == {"local", "network"}
    assert config.schedule == "*-*-* 00:00:00"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[backup]\nkeep = 0\n[[destination]]\nname='a'\npath='/x'\n", "keep"),
        ("[backup]\n", "at least one"),
        ("[[destination]]\nname='a'\ntype='ftp'\npath='/x'\n", "type must be"),
        ("[[destination]]\nname='a'\ntype='smb'\nshare='fs/x'\n", "//server/share"),
        ("[[destination]]\nname='a'\npath='/x'\n[[destination]]\nname='a'\npath='/y'\n", "used twice"),
    ],
)
def test_a_bad_configuration_names_the_problem(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "backup.toml"
    path.write_text(text)
    with pytest.raises(backup.ConfigError, match=message):
        backup.load_config(path)


def test_extract_refuses_a_folder_that_is_not_empty(platform_root: Path, tmp_path: Path) -> None:
    config = make_config(platform_root, tmp_path)
    backup.cmd_run(config, dry_run=False, trigger="test")
    archive = next((tmp_path / "local_backups").glob("*.tar.gz"))
    occupied = tmp_path / "restore"
    occupied.mkdir()
    (occupied / "something").write_text("x")
    assert backup.cmd_extract(archive, occupied) == backup.EXIT_CONFIG
    fresh = tmp_path / "restore2"
    assert backup.cmd_extract(archive, fresh) == backup.EXIT_OK
    assert (fresh / "shared/config/config.intranet.json").read_text() == '{"secret_key": "s"}'
    assert os.readlink(fresh / "current") == "releases/1.19.0"


def test_an_interrupted_copy_is_cleared_and_never_counted(platform_root: Path, tmp_path: Path) -> None:
    folder = tmp_path / "local_backups"
    folder.mkdir()
    leftover = folder / f"mlplatform-{HOST}-20200101T000000Z-suite1.0.0.tar.gz.partial"
    leftover.write_bytes(b"half")
    config = make_config(platform_root, tmp_path)
    assert backup.cmd_run(config, dry_run=False, trigger="test") == backup.EXIT_OK
    assert not leftover.exists()
    assert len(list(folder.glob("*.tar.gz"))) == 1


def test_the_timer_runs_the_current_release_at_the_configured_time(
    platform_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = tmp_path / "units"
    monkeypatch.setattr(backup, "UNIT_DIR", units)
    config = make_config(platform_root, tmp_path)
    assert backup.cmd_install_timer(config) == backup.EXIT_OK
    service = (units / "ml-backup.service").read_text()
    timer = (units / "ml-backup.timer").read_text()
    # Through the `current` link, so every upgrade's backup.py is the one that runs.
    assert f"{platform_root}/current/deploy/backup.py run --trigger timer" in service
    assert "IOSchedulingClass=idle" in service
    assert "OnCalendar=*-*-* 00:00:00" in timer
    # A night missed while the server was off runs at the next boot.
    assert "Persistent=true" in timer
    # Not ml-platform-*: update.sh owns that prefix and regenerates those units.
    assert not any(path.name.startswith("ml-platform-") for path in units.iterdir())
