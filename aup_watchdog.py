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
4. Manual — SIGUSR1 forces a /clear + scrub on the next tick, bypassing
   debounce. For testing the scrub flow on demand:
       systemctl --user kill -s SIGUSR1 claude-noema-aup-watchdog.service

All triggers share one cooldown window so back-to-back clears never
stack. Nothing here runs claude itself — it only kicks the pane.

Config via env (set by the systemd unit):

    CLAUDE_TGBOT_CONSUMER_DIR   absolute consumer dir, e.g. /home/user/code/myproject
    CLAUDE_TGBOT_PROJECT_DIR    (optional) explicit path to Claude Code's per-project
                                jsonl dir. Defaults to the encoded form under
                                ~/.claude/projects/. Override when CC's encoding
                                algorithm changes, or when running in a non-default
                                HOME layout.
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
import signal
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

# ~/.claude/projects/ encodes the project cwd as a single-segment name.
# The encoding CC uses today swaps both `/` and `.` for `-` — a consumer
# dir like /srv/www/nhaima.org lands at
# ~/.claude/projects/-srv-www-nhaima-org/ (NOT -srv-www-nhaima.org).
# Allow an explicit override via CLAUDE_TGBOT_PROJECT_DIR so we don't
# re-break every time CC tweaks the algorithm.
_project_override = os.environ.get("CLAUDE_TGBOT_PROJECT_DIR", "").strip()
if _project_override:
    PROJECT_DIR = Path(_project_override).expanduser()
else:
    PROJECT_DIR = (
        Path.home() / ".claude" / "projects"
        / ("-" + str(CONSUMER_DIR).lstrip("/").replace("/", "-").replace(".", "-"))
    )

SCRUB_PROMPT_FILE = CONSUMER_DIR / "scrub_prompt.txt"
BOT_FIFO = CONSUMER_DIR / "bot.send"

POLL_SEC = 2
DEBOUNCE_SEC = 60           # any clear — AUP / idle / turns — holds this window
                            # (must be ≥ PRE_CLEAR_WARN_SEC + POST_CLEAR_WAIT + buffer)
IDLE_SEC = 600              # 10 min of no jsonl writes = idle
MAX_TURNS = 100             # assistant turns since last clear → eager clear
IDLE_MIN_TURNS = 30         # skip IDLE fire unless this many assistant turns accumulated
                            # since last clear — post-scrub /start bootstrap alone
                            # produces ~10-20 turns; firing IDLE off a mere
                            # post-bootstrap lull cycles /clear forever on sessions
                            # that are just waiting for their next TG message.
TAIL_SCAN = 200             # lines from end to check for pending tool_use
PRE_CLEAR_WARN_SEC = 15     # grace window between warning prompt and /clear
                            # so Claude can persist in-flight state to disk
POST_CLEAR_WAIT = 3         # seconds for /clear to settle before scrub prompt (capture-verify + retries cover Ink race)
SCRUB_VERIFY_TRIES = 4      # retries if paste didn't land in input
SCRUB_VERIFY_GAP = 3        # seconds between verify retries
RESOLVE_ALERT_SEC = 300     # consecutive resolve failures before Telegram escalation
LOG_DEDUP_SEC = 60          # collapse identical consecutive log lines for this long

STUCK_PATTERNS = re.compile(
    r"(unable to respond to this request|appears to violate our Usage Policy|Usage Policy)",
    re.IGNORECASE,
)

