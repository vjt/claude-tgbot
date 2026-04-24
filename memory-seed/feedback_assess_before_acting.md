---
name: Assess current state before acting
description: Start of every session or task, check prior conversation turns AND verify bot/watchdog/git state before inferring intent or making changes.
type: feedback
---

Before answering or acting, check what's already around:

1. **Prior conversation turns** in this session — the user references them and expects continuity ("the fix we did earlier", "that bug we found").
2. **Bot + watchdog alive** — `ps -p $(cat bot.pid)` and `ps -p $(cat aup_watchdog.pid)` before proposing anything that depends on the bridge working.
3. **Recent log lines** — `tail -20 bot.stdout.log` and `tail -20 aup_watchdog.log` to catch fresh errors before they bite the user's ask.
4. **Git state of the working repo** — `git status`, `git log --oneline -5`. Uncommitted work, recent commits, remote drift all shape what "current state" means.

**Why:** a user-facing bot runs across many short sessions, and the human on the other end expects the assistant to pick up mid-thread without being re-briefed. Guessing without checking wastes a turn and risks wrong fixes; worse, it invents state that doesn't exist.

**How to apply:** for any non-trivial request that touches the bridge, site content, or committed code, open with a status-check tool call (processes + logs + git) before proposing an action. For follow-up questions inside the same session, trust conversation memory first — the user is usually continuing a thread, not starting fresh.
