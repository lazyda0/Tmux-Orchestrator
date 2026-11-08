#!/bin/bash
# Dynamic scheduler with note for next check
# Usage: ./schedule_with_note.sh <minutes> "<note>" [target_window]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTE_FILE="$SCRIPT_DIR/next_check_note.txt"
PID_FILE="$SCRIPT_DIR/.scheduled.pid"

MINUTES=${1:-3}
NOTE=${2:-"Standard check-in"}
TARGET=${3:-"tmux-orc:0"}

# Verify the target window exists before scheduling
if ! tmux display-message -t "$TARGET" -p "#{pane_id}" >/dev/null 2>&1; then
    echo "Error: target window '$TARGET' does not exist." >&2
    echo "Hint: use \$(tmux display-message -p '#{session_name}:#{window_index}') for self-scheduling" >&2
    exit 1
fi

# Create a note file for the next check
{
    echo "=== Next Check Note ($(date)) ==="
    echo "Scheduled for: $MINUTES minutes"
    echo ""
    echo "$NOTE"
} > "$NOTE_FILE"

echo "Scheduling check in $MINUTES minutes with note: $NOTE"

# Calculate the exact time when the check will run
CURRENT_TIME=$(date +"%H:%M:%S")
RUN_TIME=$(date -v +"${MINUTES}"M +"%H:%M:%S" 2>/dev/null || date -d "+${MINUTES} minutes" +"%H:%M:%S" 2>/dev/null)

# Use nohup to completely detach the sleep process
SECONDS=$((MINUTES * 60))
nohup bash -c "
    sleep $SECONDS
    tmux send-keys -t '$TARGET' -l 'Time for orchestrator check! cat \"$NOTE_FILE\" && python3 \"$SCRIPT_DIR/tmux_utils.py\" snapshot'
    sleep 1
    tmux send-keys -t '$TARGET' Enter
" > /dev/null 2>&1 &

# Save the PID so the schedule can be cancelled with: kill \$(cat .scheduled.pid)
SCHEDULE_PID=$!
echo "$SCHEDULE_PID" > "$PID_FILE"

echo "Scheduled successfully - process detached (PID: $SCHEDULE_PID, saved to .scheduled.pid)"
echo "SCHEDULED TO RUN AT: $RUN_TIME (in $MINUTES minutes from $CURRENT_TIME)"
