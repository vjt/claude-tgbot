# claude-tgbot — maintainer notes

This repo is the **bridge code**, not the agent's operating manual. A Claude Code session entering this directory is here to maintain or extend the Telegram bridge itself — the bot process, the permission hook, the systemd unit, the event protocol.

The agent that *uses* the bridge runs with `cwd` set to the driven project (whatever that is — a blog, a codebase, an ops target). That project's own `CLAUDE.md` is the operating manual for the session's operator persona (reply policy, memory discipline, skills, command vocabulary). Do not add operator-persona rules here — they belong with the consumer. Keep this repo task-agnostic.

## Filesystem map

Bridge repo is pure code. Everything consumer-scoped (config, runtime state, systemd unit) lives in the consumer's own repo.

```
claude-tgbot/                        ← THIS repo — pure bridge code, committed
├── bot.py                           ← PTB Application + async FIFO reader + stdout event stream
├── aup_watchdog.py                  ← clear-sidecar: tails jsonl, kicks tmux pane on AUP / idle / turns
├── .env.example                     ← template for consumers to copy
├── hooks/
│   └── gate-permission.py           ← shipped as code; consumers symlink into their .claude/hooks/
├── systemd/
│   ├── consumer.service.example     ← template bot unit for consumers
│   └── aup-watchdog.service.example ← template watchdog unit for consumers
└── .claude/                         ← for maintainer sessions in THIS repo (editing bot.py etc.)
    ├── settings.json                ← tracked: generic allow rules, hook wiring → $CLAUDE_PROJECT_DIR/hooks/gate-permission.py
    └── settings.local.json          ← gitignored: host-specific Edit/Write globs

<consumer>/                          ← e.g. ~/code/myproject — owns everything runtime
├── .env                             ← bot token + allowlists (gitignored)
├── bot.send                         ← FIFO — agent writes verbs here (gitignored)
├── bot.log                          ← raw inbound/outbound log (gitignored)
├── bot.stdout.log                   ← systemd-captured event stream that Monitor tails (gitignored)
├── aup_watchdog.log                 ← watchdog systemd-captured log (gitignored)
├── scrub_prompt.txt                 ← optional: pasted into the pane after each /clear
├── inbox/                           ← downloaded attachments (gitignored)
├── systemd/
│   ├── claude-<name>-bot.service          ← concrete bot unit
│   └── claude-<name>-aup-watchdog.service ← concrete watchdog unit
└── .claude/
    ├── settings.json                ← consumer's allow-list + hook wiring
    ├── settings.local.json          ← host-specific
    └── hooks/
        └── gate-permission.py       ← symlink → /path/to/claude-tgbot/hooks/gate-permission.py
```

**bot.py resolves runtime paths** against `$CLAUDE_TGBOT_CONSUMER_DIR` (set by the systemd unit). No code path writes to the bridge repo at runtime.

## Event protocol

Keep events **one line each**, with text fields JSON-encoded so newlines survive. The Monitor tool is line-oriented — a multiline emit breaks the split. If a new event type needs a payload, encode it as `key=json(value)` segments.

## FIFO verbs

Sync from the agent side. Verbs are the stable API surface. Adding a verb is cheap; renaming one will break every consumer — think twice before. The ones in place now:

```
SAY / SAYFILE / REPLY / REPLYFILE / TYPING / DOCUMENT / PHOTO / EDIT / KEYBOARD / BAN / QUIT
```

Text bodies on `SAY` / `REPLY` / `EDIT` (and `DOCUMENT` / `PHOTO` captions) accept two-char escapes — `\n` → newline, `\t` → tab, `\\` → backslash — so senders can emit short multi-line messages despite the FIFO being newline-delimited. Anything longer or containing shell-fragile characters should use `SAYFILE` / `REPLYFILE`: sender writes the body to any UTF-8 file path, verb points at the path, bot reads raw (newlines preserved), auto-chunks at 4000 chars, and unlinks on success. This is the authoritative wire format — keep the bot-side list in `bot.py`, the maintainer doc here, and the public README (`### FIFO verbs`) in sync whenever you add or rename a verb.

## Authorization

Three lists in the **consumer's** `.env`:

- `TELEGRAM_ADMIN_USER_IDS` — full access + receive permission-prompt DMs.
- `TELEGRAM_PUBLISHER_USER_IDS` — full access, no permission DMs.
- `TELEGRAM_ALLOWED_CHAT_IDS` — chats the bot listens in.

Allowed users = union of admins and publishers. Chat must match too. Non-listed users get `DENIED` on stdout and **never** surface as `MSG` / `DOCUMENT` / `PHOTO` / `COMMAND` / `CALLBACK`. This is the only trust boundary — do not move it into the agent.

Every forwarded event carries a `role=admin|publisher` suffix so the agent can differentiate behaviour (e.g. confirm destructive commands only when role=publisher).

`/start` is the sole exception: it always answers, because it's how new users learn their own `user_id` and `chat_id` for the operator to add them.

