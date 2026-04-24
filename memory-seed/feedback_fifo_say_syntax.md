---
name: FIFO verb syntax — no colon after chat_id
description: bot.send verbs take a bare chat_id and bare message. SAY 12345 hello — NOT SAY 12345 :hello.
type: feedback
---

FIFO verb shape: `<VERB> <chat_id> <message>` — chat_id bare, message bare. The bot constructs the Telegram API call itself; don't pre-add any IRC-style colon.

Wrong: `SAY -1234567890 :pronta`
Right: `SAY -1234567890 pronta`

Same for `REPLY`, `SAYHTML`, `REPLYHTML`, etc. A `:` inside the message body (addressing, timestamps, URLs) is fine — it's only a leading `:` immediately after the target that's wrong.

**Why:** the claude-tgbot bridge is a port of an IRC bridge where the IRC wire format requires `PRIVMSG target :message` — the leading colon is mandatory there. The TG bridge takes a normal string and passes it to the Telegram API, which has no such wire convention. Copy-pasting IRC muscle memory leaks the colon into the TG message body verbatim — ugly and confusing.

**How to apply:** every `printf 'SAY ...' > bot.send` or `echo 'REPLY ...' > bot.send` — verify no `:` between chat_id and message body.
