#!/usr/bin/env python3
"""
claude-tgbot — Telegram bridge for a Claude Code session.

Mirrors claude-ircbot: auth-gated inbound events to stdout, FIFO inbox for
outbound actions. No LLM-layer trust — if an event reaches stdout, the user
and chat are already in the allowlist.

Events emitted to stdout (one line each, JSON-quoted text fields):
  READY
  FIFO_READY <path>
  MSG <chat_id> <msg_id> <json_text> user=<username>
  DOCUMENT <chat_id> <msg_id> <path> user=<username>
  PHOTO <chat_id> <msg_id> <path> user=<username>
  COMMAND <chat_id> <msg_id> <cmd> args=<json_args> user=<username>
  CALLBACK <chat_id> <msg_id> <json_data> user=<username>
  IDENTIFY chat_id=<n> user_id=<n> username=<s>
  DENIED <kind> user_id=<n> chat_id=<n> username=<s>
  MARKUP_ERROR <chat_id> <path_or_dash> <json_detail>
  ERROR <stage> <detail>

FIFO verbs (one per line written to bot.send):
  SAY <chat_id> <text>
  SAYFILE <chat_id> <path>
  SAYHTML <chat_id> <html>
  SAYFILEHTML <chat_id> <path>
  REPLY <chat_id> <msg_id> <text>
  REPLYFILE <chat_id> <msg_id> <path>
  REPLYHTML <chat_id> <msg_id> <html>
  REPLYFILEHTML <chat_id> <msg_id> <path>
  TYPING <chat_id>
  DOCUMENT <chat_id> <path> [caption]
  PHOTO <chat_id> <path> [caption]
  EDIT <chat_id> <msg_id> <text>
  KEYBOARD <chat_id> <reply_to_msg_id> <json_payload>
  BAN <chat_id> <user_id>
  QUIT

Text bodies (SAY/REPLY/EDIT, plus DOCUMENT/PHOTO captions) accept two-char
escapes — \\n → newline, \\t → tab, \\\\ → backslash — so senders can emit
multi-line messages despite the FIFO being line-delimited.

SAYFILE / REPLYFILE bypass escaping entirely: the sender writes a UTF-8
text file to any path and points the verb at it. The bot reads the file
raw (newlines preserved), sends the content as a Telegram text message
(auto-chunked), and unlinks the file on successful send. Preferred for
outbound prose, which doesn't survive brittle shell quoting.

HTML variants (SAYHTML / SAYFILEHTML / REPLYHTML / REPLYFILEHTML) send
with parse_mode=HTML. Telegram's HTML subset: <b>, <i>, <u>, <s>, <code>,
<pre>, <a href>, <blockquote>, <tg-spoiler>. Callers must escape literal
<, >, & as &lt; &gt; &amp;. If Telegram rejects the body as unparseable,
the bot emits MARKUP_ERROR (not generic ERROR) and — for file verbs —
keeps the file on disk so the sender can rewrite it and resubmit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Consumer owns state. Bridge is pure code — no .env, FIFO, logs, or inbox in
# the bridge repo. The systemd unit (shipped by the consumer) sets
# CLAUDE_TGBOT_CONSUMER_DIR and WorkingDirectory so everything lives alongside
# the driven project.
_consumer_dir = os.environ.get("CLAUDE_TGBOT_CONSUMER_DIR")
if not _consumer_dir:
    print(
        "ERROR missing-consumer-dir (set CLAUDE_TGBOT_CONSUMER_DIR=/path/to/consumer)",
        file=sys.stderr,
    )
    sys.exit(2)
CONSUMER_DIR = Path(_consumer_dir).resolve()
ENV_FILE = CONSUMER_DIR / ".env"
LOG_FILE = CONSUMER_DIR / "bot.log"
FIFO = CONSUMER_DIR / "bot.send"
INBOX = CONSUMER_DIR / "inbox"

MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB cap on Telegram attachments.

# /start is the only command the bot interprets directly (identity bootstrap).
# Every other slash-command flows through as a COMMAND event — the agent owns
# the vocabulary, so adding a new command never requires a bot change.
RESERVED_COMMANDS = {"start"}


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
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


ENV = load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""


def _parse_ids(spec: str) -> set[int]:
    out: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


ADMIN_USER_IDS = _parse_ids(ENV.get("TELEGRAM_ADMIN_USER_IDS", ""))
PUBLISHER_USER_IDS = _parse_ids(ENV.get("TELEGRAM_PUBLISHER_USER_IDS", ""))
ALLOWED_USER_IDS = ADMIN_USER_IDS | PUBLISHER_USER_IDS  # union = can reach the agent
# Private chats have chat_id == user_id on Telegram, so an allowed user DMing
# the bot (for /start bootstrap, for receiving permission-prompt DMs) is by
# definition in a chat the agent should accept. Group chat_ids still come from
# .env explicitly.
ALLOWED_CHAT_IDS = _parse_ids(ENV.get("TELEGRAM_ALLOWED_CHAT_IDS", "")) | ALLOWED_USER_IDS


def _role_of(user_id: int) -> str:
    if user_id in ADMIN_USER_IDS:
        return "admin"
    if user_id in PUBLISHER_USER_IDS:
        return "publisher"
    return "none"


def emit(kind: str, *parts: object) -> None:
    """Single-line structured event to stdout (consumed by Monitor)."""
    print(kind + (" " + " ".join(str(p) for p in parts) if parts else ""), flush=True)


def log(direction: str, line: str) -> None:
    """Append-only raw log. direction: '<' inbound, '>' outbound, '*' internal."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {direction} {line}\n")
    except Exception:
        pass