## Permission hook

`hooks/gate-permission.py` is project-agnostic. Consumers symlink it into their own `.claude/hooks/`; it resolves the current project via `$CLAUDE_PROJECT_DIR` and reads that project's `settings.json` / `settings.local.json`. Denials write one `SAY <admin_user_id> [PERM] …` per admin to the **consumer's** FIFO (same resolution) so blocked tool calls appear as a DM in Telegram instead of a silent CLI prompt. Publishers are never notified.

**Decision logic.** Match found → `allow`. No match + Telegram DM dispatched (admins configured, FIFO writable) → `deny` with "wait for ack" reason. No match + no Telegram path (bridge-maintenance session, bot down, no admins) → `ask`, falling back to CC's native interactive prompt. This keeps the gated-via-Telegram model for consumer sessions without silently bricking bridge-maintenance sessions that have no operator to DM.

When touching the hook, remember it runs from both consumer sessions (via symlink) and bridge-maintenance sessions. Don't hardcode paths; use `$CLAUDE_PROJECT_DIR`. Don't assume a FIFO exists — the hook is best-effort (silent on missing FIFO / missing admin IDs).

## AUP / idle / turns watchdog

`aup_watchdog.py` is a sidecar that keeps a long-lived Claude Code session healthy by injecting `/clear` into its tmux pane on three triggers:

- **AUP refusal** — assistant text matches "Usage Policy" / "unable to respond" → immediate clear.
- **Turns ≥ MAX_TURNS** (100) since last clear — eager preemptive clear.
- **Idle ≥ IDLE_SEC** (600s) with no pending tool_use — free KV on quiet periods.

All triggers share one `DEBOUNCE_SEC` (30s) cooldown. Nothing here runs claude itself — it only kicks the pane.

**Config via env** (set by the systemd unit):

- `CLAUDE_TGBOT_CONSUMER_DIR` — absolute consumer dir. Used to locate the jsonl project dir under `~/.claude/projects/<encoded-path>/`, to match the right tmux pane by `pane_current_path`, and to find the optional `scrub_prompt.txt`.
- `CLAUDE_TGBOT_TMUX_WINDOW` — tmux target for `list-panes`, e.g. `0:myproject`.

**Pane disambiguation.** A tmux window can host multiple `claude` panes (consumer session + bridge-maintenance session). The watchdog picks the one whose `pane_current_command == claude` AND `pane_current_path == CLAUDE_TGBOT_CONSUMER_DIR`. Always match on both — matching on command alone will target whichever claude pane tmux lists first, which is usually the wrong one.

