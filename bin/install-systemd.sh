#!/bin/bash
# Render + install user-systemd service units for claude-tgbot.
# Consumer dir = $PWD. Templates live in $PLUGIN/rc-templates/systemd/.
# Expects systemd user-lingering already enabled (plugin's /tgbot-install
# preflight verifies that).
#
# Usage:
#   install-systemd.sh [--render-only] <bot|watchdog|both> [bot|watchdog|both]...
#
#   --render-only   write unit files + daemon-reload, don't enable/start.
#                   Used by /tgbot-update to refresh after a plugin pull
#                   (enable was already done at first install).

set -euo pipefail

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMER="$PWD"
VENV="${VENV:-$HOME/.venv-tgbot}"
UNIT_DIR="$HOME/.config/systemd/user"

render_only=0
targets=()
for arg in "$@"; do
  case "$arg" in
    --render-only) render_only=1 ;;
    bot|watchdog) targets+=("$arg") ;;
    both) targets+=(bot watchdog) ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

[ "${#targets[@]}" -gt 0 ] || { echo "no targets (bot|watchdog|both)" >&2; exit 2; }

mkdir -p "$UNIT_DIR"

render() {
  local svc="$1" src dest
  case "$svc" in
    bot)
      src="$PLUGIN/rc-templates/systemd/bot.service.tmpl"
      dest="$UNIT_DIR/claude-tgbot-bot.service"
      ;;
    watchdog)
      src="$PLUGIN/rc-templates/systemd/aup-watchdog.service.tmpl"
      dest="$UNIT_DIR/claude-tgbot-aup-watchdog.service"
      ;;
  esac
  [ -f "$src" ] || { echo "missing template: $src" >&2; return 1; }
  sed -e "s|@CONSUMER@|$CONSUMER|g" \
      -e "s|@PLUGIN@|$PLUGIN|g" \
      -e "s|@VENV@|$VENV|g" \
      -e "s|@USER@|$USER|g" \
      "$src" > "$dest"
  echo "rendered: $dest"
}

for t in "${targets[@]}"; do render "$t"; done

systemctl --user daemon-reload

unit_of() {
  case "$1" in
    bot) echo claude-tgbot-bot.service ;;
    watchdog) echo claude-tgbot-aup-watchdog.service ;;
  esac
}

if [ "$render_only" -eq 1 ]; then
  for t in "${targets[@]}"; do
    u="$(unit_of "$t")"
    systemctl --user try-restart "$u" || true
    echo "try-restarted: $u"
  done
  exit 0
fi

for t in "${targets[@]}"; do
  u="$(unit_of "$t")"
  systemctl --user enable "$u"
  systemctl --user restart "$u"
done

# Short status dump for feedback
sleep 1
for t in "${targets[@]}"; do
  u="$(unit_of "$t")"
  systemctl --user status "$u" --no-pager -n 3 || true
  echo
done
