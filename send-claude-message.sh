#!/bin/bash
# Send message to Claude agent in tmux window
# Usage: send-claude-message.sh <session:window[.pane]> <message>
#
# Thin shim over orchestrator.py - adds delivery verification and
# audit logging (registry/orchestrator.db) while keeping the
# documented interface unchanged.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -lt 2 ]; then
    echo "Usage: $0 <session:window> <message>"
    echo "Example: $0 agentic-seek:3 'Hello Claude!'"
    exit 1
fi

WINDOW="$1"
shift  # Remove first argument, rest is the message
MESSAGE="$*"

exec python3 "$SCRIPT_DIR/orchestrator.py" send "$WINDOW" "$MESSAGE"