PRE_CLEAR_PROMPT = (
    "WATCHDOG NOTICE: a /clear will fire in 15 seconds. "
    "Persist any in-flight conversational state to disk NOW: "
    "pending requests, messages-to-relay, partial work, "
    "new standing orders, lessons learned. "
    "Use memory/ for long-term, activity logs for ephemera. "
    "Do not reply on the bridge unless mid-sentence. Write only."
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


def _descendant_pids(pid: str) -> list[str]:
    """Return pid and all its descendants via `pgrep -P`. Works on
    Linux/macOS/FreeBSD; returns just [pid] if pgrep isn't available."""
    out = [pid]
    try:
        kids = subprocess.check_output(["pgrep", "-P", pid], text=True).split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return out
    for k in kids:
        out.extend(_descendant_pids(k))
    return out


def resolve_claude_pane() -> str | None:
    """Scan ALL tmux panes (session-agnostic) and pick the one running a
    claude session. Two strategies, tried in order:

    1. Fast path — match `pane_current_command == "claude"` AND
       `pane_current_path == CONSUMER_DIR`. This is what Linux + macOS
       typically report: the claude binary sets its own argv[0] (so tmux
       sees "claude" not "node"), and `pane_current_path` is populated
       from /proc/<pid>/cwd.

    2. Descendant walk — when the fast path returns zero matches (most
       commonly on FreeBSD, where tmux reports `pane_current_command=node`
       for node-launched CLIs and leaves `pane_current_path=""`), scan
       each pane's process tree with `pgrep -P` and grep the full
       `ps -o command=` output of every descendant for the literal
       string "claude". If any descendant matches, claim that pane.

    We used to target `-t TMUX_WINDOW`, but sessions get renamed/recreated
    between reboots and hardcoded targets break silently. `-a` scans all
    sessions; the cmd/descendant match keeps it unambiguous.
    """
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"],
            text=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"tmux list-panes -a failed: {e}")
        return None

    rows: list[tuple[str, str, str, str]] = []
    for line in out.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 4:
            rows.append((parts[0], parts[1], parts[2], parts[3]))

    # 1. Fast path: cmd=claude AND cwd=CONSUMER_DIR
    fast_matches: list[str] = []
    for pane_id, _pane_pid, cmd, cwd in rows:
        if cmd != "claude":
            continue
        try:
            if Path(cwd).resolve() == CONSUMER_DIR:
                fast_matches.append(pane_id)
        except OSError:
            continue
    if fast_matches:
        if len(fast_matches) > 1:
            log(f"multiple candidate panes {fast_matches} — picking first")
        return fast_matches[0]

    # 2. Fallback: walk each pane's descendants, grep ps COMMAND for "claude"
    slow_matches: list[str] = []
    for pane_id, pane_pid, _cmd, _cwd in rows:
        pids = _descendant_pids(pane_pid)
        try:
            ps_out = subprocess.check_output(
                ["ps", "-o", "command=", "-p", ",".join(pids)],
                text=True, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            continue
        if any("claude" in ln.lower() for ln in ps_out.splitlines()):
            slow_matches.append(pane_id)
    if slow_matches:
        if len(slow_matches) > 1:
            log(f"multiple descendant-match panes {slow_matches} — picking first")
        return slow_matches[0]

    log(f"no pane matched — fast path (cmd=claude cwd={CONSUMER_DIR}) "
        "nor descendant-walk found a claude process")
    return None


def inject_clear(pane: str) -> bool:
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "/clear", "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux send-keys failed on {pane}: {e}")
        return False


def inject_pre_clear_warning(pane: str) -> bool:
    """Best-effort: tell Claude a /clear is imminent so it can persist
    in-flight state to disk before the buffer is wiped. No verify — if
    the keys don't land, /clear still fires; we lose the warning, not
    the system. send-keys -l + Enter, same pattern as inject_scrub but
    without the capture-pane retry loop (a missed warning is less bad
    than a missed scrub: the scrub keeps the activity-log trim chain
    going, the warning is opportunistic)."""
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "-l", PRE_CLEAR_PROMPT]
        )
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"pre-clear warning send-keys failed on {pane}: {e}")
        return False


