#!/bin/sh
# Encode an absolute path the way Claude Code encodes per-project jsonl dirs
# under ~/.claude/projects/: swap every `/` and `.` for `-`, prepend a `-`.
# Matches the algorithm aup_watchdog.py relies on — keep in sync if CC changes it.
#
#   /srv/www/nhaima.org  ->  -srv-www-nhaima-org
#   /home/foo/proj       ->  -home-foo-proj

set -e
dir="${1:?usage: $0 <absolute-path>}"
case "$dir" in
  /*) ;;
  *) echo "path must be absolute: $dir" >&2; exit 2 ;;
esac

# strip leading slash, swap / and . for -, prepend -
printf -- '-%s\n' "$(printf '%s' "${dir#/}" | sed 's/[/.]/-/g')"
