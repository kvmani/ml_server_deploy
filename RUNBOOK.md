# ML Server suite — office runbook

This file ships inside every release archive. It assumes no internet, no GitHub,
and no help beyond what is on the machine.

Everything here is run as `kvmani`. Nothing needs `sudo` unless the deployment
uses the legacy system-wide layout, and the scripts will tell you if it does.

---

## The normal update

Two files come across from the Internet-side machine, together:

```
ml-server-suite-v1.4.0.tar.gz
ml-server-suite-v1.4.0.tar.gz.sha256
```

Both are required. The `.sha256` is what proves the transfer did not corrupt the
archive, and `update.sh` refuses to proceed without it.

```bash
cd ~/ml_platform/current/deploy
./update.sh ~/deployment_inbox/ml-server-suite-v1.4.0.tar.gz
```

That is the whole procedure. The script verifies the archive, prints what it is
about to do, installs, restarts only what changed, checks health, and rolls back
by itself if anything fails.

### Look before you leap

To see exactly what would happen without changing anything:

```bash
./update.sh ~/deployment_inbox/ml-server-suite-v1.4.0.tar.gz --dry-run
```

`--dry-run` is genuinely read-only — it does not even create a log file. It is
safe to run against the live system at any time, as often as you like.

---

## Checking the system

```bash
./status.sh          # what is deployed, from which commits, and is it up
./health_check.sh    # just the health probes; safe for cron
./status.sh --json   # same thing for scripts
```

`status.sh` is the fastest way to answer "what is actually running here?" months
later: it prints the suite version, every component's repository, tag and exact
commit, the archive checksum it came from, and the state of each service.

---

## When something is wrong

### Go back to the previous release

```bash
./rollback.sh              # to the last release that passed its health checks
./rollback.sh --list       # see what is available first
./rollback.sh --to 1.3.0   # to a specific one
```

Rollback is a symlink swap and a restart. It needs no network and no package
mirror, which is deliberate: a rollback happens exactly when other things are
already broken.

**Rollback never touches your data.** Everything under `~/ml_platform/shared` —
the engagement database, uploads, model checkpoints, configuration — lives
outside every release directory. Going back to an older release cannot revert or
delete any of it.

### Look at the logs

```bash
# What the deployment itself did
ls -t ~/ml_platform/shared/logs/ | head
less ~/ml_platform/shared/logs/update-1.4.0-*.log

# What a service is doing
journalctl --user -u ml-platform-portal.service -n 100
journalctl --user -u 'ml-platform-*' -f
```

### Restart one service by hand

```bash
systemctl --user restart ml-platform-pytex.service
systemctl --user status  ml-platform-pytex.service
```

---

## Things that will stop an update, and what they mean

| Message | What happened | What to do |
| --- | --- | --- |
| `no checksum file beside the archive` | The `.sha256` was not transferred | Copy it across too; it is next to the archive on the Release page |
| `checksum MISMATCH` | The archive is corrupt | Transfer it again. Do not override this |
| `port NNNN is in use by something that is not our service` | Something else is on a suite port | Find it with `ss -ltnp`, stop it, or use `--port-offset 100` |
| `pip check FAILED` | The release's dependencies are mutually incompatible | Nothing was activated; the old release is still serving. Report the output — the fix is on the development side |
| `dependency installation failed` | The pip mirror is unreachable, or is missing a package | Check `/etc/pip.conf`. `requirements/mirror_audit.txt` in the release lists every package the mirror must carry |
| `No matching distribution found for torch==...+cpu` | The CPU-only torch index is not configured | See "The torch index" below |
| `found a legacy installation at /opt/ml_server` | An older-style install is present | This is a migration, not a routine update. Use `--adopt-legacy` only deliberately |
| `another deployment is already running` | Two updates at once | Wait for the first to finish |
| `the user systemd session is unavailable` | Linger is off, or you are on a bare SSH session | `loginctl enable-linger kvmani`, then log out and back in |
| `health checks failed` | The new release came up but does not work | It rolled back automatically. The failed release is kept under `releases/` for diagnosis |

---

## First-time setup on a new machine

Only needed once, and only if the suite has never been installed here.

```bash
# Services must survive logout.
loginctl enable-linger kvmani

# Confirm the essentials are present.
python3 --version          # must be 3.12 or newer
cat /etc/pip.conf          # must point at the internal mirror

# Then deploy as normal.
./update.sh /path/to/ml-server-suite-vX.Y.Z.tar.gz
```

### The torch index

Hydride needs PyTorch, and the release pins it to a CPU-only build such as
`torch==2.13.0+cpu`. That exact wheel exists only on a PyTorch CPU index — it is
never on PyPI — so the install needs to be told where to find it. Point it at
the office's internal mirror:

```bash
export ML_PIP_EXTRA_INDEX_URL=http://pytorch-cpu.intranet.local/whl/cpu
./update.sh /path/to/ml-server-suite-vX.Y.Z.tar.gz
```

or per-run:

```bash
./update.sh <archive> --extra-index-url http://pytorch-cpu.intranet.local/whl/cpu
```

If your site mirror already serves the CPU torch wheels through the index in
`/etc/pip.conf`, set `ML_PIP_EXTRA_INDEX_URL=""` so nothing else is contacted.

Never let this host install the default PyTorch wheels: they bundle the CUDA
runtime, adding several gigabytes of GPU libraries to a machine with no GPU.

### Hydride model checkpoints

The trained checkpoints are **not** in the release archive and never will be —
they are not in git either. They live outside the releases, and are copied in
once:

```bash
mkdir -p ~/ml_platform/shared/models/hydride
cp /path/to/checkpoints/*.pt ~/ml_platform/shared/models/hydride/
```

Once they are there, every future upgrade and rollback leaves them alone.
`health_check.sh` warns if that directory is empty.

---

## Where everything lives

```
~/ml_platform/
├── current -> releases/1.4.0     the active release (an atomic symlink)
├── releases/                     the last few releases, kept for rollback
├── shared/                       YOUR DATA. Never touched by deploy scripts
│   ├── data/                     engagement database
│   ├── models/                   hydride checkpoints
│   ├── uploads/  config/         
│   ├── logs/                     deployment and application logs
│   └── state/history.jsonl       every deployment ever made here
├── backups/                      pre-update checkpoints (database, units, freeze)
├── .venv/                        the shared Python environment
└── deployment_inbox/             where to put transferred archives
```

Unit files are in `~/.config/systemd/user/ml-platform-*.service`. They are
generated from the manifest on every update; editing them by hand will be
silently undone by the next deployment. Change the manifest instead.
