# AGENTS.md — ML Server suite deployment

Deployment umbrella for the ML Server scientific suite. Each application lives and is developed in
its own repository; this repository only pins exact component commits in
[manifest.yml](manifest.yml), turns them into one browser-downloadable archive, and installs that
archive on an office Ubuntu server with no GitHub access (`deploy/update.sh`, health check, automatic
rollback). This file is the operating contract for every coding agent (Codex, Claude, Gemini, …)
and human contributor; it is the only agent-instruction file in the repository.

**Order of authority:** the owner's instruction for the task → this file → [README.md](README.md)
("Design decisions worth knowing") → [RELEASING.md](RELEASING.md) → [RUNBOOK.md](RUNBOOK.md).

## Cardinal rules (owner-mandated, every repository)

1. **Fetch before you change anything.** Start every task with
   `git fetch origin && git pull --rebase --autostash origin main`. Several people and agents push
   to this repository; never build on a stale checkout.
2. **Work on `main` only.** Commit and push directly to `main` unless the owner names another
   branch for the task. No feature branches or pull requests by default. Fetch again before
   pushing; if the push is rejected, `git pull --rebase origin main`, rerun the affected tests,
   then push. Never force-push or rewrite published history.
3. **Keep the progress ledger current:** [`docs/PROGRESS.md`](docs/PROGRESS.md). Before coding, add (or claim)
   the task entry: goal, plan, who is working on it, date. After each meaningful step record what
   was done, the commit, test evidence, decisions, blockers and the next step. Commit the ledger
   with the work it describes. A new session must be able to resume from the ledger and git
   history alone, without the chat that started the work.
4. **Targeted tests for ordinary changes.** Run only the tests that cover what you touched (see
   [Tests](#tests)). Run the full suite only when preparing a release or when the owner asks.
5. **CI runs only on request.** GitHub Actions workflows start by `workflow_dispatch` only;
   tag-triggered release workflows are the single exception. Never add push, pull-request or
   schedule triggers, and never start a workflow unless the owner asks.
6. **Be honest.** Never report a check as passed unless you ran it and saw it pass; record skipped
   checks and the reason. Never weaken, skip or delete a test to make it pass.
7. **Keep git clean.** Commit sources and canonical assets only: no secrets, real personal data,
   virtual environments, caches, logs or build output.

The cardinal rules decide *how work is done* (sync, branch, ledger, test scope, CI). The project
rules in this file decide *what this repository must achieve and protect*; nothing generic here
relaxes them. If a project rule and a cardinal rule genuinely conflict, stop and ask the owner.

## Project rules

These protect a production install that office staff run alone, offline.

- **Application code is never pip-installed.** Only dependencies are installed; each service runs
  from its release directory via `PYTHONPATH`, so rollback stays a symlink swap and a restart.
- **Unit files never vary with the suite version.** They reference the `current` symlink and record
  only their own component's commit; otherwise every release restarts every service. The
  `single_component` rehearsal scenario guards this.
- **Persistent state lives outside every release** in `shared/` (database, uploads, models,
  configuration). An upgrade must never be able to delete data; rollback restores data only with
  `--restore-data`.
- **Phase A of `update.sh` changes nothing.** All checks (checksum, archive structure,
  path-traversal safety, platform, ports, systemd session, plan) run before a byte is written;
  `--dry-run` is read-only and every refusal leaves the server byte-identical.
- **Seeds are seeded once, never overwritten;** a `required` seed nothing can supply stops the
  deployment in preflight. `verify` assertions (no loopback in the catalog, no known-bad journal
  lines) stay in place.
- **Documentation builds are best-effort and last:** `docs_build` runs after health checks pass,
  can never fail a deployment, and is skipped when the component commit is unchanged.
- **Hydride shares the portal environment** by explicit decision; `pip check` on the combined set
  is a blocking release gate. If it goes red, set `env: isolated` on that service.
- **Never prune a release a running service still uses.**
- **Text hygiene:** LF line endings, no BOM, no Windows paths in shipped files
  (`tools/check_text_hygiene.py`). The office side reads RUNBOOK.md with no internet and no help —
  keep it plain, complete and current whenever behaviour changes.
- **Releases:** component releases first, then bump `ref`s and `suite_version` in the manifest
  (leave `commit` empty), then this repository's `release.yml` by hand — rehearsal
  (`publish = false`) before `publish = true`. See RELEASING.md.

## Quality bar

- Shell scripts: `set -euo pipefail`, quoted variables, shared helpers from `deploy/lib/common.sh`,
  clear messages that tell the operator what to do next.
- Python tools: type hints, docstrings, explicit errors; the smallest change that fully solves the
  task.
- Every behaviour change ships with a unit test or a rehearsal scenario in the same commit.
- Operator-visible changes update RUNBOOK.md (and README/RELEASING.md where relevant) in the same
  commit.

## How to work

1. Sync (cardinal rule 1) and read the ledger.
2. Read the README design decision that governs the area, then the script and its tests.
3. Plan files, tests and docs; note them in the ledger. Implement in small verified steps,
   committing, pushing and updating the ledger after each.

## Tests

Requires Python 3.12+ and PyYAML.

```bash
python -m pytest tests/unit/test_<area>.py -q    # targeted (ordinary changes)
python tools/manifest.py --validate manifest.yml # any manifest change
python tools/check_text_hygiene.py               # any shipped text file (--fix to repair)
python -m pytest tests/unit -q                   # full unit suite (release or on request)
```

| You changed | Run |
| --- | --- |
| `manifest.yml`, `tools/manifest.py` | `--validate` above + `tests/unit/test_manifest.py` |
| `requirements/constraints.txt`, `tools/dependency_gate.py` | `tests/unit/test_mirror_constraints.py` |
| `tools/build_suite.py` (archive contents) | `tests/unit/test_packaging_exclusions.py tests/unit/test_packaging_portability.py` |
| release pruning or the in-use check | `tests/unit/test_release_in_use.py` |
| `.github/workflows/release.yml` | `tests/unit/test_release_workflow.py` |
| `deploy/backup.*` | `tests/unit/test_backup.py` |
| anything in `deploy/` or `tests/rehearsal/` | the affected rehearsal scenario: `./tests/rehearsal/run.sh <scenario>` (`--list` shows them) |

The rehearsal runs the real scripts against a disposable server in WSL/Ubuntu (real
`systemd --user` units, GitHub unreachable). It copies the repository onto the Linux filesystem on
purpose: under `/mnt/c` symlinks and permission bits do not behave as on the server. Run all
scenarios before a suite release that includes `deploy/` changes. CI never runs the rehearsal.

## Definition of done

- [ ] Targeted tests (and rehearsal scenarios for `deploy/`) pass and were run.
- [ ] Manifest validates; text hygiene clean.
- [ ] RUNBOOK.md / RELEASING.md / README updated for operator-visible changes.
- [ ] Ledger updated; committed and pushed to `main`.

## When to stop and ask

Stop and record the question in the ledger when a change could touch data in `shared/`, alters
what a rollback restores, changes a component's port or health path, or needs network access on
the office server.

## Map

| Need | Read |
| --- | --- |
| Components, ports, health paths, design decisions | [README.md](README.md) |
| Cutting a release (components, then the suite) | [RELEASING.md](RELEASING.md) |
| Office operation, recovery, first-time setup | [RUNBOOK.md](RUNBOOK.md) |
| What is pinned and seeded | [manifest.yml](manifest.yml) |
| Office host inventory reference | [docs/office_inventory_reference.txt](docs/office_inventory_reference.txt) |
| Current work and history | [docs/PROGRESS.md](docs/PROGRESS.md) |
