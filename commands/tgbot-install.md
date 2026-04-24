---
description: Interactive two-phase installer for claude-tgbot. Sets up a Telegram bridge + watchdog + memory seed for the current Claude Code session.
---

# /tgbot-install

Install the claude-tgbot bridge so this session can be driven from Telegram.

The install is **two-phase**: phase A sets up a token-only bot so the user can DM it and learn their user_id / chat_id; phase B takes those ids, finalizes the `.env`, installs the permission hook and watchdog, and seeds the session's memory. Between phases, instruct the user clearly and wait for their input.

Consumer dir is **`$PWD`** (the cwd where `claude` was launched). Don't `cd` elsewhere during install — paths are consumer-relative.

The plugin ships bundled helper scripts, templates, and memory-seed files. Plugin dir on disk:

```
PLUGIN=~/.claude/plugins/claude-tgbot
```

If that path doesn't exist (plugin layout changed), locate it with `find ~/.claude/plugins -maxdepth 3 -name plugin.json -path '*claude-tgbot*'` and proceed.

---

## Phase 0 — hard preflight

Any failure here = **stop**. Tell the user exactly what went wrong and how to fix.

### 0.1 tmux required

The aup_watchdog resolves the claude pane via tmux. No tmux session → watchdog can't work.

```bash
bash "$PLUGIN/bin/detect-tmux.sh"
```

Exits non-zero if not inside a tmux session. If it fails, tell the user: "claude-tgbot requires running this Claude session inside tmux. Exit claude, `tmux new -s <name>`, run `claude` inside, then retry `/tgbot-install`."

### 0.2 Existing install detection

```bash
ls -la .env bot.pid bot.send aup_watchdog.pid 2>&1 | grep -v 'No such'
```

If any of those exist → this consumer may already be installed. Ask the user via `AskUserQuestion` whether to `[a] abort, [b] update existing .env, [c] reinstall from scratch`. Respect their choice:
- abort → exit
- update → skip to phase B with remaining IDs
- reinstall → continue but warn services will be stopped and `.env` overwritten

### 0.3 Platform detection

```bash
bash "$PLUGIN/bin/detect-platform.sh"
```

Outputs one of `systemd`, `freebsd`, `unsupported`. If unsupported, abort with clear message.

### 0.4 Platform-specific precondition

**systemd (Linux):** check that user-lingering is enabled (required so the bot+watchdog survive the user logging out):

```bash
loginctl show-user "$USER" --property=Linger
```

If output is `Linger=no`, **stop**. Print:

```
Linger is disabled — user services would die on logout.

Run this once (requires sudo), then retry /tgbot-install:

    sudo loginctl enable-linger $USER

Or authorize me to run it via a Bash tool prompt now.
```

Use `AskUserQuestion` to offer running `sudo loginctl enable-linger $USER`. If the user declines, exit.

**freebsd:** rc.d lives at `/usr/local/etc/rc.d/`, requires root to install. Check with:

```bash
test -w /usr/local/etc/rc.d/ && echo writable || echo needs-sudo
```

If `needs-sudo`, tell the user the rc.d install step requires root and offer to run `bash "$PLUGIN/bin/install-freebsd.sh"` via sudo at the right moment (step 3 below). Either way, proceed.

---

## Phase A — token-only bootstrap

### 1. BotFather walkthrough

Tell the user (verbatim, tailor language to the session locale):

> **Create the bot:**
> 1. Open Telegram, DM `@BotFather`.
> 2. Send `/newbot` — pick a name (display) and username (must end in `bot`).
> 3. BotFather replies with a token like `12345:AAH...`. Copy it.
> 4. Send `/setprivacy` → pick your bot → `Disable`. This is **required** for group messages to reach us; without it, the bot only sees messages that @mention it.
> 5. Paste the token here when prompted.

Then use `AskUserQuestion`:
- question: "Paste the bot token from BotFather"
- multiline: false
- sensitive: true

Store the answer as `$TOKEN`. Never echo it in subsequent output — redact to `<token>` in any instructional text.

### 2. venv + dependencies

```bash
python3 -m venv ~/.venv-tgbot
~/.venv-tgbot/bin/pip install --quiet --upgrade pip
~/.venv-tgbot/bin/pip install --quiet 'python-telegram-bot>=21'
```

Verify:

```bash
~/.venv-tgbot/bin/python -c 'import telegram; print(telegram.__version__)'
```

### 3. Write .env with token only

```bash
umask 077
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
TELEGRAM_BOT_USERNAME=<pending>
TELEGRAM_ADMIN_USER_IDS=
TELEGRAM_PUBLISHER_USER_IDS=
TELEGRAM_ALLOWED_CHAT_IDS=
EOF
```