**Post-clear scrub prompt.** If `<consumer>/scrub_prompt.txt` exists, its contents are pasted into the pane after each `/clear` (via `tmux load-buffer` + `paste-buffer` + separate Enter — sending text+Enter in one `send-keys` call races CC's Ink renderer and drops the text). Absent file = just clear, no rehydration. The elegant consumer idiom is `/start` — single-line file that re-runs the consumer's `/start` skill to bring the freshly-cleared session back online (log trim, bot check, Monitor reattach).

**Deploy.** Copy `systemd/aup-watchdog.service.example` into `<consumer>/systemd/claude-<name>-aup-watchdog.service`, fill in the two Environment vars + paths, symlink into `~/.config/systemd/user/`, `daemon-reload`, `enable --now`. Restart after `aup_watchdog.py` changes.

**When to touch the defaults.** `MAX_TURNS`, `IDLE_SEC`, `DEBOUNCE_SEC` are file-level constants — tune them in `aup_watchdog.py` itself if a consumer needs different cadence. Don't add per-consumer env knobs until a second consumer actually needs it.

## Deployment

One systemd unit per consumer, living in the consumer's repo. Copy `systemd/consumer.service.example` from here into `<consumer>/systemd/claude-tgbot-bot.service`, fill in `CLAUDE_TGBOT_CONSUMER_DIR` + absolute paths, then:

```bash
ln -s <consumer>/systemd/claude-tgbot-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-tgbot-bot.service
journalctl --user -u claude-tgbot-bot.service -f
```

Restart after `bot.py` changes: `systemctl --user restart claude-tgbot-bot.service`. The bot is stateless between restarts — no inflight Telegram updates are lost because `drop_pending_updates=True` + Telegram's own retry ensures clean resume.

## What's intentionally absent

- No inline help text. `/help` is forwarded as a `COMMAND` event; the agent owns the reply text.
- No rate limiting. Low-volume 1:1 chat + small group; Telegram's own limits are forgiving.
- No multiplexing *per instance*. One bot token = one running `bot.py` = one consumer. Second consumer runs its own unit with its own token + its own `CLAUDE_TGBOT_CONSUMER_DIR` — one bridge checkout can host both.

## Design decisions

Why the bridge is shaped the way it is. Each entry captures the tradeoff so a future maintainer can judge whether the reason still applies before reverting.

### Task-agnostic transport
The bot interprets only `/start` (identity bootstrap). Every other slash-command flows through as a `COMMAND` event — the consuming agent defines the vocabulary (`/draft`, `/publish`, anything). A new consumer-side command never requires a bot change.

### No `bot.trust` layer
claude-ircbot carries a services-auth second-layer check because IRC nicks are spoofable. Telegram identity is numeric and BotFather-enforced, so a single `.env` allowlist on `(user_id, chat_id)` is sufficient. Don't add a second trust layer — it would duplicate the protocol-level guarantee.

### Admin vs publisher split
Two tiers inside the allowlist. **Admins** receive permission-prompt DMs from the Claude Code hook (the interactive operator). **Publishers** can use the bot but don't get perm DMs (they drive content via the agent). Every emitted event carries the role so the agent can differentiate — confirm destructive commands from publishers, auto-run for admins.

### Single-line events with JSON-encoded text
Monitor is line-oriented — one notification per line. Text fields (user messages, callback data) can contain newlines, so they're JSON-encoded at emit time and decoded by the agent. A multi-line emit would split one logical event across multiple notifications and break the parser. Convention: `EVENT_TYPE arg1 arg2 json_text trailing_key=value`.

### Async-native FIFO reader
`loop.add_reader(fd)` with `O_RDWR | O_NONBLOCK` keeps the FIFO hot — the kernel doesn't surface EOF when the last external writer closes, because this fd itself counts as a writer. No dedicated thread, no blocking read, no write-close edge cases. The alternative (thread + blocking read) is simpler to reason about but harder to shutdown cleanly and wastes a thread.

### Consumer-owned runtime
Bridge repo ships only code (`bot.py`, hook, templates, docs). Everything runtime — `.env`, FIFO, logs, `inbox/`, the concrete systemd unit — lives in the consumer repo. `bot.py` reads `$CLAUDE_TGBOT_CONSUMER_DIR` and resolves every path against it.

Rationale: each bot instance's identity and auth boundary is de facto consumer-scoped (different consumer → different BotFather registration → different token → different admin set). Co-locating config with the consumer code makes ownership unambiguous, and lets a single bridge checkout host N consumers via N systemd units without cloning.

### Hook lives in `hooks/`, not `.claude/hooks/`
The permission hook is shipped code, not maintainer-session config. Putting it next to `bot.py` clarifies ownership. Consumers symlink `.claude/hooks/gate-permission.py → $BRIDGE/hooks/gate-permission.py` — one source of truth, bug fixes propagate automatically. `.claude/` in the bridge repo is reserved for *bridge-maintainer* session config (editing the bridge itself).

### No skills in the bridge repo
Skills live in the consumer's `.claude/skills/`. The session's cwd is the consumer project, so that's where Claude Code looks. Ritual skills (`start`, `close`) reference bridge paths but they're the *consumer's* skills for operating the bridge — each consumer clones its own. Considered: branch-per-consumer in the bridge repo hosting skills; rejected because it drags consumer config into bridge git history.

### Auto-allow DM chats for authorized users
Telegram private chats have `chat_id == user_id`. An allowlisted user DMing the bot (for `/start` bootstrap, for receiving perm-prompt DMs) is by definition in a chat the bridge should accept. `ALLOWED_CHAT_IDS` is extended at bot init with `ALLOWED_USER_IDS` so this works without operator intervention. Group chat_ids still come from `.env` explicitly.

### One bridge checkout, N bot instances
Not one clone per consumer. One shared bridge code path on the host, N systemd units, each with its own `CLAUDE_TGBOT_CONSUMER_DIR` + own bot token. Consumer repos carry concrete systemd units; the bridge ships only the `.example` template. Two consumers on the same host = two unit names (e.g. `claude-<consumer1>-bot.service`, `claude-<consumer2>-bot.service`), zero code duplication.

### File-upload cap at 20 MB
Matches Telegram's Bot API default for non-local-API uploads. Photos and documents over 20 MB are rejected with a polite reply; no attempt to split or stream. If you hit this limit in practice, the right fix is a local Bot API server, not patching the cap.

## Traps to avoid

- **Don't move the permission hook file without updating `settings.json` in the same tool call.** The hook script's path is baked into `.claude/settings.json`. If the file doesn't exist at that path when Claude Code looks, every subsequent tool call fails with "hook error" (not "deny") and you can't repair via Edit/Write/Bash because those also route through the broken hook. Escape requires manual shell intervention from the operator. Rename order: update `settings.json` first, then move the file; or move and drop a rescue symlink at the old path before the first tool call that follows.
- **Don't write runtime state to the bridge repo.** If you catch yourself writing `REPO / ".env"` or `REPO / "bot.send"` in `bot.py`, you've reverted consumer-owned-runtime. All path resolution must go through `$CLAUDE_TGBOT_CONSUMER_DIR`.
- **Don't add consumer-flavoured strings to the bridge.** Hardcoded command names, project paths, or persona text belong in the consumer's repo. If a PR adds `"noema"` (or any consumer name) anywhere outside the `.example` templates, push back.
