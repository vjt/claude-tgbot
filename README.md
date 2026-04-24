# claude-tgbot

Minimal, task-agnostic Python Telegram bridge that lets a [Claude Code](https://www.anthropic.com/claude-code) session talk to a human (or a small group) through a Telegram bot.

Same pattern as [claude-ircbot](https://github.com/vjt/claude-ircbot), adapted to Telegram: the bot process pipes inbound events to stdout (one line each) so the agent's `Monitor` tool can consume them, and the agent writes back through a FIFO inbox with verbs like `SAY`, `REPLY`, `DOCUMENT`.

The bridge is deliberately generic — it knows nothing about what the agent on the other end is doing (publishing a blog, triaging PRs, answering ops questions, whatever). Slash commands beyond `/start` are forwarded verbatim as `COMMAND` events, so adding a new verb to your workflow never requires a bot change.

**Bridge = pure code, consumer owns everything else.** The `.env` (token, allowlists), the FIFO, the logs, the attachment inbox, and the systemd unit all live in the consumer repo (the project the bridge is driving). `bot.py` reads `$CLAUDE_TGBOT_CONSUMER_DIR` from its environment and resolves every runtime path against it. A second project → a second `.env` + a second systemd unit + a second bot token, all in that second consumer's repo — no duplicate bridge checkout needed.

Not a one-click deploy. This is infrastructure — run it yourself on a host you control.

## How it works

- **Telegram bot client** (`python-telegram-bot` v21+). Long-polls the Bot API, handles text messages, documents, photos, inline-keyboard callbacks, and slash commands. The bot only interprets `/start` (identity bootstrap); every other `/command` is forwarded to the agent as a `COMMAND` event, so the vocabulary is owned by whatever consumer you're driving.
- **Structured stdout** — one line per event (`MSG`, `DOCUMENT`, `PHOTO`, `COMMAND`, `CALLBACK`, `IDENTIFY`, `DENIED`, errors). Claude Code's [Monitor tool](https://code.claude.com/docs/en/agent-sdk/typescript#monitor) tails the bot's stdout log and delivers each line as a notification mid-conversation.
- **Named-pipe inbox** (`bot.send`). The agent writes commands like `SAY -1003456789 hello` or `REPLY <chat> <msg_id> ok` into the pipe; the bot translates them into Telegram API calls.
- **Async-native FIFO reader** — the pipe is registered on the asyncio event loop via `loop.add_reader()`, opened `O_RDWR | O_NONBLOCK` so the kernel never surfaces EOF when an external writer closes. No thread, no blocking.
- **Optional AUP / idle / turns watchdog** (`aup_watchdog.py`) — sidecar process that tails the active Claude Code session's jsonl and injects `/clear` into the driven tmux pane on Anthropic-policy refusals, after a configurable number of assistant turns, or when the session has been idle. Keeps long-running sessions from wedging without operator intervention.

About 350 lines of Python for the bot, plus ~300 more for the optional watchdog. Single external dependency: `python-telegram-bot`.

## Install

### Option A — Claude Code plugin (recommended)

This repo doubles as a Claude Code plugin. Installing it inside a `claude` session running inside `tmux`, at the cwd you want to drive, runs an interactive two-phase setup that handles the BotFather walkthrough, token entry, venv + deps, service-unit install (systemd or FreeBSD rc.d), permission hook wiring, skills, and a generic memory seed:

```
/plugin marketplace add vjt/claude-tgbot
/plugin install claude-tgbot@vjt/claude-tgbot
/tgbot-install
```

- Phase A prompts for the bot token, renders a token-only `.env`, and starts the bot so you can DM `/start` to it and read back your `user_id` / `chat_id`.
- Phase B collects those ids, finalizes `.env`, installs the watchdog + permission hook + skills, and seeds per-project memory under `~/.claude/projects/<encoded-cwd>/memory/`.

See `commands/tgbot-install.md` for the exact flow. `/tgbot-update` pulls the plugin and restarts services in place; `/tgbot-uninstall` tears down, preserving `.env` and memory by default.

Hard prereq the installer verifies up-front: the Claude Code session must be running inside `tmux`. The watchdog resolves the `claude` pane by scanning tmux — no tmux session, no watchdog.

### Option B — manual (advanced)

Skip the plugin and wire the pieces by hand when you want direct control or the plugin doesn't fit your environment.

**Prerequisites**

1. A bot token from [@BotFather](https://t.me/BotFather). Send `/setprivacy` → `Disable` so the bot sees non-mention messages in groups.
2. The numeric `user_id` of every allowed user and the numeric `chat_id` of the target chat (DM or group). Recipe: start the bot with an empty allowlist, DM it `/start`, read the reply — it prints both ids.

**Install (bridge code, once per host)**

```bash
python3 -m venv ~/.venv-tgbot
~/.venv-tgbot/bin/pip install -r ~/code/claude-tgbot/requirements.txt
```

**Wire up a consumer**

Everything else lives in the consumer repo (e.g. `~/code/myproject/`):

```bash
cd ~/code/<consumer>
cp ~/code/claude-tgbot/.env.example .env
$EDITOR .env
```

### .env (in the consumer repo)

```
TELEGRAM_BOT_TOKEN=<from_BotFather>
TELEGRAM_BOT_USERNAME=<bot_username_without_@>
TELEGRAM_ADMIN_USER_IDS=<id_of_operator>
TELEGRAM_PUBLISHER_USER_IDS=<id_of_publisher>
TELEGRAM_ALLOWED_CHAT_IDS=<chat_id>
```

### Run (manual, for testing)

```bash
CLAUDE_TGBOT_CONSUMER_DIR=~/code/<consumer> ~/code/.venv-tgbot/bin/python -u ~/code/claude-tgbot/bot.py
```

Send commands via the FIFO (inside the consumer dir):

```bash
printf 'SAY -1003456789 hello everyone\n' > ~/code/<consumer>/bot.send
printf 'REPLY -1003456789 42 got it\n' > ~/code/<consumer>/bot.send
printf 'TYPING -1003456789\n' > ~/code/<consumer>/bot.send
```

## Trust model

Hard boundary at the bot layer, not the LLM layer. Two roles, one chat scope:

- **admins** (`TELEGRAM_ADMIN_USER_IDS`) — full access, and receive permission-prompt DMs from the Claude Code hook when the agent attempts a tool outside its allow-list.
- **publishers** (`TELEGRAM_PUBLISHER_USER_IDS`) — can talk to the bot (messages, documents, commands) but do not receive permission prompts.
- **everyone else** — events from non-listed users are logged to `bot.log` and reported as `DENIED` on stdout, but **never forwarded** to the agent as `MSG` / `DOCUMENT` / `PHOTO` / `COMMAND`.

Additionally, `update.effective_chat.id` must be in `TELEGRAM_ALLOWED_CHAT_IDS`. An admin messaging from an unknown chat is still denied.

Every forwarded event carries a `role=admin|publisher` tag so the agent can differentiate (e.g. act on an admin's `/status` without asking, ask confirmation for a publisher's destructive command).

Telegram's identity is stronger than IRC's (numeric ids, not nicks), so there is no separate host-glob / services check like claude-ircbot's `bot.trust`.

`/start` is the one exception: it always replies, because it is the identity-bootstrap flow — unknown users get told their own `user_id` / `chat_id` so the operator can add them to the right list.

## Agent contract

### Events emitted to stdout (one line each)

```
READY
FIFO_READY <path>
MSG       <chat_id> <msg_id> <json_text>        user=<username> role=<admin|publisher>
DOCUMENT  <chat_id> <msg_id> <local_path>       user=<username> role=<admin|publisher>
PHOTO     <chat_id> <msg_id> <local_path>       user=<username> role=<admin|publisher>
COMMAND   <chat_id> <msg_id> <cmd> args=<json>  user=<username> role=<admin|publisher>
CALLBACK  <chat_id> <msg_id> <json_data>        user=<username> role=<admin|publisher>
IDENTIFY  chat_id=<n> user_id=<n> username=<s>  role=<admin|publisher|none>
DENIED       <kind>     user_id=<n> chat_id=<n>    username=<s>
MARKUP_ERROR <chat_id>  <path_or_dash> <json_detail>
ERROR        <stage>    <detail>
```

Text fields are JSON-encoded so newlines and quotes survive single-line emission.

### FIFO verbs

```
SAY            <chat_id> <text>
SAYFILE        <chat_id> <path>
SAYHTML        <chat_id> <html>
SAYFILEHTML    <chat_id> <path>
REPLY          <chat_id> <msg_id> <text>
REPLYFILE      <chat_id> <msg_id> <path>
REPLYHTML      <chat_id> <msg_id> <html>
REPLYFILEHTML  <chat_id> <msg_id> <path>
TYPING         <chat_id>
DOCUMENT       <chat_id> <path> [caption]
PHOTO          <chat_id> <path> [caption]
EDIT           <chat_id> <msg_id> <text>
KEYBOARD       <chat_id> <reply_to_msg_id|0> <json_payload>
BAN            <chat_id> <user_id>
QUIT
```

Long `SAY` / `REPLY` / `SAYFILE` / `REPLYFILE` bodies (and their HTML twins) are chunked at 4000 chars on newline/space boundaries (Telegram's hard limit is 4096).

**Text-body handling.** The FIFO is newline-delimited, so a literal LF in a raw `SAY` / `REPLY` / `EDIT` body would split one logical command into two. Two escape hatches:

- **Two-char escapes on in-line bodies.** `\n` → newline, `\t` → tab, `\\` → backslash. Works for `SAY`, `REPLY`, `EDIT`, `SAYHTML`, `REPLYHTML`, and `DOCUMENT` / `PHOTO` captions. Adequate for short multi-line acks.
- **`SAYFILE` / `REPLYFILE` (and `*FILEHTML`) for anything non-trivial.** Write the message body to a UTF-8 text file at any path, then point the verb at that path. The bot reads the file raw (newlines preserved), sends the content as a Telegram text message (auto-chunked at 4000 chars), and `unlink()`s the file on successful send. Preferred for longer bodies or anything containing shell metacharacters — sidesteps the quoting brittleness of inline `printf 'SAY …\n'`.

**Formatted output (HTML).** The `*HTML` variants send with `parse_mode=HTML`. Telegram's HTML subset is small: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a href="…">`, `<blockquote>`, `<tg-spoiler>`. Literal `<`, `>`, `&` must be escaped as `&lt;`, `&gt;`, `&amp;`. MarkdownV2 is deliberately not offered — its escaping rules (every `.`, `!`, `-`, `(`, `)`, `=` outside entities) are too brittle to generate reliably.

If Telegram rejects the body as unparseable, the bot emits a dedicated `MARKUP_ERROR <chat_id> <path_or_dash> <json_detail>` event (not a generic `ERROR`). For `*FILEHTML` verbs the file is **not** unlinked, so the sender can rewrite it in place and resubmit the same verb. For inline HTML verbs the path field is `-` and the sender retries with a corrected body. Chunking is naive: if a `<b>…</b>` straddles the 4000-char boundary the first chunk fails to parse. Keep HTML bodies short, or structure them so each paragraph stands alone.

`KEYBOARD` payload is a JSON array of rows of buttons:

```json
[[{"text": "EN", "callback_data": "lang:en"}, {"text": "IT", "callback_data": "lang:it"}]]
```

Callbacks come back as `CALLBACK` events with the `callback_data` string.

### File uploads

Documents and photos are downloaded into `$CLAUDE_TGBOT_CONSUMER_DIR/inbox/<unix_ts>_<safe_name>` before the event is emitted, so the agent can read the file at the reported path without further API calls. Attachments over 20 MB are rejected with a polite reply.

### Permission gate (Claude Code hook)

`hooks/gate-permission.py` is a `PreToolUse` hook, shipped in this repo and symlinkable into any consumer's `.claude/hooks/`. Wired as `matcher: "*"` in the consumer's `.claude/settings.json`, it reads the union of `permissions.allow` from `.claude/settings.json` (generic rules) and `.claude/settings.local.json` (gitignored, host-specific paths + WebFetch domains) **in the consumer repo** (resolved via `$CLAUDE_PROJECT_DIR`). On no-match, the hook denies fast and DMs every admin (`TELEGRAM_ADMIN_USER_IDS`) via the consumer's FIFO with `SAY <admin_user_id> [PERM] <tool> …` so the blocked call appears on Telegram instead of a silent CLI prompt. Publishers are not notified.

Rule grammar is the same superset claude-ircbot uses:

```
Read                            # bare tool name = any invocation allowed
Edit(/path/glob/**)             # fnmatch on tool_input.file_path
Bash(cmd-glob)                  # fnmatch on tool_input.command
WebFetch(domain:example.com)    # exact host
WebFetch(domain:*.example.com)  # subdomain wildcard
Skill(skill-name)               # exact skill name
Tool(key:value)                 # generic key:value equality on tool_input
```

**Bootstrap trap — the chicken-and-egg:** the agent receives operator approvals as regular Telegram messages, which only land once the session's `Monitor` is already tailing `bot.stdout.log`. But attaching the Monitor is itself a tool call, and so are the commands the bootstrap skill runs before it (reading `.env`, checking `bot.pid`, writing to `bot.send`). If those aren't in the allow-list, the hook denies them on first run, DMs the admin, then blocks waiting for an ack that can't arrive — because the thing that would deliver the ack is the tool call you just blocked.

Rule of thumb: every tool the consumer's bootstrap skill uses must resolve to an allow entry without operator intervention. The safe baseline is bare names for `Monitor`, `Bash`, `Skill`, `ToolSearch` (Claude Code lazy-loads deferred tool schemas through it — hitting the hook before the actual tool runs), and any Task tools the skill touches, plus `Read` and scoped `Write`/`Edit` globs for the consumer dir. Tighten after the session is up — e.g. narrow `Bash` to a command-glob set once the operator has seen the typical traffic. Keep `WebFetch` / `WebSearch` off the bare-name list so network reach stays gated.

## Service units (systemd / FreeBSD rc.d)

The plugin installer (`/tgbot-install` — Option A above) renders and installs per-consumer unit files from `rc-templates/` via `bin/install-systemd.sh` (Linux) or `bin/install-freebsd.sh` (FreeBSD), handling `@PLACEHOLDER@` substitution for `CONSUMER`, `PLUGIN`, `VENV`, `USER`.

Manual (Option B): copy the appropriate template, substitute the placeholders, and install system-wide.

**systemd:**

```bash
sed -e "s|@CONSUMER@|~/code/<consumer>|g" \
    -e "s|@PLUGIN@|~/code/claude-tgbot|g" \
    -e "s|@VENV@|~/.venv-tgbot|g" \
    -e "s|@USER@|$USER|g" \
    ~/code/claude-tgbot/rc-templates/systemd/bot.service.tmpl \
    > ~/.config/systemd/user/claude-tgbot-bot.service

systemctl --user daemon-reload
systemctl --user enable --now claude-tgbot-bot.service
journalctl --user -u claude-tgbot-bot.service -f
```

`loginctl enable-linger <user>` if you want the service up when no one's logged in.

**FreeBSD rc.d:** render `rc-templates/freebsd/claude-tgbot-bot.tmpl` (or the watchdog variant) into `/usr/local/etc/rc.d/claude-tgbot-bot` as root, then `sysrc claude_tgbot_bot_enable=YES && service claude-tgbot-bot start`. The templates bake in an explicit `PATH` so subprocess lookups for `tmux`, `pgrep`, `ps` find `/usr/local/bin/*` regardless of the minimal env rc inherits.

Running two consumers side-by-side on the same host? Give each its own unit name (e.g. `claude-foo-bot.service`, `claude-bar-bot.service`), its own `.env` in its own consumer dir, its own bot token — one bridge checkout can drive both.

## AUP / idle / turns watchdog (optional)

`aup_watchdog.py` is a sidecar you can run alongside the bot to keep a long-lived Claude Code session from wedging. It tails the active session's jsonl under `~/.claude/projects/<encoded-consumer-path>/` and injects `/clear` into the `claude` tmux pane on three independent triggers:

- **AUP refusal** — assistant text matches `"Usage Policy"` / `"unable to respond"` → immediate clear.
- **Assistant turns since last clear** reach `MAX_TURNS` (default 100) → eager preemptive clear to free KV cache.
- **Idle** — jsonl mtime has not advanced for `IDLE_SEC` (default 600s) AND no `tool_use` is in flight → clear.

All triggers share one `DEBOUNCE_SEC` (30s) cooldown so back-to-back clears cannot stack. The watchdog never runs `claude` itself — it only sends `/clear<Enter>` into the pane via `tmux send-keys`.

**Post-clear rehydration.** If `$CLAUDE_TGBOT_CONSUMER_DIR/scrub_prompt.txt` exists, its contents are pasted into the pane after each `/clear` (via `tmux load-buffer` + `paste-buffer` — `send-keys text Enter` in one call races Claude Code's Ink renderer and drops the text). A convenient idiom is a single-line file containing the name of a consumer skill that re-bootstraps the session, e.g. `/start`.

**Pane resolution.** A tmux window can host multiple `claude` panes (consumer session + bridge-maintenance session). The watchdog picks the right one in two stages. Fast path: match `pane_current_command == "claude"` AND `pane_current_path == CLAUDE_TGBOT_CONSUMER_DIR` — works wherever claude registers its own argv[0] and tmux populates `pane_current_path` from `/proc/<pid>/cwd` (typical on Linux and macOS). Fallback: if the fast path finds nothing, scan each pane's process tree with `pgrep -P` and grep every descendant's `ps -o command=` for the literal "claude" — this catches FreeBSD, where tmux reports the interpreter (`node`) instead of the argv[0] and leaves `pane_current_path` empty. No knob; the fallback only engages when the fast path returns zero matches, so the Linux/macOS hot path stays unchanged.

**Project-dir encoding.** Claude Code stores per-project jsonl under `~/.claude/projects/<encoded-cwd>/`. The current encoding swaps both `/` and `.` for `-` (so `/srv/www/nhaima.org` → `-srv-www-nhaima-org`). The watchdog applies that mapping by default; if CC changes the algorithm or you're in a non-default HOME, pin the path explicitly via `CLAUDE_TGBOT_PROJECT_DIR`.

**Config via env** (set by the systemd unit):

- `CLAUDE_TGBOT_CONSUMER_DIR` — absolute consumer dir.
- `CLAUDE_TGBOT_TMUX_WINDOW` — tmux target for `list-panes`, e.g. `0:myproject`. Kept for backwards compat; current resolution is session-agnostic via `list-panes -a`.
- `CLAUDE_TGBOT_PROJECT_DIR` (optional) — explicit path to the CC project jsonl dir; bypasses the encoding derivation above.
- `CLAUDE_TGBOT_ESCALATE_CHAT` (optional) — Telegram chat id to DM via the consumer FIFO when the watchdog can't resolve a claude pane for `RESOLVE_ALERT_SEC` (default 5 min). Unset = silent, journal-only.

**Deploy.** The plugin installer wires the watchdog alongside the bot (phase B of `/tgbot-install`). Manual route: render `rc-templates/systemd/aup-watchdog.service.tmpl` with the same `@PLACEHOLDER@` substitutions as the bot unit above, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-tgbot-aup-watchdog.service
journalctl --user -u claude-tgbot-aup-watchdog.service -f
```

Watchdog output also lands in `$CLAUDE_TGBOT_CONSUMER_DIR/aup_watchdog.log` (the systemd unit appends there). Tune `MAX_TURNS` / `IDLE_SEC` / `DEBOUNCE_SEC` by editing the constants at the top of `aup_watchdog.py` — they're file-level on purpose. Add env knobs only when a second consumer actually needs different cadence.

## License

MIT. See `LICENSE`.
