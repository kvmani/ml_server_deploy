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
| `refusing to deploy without required persistent state` | Something the suite needs is not on this host and cannot be generated — normally the hydride checkpoints | Nothing was changed. The message lists every path it looked in; put the data at one of them and re-run. See "Persistent state" below |
| `advertises 127.0.0.1 instead of ...` | The portal is serving links nobody else can follow | The environment file was missing or not loaded. Check `~/ml_platform/shared/config/ml-platform.env` and re-run the update |
| `journal reports: Warm load failed` | Hydride is running but resolved no model | `shared/models/hydride` is empty or has no `model_registry.json`. Seed it and restart |

---

## Prerequisites: let the script tell you

Do not try to work out in advance what this host needs. Run the update and let
preflight tell you — it checks everything and reports **all** of it at once,
before changing anything:

```
This host is missing 2 prerequisite(s). NOTHING HAS BEEN CHANGED.

  * sqlite3 (apt: sqlite3) -- Taking a consistent backup of the engagement
    database before an upgrade. Without it the backup falls back to cp, which
    can copy a torn file from a live write-ahead log...
  * pdftoppm (apt: poppler-utils) -- PDF page previews in the PDF Tools
    workbench; pdf2image shells out to this binary...

Install the system packages from your internal apt mirror:
    sudo apt-get install -y sqlite3 poppler-utils

Then run this same command again.
```

Install what it names, from your internal mirrors, and re-run the same command.
Nothing was changed in the meantime, so there is no half-finished state to
clean up and no need to start over.

Two kinds of prerequisite are reported this way, and neither is ever installed
for you:

- **System packages** — they need root and come from your apt mirror.
- **Hand-installed python packages** (`torch`, `torchvision`) — see below.

## First-time setup on a new machine

Only needed once, and only if the suite has never been installed here.

```bash
# Services must survive logout.
loginctl enable-linger kvmani

# Then just run the update; it will tell you what else is missing.
./update.sh /path/to/ml-server-suite-vX.Y.Z.tar.gz
```

### PyTorch is never installed or upgraded by this script

`torch` and `torchvision` are expected to be already present in the deployment
environment, installed once by hand, offline. The update **verifies** them and
otherwise leaves them completely alone — it will not install, upgrade or
downgrade them, and it will not try to download them.

This matters because the release pins an exact version. pip treats
`2.13.0+cpu` and `2.4.0+cpu` as different requirements, so without this rule an
update would try to fetch one specific build and fail on an air-gapped host even
though a perfectly good torch was already installed.

If they are present, the update simply reports them and moves on:

```
[OK] pre-installed torch 2.13.0+cpu found in the environment
     holding back torch==2.13.0+cpu (already installed on this host)
```

If the version differs from the one the release was tested against, you get a
warning and the installed one is kept. That is your call to make, not the
release's.

If they are missing, the update stops in preflight, before changing anything,
and tells you exactly where to put them:

```
ERROR required package(s) are not installed in the deployment environment:
ERROR     torch
ERROR environment: /home/kvmani/ml_platform/.venv
ERROR Install them offline into that environment, for example:
ERROR     /home/kvmani/ml_platform/.venv/bin/python -m pip install \
ERROR         --no-index --find-links /path/to/wheels torch torchvision
```

Install them and re-run. Nothing was changed in the meantime.

Never let this host install the default PyTorch wheels: they bundle the CUDA
runtime, adding several gigabytes of GPU libraries to a machine with no GPU.

To change which packages are treated this way, edit `pip.preinstalled` in the
manifest and cut a new suite release.

### Hydride model checkpoints

The trained checkpoints are **not** in the release archive and never will be —
they are not in git either. They live in `shared/models/hydride`, outside every
release, and the update seeds them for you on the first deployment: it copies
them from the pre-suite install at `/opt/microseg/HydrideSegmentation/frozen_checkpoints`,
reading it, never moving it.

If there is nowhere to copy them from, the update **refuses in preflight**,
before anything has changed, and prints every path it looked in. Put them at
any one of those paths, or straight into the target:

```bash
mkdir -p ~/ml_platform/shared/models/hydride
cp -a /path/to/frozen_checkpoints/. ~/ml_platform/shared/models/hydride/
```

The directory must contain `model_registry.json` — an empty directory is
treated as missing, because that is precisely what the service cannot work
with. To deploy anyway, knowing hydride will not resolve a model, re-run with
`--allow-missing-seeds`.

Once they are there, every future upgrade and rollback leaves them alone.

### Sample micrographs

`shared/data/test_library` is the image library the segmentation UI offers for
a trial run. Nothing can invent these, so an empty one is only a warning —
the service falls back to two built-in examples and says so in its journal.
Drop a set of images in once and every future release picks them up:

```bash
mkdir -p ~/ml_platform/shared/data/test_library
cp /path/to/samples/*.tif ~/ml_platform/shared/data/test_library/
systemctl --user restart ml-platform-hydride.service
```

---

## Persistent state, and the address the portal advertises

`shared/` holds everything that is yours rather than the release's, and no
deployment ever overwrites what is already in it. What the update *will* do is
put something there when it is missing — once — so that a fresh host comes up
working instead of coming up empty:

| What | Where | If it is missing |
| --- | --- | --- |
| Portal environment file | `shared/config/ml-platform.env` | Copied from the pre-suite `~/ml_platform/config/ml-platform.env`, or generated from the manifest |
| Site configuration | `shared/config/config.intranet.json` | Copied from the release's template if it ships one; otherwise a warning and the portal's own defaults |
| Hydride checkpoints | `shared/models/hydride` | Adopted from `/opt/microseg/…/frozen_checkpoints`; **refuses to deploy** if there is nowhere to take them from |
| Sample micrographs | `shared/data/test_library` | A warning only |

### The environment file

The portal renders the links everyone else clicks, and it reads them from
`shared/config/ml-platform.env`, which systemd loads through `EnvironmentFile=`
in `ml-platform-portal.service`. Without it the portal falls back to
`127.0.0.1` — links that work perfectly from the server and from nowhere else.

The file is generated on the first deployment from the address on this host's
default route:

```
HYDRIDE_SEGMENTATION_URL=http://10.20.30.40:5005
PYTEX_URL=http://10.20.30.40:8765
SCIENTIFIC_CALCULATOR_URL=http://10.20.30.40:5055
UNIT_CONVERTER_URL=http://10.20.30.40:5065
```

Edit it freely. **Nothing overwrites a value that is already there** — not an
upgrade, not a rollback. A later release that adds a service appends the one
new variable and touches nothing else. Restart the portal after editing:

```bash
systemctl --user restart ml-platform-portal.service
```

If this host has more than one interface and the update picks the wrong
address, name the right one:

```bash
./update.sh <archive> --intranet-host 10.20.30.40
```

Or set it permanently in `manifest.yml` under `runtime.intranet_host` and cut a
release.

### The administrator console (new in suite 1.6.0)

The portal gained a password-protected operations console at `/admin/`, reached
from the "Administrator sign in" link in the footer of every page. It shows who
is using what right now, how long each operation takes, how many different
machines used the platform this month, and a filterable view of the log — the
questions support actually asks during an incident.

**It is off until you give it a password, and off is safe.** No credential is in
the release archive, in the manifest or in git, which is where a password must
never be. With nothing configured the console refuses every login rather than
falling open, so a deployment that skips this section is not exposed; it simply
has no console.

To turn it on, generate a hash on the server. The portal ships a command for it,
which prompts twice without echoing, so the password never reaches your shell
history or `ps`:

```bash
cd ~/ml_platform/current/apps/ml_server
PYTHONPATH=src ~/ml_platform/.venv/bin/python -m ml_server.cli --hash-admin-password
```

(The portal's own code runs from source over `PYTHONPATH` rather than being
installed into the virtual environment -- that is what makes a rollback a
symlink swap -- so the module is invoked directly rather than through a
console script.)

It prints one `ML_SERVER_ADMIN_PASSWORD_HASH=...` line. Append that line to
`shared/config/ml-platform.env`, then restart the portal:

