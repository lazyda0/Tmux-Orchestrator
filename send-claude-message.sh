#!/bin/bash
# Send message to Claude agent in tmux window
# Usage: send-claude-message.sh <session:window[.pane]> <message>

if [ $# -lt 2 ]; then
    echo "Usage: $0 <session:window> <message>"
    echo "Example: $0 agentic-seek:3 'Hello Claude!'"
    exit 1
fi

WINDOW="$1"
shift  # Remove first argument, rest is the message
MESSAGE="$*"

# Verify the target exists before sending
if ! tmux display-message -t "$WINDOW" -p "#{pane_id}" >/dev/null 2>&1; then
    echo "Error: target '$WINDOW' does not exist." >&2
    exit 1
fi

# Send the message literally (-l prevents tmux interpreting words like
# "Enter" or "C-c" inside the message text as key names)
tmux send-keys -t "$WINDOW" -l "$MESSAGE"

# Wait 0.5 seconds for UI to register
sleep 0.5

# Send Enter to submit
tmux send-keys -t "$WINDOW" Enter

echo "Message sent to $WINDOW: $MESSAGE"
