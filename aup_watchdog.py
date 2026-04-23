#!/usr/bin/env python3
"""
Watchdog / clear-sidecar for a claude-tgbot consumer's long-lived Claude
Code session. Tails the active session JSONL under
~/.claude/projects/<encoded-consumer-path>/ and injects `/clear` into
the tmux pane running `claude` with cwd == consumer dir on three
independent triggers:

1. AUP refusal — assistant message matches "Usage Policy" / "unable to
   respond" pattern → clear immediately.
2. Turns — MAX_TURNS assistant turns since last clear → eager clear.
3. Idle — jsonl mtime hasn't advanced for IDLE_SEC AND there is no
   pending assistant tool_use awaiting a user tool_result → clear.

All triggers share one cooldown window so back-to-back clears never
stack. Nothing here runs claude itself — it only kicks the pane.

Config via env (set by the systemd unit):

    CLAUDE_TGBOT_CONSUMER_DIR   absolute consumer dir, e.g. /home/user/code/myproject
    CLAUDE_TGBOT_TMUX_WINDOW    tmux target for list-panes, e.g. 0:myproject
    CLAUDE_TGBOT_ESCALATE_CHAT  (optional) Telegram chat_id to DM when pane
                                resolution stays broken for RESOLVE_ALERT_SEC.
                                Uses consumer's bot.send FIFO (SAY verb).
                                Unset = silent (log-only).

Optional per-consumer scrub: if `<consumer>/scrub_prompt.txt` exists,
its contents get pasted into the pane after each `/clear` (so the
post-clear session wakes up and runs whatever housekeeping the
consumer wants). Absent file = just clear, no scrub.

Port of claude-ircbot's aup_watchdog.py, generalised for the tgbot bridge.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CONSUMER_DIR = Path(os.environ["CLAUDE_TGBOT_CONSUMER_DIR"]).resolve()
# TMUX_WINDOW kept for backwards compat / unit-file stability but NO LONGER
# used for pane resolution — sessions get renamed/recreated, so we scan all
# sessions via `list-panes -a` and pick by cmd=claude + cwd=CONSUMER_DIR.
TMUX_WINDOW = os.environ.get("CLAUDE_TGBOT_TMUX_WINDOW", "")
ESCALATE_CHAT = os.environ.get("CLAUDE_TGBOT_ESCALATE_CHAT", "").strip()

# ~/.claude/projects/ encodes the project cwd by replacing `/` with `-`.
PROJECT_DIR = (
    Path.home() / ".claude" / "projects"
    / ("-" + str(CONSUMER_DIR).lstrip("/").replace("/", "-"))
)

SCRUB_PROMPT_FILE = CONSUMER_DIR / "scrub_prompt.txt"
BOT_FIFO = CONSUMER_DIR / "bot.send"

POLL_SEC = 2
DEBOUNCE_SEC = 30           # any clear — AUP / idle / turns — holds this window
IDLE_SEC = 600              # 10 min of no jsonl writes = idle
MAX_TURNS = 100             # assistant turns since last clear → eager clear
TAIL_SCAN = 200             # lines from end to check for pending tool_use
POST_CLEAR_WAIT = 3         # seconds for /clear to settle before scrub prompt
RESOLVE_ALERT_SEC = 300     # consecutive resolve failures before Telegram escalation
LOG_DEDUP_SEC = 60          # collapse identical consecutive log lines for this long

STUCK_PATTERNS = re.compile(
    r"(unable to respond to this request|appears to violate our Usage Policy|Usage Policy)",
    re.IGNORECASE,
)


_log_state: dict[str, float | str | int] = {"last_msg": "", "last_ts": 0.0, "repeat": 0}


def _emit(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def log(msg: str) -> None:
    """Print with dedup: identical consecutive lines collapse for LOG_DEDUP_SEC,
    then emit a '(repeated N times in X s)' summary on transition."""
    now = time.time()
    last_msg = _log_state["last_msg"]
    last_ts = float(_log_state["last_ts"])
    repeat = int(_log_state["repeat"])
    if msg == last_msg and now - last_ts < LOG_DEDUP_SEC:
        _log_state["repeat"] = repeat + 1
        return
    if repeat > 0 and isinstance(last_msg, str):
        _emit(f"(last line repeated {repeat}x over {int(now - last_ts)}s)")
    _emit(msg)
    _log_state["last_msg"] = msg
    _log_state["last_ts"] = now
    _log_state["repeat"] = 0


def send_fifo_say(chat_id: str, msg: str) -> None:
    """Fire a SAY to the tgbot FIFO — best effort, never raises."""
    if not chat_id:
        return
    try:
        with BOT_FIFO.open("w") as f:
            f.write(f"SAY {chat_id} {msg}\n")
    except OSError as e:
        _emit(f"FIFO write failed: {e!r}")


def load_scrub_prompt() -> str | None:
    try:
        text = SCRUB_PROMPT_FILE.read_text(encoding="utf-8").strip()
        return text or None
    except FileNotFoundError:
        return None
    except OSError as e:
        log(f"scrub prompt read failed: {e}")
        return None


def latest_jsonl() -> Path | None:
    candidates = sorted(
        PROJECT_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_claude_pane() -> str | None:
    """Scan ALL tmux panes (session-agnostic) and pick the one whose
    command is `claude` AND whose cwd resolves to CONSUMER_DIR.

    We used to target `-t TMUX_WINDOW` (e.g. `0:noema`), but tmux sessions
    get renamed/recreated between reboots and the hard-coded numeric/named
    session breaks silently — list-panes fails with "can't find session"
    and the watchdog spams "skipping" without ever firing /clear. `-a`
    makes resolution survive session churn; cwd match keeps it unique.
    """
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"tmux list-panes -a failed: {e}")
        return None
    matches: list[str] = []
    for line in out.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 3:
            continue
        pane_id, cmd, cwd = parts
        if cmd != "claude":
            continue
        try:
            if Path(cwd).resolve() == CONSUMER_DIR:
                matches.append(pane_id)
        except OSError:
            continue
    if not matches:
        log(f"no pane with cmd=claude cwd={CONSUMER_DIR}")
        return None
    if len(matches) > 1:
        log(f"multiple candidate panes {matches} — picking first")
    return matches[0]


def inject_clear(pane: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "/clear", "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux send-keys failed on {pane}: {e}")
        return False


def inject_scrub(pane: str, prompt: str) -> bool:
    """After /clear has settled, paste the consumer's scrub prompt.

    Delivered via paste-buffer (atomic) + separate Enter. `send-keys text
    Enter` in one call sends chars+Enter back-to-back, which races CC's
    Ink/React renderer mid-/clear — chars get dropped and only the Enter
    registers, producing an empty submit. paste-buffer hands the whole
    string to the pty in one syscall (bracketed paste), then Enter submits
    after a small settle delay.
    """
    time.sleep(POST_CLEAR_WAIT)
    buf_name = f"claude-tgbot-scrub-{os.getpid()}"
    try:
        # check_call doesn't accept `input=` (Popen has no such kwarg).
        # Use subprocess.run for the one call that pipes stdin.
        subprocess.run(
            ["tmux", "load-buffer", "-b", buf_name, "-"],
            input=prompt.encode(),
            check=True,
        )
        subprocess.check_call(
            ["tmux", "paste-buffer", "-b", buf_name, "-d", "-t", pane]
        )
        time.sleep(0.5)
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux paste-buffer/send-keys (scrub) failed on {pane}: {e}")
        return False


def line_is_assistant_turn(line: str) -> bool:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return False
    return rec.get("type") == "assistant"


def line_matches_aup(line: str) -> bool:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return False
    if rec.get("type") != "assistant":
        return False
    content = rec.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            if STUCK_PATTERNS.search(block.get("text", "")):
                return True
    return False


def tail_lines(path: Path, n: int = TAIL_SCAN) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:]
    except OSError:
        return []


def has_pending_tool_use(lines: list[str]) -> bool:
    """True if any assistant tool_use in the tail has no matching user
    tool_result afterwards — i.e., a turn is in flight."""
    used: list[str] = []
    results: set[str] = set()
    for raw in lines:
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        typ = rec.get("type")
        msg = rec.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        if typ == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = b.get("id")
                    if tid:
                        used.append(tid)
        elif typ == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid:
                        results.add(tid)
    return any(tid not in results for tid in used)


_resolve_state: dict[str, float | bool] = {"fail_since": 0.0, "alerted": False}


def fire_clear(reason: str) -> bool:
    pane = resolve_claude_pane()
    now = time.time()
    if pane is None:
        fail_since = float(_resolve_state["fail_since"])
        if fail_since == 0.0:
            _resolve_state["fail_since"] = now
        elif (
            not _resolve_state["alerted"]
            and now - fail_since >= RESOLVE_ALERT_SEC
        ):
            msg = (
                f"watchdog: can't find claude pane "
                f"(cwd={CONSUMER_DIR}, cmd=claude) for "
                f"{int(now - fail_since)}s — /clear injection stalled ({reason})"
            )
            log(f"ESCALATING to chat={ESCALATE_CHAT or '<unset>'}: {msg}")
            send_fifo_say(ESCALATE_CHAT, msg)
            _resolve_state["alerted"] = True
        log(f"{reason} but no claude pane found with cwd={CONSUMER_DIR} — skipping")
        return False
    if _resolve_state["alerted"]:
        send_fifo_say(
            ESCALATE_CHAT,
            f"watchdog: pane resolved again ({pane}) — back to normal",
        )
    _resolve_state["fail_since"] = 0.0
    _resolve_state["alerted"] = False
    log(f"{reason} → injecting /clear into {pane}")
    if not inject_clear(pane):
        return False
    scrub = load_scrub_prompt()
    if scrub and inject_scrub(pane, scrub):
        log(f"scrub prompt injected into {pane}")
    return True


def main() -> int:
    if not PROJECT_DIR.exists():
        log(f"project dir missing (will appear on first claude session): {PROJECT_DIR}")
        # Don't exit — wait for it to appear.

    log(f"watchdog starting — consumer={CONSUMER_DIR} "
        f"project_dir={PROJECT_DIR} escalate_chat={ESCALATE_CHAT or '<unset>'} "
        f"(IDLE_SEC={IDLE_SEC}, DEBOUNCE_SEC={DEBOUNCE_SEC}, MAX_TURNS={MAX_TURNS})")
    boot_ts = time.time()
    current_file: Path | None = None
    current_pos = 0
    last_fire = 0.0
    first_attach = True
    turns_since_clear = 0

    while True:
        try:
            if not PROJECT_DIR.exists():
                time.sleep(POLL_SEC)
                continue

            latest = latest_jsonl()
            if latest is None:
                time.sleep(POLL_SEC)
                continue

            if latest != current_file:
                if first_attach:
                    current_pos = latest.stat().st_size
                    log(f"tailing {latest.name} from offset {current_pos} (skipping history)")
                    first_attach = False
                else:
                    current_pos = 0
                    log(f"tailing {latest.name} (new session)")
                current_file = latest
                turns_since_clear = 0

            # --- AUP trigger: tail new lines and pattern-match ---
            with current_file.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(current_pos)
                chunk = f.read()
                current_pos = f.tell()

            now = time.time()
            fired_this_tick = False
            if chunk:
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    if line_is_assistant_turn(line):
                        turns_since_clear += 1
                    if line_matches_aup(line):
                        if now - last_fire < DEBOUNCE_SEC:
                            log("AUP match (debounced, skipping)")
                            break
                        if fire_clear("AUP STUCK DETECTED"):
                            last_fire = now
                            turns_since_clear = 0
                            fired_this_tick = True
                        break

            # --- Turns trigger: MAX_TURNS assistant turns since last clear ---
            if not fired_this_tick and turns_since_clear >= MAX_TURNS:
                if now - last_fire < DEBOUNCE_SEC:
                    pass  # wait out debounce, fire next tick
                elif has_pending_tool_use(tail_lines(current_file)):
                    log(f"turns {turns_since_clear} but pending tool_use — skipping")
                elif fire_clear(f"TURNS {turns_since_clear}"):
                    last_fire = now
                    turns_since_clear = 0
                    fired_this_tick = True

            # --- Idle trigger: jsonl quiet + no pending tool_use ---
            # `mtime > last_fire` gates on evidence that CC actually processed
            # the previous /clear (wrote at least one line after it). Without
            # this, a stuck pane never advances mtime → age stays huge → every
            # DEBOUNCE_SEC fires another /clear → clears pile up in CC's input.
            if not fired_this_tick:
                age = now - current_file.stat().st_mtime
                boot_age = now - boot_ts
                if (
                    age >= IDLE_SEC
                    and boot_age >= IDLE_SEC
                    and now - last_fire >= DEBOUNCE_SEC
                    and current_file.stat().st_mtime > last_fire
                ):
                    if has_pending_tool_use(tail_lines(current_file)):
                        log(f"idle {int(age)}s but pending tool_use — skipping")
                    elif fire_clear(f"IDLE {int(age)}s"):
                        last_fire = now
                        turns_since_clear = 0

            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            log("interrupted, exiting")
            return 0
        except Exception as e:
            log(f"loop error: {e!r}")
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