```bash
systemctl --user restart ml-platform-portal.service
```

That file is seeded once and **nothing overwrites a value already in it**, so
this is done once and not again at every release. The portal rejects placeholder
values such as `changeme`, `admin` and `password`, so a half-finished
configuration fails closed rather than leaving the console open.

Full detail is in `docs/ADMIN_DASHBOARD.md` inside the deployed portal source.

### What is checked after a deployment

`health_check.sh` no longer only asks whether the services are up — v1.4.0 was
entirely up and still broken. It also asserts that:

- every unit that declares an environment file really loads it, and that the
  file exists;
- `/api/catalog` advertises the intranet address and **no** loopback address;
- hydride's journal for its current run shows the model preload finishing, with
  no warm-load failure;
- every seeded item is really populated, and the checkpoints resolve through
  the release's `frozen_checkpoints` link.

Run it any time; it changes nothing:

```bash
~/ml_platform/current/deploy/health_check.sh
```

---

## The workbench's own documentation

`http://<server>:8765/docs/` serves PyTex's theory notes, algorithm pages and
worked examples. On an air-gapped host there is nowhere else to read them, so
they are **built here, by the deployment**, rather than shipped.

They are not in the release archive on purpose: the built site is around 55 MB
of generated HTML, and PyTex renders it into its *installed package* -- which
this suite never creates, because application code is run from source over
`PYTHONPATH` so that a rollback stays a symlink swap needing no network.

### What the deployment does

The build is the **last** step of `update.sh`, after the health checks have
passed and the release has been recorded. By then the suite is already serving,
so nothing waits on it -- which matters, because the site executes thirty-four
notebooks and takes tens of minutes.

It cannot fail a deployment. A missing Sphinx, an unreachable mirror, a notebook
that will not run: each is a warning, and `/docs` keeps whatever it had.

The result goes to `shared/docs/pytex`, outside every release, so it survives
upgrades, rollbacks and pruning. It is stamped with the component commit it was
built from, so a suite release that does not move PyTex reuses it instead of
spending the time again.

### What the mirror needs

Only PyTex's `docs` extra: `sphinx`, `furo`, `myst-nb`, `myst-parser`,
`sphinx-design`, `sphinx-copybutton`, `sphinxcontrib-bibtex`. All are
pure-Python wheels on PyPI. Every scientific package the notebooks import is
already a required PyTex dependency and is therefore already installed.

### Doing it separately

To get the suite back quickly and build the documentation later:

```bash
./deploy/update.sh <archive> --skip-docs
```

To build against whatever is already deployed -- after installing Sphinx, say,
or when the build failed during a rollout:

```bash
./deploy/build_docs.sh                 # every component that declares one
./deploy/build_docs.sh pytex           # just this one
./deploy/build_docs.sh --force pytex   # rebuild even if the stamp matches
```

That script restarts nothing and changes no release. The workbench picks up the
new directory on the next request.

### If /docs still answers 404

Look in `shared/logs/update-*.log` or `shared/logs/build-docs-*.log` for the
build. The usual cause is that the office mirror does not carry the `docs`
extra, in which case the log names the packages to ask IT for.

---

## Where everything lives

```
~/ml_platform/
├── current -> releases/1.4.0     the active release (an atomic symlink)
├── releases/                     the last few releases, kept for rollback
├── shared/                       YOUR DATA. Never touched by deploy scripts
│   ├── data/                     engagement database, test_library/
│   ├── models/hydride/           checkpoints and model_registry.json
│   ├── uploads/
│   ├── config/                   ml-platform.env, config.intranet.json
│   ├── logs/                     deployment and application logs
│   └── state/history.jsonl       every deployment ever made here
├── backups/                      pre-update checkpoints (database, units, freeze)
├── .venv/                        the shared Python environment
└── deployment_inbox/             where to put transferred archives
```

Unit files are in `~/.config/systemd/user/ml-platform-*.service`. They are
generated from the manifest on every update; editing them by hand will be
silently undone by the next deployment. Change the manifest instead.
