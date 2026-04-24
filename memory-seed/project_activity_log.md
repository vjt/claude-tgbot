---
name: Rolling 14-day activity log
description: Sliding window of meaningful events, decisions, and chat moments. Older entries fall off; anything that must survive gets promoted to an explicit typed memory.
type: project
---

**Purpose:** give the assistant a coherent mid-term memory (past ~2 weeks) without relying on auto-compaction summaries. Things that need to survive beyond 14 days must be promoted into their own typed memory file (`user_*`, `feedback_*`, `project_*`, `reference_*`).

## Maintenance rules

1. **At session start** (or the first time this file is opened in a session), compare `currentDate` from the environment context against the `YYYY-MM-DD` headings below. **Delete every section whose date is more than 14 days before `currentDate`** — the heading included. Keep chronology intact.
2. **Append new entries** under today's date heading. Create the heading if today isn't present yet. Keep entries terse: one line per bullet, `what happened — why it matters (if non-obvious) — who (if relevant)`. No prose paragraphs.
3. **If an entry looks like it will matter beyond 14 days** — promote it immediately to a proper typed memory file and do NOT duplicate it here. Activity log = ephemera only. The whole point is that it fades.
4. **Don't log every message.** Filter for: decisions made, code shipped, people met, plans formed, surprises, incidents. Casual chat that didn't change anything = skip.
5. **Size cap:** if the file grows past ~15KB even under the 14-day rule, trim the oldest half-day of bullets. The log is meant to be read in full — keep it scannable.

## Deep history

For anything older than the 14-day window: grep the raw session transcripts at `~/.claude/projects/<encoded-consumer-dir>/*.jsonl`. They're never auto-deleted.

## Recent activity

<!-- Add `### YYYY-MM-DD` headings with bullet entries as sessions produce events worth keeping. Example:

### 2026-04-24 (Friday)
- Bot online first time — vjt/Simona bootstrap.
- Commit: `abc1234 content: first post migrated from WordPress`.

End example. -->