def _username_of(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "?"
    return u.username or (u.full_name.replace(" ", "_") if u.full_name else str(u.id))


def _tail(update: Update) -> str:
    """Common trailing fields for emitted events: user + role."""
    u = update.effective_user
    role = _role_of(u.id) if u else "none"
    return f"user={_username_of(update)} role={role}"


def _one_line(s: str | None) -> str:
    """JSON-encode so newlines/quotes survive single-line emission."""
    return json.dumps(s if s is not None else "", ensure_ascii=False)


def _authorize(update: Update) -> tuple[bool, str]:
    """Gate every inbound update. Returns (ok, reason-if-not).

    Empty allowlist = deny-all. Operator must bootstrap ids via /start DM
    before anyone other than /start goes through.
    """
    u = update.effective_user
    c = update.effective_chat
    if not u or not c:
        return False, "no-user-or-chat"
    if not ALLOWED_USER_IDS or u.id not in ALLOWED_USER_IDS:
        return False, "user-not-allowed"
    if not ALLOWED_CHAT_IDS or c.id not in ALLOWED_CHAT_IDS:
        return False, "chat-not-allowed"
    return True, "ok"


async def _deny(update: Update, kind: str, reply_text: str | None) -> None:
    u = update.effective_user
    c = update.effective_chat
    uid = u.id if u else 0
    cid = c.id if c else 0
    uname = _username_of(update)
    emit("DENIED", kind, f"user_id={uid}", f"chat_id={cid}", f"username={uname}")
    log("*", f"DENIED {kind} user_id={uid} chat_id={cid} username={uname}")
    if reply_text and update.effective_message:
        try:
            await update.effective_message.reply_text(reply_text)
        except Exception as e:
            emit("ERROR", "deny-reply", repr(e))


# ---- Inbound handlers ---------------------------------------------------


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — identity bootstrap. Always responsive: if unknown, print ids."""
    u = update.effective_user
    c = update.effective_chat
    if not u or not c or not update.effective_message:
        return
    uid = u.id
    cid = c.id
    uname = _username_of(update)
    role = _role_of(uid)
    ok, _reason = _authorize(update)
    emit("IDENTIFY", f"chat_id={cid}", f"user_id={uid}", f"username={uname}", f"role={role}")
    log("<", f"/start from user_id={uid} chat_id={cid} username={uname} role={role} authorized={ok}")
    if ok:
        await update.effective_message.reply_text(
            f"bridge online. your user_id={uid}, chat_id={cid}, role={role}."
        )
    else:
        await update.effective_message.reply_text(
            f"your user_id is {uid}, chat_id is {cid}. not authorized — ask operator to allow."
        )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok, reason = _authorize(update)
    if not ok:
        await _deny(update, "MSG", "not authorized")
        return
    m = update.effective_message
    if not m:
        return
    chat_id = update.effective_chat.id
    msg_id = m.message_id
    text = m.text or m.caption or ""
    emit("MSG", chat_id, msg_id, _one_line(text), _tail(update))
    log("<", f"MSG chat={chat_id} msg={msg_id} user={_username_of(update)} text={text[:200]!r}")


async def _download_to_inbox(file_id: str, suggested_name: str, bot) -> Path:
    INBOX.mkdir(exist_ok=True)
    ts = int(time.time())
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in suggested_name) or "blob"
    dest = INBOX / f"{ts}_{safe}"
    f = await bot.get_file(file_id)
    await f.download_to_drive(custom_path=str(dest))
    return dest


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok, _ = _authorize(update)
    if not ok:
        await _deny(update, "DOCUMENT", "not authorized")
        return
    m = update.effective_message
    if not m or not m.document:
        return
    doc = m.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await m.reply_text(f"file too big (> {MAX_FILE_BYTES // (1024*1024)} MB), rejected")
        emit("ERROR", "document-too-big", f"size={doc.file_size}")
        return
    try:
        path = await _download_to_inbox(doc.file_id, doc.file_name or "doc.bin", context.bot)
    except Exception as e:
        emit("ERROR", "document-download", repr(e))
        return
    emit("DOCUMENT", update.effective_chat.id, m.message_id, str(path), _tail(update))
    log("<", f"DOCUMENT chat={update.effective_chat.id} msg={m.message_id} path={path}")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok, _ = _authorize(update)
    if not ok:
        await _deny(update, "PHOTO", "not authorized")
        return
    m = update.effective_message
    if not m or not m.photo:
        return
    photo = m.photo[-1]  # highest-res variant
    if photo.file_size and photo.file_size > MAX_FILE_BYTES:
        await m.reply_text(f"photo too big (> {MAX_FILE_BYTES // (1024*1024)} MB), rejected")
        emit("ERROR", "photo-too-big", f"size={photo.file_size}")
        return
    try:
        path = await _download_to_inbox(photo.file_id, f"photo_{photo.file_unique_id}.jpg", context.bot)
    except Exception as e:
        emit("ERROR", "photo-download", repr(e))
        return
    emit("PHOTO", update.effective_chat.id, m.message_id, str(path), _tail(update))
    log("<", f"PHOTO chat={update.effective_chat.id} msg={m.message_id} path={path}")


def _parse_command(text: str) -> tuple[str, str]:
    """Extract the command name + raw argument string from a slash-command
    message body. Strips `/`, handles Telegram's optional `@botname` suffix
    (e.g. `/draft@my_bot args`), returns (name, args). name is lowercased."""
    body = (text or "").lstrip()
    if not body.startswith("/"):
        return "", body
    first, _, rest = body[1:].partition(" ")
    name, _, _suffix = first.partition("@")
    return name.lower(), rest.strip()


async def on_any_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for every /slash command except /start.

    The bot is a transport: the agent defines the vocabulary. Adding a new
    consumer-side command never requires editing the bot.
    """
    ok, _ = _authorize(update)
    if not ok:
        await _deny(update, "COMMAND", "not authorized")
        return
    m = update.effective_message
    if not m:
        return
    name, args = _parse_command(m.text or m.caption or "")
    if not name or name in RESERVED_COMMANDS:
        return  # /start is handled by its dedicated handler
    emit(
        "COMMAND",
        update.effective_chat.id,
        m.message_id,
        name,
        f"args={_one_line(args)}",
        _tail(update),
    )
    log("<", f"COMMAND /{name} chat={update.effective_chat.id} args={args!r}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok, _ = _authorize(update)
    q = update.callback_query
    if not q:
        return
    if not ok:
        try:
            await q.answer("not authorized")
        except Exception:
            pass
        await _deny(update, "CALLBACK", None)
        return
    try:
        await q.answer()
    except Exception:
        pass
    chat_id = q.message.chat.id if q.message else 0
    msg_id = q.message.message_id if q.message else 0
    emit("CALLBACK", chat_id, msg_id, _one_line(q.data or ""), _tail(update))
    log("<", f"CALLBACK chat={chat_id} msg={msg_id} data={q.data!r}")


# ---- FIFO outbox --------------------------------------------------------


def _split(s: str, n: int) -> list[str]:
    parts = s.split(" ", n)
    return parts


def _unescape_body(text: str) -> str:
    r"""Decode \n / \t / \\ in FIFO text bodies.

    FIFO lines are newline-delimited, so a literal LF in a SAY/REPLY body
    splits the command. Senders encode newlines as the two-char sequence \n
    and this restores them. Tabs via \t, literal backslash via \\.
    """
    SENTINEL = "\x00ESC_BACKSLASH\x00"
    return (text
            .replace("\\\\", SENTINEL)
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace(SENTINEL, "\\"))


def _chunk_text(text: str, limit: int = 4000) -> list[str]:
    """Telegram caps at 4096; leave headroom. Prefer newline boundaries."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < int(limit * 0.5):
            cut = remaining.rfind(" ", 0, limit)
        if cut < int(limit * 0.5):
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


async def _send_chunked(
    bot,
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    parse_mode: str | None = None,
) -> None:
    """Send a (possibly long) text body as one or more Telegram messages.

    Only the first chunk carries reply_to_message_id so the whole thread
    anchors to the original message without each chunk quoting it.
    """
    first = True
    for chunk in _chunk_text(text):
        kw: dict = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            kw["parse_mode"] = parse_mode
        if first and reply_to:
            kw["reply_to_message_id"] = reply_to
        first = False
        await bot.send_message(**kw)


def _is_markup_error(exc: BaseException) -> bool:
    """Telegram returns BadRequest on invalid HTML markup. Match on message
    text because BadRequest is also raised for unrelated issues (chat not
    found, etc.) which should flow to the generic ERROR path instead."""
    if not isinstance(exc, BadRequest):
        return False
    msg = str(exc).lower()
    return "parse" in msg or "entities" in msg or "tag" in msg


def _parse_keyboard_payload(payload: str) -> InlineKeyboardMarkup | None:
    """Payload is JSON: [[{'text':..., 'callback_data':...}, ...], ...]."""
    try:
        data = json.loads(payload)
        rows = []
        for row in data:
            buttons = []
            for btn in row:
                kwargs = {"text": btn["text"]}
                for k in ("callback_data", "url", "switch_inline_query"):
                    if k in btn:
                        kwargs[k] = btn[k]
                buttons.append(InlineKeyboardButton(**kwargs))
            rows.append(buttons)
        return InlineKeyboardMarkup(rows)
    except Exception as e:
        emit("ERROR", "keyboard-parse", repr(e))
        return None


async def process_cmd(app: Application, line: str) -> None:
    line = line.rstrip("\r\n")
    if not line:
        return
    if " " in line:
        verb, rest = line.split(" ", 1)
    else:
        verb, rest = line, ""
    verb = verb.upper()
    log(">", f"{verb} {rest[:200]}")
    bot = app.bot
    try:
        if verb == "SAY":
            target, text = _split(rest, 1)
            await _send_chunked(bot, int(target), _unescape_body(text))
        elif verb == "SAYFILE":
            target, path = _split(rest, 1)
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            await _send_chunked(bot, int(target), text)
            try:
                os.unlink(path)
            except OSError:
                pass
        elif verb == "SAYHTML":
            target, text = _split(rest, 1)
            try:
                await _send_chunked(
                    bot, int(target), _unescape_body(text), parse_mode=ParseMode.HTML
                )
            except BadRequest as e:
                if _is_markup_error(e):
                    emit("MARKUP_ERROR", int(target), "-", _one_line(str(e)))
                    return
                raise
        elif verb == "SAYFILEHTML":
            target, path = _split(rest, 1)
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            try:
                await _send_chunked(bot, int(target), text, parse_mode=ParseMode.HTML)
            except BadRequest as e:
                if _is_markup_error(e):
                    # Keep file on disk so sender can rewrite and resubmit.
                    emit("MARKUP_ERROR", int(target), path, _one_line(str(e)))
                    return
                raise
            try:
                os.unlink(path)
            except OSError:
                pass
        elif verb == "REPLY":
            target, msg_id, text = _split(rest, 2)
            await _send_chunked(
                bot, int(target), _unescape_body(text), reply_to=int(msg_id)
            )
        elif verb == "REPLYFILE":
            target, msg_id, path = _split(rest, 2)
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            await _send_chunked(bot, int(target), text, reply_to=int(msg_id))
            try:
                os.unlink(path)
            except OSError:
                pass
        elif verb == "REPLYHTML":
            target, msg_id, text = _split(rest, 2)
            try:
                await _send_chunked(
                    bot,
                    int(target),
                    _unescape_body(text),
                    reply_to=int(msg_id),
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest as e:
                if _is_markup_error(e):
                    emit("MARKUP_ERROR", int(target), "-", _one_line(str(e)))
                    return
                raise
        elif verb == "REPLYFILEHTML":
            target, msg_id, path = _split(rest, 2)
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            try:
                await _send_chunked(
                    bot,
                    int(target),
                    text,
                    reply_to=int(msg_id),
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest as e:
                if _is_markup_error(e):
                    emit("MARKUP_ERROR", int(target), path, _one_line(str(e)))
                    return
                raise
            try:
                os.unlink(path)
            except OSError:
                pass
        elif verb == "TYPING":
            target = rest.strip()
            await bot.send_chat_action(chat_id=int(target), action=ChatAction.TYPING)
        elif verb == "DOCUMENT":
            parts = _split(rest, 2)
            target, path = parts[0], parts[1]
            caption = _unescape_body(parts[2]) if len(parts) > 2 else None
            with open(path, "rb") as fh:
                await bot.send_document(chat_id=int(target), document=fh, caption=caption)
        elif verb == "PHOTO":
            parts = _split(rest, 2)
            target, path = parts[0], parts[1]
            caption = _unescape_body(parts[2]) if len(parts) > 2 else None
            with open(path, "rb") as fh:
                await bot.send_photo(chat_id=int(target), photo=fh, caption=caption)
        elif verb == "EDIT":
            target, msg_id, text = _split(rest, 2)
            await bot.edit_message_text(chat_id=int(target), message_id=int(msg_id), text=_unescape_body(text))
        elif verb == "KEYBOARD":
            target, msg_id_or_zero, payload = _split(rest, 2)
            markup = _parse_keyboard_payload(payload)
            if markup is None:
                return
            reply_to = int(msg_id_or_zero)
            kw = {"chat_id": int(target), "text": "⬇", "reply_markup": markup}
            if reply_to:
                kw["reply_to_message_id"] = reply_to
            await bot.send_message(**kw)
        elif verb == "BAN":
            target, user_id = _split(rest, 1)
            await bot.ban_chat_member(chat_id=int(target), user_id=int(user_id))
        elif verb == "QUIT":
            emit("QUIT", rest or "bye")
            log("*", f"QUIT {rest}")
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            emit("ERROR", "unknown-verb", verb)
    except Exception as e:
        emit("ERROR", f"{verb.lower()}-fail", repr(e))


def _ensure_fifo() -> None:
    if FIFO.exists() and not FIFO.is_fifo():
        FIFO.unlink()
    if not FIFO.exists():
        os.mkfifo(str(FIFO), 0o600)


def attach_fifo_reader(app: Application, loop: asyncio.AbstractEventLoop) -> None:
    """Async-native FIFO reader via loop.add_reader(fd).

    Opens FIFO O_RDWR|O_NONBLOCK so the kernel never surfaces EOF when the
    last external writer closes — one persistent fd keeps the pipe hot, and
    reads simply return EAGAIN while it's empty. Lines are drained from a
    byte buffer and dispatched as tasks; no thread, no blocking.
    """
    _ensure_fifo()
    fd = os.open(str(FIFO), os.O_RDWR | os.O_NONBLOCK)
    buf = bytearray()

    def _on_readable() -> None:
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                return
            except OSError as e:
                emit("ERROR", "fifo-read", repr(e))
                return
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                raw, _, _ = bytes(buf[:nl]), None, None
                del buf[: nl + 1]
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                if not line:
                    continue
                loop.create_task(process_cmd(app, line))

    loop.add_reader(fd, _on_readable)
    emit("FIFO_READY", str(FIFO))
    log("*", f"FIFO_READY {FIFO}")


# ---- App bootstrap ------------------------------------------------------


async def _post_init(app: Application) -> None:
    loop = asyncio.get_running_loop()
    attach_fifo_reader(app, loop)
    emit("READY")
    log("*", "READY")


def main() -> int:
    if not TOKEN:
        print("ERROR missing-token (set TELEGRAM_BOT_TOKEN in .env)", file=sys.stderr)
        return 2
    # PTB logs via stdlib logging; push to stderr to keep stdout = event stream only.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    INBOX.mkdir(exist_ok=True)
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(MessageHandler(filters.COMMAND, on_any_command))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))

    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            stop_signals=(signal.SIGINT, signal.SIGTERM),
        )
    except Exception as e:
        emit("FATAL", repr(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
