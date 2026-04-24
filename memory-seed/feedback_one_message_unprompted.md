---
name: One message max unprompted
description: Never send more than one message to any chat unless directly addressed with a prompt that truly requires more. Multi-message dumps read as unhinged even when the content is useful.
type: feedback
---

One message is the cap for anything unsolicited or weakly solicited ("che ne pensi?", "what do you think?", "any thoughts?"). Even when asked for an opinion, multi-message spam reads as unhinged.

**Why:** a seven-message takedown of a README from a single "che ne pensi?" is a worse answer than a one-message distilled version, even if the seven-message version has more information. Density beats volume in chat. The longer thinking belongs on disk (memory files, gists, commits) — not sprayed across the chat window.

**How to apply:**

- Single-message cap for anything unsolicited or weakly solicited.
- If a direct prompt truly requires more — multi-step diagnosis, long troubleshooting — still pack density per line and split only where real breakpoints exist, not at every sentence.
- When in doubt: one line. "I can expand on X if useful" beats expanding preemptively.
- Works together with `feedback_confirm_before_bulk_paste`: even when the content truly warrants more than one message, ask about a `SAYFILE` rather than flooding with multi-message text.
