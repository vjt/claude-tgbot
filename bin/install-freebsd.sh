#!/bin/sh
# Render + install FreeBSD rc.d service scripts for claude-tgbot.
# MUST be run as root (rc.d lives in /usr/local/etc/rc.d/).
# Consumer dir = $PWD of the invoking user — captured BEFORE sudo-elevation
# via the CLAUDE_TGBOT_CONSUMER env var; if unset, falls back to $PWD.
#
# Usage (via sudo):
#   env CLAUDE_TGBOT_CONSUMER="$PWD" sudo -E sh install-freebsd.sh [--render-only] <bot|watchdog|both>...

set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (sudo). Example:" >&2
  echo "  env CLAUDE_TGBOT_CONSUMER=\"\$PWD\" sudo -E sh $0 bot watchdog" >&2
  exit 1
fi

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
CONSUMER="${CLAUDE_TGBOT_CONSUMER:-$PWD}"
VENV="${VENV:-$HOME/.venv-tgbot}"
SERVICE_USER="${SUDO_USER:-${USER:-root}}"
RC_DIR="/usr/local/etc/rc.d"

render_only=0
targets=""
for arg in "$@"; do
  case "$arg" in
    --render-only) render_only=1 ;;
    bot|watchdog) targets="$targets $arg" ;;
    both) targets="$targets bot watchdog" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

[ -n "$targets" ] || { echo "no targets (bot|watchdog|both)" >&2; exit 2; }

render() {
  svc="$1"
  case "$svc" in
    bot)
      src="$PLUGIN/rc-templates/freebsd/claude-tgbot-bot.tmpl"
      dest="$RC_DIR/claude-tgbot-bot"
      ;;
    watchdog)
      src="$PLUGIN/rc-templates/freebsd/claude-tgbot-aup-watchdog.tmpl"
      dest="$RC_DIR/claude-tgbot-aup-watchdog"
      ;;
  esac
  [ -f "$src" ] || { echo "missing template: $src" >&2; return 1; }
  tmp="$(mktemp)"
  sed -e "s|@CONSUMER@|$CONSUMER|g" \
      -e "s|@PLUGIN@|$PLUGIN|g" \
      -e "s|@VENV@|$VENV|g" \
      -e "s|@USER@|$SERVICE_USER|g" \
      "$src" > "$tmp"
  install -m 755 "$tmp" "$dest"
  rm -f "$tmp"
  echo "installed: $dest"
}

for t in $targets; do render "$t"; done

# Ensure rc.conf enables the services — idempotent append
for t in $targets; do
  var="claude_tgbot_${t}_enable"
  [ "$t" = "watchdog" ] && var="claude_tgbot_aup_watchdog_enable"
  if ! grep -q "^${var}=" /etc/rc.conf 2>/dev/null; then
    echo "${var}=\"YES\"" >> /etc/rc.conf
    echo "added to /etc/rc.conf: ${var}=YES"
  fi
done

for t in $targets; do
  svc_name="claude-tgbot-$t"
  [ "$t" = "watchdog" ] && svc_name="claude-tgbot-aup-watchdog"
  if [ "$render_only" -eq 1 ]; then
    service "$svc_name" restart || true
  else
    service "$svc_name" restart || service "$svc_name" start || true
  fi
  service "$svc_name" status 2>&1 | head -3 || true
  echo
done