def _capture_pane(pane: str) -> str:
    try:
        return subprocess.check_output(
            ["tmux", "capture-pane", "-p", "-t", pane, "-S", "-40"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return ""


def inject_scrub(pane: str, prompt: str) -> bool:
    """After /clear settles, type the scrub prompt into CC's input and submit.

    Previously used `tmux paste-buffer` + Enter — looked atomic but lost
    chars: on the Pi, /clear's Ink/React rerender still ran past
    POST_CLEAR_WAIT and swallowed the paste, so only the Enter registered
    (empty submit). New approach:

    1. Sleep POST_CLEAR_WAIT for /clear to settle.
    2. Type the prompt with `send-keys -l` (literal chars, no key-name
       interpretation) — one syscall per type.
    3. Verify the chars actually showed up in the pane via capture-pane;
       retry SCRUB_VERIFY_TRIES times if not.
    4. Send Enter only after verification.
    """
    time.sleep(POST_CLEAR_WAIT)
    # First N chars we expect to find on-screen. Strip whitespace so literal
    # leading/trailing newlines don't confuse the capture match.
    needle = prompt.strip().splitlines()[0][:40]
    landed = False
    for attempt in range(1, SCRUB_VERIFY_TRIES + 1):
        try:
            subprocess.check_call(
                ["tmux", "send-keys", "-t", pane, "-l", prompt]
            )
        except subprocess.CalledProcessError as e:
            log(f"tmux send-keys -l (scrub) failed on {pane} attempt {attempt}: {e}")
            time.sleep(SCRUB_VERIFY_GAP)
            continue
        time.sleep(SCRUB_VERIFY_GAP)
        if needle and needle in _capture_pane(pane):
            landed = True
            break
        log(f"scrub paste not yet visible on {pane} (attempt {attempt}/{SCRUB_VERIFY_TRIES})")
    if not landed:
        log(f"scrub paste never landed on {pane} after {SCRUB_VERIFY_TRIES} tries — giving up")
        return False
    try:
        subprocess.check_call(
            ["tmux", "send-keys", "-t", pane, "Enter"]
        )
        return True
    except subprocess.CalledProcessError as e:
        log(f"tmux send-keys Enter (scrub submit) failed on {pane}: {e}")
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

# Set by SIGUSR1 handler to force a /clear on the next main-loop tick.
# Use case: manual test of the scrub flow without waiting for idle/turn-cap.
#   systemctl --user kill -s SIGUSR1 claude-noema-aup-watchdog.service
_manual_fire = False


def _handle_sigusr1(_signum, _frame) -> None:
    global _manual_fire
    _manual_fire = True
    _emit("SIGUSR1 received — will fire /clear + scrub on next tick")


def fire_clear(
    reason: str,
    *,
    force: bool = False,
) -> bool:
    """Inject /clear + scrub into the claude pane. Callers gate on their own
    trigger conditions (AUP / TURNS / IDLE / SIGUSR1); this function is the
    bare mechanism. `force=True` is reserved for SIGUSR1 (currently only
    semantic, kept as an explicit operator-intent signal)."""
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
    log(f"{reason} → pre-clear warning into {pane}, sleeping {PRE_CLEAR_WARN_SEC}s")
    inject_pre_clear_warning(pane)
    time.sleep(PRE_CLEAR_WARN_SEC)
    log(f"{reason} → injecting /clear into {pane}")
    if not inject_clear(pane):
        return False
    scrub = load_scrub_prompt()
    if scrub and inject_scrub(pane, scrub):
        log(f"scrub prompt injected into {pane}")
    return True


def main() -> int:
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

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

            # --- Manual trigger: SIGUSR1 forces a /clear + scrub now ---
            # Bypasses debounce and AUP/turns/idle logic — for testing the
            # scrub flow on demand. `systemctl --user kill -s SIGUSR1 …`.
            global _manual_fire
            if _manual_fire:
                _manual_fire = False
                if fire_clear("MANUAL SIGUSR1", force=True):
                    last_fire = now
                    turns_since_clear = 0
                    fired_this_tick = True

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
            # Gated on IDLE_MIN_TURNS so a post-scrub /start bootstrap lull
            # doesn't keep cycling /clear on sessions that are legitimately
            # waiting for their next user (TG / IRC / CLI) message. The
            # bootstrap skill alone produces ~10-20 assistant turns; demand
            # real ongoing work before IDLE is allowed to fire.
            if not fired_this_tick:
                age = now - current_file.stat().st_mtime
                boot_age = now - boot_ts
                if (
                    age >= IDLE_SEC
                    and boot_age >= IDLE_SEC
                    and now - last_fire >= DEBOUNCE_SEC
                    and turns_since_clear >= IDLE_MIN_TURNS
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
