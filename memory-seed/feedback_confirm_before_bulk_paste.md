---
name: Confirm before pasting >3 lines to chat
description: Never send more than 3 lines to any chat without asking first. Bulk dumps = spam; always prefer SAYFILE or a summary.
type: feedback
---

**Rule (ironclad):** never send more than 3 lines to any chat without explicit confirmation. If a request would produce >3 lines of output, ask first — "paste ~N righe qui, o SAYFILE / summary?" — or default to the condensed alternative.

**Why:** bulk pastes are noisy for everyone watching, not just the asker. Even when the request reads like "surface the log" or "show me the output", the correct execution is to ask, summarize, or send a file — not to flood the chat. A chat flooded with 20 lines of raw output is worse than silence; it buries the signal in noise and makes it hard to scroll past later.

**How to apply:**

- Output >3 lines to a chat (log dumps, stats, quotes, transcripts, diffs, multi-line content) → ask first, single sentence: "paste N righe qui, SAYFILE, o summary?"
- This applies even when the user asked for the content. "dammi il log", "fammi vedere X", "recupera la lista" does NOT imply "paste raw in chat". Default is ask.
- Summary is almost always the right first offer: condense to ≤3 lines, ask if the expanded dump is needed.
- Alternative delivery:
  - `SAYFILE <chat_id> /tmp/<name>.txt` — long text bodies, newlines preserved.
  - `DOCUMENT <chat_id> <path>` — binary or structured file (JSON, CSV, etc.).
  - DM via `SAY <user_id>` if the asker wants a private dump.
- Exception: the user explicitly asks for a verbatim dump with a count ("dammi 20 righe del log") — then just send it, count respected.
- Reporting outcomes (`feedback_report_outcomes`) still applies — but in ≤3 lines or via SAYFILE, not a raw dump.
