# Releasing the suite

A suite release can be made end to end by an agent working in a cloud session
(Claude Code on the web, Codex) on the maintainer's request, or by the
maintainer from the browser. Nobody has to create a tag or a Release by hand,
and no workflow ever starts on its own: each run below is started deliberately.

The one fact everything rests on: a cloud agent can **start** a GitHub Actions
workflow but cannot **push** a tag. So every release workflow, started by hand
on `main`, checks the version, creates the tag itself and publishes the
Release.

| Repository | Workflow | Started with | It does |
|------------|----------|--------------|---------|
| a component (pytex, ml_server, ...) | `release.yml` ("Publish release") | `version = X.Y.Z` | checks the version literal and the CHANGELOG section, tags `vX.Y.Z` on that commit, publishes the Release with the changelog's notes |
| ml_server_deploy | `release.yml` ("Build suite release") | `publish = false` | rehearsal: every gate and the full build, the archive attached to the run; nothing tagged or published |
| ml_server_deploy | `release.yml` | `publish = true` | the same build; only if every gate passes, tags `v<suite_version>` and publishes the archive and its `.sha256` |

Pushing a tag by hand still works for every repository, as before.

---

## The procedure

### 0. What is new?

```bash
python tools/component_updates.py
```

For each component: the ref the manifest pins, the newest `vX.Y.Z` tag in its
repository, and whether its default branch has commits no release includes yet.
"Commits after the newest tag" may be documentation or CI only; look at them
before deciding the component needs a release.

### 1. Release each component that changed

In the component's repository, on `main`:

1. Set the version literal (for example `src/pytex/_version.py`) and add a
   `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md`. The workflow refuses
   to publish without both.
2. Run the component's own checks, commit, push `main`.
3. Start its `release.yml` on `main` with `version = X.Y.Z`; wait for success.

A component without `release.yml` gets one from
[tools/templates/component-release.yml](tools/templates/component-release.yml):
copy it to `.github/workflows/release.yml` and set `PRODUCT` and
`VERSION_FILE`, nothing else.

### 2. Point the suite at the new versions

In this repository, on `main`:

1. Change each component's `ref:` in `manifest.yml` to its new tag, and bump
   `suite_version`. Leave `commit` empty; the build resolves it.
2. Run the local gates:

   ```bash
   python tools/manifest.py --validate manifest.yml
   python tools/check_text_hygiene.py
   python -m pytest tests/unit -q
   ```

3. Commit and push `main`.

### 3. Rehearse, then publish

1. Start `release.yml` on `main` with `publish = false`. It runs every gate,
   including the dependency gate, which is where a suite usually breaks. A
   failure here costs nothing: no tag, no Release.
2. Start it again on `main` with `publish = true`. Release
   `v<suite_version>` appears with `ml-server-suite-v<suite_version>.tar.gz`
   and its `.sha256`.
3. Give the maintainer the Release link and the checksum. Installing it is in
   [RUNBOOK.md](RUNBOOK.md), "The normal update".

### When something goes wrong

- **A version that already exists** (on another commit) is refused before
  anything is built. Bump the version.
- **A failed build** leaves nothing behind: the tag is created only after the
  last gate. Fix, push, start again.
- **A run that tagged but failed to publish** (rare: the Releases API) is
  simply started again on the same commit; the existing tag is reused.

---

## How to start a workflow

| From | Command |
|------|---------|
| Claude Code (GitHub tool) | `actions_run_trigger`, method `run_workflow`, `workflow_id: release.yml`, `ref: main`, `inputs: {"publish": "true"}` (or `{"version": "X.Y.Z"}`); then `actions_list` / `get_job_logs` to follow it |
| `gh` (Codex, a workstation) | `gh workflow run release.yml -R kvmani/ml_server_deploy -r main -f publish=true`, then `gh run watch -R kvmani/ml_server_deploy` |
| Browser | the repository's Actions tab, the workflow, "Run workflow", branch `main`, fill in the input |

---

## One-time setup, so an agent can do all of this

**Claude Code on the web.** The Claude GitHub App must be installed on every
repository involved, with read and write access to Contents and Actions. It
is for `kvmani/pytex`, `kvmani/ml_server` and `kvmani/ml_server_deploy`; add
the other components (`scientific_calculator`, `unit_converter`,
`OnlineAnnotator`, `pdf_tools`, `tabular_ml`) to the app's installation, and
to the cloud environment's repositories, before asking for a release of
them. `Pushpalathadevi/HydrideSegmentation` belongs to another account: its
owner must install the app there (or release it themselves); the suite then
just pins the tag they publish.

**Codex, or any agent with the `gh` CLI.** Give its environment a
fine-grained personal access token as `GH_TOKEN`, limited to these
repositories, with *Contents: read and write*, *Actions: read and write* and
*Metadata: read*. Replace it when it expires; nothing else stores it.

**Every repository.** Nothing to change unless an organisation policy or a
tag ruleset gets in the way: the workflows request `contents: write`
themselves, and GitHub's own bot creates the tags. If a component repository is
private, the existing `COMPONENT_READ_TOKEN` secret here lets the suite build
read it.
