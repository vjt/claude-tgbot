---
name: close
description: Graceful close of the bot-driven Claude Code session. Says goodbye in Telegram, stops the Monitor, exits.
---

# close

Someone asked you to shut down, or you're about to `/clear` voluntarily. Tie off the TG side cleanly.

## Steps

1. **Ack in Telegram** — same chat as `/start`:

   ```bash
   CHAT_ID=$(grep -m1 '^TELEGRAM_ALLOWED_CHAT_IDS=' .env | cut -d= -f2 | tr -d '\r' | cut -d, -f1)
   printf 'SAY %s a dopo.\n' "$CHAT_ID" > bot.send
   ```

2. **Stop the Monitor** — use `TaskStop` with the monitor's id. If you didn't note it at `/start`, find it via `TaskList` (description `tgbot events`).

3. **Surface pending work** — if there's uncommitted code, an in-flight push, or a build not yet triggered, say so in the ack so the user isn't surprised on return.

Bot and watchdog stay up across session closes — that's the service manager's job. You're ending this Claude session only, not stopping the daemons.
