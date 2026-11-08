#!/bin/bash
# Dynamic scheduler with note for next check
# Usage: ./schedule_with_note.sh <minutes> "<note>" [target_window]
#
# Thin shim over orchestrator.py - schedules are persisted in
# registry/orchestrator.db, survive process loss via 'recover',
# and duplicates per target are auto-cancelled.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/orchestrator.py" schedule "$@"
