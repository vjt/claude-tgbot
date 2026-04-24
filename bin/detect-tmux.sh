#!/bin/sh
# Exit 0 if the calling shell is inside a tmux session, 1 otherwise.
# The aup_watchdog resolves the claude pane via tmux — no tmux = no watchdog.

if [ -n "${TMUX:-}" ]; then
  sess="$(tmux display-message -p '#S' 2>/dev/null || echo '?')"
  echo "tmux detected (session=$sess)"
  exit 0
fi

echo "not inside a tmux session" >&2
exit 1
