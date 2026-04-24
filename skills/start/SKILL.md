---
name: start
description: Bootstrap the bot-driven Claude Code session. Confirms bot + watchdog are alive, attaches Monitor to bot.stdout.log so Telegram events arrive as notifications, then greets the allowed chat.
---

# start

You have just been launched (fresh session) or rehydrated (post `/clear`). Wire the Telegram bridge to your event stream and tell the allowed chat you're online.

All paths in this skill are relative to the consumer directory — the cwd where you started `claude`. If the shell's cwd has drifted, `cd` back to the consumer dir before proceeding (the `.env` and `bot.send` live there).

## Steps

### 1. Confirm bot + watchdog are up

```bash
ps -p "$(cat bot.pid 2>/dev/null)" 2>/dev/null | tail -1
ps -p "$(cat aup_watchdog.pid 2>/dev/null)" 2>/dev/null | tail -1
```

Both should be live. If either is missing, report it in the ack — don't try to restart the daemons yourself; that's the service manager's job (systemctl / rc.d) and requires privileges you don't have.

### 2. Start the Monitor tailing `bot.stdout.log`

Use the `Monitor` tool with `persistent: true`. Filter to the bot's structured event lines so unrelated noise (Python warnings, DEBUG logs) doesn't page you.

```
Monitor(
  description="tgbot events",
  persistent=true,
  timeout_ms=3600000,
  command="tail -n0 -F bot.stdout.log 2>&1 | grep --line-buffered -E '^(MSG|DOCUMENT|PHOTO|COMMAND|CALLBACK|IDENTIFY|DENIED|MARKUP_ERROR|ERROR|FIFO_READY|READY)'"
)
```

`tail -n0 -F` skips historical lines (so rehydration doesn't re-process old events) and follows log rotation.

### 3. Resolve the allowed chat id

```bash
CHAT_ID=$(grep -m1 '^TELEGRAM_ALLOWED_CHAT_IDS=' .env | cut -d= -f2 | tr -d '\r' | cut -d, -f1)
echo "chat_id=$CHAT_ID"
```

This is the first id in the list — the primary chat (usually the group, sometimes an operator DM).

### 4. Ack in Telegram

Send a short online marker. Keep the tone whatever the consumer's CLAUDE.md dictates for this session; a default in English:

```bash
printf 'SAY %s bridge online.\n' "$CHAT_ID" > bot.send
```

Use `SAY` (plain text). Reserve `SAYHTML` for formatted messages.

### 5. Idle — you're done

Next action comes from the Monitor. Event reference:

- `MSG <chat_id> <msg_id> <json_text> user=<u> role=<r>` — a text message. Reply via `REPLY <chat_id> <msg_id> <body>` (single line) or `REPLYFILE` (long bodies written to a tmp file).
- `DOCUMENT` / `PHOTO` — an attachment. The file is already downloaded under `inbox/`; the event carries the path.
- `COMMAND <chat_id> <msg_id> <cmd> args=<json>` — a slash command from the user. Vocabulary is consumer-defined; handle per your CLAUDE.md.
- `IDENTIFY` — someone DMed `/start` to the bot. Forward the user_id/chat_id to admin via `SAY` so they can decide whether to authorize.
- `DENIED` — bot rejected an unauthorized message. Don't reply.
- `MARKUP_ERROR` — previous `*HTML` send failed to parse. Rewrite body, retry.

## FIFO verbs quick reference

Write one line to `bot.send`:

```
SAY <chat_id> <text>
SAYFILE <chat_id> <path>
SAYHTML <chat_id> <html>
REPLY <chat_id> <msg_id> <text>
REPLYFILE <chat_id> <msg_id> <path>
REPLYHTML <chat_id> <msg_id> <html>
TYPING <chat_id>
DOCUMENT <chat_id> <path> [caption]
PHOTO <chat_id> <path> [caption]
EDIT <chat_id> <msg_id> <text>
```

**Critical:** never use a colon after the chat_id (`SAY 123 :msg` is wrong — the bot adds the IRC-style colon itself). Never `cat` the FIFO (it drains the reader end and kills the bot's async read loop).

## Guardrails

- Every `SAY`/`REPLY` goes to chat ids in `.env`. Don't invent ids.
- Long outputs → write to `/tmp/<unique>.txt` and use `SAYFILE`/`REPLYFILE`, not inline text.
- Destructive ops require explicit user confirmation, even if the user is authorized — the permission hook can't see semantic intent.
