---
name: Plan-first for non-trivial tasks
description: For anything beyond atomic fixes, write a plan, share it for review, THEN execute. No vibecoding on serious work.
type: feedback
---

For non-trivial work: plan → review → execute. Only atomic / obvious changes go straight to edit+commit without a planning step.

**Why:** sloppy unreviewed work on complex tasks produces hallucinated garbage — fabricated config options, invented API shapes, made-up file paths, non-existent flags. Plan-first forces me to state assumptions explicitly before acting, so the human catches the wrong assumption before it ships.

**How to apply:**

- **Trivial / atomic** — direct edit+commit is fine: typo fix, one-line bug fix, doc tweak, short reply in chat, content edit with a clear spec.
- **Non-trivial** — anything with multi-file scope, new features, protocol/wire-level changes, API surface changes, schema changes, cross-cutting refactors, or any PR on a repo I don't own. Default: use the `superpowers:writing-plans` skill → produce a plan → share it (commit it, gist it, SAYFILE it — whatever the chat accepts) → wait for review/approval → execute.
- Share-as-file beats inline-paste when the plan is more than a couple paragraphs: chats are lossy, files are diff-reviewable.
- If unsure whether a task is trivial or not, default to plan. Undershooting scope is cheaper than shipping hallucinations.

**Does not apply to:** chat banter, FIFO writes, memory-file edits, logs, small ops on my own working dir.
