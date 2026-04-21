#!/usr/bin/env python3
"""PreToolUse gate — project-agnostic, symlinkable.

Fires on every tool (matcher `*` wired in the consuming project's
`.claude/settings.json`). Allow-list is the union of `permissions.allow` from
that project's `.claude/settings.json` (generic rules) and
`.claude/settings.local.json` (host-specific paths + WebFetch domains). No
match → fast deny + Telegram DM to the operator via the bridge FIFO so a
blocked call surfaces instead of CC's silent interactive prompt.

The hook lives in the bridge repo but is symlinkable into any consumer
project's `.claude/hooks/`. Project resolution via `$CLAUDE_PROJECT_DIR`
(set by Claude Code to the current session's cwd) — settings files come from
*there*, not from the symlink target. FIFO + admin .env always live in the
bridge, regardless of which project triggered the hook.

Allow-rule grammar (superset of CC native):
    <Tool>                          — bare = any use
    Read(<path-glob>)               — fnmatch on tool_input.file_path
    Edit(<path-glob>)               — idem
    Write(<path-glob>)              — idem
    NotebookEdit(<path-glob>)       — idem
    Bash(<cmd-glob>)                — fnmatch on tool_input.command
    WebFetch(domain:<host>)         — exact host
    WebFetch(domain:*.<suffix>)     — subdomain wildcard
    Skill(<name>)                   — exact skill name
    <Tool>(<key>:<value>)           — generic key:value equality on tool_input
"""
import fnmatch
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

# Everything consumer-scoped lives in the consumer repo: settings, .env,
# FIFO. Bridge ships only generic code. Project resolution via
# $CLAUDE_PROJECT_DIR (Claude Code sets it to the session cwd); fallback
# walks up from __file__ WITHOUT .resolve() so a symlinked hook still
# reports the consumer dir as its project.
_cpd = os.environ.get("CLAUDE_PROJECT_DIR")
PROJECT = Path(_cpd) if _cpd else Path(__file__).parent.parent.parent
HERE = PROJECT / ".claude"
SETTINGS_FILES = [HERE / "settings.json", HERE / "settings.local.json"]
ENV_FILE = PROJECT / ".env"
BOT_FIFO = PROJECT / "bot.send"
PROJECT_NAME = PROJECT.name or "project"


def load_env():
    out = {}
    try:
        with open(ENV_FILE) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def admin_user_ids():
    """Admin user_ids receive permission-prompt DMs.

    On Telegram, a user's private-chat chat_id equals their user_id, so the
    same numeric id is both the identity and the DM target.
    """
    env = load_env()
    raw = env.get("TELEGRAM_ADMIN_USER_IDS") or os.environ.get("TELEGRAM_ADMIN_USER_IDS") or ""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


def load_allow():
    out = []
    for p in SETTINGS_FILES:
        try:
            out.extend(json.loads(p.read_text()).get("permissions", {}).get("allow", []))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return out


def notify(text):
    """Non-blocking FIFO write — silent if bot is down (no reader).

    One SAY per admin; each admin receives the prompt as a DM.
    Returns True if a DM was actually dispatched (admins configured AND
    FIFO writable), False otherwise. The caller uses this to decide
    whether to deny ("we told the operator, wait") or ask ("no Telegram
    path available, fall back to CC's interactive prompt").
    """
    admins = admin_user_ids()
    if not admins:
        return False
    try:
        fd = os.open(str(BOT_FIFO), os.O_WRONLY | os.O_NONBLOCK)
        try:
            for a in admins:
                os.write(fd, f"SAY {a} {text}\n".encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def host_matches(pattern, host):
    if pattern.startswith("*."):
        return host == pattern[2:] or host.endswith(pattern[1:])
    return host == pattern


def rule_matches(rule, tool, tool_input):
    if rule == tool:
        return True
    prefix = f"{tool}("
    if not (rule.startswith(prefix) and rule.endswith(")")):
        return False
    inner = rule[len(prefix):-1]

    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        return fnmatch.fnmatchcase(tool_input.get("file_path", ""), inner)

    if tool == "Bash":
        return fnmatch.fnmatchcase(tool_input.get("command", ""), inner)

    if tool == "WebFetch" and inner.startswith("domain:"):
        host = urlparse(tool_input.get("url", "")).hostname or ""
        return host_matches(inner[7:], host)

    if tool == "Skill":
        return tool_input.get("skill", "") == inner

    if ":" in inner:
        k, v = inner.split(":", 1)
        return str(tool_input.get(k, "")) == v

    return False


def hint_for(tool, tool_input):
    if tool == "WebFetch":
        h = urlparse(tool_input.get("url", "")).hostname or "?"
        return f' — "{PROJECT_NAME}: allow WebFetch(domain:{h})"'
    if tool in ("Read", "Edit", "Write", "NotebookEdit"):
        p = tool_input.get("file_path", "?")
        return f' — "{PROJECT_NAME}: allow {tool}({p})"'
    if tool == "Bash":
        c = (tool_input.get("command", "") or "").split()[0] or "?"
        return f' — "{PROJECT_NAME}: allow Bash({c} *)"'
    if tool == "Skill":
        n = tool_input.get("skill", "?")
        return f' — "{PROJECT_NAME}: allow Skill({n})"'
    return f' — "{PROJECT_NAME}: allow {tool}"'


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}
    for rule in load_allow():
        if rule_matches(rule, tool, tool_input):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": f"matched rule: {rule}",
                }
            }))
            return

    snippet = json.dumps(tool_input, ensure_ascii=False)[:180]
    dispatched = notify(f"[PERM] {tool} {snippet}{hint_for(tool, tool_input)}")
    if dispatched:
        decision = "deny"
        reason = f"{tool} not in allow list — asked operator via Telegram, wait for ack before retry"
    else:
        # No Telegram path (bridge-maintenance session, bot down, or no
        # admins). Fall back to CC's native interactive prompt rather than
        # denying silently with a misleading message.
        decision = "ask"
        reason = f"{tool} not in allow list — no Telegram operator configured, prompt locally"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


if __name__ == "__main__":
    main()
