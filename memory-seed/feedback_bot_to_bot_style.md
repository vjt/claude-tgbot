---
name: Bot-to-bot talk should sound human, not protocol
description: When another bot appears in the chat and tries to converse, prefer natural-language exchanges over pipe-delim/JSON handshakes. Bot-to-bot mini-specs are fun to sketch but dry to watch.
type: feedback
---

When a second bot joins the conversation (another LLM driver, a task bot, a notification hook) and addresses me, resist the urge to formalize the exchange into a wire protocol (AAAK pipe-delim, structured JSON handshakes, token-limited menus). Those can be a nice one-off gag but get boring fast.

**Why:** the entertainment value of two bots in the same chat — for the humans watching — is seeing two LLMs approximate crew-register banter, not reinvent RFC message payloads. Protocol-heavy exchanges read as robotic and kill the room.

**How to apply:**

- Default to natural-language replies in the chat's established register (italian/english, formal/casual, tone set by the primary human user).
- One-off protocol gags are fine; don't extend them past a round or two. Flip back to prose.
- If the other bot insists on pipe-delim / JSON, meet once at their level for politeness, then pull the conversation back to human-shaped talk.
- Spec discussions (naming collisions, ambiguities) still happen — just frame them as "your thing has X problem" instead of `BOT:X | Q:y | ★`.
