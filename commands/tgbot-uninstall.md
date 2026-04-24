---
description: Remove a claude-tgbot consumer's services, hook, and skills. Preserves .env and memory unless the user opts to wipe.
---

# /tgbot-uninstall

Tear down a claude-tgbot install for the current consumer at `$PWD`. Leaves data (`.env`, attachments in `inbox/`, memory seed, logs) intact by default — the user can wipe explicitly.

```
PLUGIN=~/.claude/plugins/claude-tgbot
```

## Steps

### 1. Confirm

Use `AskUserQuestion`:

- "Uninstall claude-tgbot from `$PWD`? This stops services, removes the hook and skills, and disables the service units."
- Options: `yes, proceed` / `no, abort`

Abort on no.

### 2. Stop services

**systemd:**

```bash
systemctl --user stop claude-tgbot-bot claude-tgbot-aup-watchdog 2>/dev/null
systemctl --user disable claude-tgbot-bot claude-tgbot-aup-watchdog 2>/dev/null
rm -f ~/.config/systemd/user/claude-tgbot-{bot,aup-watchdog}.service
systemctl --user daemon-reload
```

**freebsd:**

```bash
sudo service claude-tgbot-bot stop 2>/dev/null
sudo service claude-tgbot-aup-watchdog stop 2>/dev/null
sudo rm -f /usr/local/etc/rc.d/claude-tgbot-bot /usr/local/etc/rc.d/claude-tgbot-aup-watchdog
```

Also remove the `*_enable="YES"` lines from `/etc/rc.conf` (prompt user to approve the sed via Bash).

### 3. Remove consumer .claude/ bits

```bash
rm -f .claude/hooks/gate-permission.py .claude/skills/start/SKILL.md .claude/skills/close/SKILL.md
rmdir .claude/skills/start .claude/skills/close 2>/dev/null
rmdir .claude/hooks .claude/skills 2>/dev/null
```

Leaves `.claude/settings.json` — the user may have customized it. Suggest they review/remove manually.

### 4. Ask about data wipe

Use `AskUserQuestion`:

- "Also wipe `.env`, `bot.send`, `bot.log`, `bot.stdout.log`, `bot.pid`, `aup_watchdog.pid`, `aup_watchdog.log`, and `inbox/`?"
- Options: `yes, wipe` / `no, keep`

If yes:

```bash
rm -f .env bot.send bot.log bot.stdout.log bot.pid aup_watchdog.pid aup_watchdog.log scrub_prompt.txt
rm -rf inbox/
```

### 5. Ask about memory wipe

Use `AskUserQuestion`:

- "Also wipe the seeded memory at `~/.claude/projects/<encoded-cwd>/memory/`? User-written memories will be lost."
- Options: `yes, wipe seed files only` / `yes, wipe entire memory dir` / `no, keep`

Behavior:
- wipe seed files only → delete files the plugin originally installed (consult `$PLUGIN/memory-seed/` for the list), leave anything else
- wipe entire memory dir → `rm -rf` the whole dir
- keep → no-op

### 6. venv

Don't touch `~/.venv-tgbot` — other consumers on this host may share it. Tell the user they can `rm -rf ~/.venv-tgbot` manually if no other consumer uses it.

### 7. Done

Tell the user:

> Uninstalled. Services stopped, units removed, hook unlinked.
> Kept (unless you opted to wipe): `.env`, `bot.log*`, `scrub_prompt.txt`, `inbox/`, memory.
> Venv at `~/.venv-tgbot` left in place — remove manually if no other consumer uses it.
