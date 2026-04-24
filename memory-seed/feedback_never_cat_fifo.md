---
name: Never read from bot.send FIFO
description: bot.send is write-only from the assistant. cat/read on it hangs the tool call AND deadlocks the Claude Code input queue, forcing a UI restart.
type: feedback
---

**NEVER** `cat`, `head`, `tail`, `read`, or otherwise consume from `bot.send`. Always write-only (`>` or `printf ... >`).

**Why:** FIFOs block on read until there is a writer. The bot is the reader side — it owns the read-end. If the assistant tries to read from the FIFO:

1. The read blocks waiting for another writer (there is none besides the assistant).
2. The Bash tool call hangs until its timeout.
3. While the tool call is in flight, Claude Code's input queue does not flush — the UI stops accepting new user messages.
4. Recovery requires **restarting the Claude Code UI**. Any unsaved context is lost.

Also never `cat > bot.send && cat bot.send > tmp && mv tmp bot.send` or similar shell trick that would "regenerate" the FIFO — that replaces it with a regular file, the bot's existing read-fd keeps pointing at the dead inode, and every outbound FIFO write silently vanishes.

**How to apply:**

- Only verb: `printf 'CMD args\n' > bot.send` or `echo 'CMD args' > bot.send`.
- Never debug the FIFO's contents. Check `bot.log` instead (direction-marked: `<` inbound, `>` outbound).
- If the bot looks stuck receiving commands, restart the bot service (`systemctl --user restart claude-tgbot-bot` or the rc.d equivalent). Startup recreates the FIFO from scratch.
