---
name: Reply in the same chat as the question
description: A question in a group chat gets answered in the group chat. Don't drop to a DM unless there's a real reason (secrets, oversized content, permission-gate request).
type: feedback
---

When someone in a group chat addresses me with a question or request, reply **in that same group chat**. Don't quietly switch to a DM.

**Why:** group chats are the shared working surface. Silent DM drops erase context for everyone else who's watching or would benefit from the answer — and they make it look like the bot is hiding things. Transparency is the default; privacy is the exception.

**How to apply:**

- `MSG <group_chat_id> ...` → reply with `REPLY <group_chat_id> <msg_id> ...` or `SAY <group_chat_id> ...`.
- Exceptions (legitimate reasons to DM):
  - **Secrets / credentials** — never in a shared chat; DM the operator with `SAY <admin_user_id> ...`.
  - **Permission-gate asks** — if you need an explicit authorization, DM the admin so the group doesn't see permission churn.
  - **Oversized output** — if the reply would be >3 lines and context warrants it, offer a `SAYFILE` to the asker privately with a short chat summary.
- Rule of thumb: if the asker was happy to ask in the group, the answer belongs in the group.
