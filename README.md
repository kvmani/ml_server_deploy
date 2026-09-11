# ml_server_deploy

Deployment umbrella for the ML Server scientific suite.

Each application stays in its own repository and is developed independently.
This repository does one job: it pins a set of exact component commits, turns
them into a single browser-downloadable archive, and installs that archive on an
office Ubuntu server that has no GitHub access.

```
     develop each app in its own repo
                  |
        bump one `ref` in manifest.yml
                  |
        tag this repo  ->  GitHub Actions
                  |
   ml-server-suite-vX.Y.Z.tar.gz + .sha256
                  |
       download in a browser, carry across
                  |
        ./update.sh <archive>   on the server
                  |
        health verified, or rolled back
```

For the office side, read [RUNBOOK.md](RUNBOOK.md) — it ships inside every
archive and assumes no internet and no help.

## Components

| Service | Repository | Port | Health | Notes |
| --- | --- | ---: | --- | --- |
| gateway | `kvmani/ml_server` | 5000 | `/health/live` | the portal; mounts the two in-process apps |
| pytex | `kvmani/pytex` | 8765 | `/api/health` | |
| calculator | `kvmani/scientific_calculator` | 5055 | `/api/health` | |
| converter | `kvmani/unit_converter` | 5065 | `/api/health` | |
| annotator | `kvmani/OnlineAnnotator` | 5070 | `/api/health` | keeps data by design in `shared/data/online_annotator` |
| pdf_tools | `kvmani/pdf_tools` | — | `/pdf_tools/health` | in-process in the gateway |
| tabular_ml | `kvmani/tabular_ml` | — | `/tabular_ml/api/v1/health` | in-process; Vite frontend built in CI |
| hydride | `Pushpalathadevi/HydrideSegmentation` | 5005 | `/health` | different owner; needs model checkpoints supplied out of band |

## Cutting a release

1. Tag the component repository you changed.
2. Edit that component's `ref` in [manifest.yml](manifest.yml). Leave `commit`
   empty — the workflow resolves it and fails if the tag has moved.
3. Bump `suite_version`.
4. Tag this repository `vX.Y.Z` and push the tag.
5. Download the two files from the Release page and carry them across.

## Design decisions worth knowing

**Only dependencies are installed; application code is never pip-installed.**
Each service runs out of its release directory via `PYTHONPATH`. That is what
makes rollback a symlink swap and a restart — no reinstall, no network, no
package mirror at the moment things are already going wrong.

**Unit files must not vary with the suite version.** They reference paths through
the `current` symlink and record only their own component's commit. If the suite
version or a render timestamp appeared in a unit, every unit would change on
every release and all seven services would restart each time — which would
defeat the point of shipping a single-component update. The `single_component`
rehearsal scenario exists to keep this honest.

**Persistent state lives outside every release.** `shared/` holds the database,
uploads, models and configuration; releases link to it rather than containing
it. An upgrade physically cannot delete data, and rollback refuses to restore a
data backup unless explicitly asked with `--restore-data`.

**Phase A of `update.sh` changes nothing at all.** Checksum, archive structure,
path-traversal safety, platform, ports, systemd session and the deployment plan
are all evaluated before a single byte is written — the log itself goes to a
temp file until Phase B begins. So `--dry-run` is genuinely read-only, and every
refusal leaves the server byte-identical. The rehearsal asserts exactly that,
by fingerprinting the whole tree before and after each refusal scenario.

**Hydride shares the environment by explicit decision.** It brings torch, OpenCV
and transformers into the same venv as the portal. The release workflow runs
`pip check` on the combined set as a blocking gate. If that ever goes red, set
`env: isolated` on the hydride service in the manifest — one line, no redesign.

## Documentation is built here, not shipped

The workbench serves PyTex's theory notes, algorithm pages and worked examples
at `/docs`, and on an air-gapped host there is nowhere else to read them. They
are not in the archive: PyTex renders its site into its *installed package*, and
this suite runs application code from source over `PYTHONPATH` so that a
rollback stays a symlink swap needing no network. So the deployment builds them.

A component declares a `docs_build` in the manifest, and `update.sh` runs it as
its very last step — after the health checks have passed and the release has
been recorded. Three properties follow, and every one of them is deliberate:

- **Nothing waits on it.** The suite is already serving, which is what makes it
  acceptable for the build to take tens of minutes.
- **It cannot fail a deployment.** A missing Sphinx, an unreachable mirror, a
  notebook that will not run: each is a warning, and `/docs` keeps what it had.
- **It is not repeated needlessly.** The build is stamped with the component
  commit it came from, so a suite release that does not move that component
  reuses it.

The result goes to `shared/docs/<component>`, outside every release, and the
service is pointed at it by an environment variable. `deploy/build_docs.sh`
does the same build on demand, against whatever is already deployed, without
redeploying or restarting anything.

## Persistent state is seeded, not assumed

`shared_dirs` in the manifest creates empty directories. The `seeds` section
says what has to be *in* them, and where to get it: the portal's environment
file, the site config, the hydride checkpoints, the sample micrographs. Each is
seeded once, from the first source that exists, and never overwritten
afterwards — a hand-edited environment file survives every future upgrade.

A seed marked `required` that nothing on the host can supply stops the
deployment in preflight, with nothing changed, rather than after activation as
a failed health check and a rollback.

The `verify` section holds the post-deployment assertions: the catalog must not
advertise loopback, and a service's journal must not contain the lines that
mean it came up unable to do its job. Both exist because v1.4.0 passed every
check the suite had at the time and was still broken for its users.

## Development

Requires Python 3.12+ and PyYAML.

```bash
python -m pytest tests/unit -q                # manifest validation
python tools/manifest.py --validate manifest.yml
python tools/check_text_hygiene.py            # CRLF, BOM, Windows paths
python tools/check_text_hygiene.py --fix
```

### The rehearsal

Development is on Windows; deployment is Ubuntu. The gap is closed by running
the real scripts against a disposable server in WSL — real `systemd --user`
units, GitHub genuinely unreachable, and 20 scenarios covering upgrade,
single-component update, idempotency, data survival, automatic rollback, and
every refusal path.

```powershell
wsl -d Ubuntu-24.04 -- bash /mnt/c/Users/kvman/PycharmProjects/ml_server_deploy/tests/rehearsal/sync_and_run.sh
```

```bash
./tests/rehearsal/run.sh --list          # the scenarios
./tests/rehearsal/run.sh single_component
```

The harness copies the repository onto the WSL filesystem first, deliberately:
under `/mnt/c` symlinks cannot be created and permission bits are not honoured,
so the atomic `current` swap and the unit-file comparison would not behave the
way they do on the server.

Nothing outside `~/rehearsal` is touched, and the units it installs are removed
at the end of the run.
