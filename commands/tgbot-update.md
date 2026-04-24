---
description: Update an installed claude-tgbot consumer — pull latest bridge code and restart services.
---

# /tgbot-update

Refresh the bridge code for the current consumer and restart the running services. Assumes the consumer was installed via `/tgbot-install` at `$PWD`.

```
PLUGIN=~/.claude/plugins/claude-tgbot
```

## Steps

### 1. Verify consumer shape

```bash
test -f .env && test -f bot.pid || { echo "no consumer here (.env or bot.pid missing). Run /tgbot-install first."; exit 1; }
```

Abort if either is missing.

### 2. Pull the plugin

```bash
cd "$PLUGIN/.." && git pull --ff-only 2>&1 | tail -20 && cd -
```

If the pull fails (non-fast-forward, merge conflict in user-local changes), **stop** — surface the error to the user and let them resolve.

### 3. Re-render service units if templates changed

```bash
bash "$PLUGIN/bin/detect-platform.sh"
```

Then:

- **systemd:** `bash "$PLUGIN/bin/install-systemd.sh" --render-only bot watchdog`
- **freebsd:** `env CLAUDE_TGBOT_CONSUMER="$PWD" sudo -E sh "$PLUGIN/bin/install-freebsd.sh" --render-only bot watchdog`

The `--render-only` flag writes the unit files but doesn't enable/start (already enabled). If rendering produces a diff, the install script restarts the affected service; otherwise it skips.

### 4. Memory seed refresh

```bash
bash "$PLUGIN/bin/install-memory.sh" "$PWD"
```

Idempotent — adds any new seed files the plugin update introduced, never overwrites existing memories.

### 5. Restart services

```bash
# systemd:
systemctl --user restart claude-tgbot-bot claude-tgbot-aup-watchdog
# freebsd:
sudo service claude-tgbot-bot restart
sudo service claude-tgbot-aup-watchdog restart
```

### 6. Verify

```bash
ps -p "$(cat bot.pid)" | tail -1
ps -p "$(cat aup_watchdog.pid)" | tail -1
tail -5 bot.stdout.log
```

Both alive, no fresh errors in the log → done.
