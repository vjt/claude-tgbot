#!/bin/sh
# Seed memory files into Claude Code's per-project memory dir for the given consumer.
# Idempotent: only copies files that don't already exist at the destination — existing
# user-written memories are preserved. Set FORCE=1 to overwrite even existing files.
#
# Usage: install-memory.sh <CONSUMER_DIR>

set -e
CONSUMER="${1:?usage: $0 <consumer-dir>}"
case "$CONSUMER" in
  /*) ;;
  *) echo "consumer dir must be absolute: $CONSUMER" >&2; exit 2 ;;
esac

PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
SEED="$PLUGIN/memory-seed"

if [ ! -d "$SEED" ]; then
  echo "no memory-seed dir at $SEED — nothing to install" >&2
  exit 1
fi

ENCODED="$("$PLUGIN/bin/encode-project-dir.sh" "$CONSUMER")"
DEST="$HOME/.claude/projects/$ENCODED/memory"
mkdir -p "$DEST"

installed=0
preserved=0
for src in "$SEED"/*.md; do
  [ -e "$src" ] || { echo "no seed files in $SEED" >&2; exit 1; }
  name="$(basename "$src")"
  if [ -e "$DEST/$name" ] && [ "${FORCE:-0}" != "1" ]; then
    preserved=$((preserved + 1))
  else
    cp "$src" "$DEST/$name"
    installed=$((installed + 1))
    echo "  + $name"
  fi
done

echo "memory seed: $installed installed, $preserved preserved at $DEST"
