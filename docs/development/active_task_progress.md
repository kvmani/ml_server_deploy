# Progress ledger

The resume point for all work in this repository (AGENTS.md, cardinal rule 3). Claim a task in
**Active** before coding, checkpoint in **Task log** after each meaningful step, and move the row
to **Done** with its commit when finished. Newest entries first; keep them short and factual.

## Active

| Task | Who | Started | Status | Next step |
| --- | --- | --- | --- | --- |

## Task log

<!-- Copy this block for each task:
### YYYY-MM-DD — <task>
- Goal:
- Plan (files, tests, docs):
- Done (commits):
- Tests run (command → result):
- Decisions / assumptions:
- Blockers / open questions:
- Next step:
-->

### 2026-11-23 — Agent instructions (done)
- Goal: give the repository its first AGENTS.md for every agent (Codex, Claude, Gemini) with the
  owner's cardinal rules, quality bar, test-selection table and map.
- Project rules were taken from the README design decisions, RELEASING.md and the development
  section: no pip-installed app code, version-independent units, state outside releases,
  side-effect-free Phase A, seeds, best-effort docs builds, shared hydride env with `pip check`
  gate, never pruning an in-use release, text hygiene, release order, rehearsal for `deploy/`.
- Added this ledger and `.gemini/settings.json`. CI was already manual-only; the tag-triggered
  release workflow is unchanged.
- Tests run: none needed (documentation only).

## Done

| Date | Task | Commit |
| --- | --- | --- |
| 2026-11-23 | Agent instructions | see git log |