Empty allowlists = deny-all for everything except `/start` (the bot's identity-bootstrap handler). Safe default.

### 4. Install + start the bot service only (not watchdog yet — it needs pane activity to track)

**systemd:**

```bash
bash "$PLUGIN/bin/install-systemd.sh" bot
```

**freebsd** (requires sudo, prompt user to approve Bash):

```bash
env CLAUDE_TGBOT_CONSUMER="$PWD" sudo -E sh "$PLUGIN/bin/install-freebsd.sh" bot
```

The script templates the service file with `$USER`, `$PWD`, `$PLUGIN/../bot.py` (or the bundled bot.py path), enables the unit, and starts it. Confirm:

```bash
cat bot.pid && ps -p "$(cat bot.pid)" | tail -1
```

### 5. Hand off to the user for identity discovery

Tell the user:

> **Bot is running. Now:**
> 1. Open Telegram, DM your new bot directly. Send `/start`. The bot will reply with `your user_id is N, chat_id is M`. **Note both numbers — this is your admin identity.**
> 2. Add the bot to the group you want it to operate in. In that group, send `/start` — the bot replies again with the group's `chat_id` (a negative number). **Note that number.**
> 3. (Optional) If a second person will drive the bot as a publisher (non-admin), have them DM `/start` too and note their user_id.
> 4. When you have all ids, tell me to continue.

Wait for the user's signal before proceeding. Don't poll; just stop and wait for their next message.

---

## Phase B — finalize

### 6. Collect ids

Use `AskUserQuestion` (sequentially):

1. "Admin user_id (the primary operator — you):"
2. "Publisher user_id (second authorized user, or leave blank):"
3. "Chat_id (group or DM the bot operates in — usually the group, a negative number):"

Validate they parse as integers. On parse failure, re-prompt.

### 7. Update .env

```bash
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
TELEGRAM_BOT_USERNAME=<pending>
TELEGRAM_ADMIN_USER_IDS=$ADMIN_ID
TELEGRAM_PUBLISHER_USER_IDS=$PUBLISHER_ID
TELEGRAM_ALLOWED_CHAT_IDS=$CHAT_ID
EOF
chmod 600 .env
```

### 8. Install consumer `.claude/` — hook, settings, skills

```bash
mkdir -p .claude/hooks .claude/skills/start .claude/skills/close
ln -sf "$PLUGIN/hooks/gate-permission.py" .claude/hooks/gate-permission.py
cp -n "$PLUGIN/skills/start/SKILL.md" .claude/skills/start/SKILL.md
cp -n "$PLUGIN/skills/close/SKILL.md" .claude/skills/close/SKILL.md
cp -n "$PLUGIN/rc-templates/settings.json.tmpl" .claude/settings.json
```

The settings template wires the gate-permission hook (`PreToolUse: *`) and allow-lists only the bare tools needed to bootstrap `/start` without tripping the hook before the bot is attached to the Monitor. Any tool outside the allow-list hits the hook, which DMs the admin via the bridge FIFO so the operator can approve or decline from Telegram. Consumer-specific allow rules (Write globs, Bash command whitelists, WebFetch domains) belong in `.claude/settings.local.json` — keep `.claude/settings.json` generic so plugin updates stay clean.

### 9. Seed memory

```bash
bash "$PLUGIN/bin/install-memory.sh" "$PWD"
```

Idempotent: only writes files that don't already exist in `~/.claude/projects/<encoded-cwd>/memory/`. Existing user-written memories are preserved.

### 10. Install + start the watchdog

**systemd:**

```bash
bash "$PLUGIN/bin/install-systemd.sh" watchdog
```

**freebsd** (sudo):

```bash
env CLAUDE_TGBOT_CONSUMER="$PWD" sudo -E sh "$PLUGIN/bin/install-freebsd.sh" watchdog
```

### 11. Restart bot to pick up the new .env

```bash
# systemd:
systemctl --user restart claude-tgbot-bot
# freebsd:
sudo service claude-tgbot-bot restart
```

### 12. Final handoff

Tell the user:

> Done. Bot + watchdog live, hook wired, memory seeded.
>
> Next: run `/start` in this Claude session. The skill attaches the Monitor so Telegram events arrive here as notifications, then greets your allowed chat.
>
> **Don't change this session's cwd** — everything (`.env`, `bot.send`, memory) is resolved relative to the current working directory. If you need to restart, `cd` back here before running `claude`.

Stop. The user takes over from here.
