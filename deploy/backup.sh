#!/usr/bin/env bash
# Nightly, verified backups of the ML Server suite. See backup.py for the design
# and RUNBOOK.md, "Backups", for the procedure.
#
#   ./backup.sh init-config              write shared/config/backup.toml to edit
#   ./backup.sh test-destinations        check every destination now
#   ./backup.sh run [--dry-run]          take a backup now
#   ./backup.sh status                   the last run, and credential ages
#   ./backup.sh list                     backups held at every destination
#   ./backup.sh set-credentials NAME     store or change a share's credentials
#   ./backup.sh verify ARCHIVE           check an archive end to end
#   ./backup.sh extract ARCHIVE --to DIR verify, then unpack for a restore
#   ./backup.sh install-timer            run every night at the configured time
#   ./backup.sh remove-timer             stop the nightly runs
#
# The system python3 is used on purpose, not the suite's .venv: a backup must
# not depend on the environment it may be needed to rebuild.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "${here}/backup.py" "$@"
