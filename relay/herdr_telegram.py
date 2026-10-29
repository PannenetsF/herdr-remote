#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["python-telegram-bot>=21.0", "websockets>=14.0"]
# ///
"""herdr-remote Telegram bot — monitor and approve agents from Telegram."""
import asyncio, hashlib, json, os, logging, secrets, time
import re
import unicodedata
from collections import Counter, OrderedDict

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    ForceReply,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters

from agent_state import apply_agent_message

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO; that URL contains the bot token. Silence it.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("herdr-tg")

TOKEN = os.environ.get("HERDR_TG_TOKEN", "")
CHAT_ID = os.environ.get("HERDR_TG_CHAT_ID", "")
RELAY_WS = os.environ.get("HERDR_RELAY", "ws://127.0.0.1:8375")
RELAY_WS_SAFE = RELAY_WS.split("?", 1)[0]  # token-free variant for display and logging; never leak the token
_RELAY_TOKEN = RELAY_WS.split("token=", 1)[1] if "token=" in RELAY_WS else ""

# Small machine-local JSON (dashboard message id, per-pane message map, recent
# completion times) so a launchd restart edits the same dashboard instead of
# posting a new one. Never contains message content or secrets; overwritten
# atomically with mode 0600.
STATE_FILE = os.environ.get(
    "HERDR_TG_STATE",
    os.path.expanduser("~/.config/herdr-remote/telegram-state.json"),
)


def scrub(value) -> str:
    """Strip secrets from any string before it is logged or sent to Telegram.

    WebSocket exceptions (e.g. InvalidURI) embed the full relay URL incl. the
    ?token= query, so raw exception text must never be surfaced unredacted.
    """
    s = str(value)
    for secret in (_RELAY_TOKEN, TOKEN):
        if secret:
            s = s.replace(secret, "<redacted>")
    return s

if not TOKEN:
    print("Set HERDR_TG_TOKEN (from @BotFather)")
    exit(1)

# State
pending: OrderedDict[tuple[int, int], str] = OrderedDict()  # (chat_id, message_id) -> pane_id
approval_tokens: dict[str, str] = {}  # pane_id -> current blocked-notification generation
blocked_prompt_ids: dict[str, str] = {}  # pane_id -> current relay prompt identity
approval_in_flight: set[str] = set()  # panes whose tapped choice is still being delivered
agents: list[dict] = []       # current agent list from relay
prev_statuses: dict[str, str] = {}  # pane_id -> last known status
# One live-view message per pane, edited in place on every /read so repeated reads refresh a
# single message instead of stacking a wall of them. (chat_id, pane_id) -> message_id.
read_views: dict[tuple[int, str], int] = {}
relay_connected = False
daily_stats: dict[str, dict] = {}  # pane_id -> {agent, project, blocked_count, working_mins, last_change}
# pf.2: a busy herd edits a small fixed set of messages instead of flooding the
# chat. pane_messages maps each human pane to its ONE reusable notification
# (blocked alert -> resume one-liner -> finished one-liner); the dashboard is a
# single auto-refreshing overview message.
pane_messages: dict[str, int] = {}   # pane_id -> its blocked/finished notification
pane_seen: dict[str, float] = {}     # pane_id -> last time present in a snapshot
done_at: dict[str, float] = {}       # pane_id -> completion time (dashboard TTL)
dashboard_msg_id: int | None = None
dash = {"dirty": False, "last_push": 0.0, "last_text": None, "last_sig": None}
# pf.3: one sticky focus pane. Plain text goes to it without a Telegram reply;
# recent panes fill the persistent bottom bar for one-tap switching.
active_pane: str | None = None
recent_panes: list[str] = []
bar = {"dirty": True, "last_push": 0.0, "sig": None}
state_dirty = False

AGENT_PAGE_SIZE = 20
RECENT_MAX = 8                    # recent panes persisted in the state file
RECENT_BAR_SLOTS = 3              # recent panes shown on the persistent bar
BAR_MIN_INTERVAL = 1.0            # fastest cadence of silent bar refreshes
# Telegram rejects a body made only of U+200B/U+2800/U+1160/U+3164/nbsp/
# ZWNJ/ZWNBSP ("Text must be non-empty" / MESSAGE_EMPTY); empirically only
# U+2060 WORD JOINER survives the trim while rendering empty. The placeholder
# is deleted immediately anyway.
BAR_PLACEHOLDER = "\u2060"
BAR_SWITCH_LABEL = "📋 切换 session"
BAR_PENDING_LABEL = "🚫 待办"
BAR_PROJECT_LABEL = "📁 项目"
BAR_DASHBOARD_LABEL = "📊 Dashboard"
BAR_UNFOCUS_LABEL = "✖ 取消聚焦"

# pf.10: the typing-"/" menu is scoped to the one authorized private chat and
# described in Chinese to match the bar and the pickers.
COMMAND_MENU = (
    ("switch", "选择 session 并聚焦"),
    ("project", "按项目浏览 session"),
    ("dashboard", "看板"),
    ("agents", "全部 session"),
    ("read", "查看对话"),
    ("trust", "批准并永久信任"),
    ("interrupt", "中断"),
    ("unfocus", "取消聚焦"),
    ("status", "连接状态"),
    ("digest", "今日统计"),
)
PENDING_LIMIT = 500
COMMAND_ACK_TIMEOUT = 20
PANE_READ_TIMEOUT = 20
DASHBOARD_MIN_INTERVAL = 3.0     # fastest cadence of dashboard edits
DASHBOARD_REFRESH_MAX_AGE = 60.0  # identical dashboards still refresh at this cadence (clock/TTL)
DASHBOARD_DONE_TTL = 30 * 60     # finished panes stay on the dashboard this long
DASHBOARD_MAX_LINES = 70         # pane lines before "+N more"
DASHBOARD_MAX_BLOCK_BUTTONS = 30
NOTIF_PROMPT_MAX = 1500          # utf-16 units of blocked prompt kept in the <pre> snippet
PANE_STATE_GRACE = 3600          # vanished panes keep their message map entry this long
STATUS_ORDER = {"blocked": 0, "working": 1, "done": 2, "idle": 3, "unknown": 3}
STATUS_LABELS = {"blocked": "BLOCKED", "working": "WORKING", "done": "DONE", "idle": "IDLE", "unknown": "IDLE"}
ACTION_CODES = {
    "read": "r",
    "interrupt": "i",
    "select_send": "s",
    "select_reply": "q",
    "trust": "t",
    "approval": "k",
    "switch": "u",
}
CODE_ACTIONS = {code: action for action, code in ACTION_CODES.items()}


# --- Relay communication ---

async def await_command_result(ws, request_id: str, command: str):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COMMAND_ACK_TIMEOUT
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RuntimeError(f"relay did not acknowledge {command}")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"relay did not acknowledge {command}") from exc
        response = json.loads(raw)
        response_request_id = response.get("request_id")
        if response.get("type") == "command_result" and response_request_id != request_id:
            continue
        if response_request_id not in (None, request_id):
            continue
        if response.get("type") == "error":
            raise RuntimeError(response.get("message", f"relay rejected {command}"))
        if response.get("type") == "command_result" and response.get("command") == command:
            if not response.get("ok"):
                raise RuntimeError(response.get("message", f"relay rejected {command}"))
            return


async def send_to_relay(pane_id: str, text: str, prompt_id: str | None = None):
    """Send a response to the relay via WebSocket."""
    import websockets
    async with websockets.connect(RELAY_WS) as ws:
        request_id = secrets.token_hex(8)
        await ws.send(json.dumps({
            "type": "respond",
            "pane_id": pane_id,
            "prompt_id": prompt_id if prompt_id is not None else blocked_prompt_ids.get(pane_id, ""),
            "text": text,
            "request_id": request_id,
        }))
        await await_command_result(ws, request_id, "respond")


async def send_keys_to_relay(pane_id: str, keys: list[str], prompt_id: str | None = None):
    """Send raw key presses to the relay via WebSocket (e.g. ["1"] to pick a prompt option)."""
    import websockets
    async with websockets.connect(RELAY_WS) as ws:
        request_id = secrets.token_hex(8)
        message = {
            "type": "send_keys",
            "pane_id": pane_id,
            "keys": keys,
            "request_id": request_id,
        }
        if prompt_id is not None:
            message["prompt_id"] = prompt_id
        await ws.send(json.dumps(message))
        await await_command_result(ws, request_id, "send_keys")


async def read_pane(pane_id: str, lines: int = 15) -> str:
    """Read pane content from relay."""
    import websockets
    try:
        async with websockets.connect(RELAY_WS) as ws:
            await ws.send(json.dumps({"type": "read_pane", "pane_id": pane_id, "lines": lines}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PANE_READ_TIMEOUT
            # The connect preamble (sessions/agents/one-blocked-per-blocked-
            # agent) can put any number of unrelated messages ahead of
            # pane_content. Skip by deadline, not by a fixed message count.
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return "(no response)"
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "pane_content":
                    return msg.get("content", "(empty)")
    except Exception as e:
        return f"(error reading pane: {scrub(e)})"


async def fetch_history(pane_id: str, limit: int | None = None) -> dict:
    """Structured conversation turns from the agent's own transcript via relay.

    The relay parses Claude/Codex/Pi/Kimi JSONL (uuid-keyed, sidechains and
    thinking dropped), so this has none of the TUI chrome a pane capture
    carries -- no spinners, no 'Update available' footer, no input box.
    """
    limit = limit or HISTORY_TURNS
    import websockets
    try:
        async with websockets.connect(RELAY_WS) as ws:
            await ws.send(json.dumps({
                "type": "get_history", "pane_id": pane_id, "limit": limit,
            }))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PANE_READ_TIMEOUT
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return {"messages": [], "unavailable": "timeout"}
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "history" and msg.get("pane_id") == pane_id:
                    return msg
                if msg.get("type") == "error":
                    return {"messages": [], "unavailable": msg.get("message", "error")}
    except Exception as e:
        return {"messages": [], "unavailable": f"error: {scrub(e)}"}


HISTORY_TURNS = 6
HISTORY_TURN_MAX = 2600      # utf-16 units budget for one rendered turn
HISTORY_VIEW_MAX = 3700      # utf-16 units budget for the whole /read body
TG_MSG_MAX = 4096            # Telegram hard cap per message, in utf-16 units
_HISTORY_ROLE_LABELS = {"user": "you", "assistant": "agent", "note": "note"}


def _u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# Display cells, not code units: CJK and emoji draw two cells wide on the
# phone, which is what actually clips an inline button. u16 budgets let
# CJK-heavy titles render at double the button width.
def _cell_len(s: str) -> int:
    width = 0
    for ch in s:
        if unicodedata.combining(ch) or ch == "\ufe0f":
            continue
        if ord(ch) >= 0x1F000 or ch == "\u25b6" or unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def _cell_truncate(s: str, budget: int) -> str:
    """Truncate by display cells; the trailing ellipsis counts in budget."""
    if budget <= 0:
        return ""
    if _cell_len(s) <= budget:
        return s
    out: list[str] = []
    used = 0  # content cells; one more cell is reserved for the ellipsis
    for ch in s:
        if unicodedata.combining(ch) or ch == "\ufe0f":
            step = 0
        elif ord(ch) >= 0x1F000 or ch == "\u25b6" or unicodedata.east_asian_width(ch) in ("W", "F"):
            step = 2
        else:
            step = 1
        if used + step > budget - 1:
            break
        out.append(ch)
        used += step
    kept = "".join(out).rstrip()
    # A title herdr itself ellipsized must not gain a second ellipsis
    # (it renders as "……"); the reserved truncation cell is spent once.
    return kept if kept.endswith("\u2026") else kept + "\u2026"


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Agent markdown -> Telegram HTML ---------------------------------------
#
# Telegram has no Markdown tables and its MarkdownV2 parser rejects most real
# agent output, so conversation text is rendered to Telegram's HTML subset
# (b/i/s/code/pre/a/blockquote). Tables -- the main thing agents use to report
# numbers -- are turned into per-row labelled lines so CJK cells need no column
# alignment and stay readable on a phone.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*\S)\s*$")
_OL_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*\S)\s*$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)\s]+\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_INLINE_CODE_RE = re.compile(r"``([^`\n]+?)``|`([^`\n]+?)`")
_BOLD_STAR_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
# No __bold__: standalone Python dunders in prose would get eaten.
_STRIKE_RE = re.compile(r"~~([^~\n]+?)~~")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w])_([^_\n]+?)_(?![\w])")


def _inline_md(text: str) -> str:
    """Inline markdown -> Telegram HTML. Input is a single line's worth of text."""
    text = _html_escape(text)
    text = _IMAGE_RE.sub(lambda m: m.group(1), text)
    text = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    # Code spans are stashed while the other markers are rewritten, so markup
    # inside code (e.g. **kwargs) stays literal.
    stashed: dict[str, str] = {}

    def _stash(m):
        token = f"\x00c{len(stashed)}\x00"
        stashed[token] = "<code>" + (m.group(1) or m.group(2)) + "</code>"
        return token

    text = _INLINE_CODE_RE.sub(_stash, text)
    text = _BOLD_STAR_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _ITALIC_STAR_RE.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDER_RE.sub(r"<i>\1</i>", text)
    for token, html in stashed.items():
        text = text.replace(token, html)
    return text


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", line)]


def _render_table(header: str, rows: list[str]) -> str:
    """One block per data row: 'first cell' plus 'header: value' metric lines."""
    headers = _split_table_row(header)
    out = []
    for row in rows:
        cells = _split_table_row(row)
        cells += [""] * (len(headers) - len(cells))
        line = f"▪ {_inline_md(cells[0])}" if cells and cells[0] else "▪"
        metrics = []
        for j in range(1, len(cells)):
            value = _inline_md(cells[j])
            if not value:
                continue
            label = _inline_md(headers[j]) if j < len(headers) and headers[j] else ""
            metrics.append(f"{label}: {value}" if label else value)
        if metrics:
            line += "\n　　" + " ｜ ".join(metrics)
        out.append(line)
    return "\n".join(out)


def _starts_block(line: str, nxt: str | None) -> bool:
    if _FENCE_RE.match(line) or _HR_RE.match(line) or _HEADING_RE.match(line):
        return True
    if _QUOTE_RE.match(line) or _UL_RE.match(line) or _OL_RE.match(line):
        return True
    return nxt is not None and "|" in line and _TABLE_SEP_RE.match(nxt) is not None


def _md_blocks(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _FENCE_RE.match(line)
        if m:
            lang = m.group(2)
            i += 1
            buf = []
            while i < n and not _FENCE_CLOSE_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence (or run off the end)
            code = _html_escape("\n".join(buf)).strip("\n")
            inner = (f'<code class="language-{_html_escape(lang)}">{code}</code>'
                     if lang else f"<code>{code}</code>")
            blocks.append(f"<pre>{inner}</pre>")
            continue
        if i + 1 < n and "|" in line and _TABLE_SEP_RE.match(lines[i + 1]):
            header = line
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(lines[i])
                i += 1
            blocks.append(_render_table(header, rows))
            continue
        m = _HEADING_RE.match(line)
        if m:
            blocks.append(f"<b>{_inline_md(m.group(2))}</b>")
            i += 1
            continue
        if _HR_RE.match(line):
            i += 1
            continue
        if _QUOTE_RE.match(line):
            buf = []
            while i < n:
                mq = _QUOTE_RE.match(lines[i])
                if not mq:
                    break
                buf.append(mq.group(1))
                i += 1
            blocks.append("<blockquote>" + _inline_md("\n".join(buf)) + "</blockquote>")
            continue
        if _UL_RE.match(line) or _OL_RE.match(line):
            items = []
            while i < n:
                mu, mo = _UL_RE.match(lines[i]), _OL_RE.match(lines[i])
                if mu:
                    items.append(("•", mu.group(2)))
                    i += 1
                elif mo:
                    items.append((f"{mo.group(2)}.", mo.group(3)))
                    i += 1
                elif lines[i].strip() and items:
                    mark, body = items[-1]
                    items[-1] = (mark, body + " " + lines[i].strip())
                    i += 1
                else:
                    break
            blocks.append("\n".join(f"{mark} {_inline_md(body)}" for mark, body in items))
            continue
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(
            lines[i], lines[i + 1] if i + 1 < n else None
        ):
            buf.append(lines[i])
            i += 1
        blocks.append(_inline_md("\n".join(buf)))
    return blocks


def md_to_html(text: str, max_units: int = HISTORY_TURN_MAX) -> str:
    """Markdown -> balanced Telegram HTML, tail-truncated to fit the budget."""
    blocks = _md_blocks(text)
    for drop in range(len(blocks)):
        candidate = "\n\n".join(blocks[drop:])
        if drop:
            candidate = "<i>(earlier part truncated)</i>\n\n" + candidate
        if _u16len(candidate) <= max_units:
            return candidate
    # One pathological oversized block: degrade to a plain escaped pre tail so
    # the message still sends instead of failing on length or tag balance.
    plain = re.sub(r"[*_`~#>]", "", text)
    while _u16len(plain) > max_units - 80:
        plain = plain[500:]
    nl = plain.find("\n")
    if nl >= 0:
        plain = plain[nl + 1:]
    return "<pre>" + _html_escape(plain.strip()) + "</pre>"


def render_history(payload: dict) -> str:
    """Recent turns as Telegram HTML, newest content always surviving the cap."""
    turns = []
    for turn in payload.get("messages", []):
        role = turn.get("role", "")
        if role == "tool":
            continue
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        label = _HISTORY_ROLE_LABELS.get(role, role or "?")
        turns.append(f"<b>{_html_escape(label)}</b>\n{md_to_html(text)}")
    kept: list[str] = []
    used = 0
    for turn in reversed(turns):
        cost = _u16len(turn) + (2 if kept else 0)
        if kept and used + cost > HISTORY_VIEW_MAX:
            break
        kept.append(turn)
        used += cost
    return "\n\n".join(reversed(kept))


async def read_agent(pane_id: str) -> tuple[str, bool]:
    """(content_html, is_raw_terminal). Transcript first; pane capture as fallback."""
    history = await fetch_history(pane_id)
    if history.get("messages") and not history.get("unavailable"):
        rendered = render_history(history)
        if rendered:
            return rendered, False
    raw = (await read_pane(pane_id))[-3000:]
    reason = _html_escape(str(history.get("unavailable") or "empty"))
    return (
        f"<i>(transcript unavailable: {reason}; showing terminal)</i>\n"
        f"<pre>{_html_escape(raw)}</pre>",
        True,
    )


async def send_text_to_relay(pane_id: str, text: str):
    """Send text to an agent using 'herdr agent prompt' for proper submission."""
    if not text or len(text) > 10000:
        raise ValueError("text must contain 1-10000 characters")
    import websockets
    async with websockets.connect(RELAY_WS) as ws:
        request_id = secrets.token_hex(8)
        await ws.send(json.dumps({
            "type": "agent_prompt",
            "pane_id": pane_id,
            "text": text,
            "request_id": request_id,
        }))
        await await_command_result(ws, request_id, "agent_prompt")


# --- Auth guard ---

def authorized(update: Update) -> bool:
    """Reject messages from unauthorized users."""
    if not CHAT_ID:
        return True  # No restriction if CHAT_ID not set (first-run discovery mode)
    return str(update.effective_chat.id) == CHAT_ID


def register_pending(chat_id: int, message_id: int, pane_id: str):
    key = (int(chat_id), int(message_id))
    pending[key] = pane_id
    pending.move_to_end(key)
    while len(pending) > PENDING_LIMIT:
        pending.popitem(last=False)


def pending_pane(chat_id: int, message_id: int) -> str | None:
    return pending.get((int(chat_id), int(message_id)))


READ_VIEW_MAX = 3500


async def show_pane_view(bot, chat_id: int, message, pane_id: str, header: str,
                         content: str, footer: str | None = None):
    """One live-view message per pane, edited in place on every read.

    Repeated /read of the same agent used to post a fresh message each time, so a few commands
    buried the chat in output to scroll past. Editing the previous view instead keeps one message
    that refreshes -- closer to watching a terminal than reading a log. content arrives as
    pre-capped, balanced Telegram HTML (rendered transcript, or a <pre> raw-capture fallback),
    so it must never be tail-sliced here. Falls back to a new message if the old
    one is gone (deleted, or too old for Telegram to edit).
    """
    text = f"<b>{_html_escape(header)}</b>\n{content}"
    if footer:
        text += f"\n\n<i>{_html_escape(footer)}</i>"
    key = (int(chat_id), pane_id)
    existing = read_views.get(key)
    if existing is not None:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=existing, text=text,
                                        parse_mode="HTML")
            return existing
        except Exception as e:
            # Identical output -> "message is not modified": nothing to do, keep the view as is
            # rather than posting a duplicate. Any other failure means the message is unusable
            # (deleted, or too old to edit), so fall through and post a fresh one.
            if "not modified" in str(e).lower():
                return existing
            read_views.pop(key, None)
    sent = await message.reply_text(text, parse_mode="HTML")
    read_views[key] = sent.message_id
    register_pending(chat_id, sent.message_id, pane_id)
    return sent.message_id



def find_agent(pane_id: str) -> dict | None:
    matches = [agent for agent in agents if agent.get("pane_id") == pane_id]
    return matches[0] if len(matches) == 1 else None


# --- Identity header: project > pane > claude session ---
#
# Every agent on a herdr-projects host reports agent="claude" and often the same
# cwd basename (thread panes live in .../<slug>/threads/t-NNNN), so the stock
# "<agent> blocked in <project>" header cannot name the pane. The pieces below
# rebuild a three-level identity from the agents snapshot:
#   project -- registered project slug, never the t-NNNN thread dir name
#   pane    -- live terminal title plus the stable herdr pane id (wT:p29)
#   session -- the agent's registered herdr name (hp-...-t-NNNN / hpc-...),
#              falling back to the harness kind (claude/codex/...)
_HP_THREAD_CWD_RE = re.compile(r"/([^/]+)/threads/t-\d+/?$")
_HP_WORKTREE_CWD_RE = re.compile(r"/worktrees/[^/]+/hp-([^/]+?)-t-\d+(?:[-/]|$)")
_HP_NAME_RE = re.compile(r"^hpc?-(.+?)(?:-t-\d+)?$")


def effective_project(rec: dict) -> str:
    # herdr-projects' registered slug is authoritative when present.
    hp_project = (rec.get("hp_project") or "").strip()
    if hp_project:
        return hp_project
    cwd = rec.get("cwd") or ""
    m = _HP_THREAD_CWD_RE.search(cwd)
    if m:
        return m.group(1)
    m = _HP_WORKTREE_CWD_RE.search(cwd)
    if m:
        return m.group(1)
    project = (rec.get("project") or "").strip()
    # Relay project is basename(cwd); a bare t-NNNN means we are inside a thread
    # dir whose project slug the relay does not know -- recover it from the
    # registered session name instead.
    if project and not re.fullmatch(r"t-\d+", project):
        return project
    m = _HP_NAME_RE.match((rec.get("label") or "").strip())
    if m:
        return m.group(1)
    return project or "unknown"


def pane_descriptor(rec: dict) -> str:
    pane_id = rec.get("pane_id") or "?"
    title = (rec.get("title") or "").strip()
    return f"{title} ({pane_id})" if title else str(pane_id)


def agent_identity(rec: dict) -> str:
    session = (rec.get("label") or "").strip() or rec.get("agent") or "agent"
    return " > ".join((effective_project(rec), pane_descriptor(rec), session))


def pane_focus_identity(rec: dict) -> str:
    """Identity for a single-pane surface (the focus preview), pane-anchored.

    Semantics differ from agent_identity's three-level notification header:
    when naming ONE conversation, the pane title IS the conversation -- it is
    the 1:1 human-readable topic (live herds put most panes in one project, so
    the slug barely discriminates anything) -- and the pane id is its stable
    key (terminal titles mutate while the pane lives). Project is the coarse
    grouping the selector row already showed and hpc-<slug> merely repeats it,
    so both are dropped from the default, leaving the short runtime tag
    (claude/codex/coord-N). Project survives only when the pane has no title,
    where it is the sole readable cue.
    """
    project = effective_project(rec)
    pane_id = rec.get("pane_id") or "?"
    title = (rec.get("title") or "").strip()
    head = title or project
    session = _short_session(rec, project)
    return f"{head} ({pane_id}) · {session}"


def identity_for_blocked(msg: dict) -> str:
    rec = find_agent(msg.get("pane_id", ""))
    if rec is not None:
        return agent_identity(rec)
    # A blocked message can beat the first agents snapshot after reconnect; use
    # whatever the blocked message itself carries.
    return agent_identity({
        "pane_id": msg.get("pane_id", "?"),
        "project": msg.get("project", ""),
        "agent": msg.get("agent", "agent"),
    })


# --- Human audience: coordinators and manual sessions, never thread workers -
#
# Mirrors config/herdr/portal-policy.py's human projection. With herdr-projects
# tokens the hp_role classification is authoritative: the sidebar's default
# agents view hides hp_role in (guest, other), and the human projection goes one
# step further and also hides thread workers -- their prompts and completions
# belong to the coordinator that owns the thread. The single exception is a
# thread escalated to hp_audience=stalled: a worker the coordinator could not
# resolve past the stall timeout is surfaced precisely so it reaches the human
# (its blocked prompt is one tap to approve). Hosts without herdr-projects
# (older server, a plain remote) send no tokens, so fall back to name/cwd
# markers and fail open rather than hiding a newly started remote session.
_HP_THREAD_NAME_RE = re.compile(r"^hp-.+-t-\d+")
_HP_ROLES_VISIBLE = {"coordinator", "session"}


def pane_human(rec: dict) -> bool:
    role = (rec.get("hp_role") or "").strip()
    if role:
        if role in _HP_ROLES_VISIBLE:
            return True
        return role == "thread" and (rec.get("hp_audience") or "").strip() == "stalled"
    name = (rec.get("label") or "").strip()
    if name.startswith("hpc-"):
        return True
    if _HP_THREAD_NAME_RE.match(name):
        return False
    cwd = rec.get("cwd") or ""
    if _HP_THREAD_CWD_RE.search(cwd) or _HP_WORKTREE_CWD_RE.search(cwd):
        return False
    if re.fullmatch(r"t-\d+", (rec.get("project") or "").strip()):
        return False
    return True


def human_agents(agent_list: list[dict] | None = None) -> list[dict]:
    return [a for a in (agents if agent_list is None else agent_list) if pane_human(a)]


def update_is_human(rec: dict) -> bool:
    """Audience check for a sparse agent_update: prefer the merged snapshot row."""
    full = find_agent(rec.get("pane_id", ""))
    return pane_human(full or rec)


def clear_relay_connection_state():
    global agents, relay_connected
    relay_connected = False
    agents = []
    approval_tokens.clear()
    blocked_prompt_ids.clear()
    mark_dashboard_dirty()
    mark_bar_dirty()


async def send_reply_prompt(message, chat, agent: dict, content: str | None = None):
    # content (when given) is the rendered-transcript HTML from read_agent.
    title = agent_identity(agent)
    parts = [f"<b>{_html_escape(title)}</b>"]
    if content is not None:
        parts.extend(["", content])
    parts.extend(["", "Reply to this message to send your response."])
    kwargs = {"parse_mode": "HTML"}
    if getattr(chat, "type", None) == "private":
        kwargs["reply_markup"] = ForceReply(
            selective=True,
            input_field_placeholder=f"Reply to {title}"[:64],
        )
    sent = await message.reply_text("\n".join(parts), **kwargs)
    register_pending(chat.id, sent.message_id, agent["pane_id"])
    return sent


def agents_for_action(action: str) -> list[dict]:
    base = human_agents()
    if action == "interrupt":
        return [agent for agent in base if agent.get("status") in ("working", "blocked")]
    if action == "trust":
        return [agent for agent in base if agent.get("status") == "blocked"]
    return base


def sorted_agents(agent_list: list[dict]) -> list[dict]:
    return sorted(agent_list, key=lambda agent: (
        STATUS_ORDER.get(agent.get("status", "unknown"), 3),
        agent.get("project", "").lower(),
        agent.get("agent", "").lower(),
        agent.get("host", "local").lower(),
        agent.get("pane_id", ""),
    ))


def pane_callback_token(pane_id: str) -> str:
    return hashlib.sha256(pane_id.encode()).hexdigest()[:16]


def pane_callback_data(action: str, pane_id: str, **extra) -> str:
    data = {"a": ACTION_CODES[action], "p": pane_callback_token(pane_id), **extra}
    return json.dumps(data, separators=(",", ":"))


def resolve_pane_token(token: str) -> str | None:
    agent_matches = [
        agent.get("pane_id") for agent in agents
        if agent.get("pane_id") and pane_callback_token(agent["pane_id"]) == token
    ]
    if len(agent_matches) == 1:
        return agent_matches[0]
    if len(agent_matches) > 1:
        return None
    pending_matches = {
        pane_id for pane_id in pending.values()
        if pane_id and pane_callback_token(pane_id) == token
    }
    return next(iter(pending_matches)) if len(pending_matches) == 1 else None


def parse_callback_data(raw: str) -> dict:
    data = json.loads(raw)
    if "a" in data:
        data["action"] = CODE_ACTIONS.get(data["a"], "invalid")
    if "p" in data:
        data["pane_id"] = resolve_pane_token(data["p"])
    return data


def build_agent_keyboard(action: str, page: int = 0, agent_list: list[dict] | None = None) -> InlineKeyboardMarkup:
    """The full-list picker (behind 📋 All panes and the read/trust/... menus).

    pf.12: rows use the SAME two-line label as /switch -- the terminal title
    owns line 1 and `<project> › <pane_id> · <session> @host` is the identity
    tail on line 2. The old single-line `[STATUS] project (agent) [pane]`
    carried no title at all, so several hand-started `project (claude)` rows
    in one project were indistinguishable.
    """
    ordered = sorted_agents(agents_for_action(action) if agent_list is None else agent_list)
    page_count = max(1, (len(ordered) + AGENT_PAGE_SIZE - 1) // AGENT_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * AGENT_PAGE_SIZE
    visible = ordered[start:start + AGENT_PAGE_SIZE]
    keyboard = [[InlineKeyboardButton(
        _switch_pane_label(agent),
        callback_data=pane_callback_data(action, agent["pane_id"]),
    )] for agent in visible]

    if page_count > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                "Previous",
                callback_data=json.dumps(
                    {"action": "page", "menu": action, "page": page - 1}, separators=(",", ":")
                ),
            ))
        if page + 1 < page_count:
            navigation.append(InlineKeyboardButton(
                "Next",
                callback_data=json.dumps(
                    {"action": "page", "menu": action, "page": page + 1}, separators=(",", ":")
                ),
            ))
        keyboard.append(navigation)
    return InlineKeyboardMarkup(keyboard)


def refresh_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Refresh agents",
            callback_data=json.dumps({"action": "page", "menu": action, "page": 0}, separators=(",", ":")),
        )
    ]])


# --- Live dashboard: one auto-refreshing overview message per chat ----------
#
# With dozens of panes a notification per event is unreadable. Instead one
# message is kept at the top of the chat (pin it once via Telegram's UI) and
# edited in place: panes grouped by project, one status line each, blocked
# panes get a button straight to the transcript. Finished panes stay for
# DASHBOARD_DONE_TTL, idle panes collapse into a per-project count.
STATUS_GLYPH = {"blocked": "🚫", "working": "▶️", "done": "✅", "idle": "💤", "unknown": "💤"}


def _u16_truncate(s: str, limit: int) -> str:
    if _u16len(s) <= limit:
        return s
    encoded = s.encode("utf-16-le")[: (limit - 1) * 2]
    return encoded.decode("utf-16-le", errors="ignore") + "…"


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _pane_line_label(rec: dict) -> str:
    """pane descriptor + registered session, plus host for remote panes."""
    session = (rec.get("label") or "").strip() or rec.get("agent") or "agent"
    host = rec.get("host") or "local"
    suffix = f" @{host}" if host != "local" else ""
    return _u16_truncate(f"{pane_descriptor(rec)} · {session}{suffix}", 150)


def _dashboard_groups(now: float) -> "OrderedDict[str, dict]":
    groups: "OrderedDict[str, dict]" = OrderedDict()

    def bucket(project: str) -> dict:
        return groups.setdefault(project, {"blocked": [], "working": [], "done": [], "idle": 0})

    for rec in human_agents():
        project = effective_project(rec)
        status = rec.get("status", "unknown")
        row = bucket(project)
        if status == "blocked":
            row["blocked"].append(rec)
        elif status == "working":
            row["working"].append(rec)
        elif done_at.get(rec.get("pane_id")) and now - done_at[rec["pane_id"]] <= DASHBOARD_DONE_TTL:
            row["done"].append(rec)
        else:
            row["idle"] += 1

    def group_key(item: tuple[str, dict]) -> tuple:
        project, row = item
        if row["blocked"]:
            rank = 0
        elif row["working"]:
            rank = 1
        elif row["done"]:
            rank = 2
        else:
            rank = 3
        return (rank, project.lower())

    return OrderedDict(sorted(groups.items(), key=group_key))


def render_dashboard(now: float | None = None) -> str:
    now = time.time() if now is None else now
    if not relay_connected:
        return (
            "🖥 <b>herdr dashboard</b>\n\n"
            "Relay disconnected — reconnecting automatically. /status for details."
        )
    groups = _dashboard_groups(now)
    if not groups:
        return "🖥 <b>herdr dashboard</b>\n\nConnected to relay. No human sessions active."

    panes = human_agents()
    n_blocked = sum(1 for a in panes if a.get("status") == "blocked")
    n_working = sum(1 for a in panes if a.get("status") == "working")
    n_done = sum(
        1 for a in panes
        if a.get("status") in ("done", "idle", "unknown")
        and done_at.get(a["pane_id"]) and now - done_at[a["pane_id"]] <= DASHBOARD_DONE_TTL
    )
    summary = " ".join(
        f"{glyph} {n}"
        for n, glyph in ((n_blocked, "🚫"), (n_working, "▶️"), (n_done, "✅"))
        if n
    )
    lines = [
        f"🖥 <b>herdr dashboard</b> · {_hhmm(now)} · {len(panes)} panes "
        f"in {len(groups)} projects" + (f" · {summary}" if summary else "")
    ]

    def pane_line(rec: dict, glyph: str, bold: bool, when: float | None = None) -> str:
        label = _html_escape(_pane_line_label(rec))
        clock = f" · {_hhmm(when)}" if when is not None else ""
        line = f"{glyph} {label}{clock}"
        return f"<b>{line}</b>" if bold else line

    budget_lines = DASHBOARD_MAX_LINES
    hidden = 0
    for project, row in groups.items():
        if budget_lines <= 2:
            hidden += sum(len(row[k]) for k in ("blocked", "working", "done"))
            continue
        lines.append(f"\n<b>{_html_escape(project)}</b>")
        budget_lines -= 1
        blocked = sorted(row["blocked"], key=lambda a: _pane_line_label(a).lower())
        working = sorted(row["working"], key=lambda a: _pane_line_label(a).lower())
        recent = sorted(row["done"], key=lambda a: done_at.get(a["pane_id"], 0), reverse=True)
        for rec in blocked:
            if budget_lines <= 1:
                hidden += 1
                continue
            lines.append(pane_line(rec, "🚫", True))
            budget_lines -= 1
        for rec in working:
            if budget_lines <= 1:
                hidden += 1
                continue
            lines.append(pane_line(rec, "▶️", False))
            budget_lines -= 1
        for rec in recent:
            if budget_lines <= 1:
                hidden += 1
                continue
            lines.append(pane_line(rec, "✅", False, done_at.get(rec["pane_id"])))
            budget_lines -= 1
        if row["idle"]:
            if budget_lines <= 1:
                hidden += row["idle"]
            else:
                lines.append(f"💤 {row['idle']} idle")
                budget_lines -= 1
    if hidden:
        lines.append(f"\n<i>… +{hidden} more panes — /agents for the full list</i>")
    return "\n".join(lines)


def _dashboard_block_button(rec: dict) -> str:
    project = _u16_truncate(effective_project(rec), 24)
    title = (rec.get("title") or "").strip() or rec.get("pane_id") or "?"
    session = (rec.get("label") or "").strip()
    label = f"🚫 {project} · {title}"
    if session:
        label += f" · {session}"
    return _u16_truncate(label, 60)


def build_dashboard_keyboard(now: float | None = None) -> InlineKeyboardMarkup:
    now = time.time() if now is None else now
    blocked = sorted(
        (a for a in human_agents() if a.get("status") == "blocked"),
        key=lambda a: (effective_project(a).lower(), _pane_line_label(a).lower()),
    )
    rows = [
        [InlineKeyboardButton(
            _dashboard_block_button(rec),
            callback_data=pane_callback_data("read", rec["pane_id"]),
        )]
        for rec in blocked[:DASHBOARD_MAX_BLOCK_BUTTONS]
    ]
    rows.append([InlineKeyboardButton(
        "📋 All panes",
        callback_data=json.dumps(
            {"action": "page", "menu": "select_reply", "page": 0}, separators=(",", ":")
        ),
    )])
    return InlineKeyboardMarkup(rows)


def _markup_signature(markup: InlineKeyboardMarkup) -> str:
    return json.dumps(
        [[(btn.text, btn.callback_data) for btn in row] for row in markup.inline_keyboard],
        ensure_ascii=False,
    )


def _text_signature(text: str) -> str:
    # The header clock must not count as a content change, or every snapshot
    # edits the message just to move the timestamp.
    return re.sub(r"· \d\d:\d\d(?= ·|\n)", "· ", text, count=1)


def mark_dashboard_dirty():
    dash["dirty"] = True


async def push_dashboard(bot, chat_id: int | None = None, force: bool = False):
    """Render and edit (or post) the singleton dashboard message."""
    global dashboard_msg_id, state_dirty
    target = int(CHAT_ID) if CHAT_ID else (int(chat_id) if chat_id is not None else None)
    if target is None:
        return
    # Never proactively post a "disconnected" dashboard before the first
    # successful connection; an existing dashboard is still edited to say so.
    if not relay_connected and dashboard_msg_id is None:
        dash["dirty"] = False
        return
    now = time.time()
    text = render_dashboard(now)
    markup = build_dashboard_keyboard(now)
    sig = (_text_signature(text), _markup_signature(markup))
    if (
        not force
        and dashboard_msg_id is not None
        and sig == (dash["last_text"], dash["last_sig"])
        and now - dash["last_push"] < DASHBOARD_REFRESH_MAX_AGE
    ):
        dash["dirty"] = False
        return

    if dashboard_msg_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=target, message_id=dashboard_msg_id, text=text,
                parse_mode="HTML", reply_markup=markup,
            )
        except Exception as e:
            if "not modified" in str(e).lower():
                pass
            else:
                # Deleted by the user or too old to edit: post a fresh one.
                log.info("dashboard message gone (%s), posting a new one", scrub(e))
                dashboard_msg_id = None

    if dashboard_msg_id is None:
        sent = await bot.send_message(
            chat_id=target, text=text, parse_mode="HTML", reply_markup=markup
        )
        dashboard_msg_id = sent.message_id
        state_dirty = True

    dash.update(last_text=sig[0], last_sig=sig[1], last_push=now, dirty=False)


async def repost_dashboard(bot, chat_id: int):
    """Explicit /dashboard: retire the old singleton and post at the bottom.

    Snapshot-driven refresh only ever edits the singleton in place, so the
    message stays wherever it was first posted -- after a day of chat it is
    impossible to find when the user wants to pin it. A manual command is an
    explicit "bring a fresh one here" request: delete the stale copy
    best-effort and let push_dashboard post the new one, which then takes over
    as the singleton future edits track. A deletion failure (too old, already
    gone) leaves the old copy frozen and never blocks the repost.
    """
    global dashboard_msg_id
    old = dashboard_msg_id
    if old is not None:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old)
        except Exception as e:
            log.info("old dashboard %s not deleted (%s); leaving it frozen",
                     old, scrub(e))
        # Forget it regardless of deletion outcome: from now on edits track
        # the freshly posted message, never the frozen leftover.
        dashboard_msg_id = None
    await push_dashboard(bot, chat_id=chat_id, force=True)


async def dashboard_loop(app: Application):
    """Coalesce the snapshot stream into edits at most every DASHBOARD_MIN_INTERVAL."""
    global state_dirty
    last_save = 0.0
    while True:
        await asyncio.sleep(1.0)
        try:
            if dash["dirty"] and time.time() - dash["last_push"] >= DASHBOARD_MIN_INTERVAL:
                await push_dashboard(app.bot)
            if bar["dirty"] and time.time() - bar["last_push"] >= BAR_MIN_INTERVAL:
                await refresh_bar(app.bot)
            if state_dirty and time.time() - last_save >= 15:
                save_state()
                state_dirty = False
                last_save = time.time()
        except Exception as e:
            log.warning("dashboard loop error: %s", scrub(e))


# --- Persistent bottom bar + sticky focus pane (pf.3) -----------------------
#
# A ReplyKeyboardMarkup lives above the input box independently of any message,
# so it is the only Telegram primitive that can hold always-available session
# controls. It is swapped silently: an invisible-placeholder message carrying
# the new markup is sent and deleted at once (the keyboard itself survives the
# delete), signature-gated so snapshots cannot spam the chat. Tapping a key sends its
# label as an ordinary text message; those labels are intercepted in
# handle_text and never forwarded to a pane.

def _bar_short_identity(rec: dict) -> str:
    """project › session, compact enough for a phone-width key.

    Own-project coordinators collapse to "coord" (same rule as /switch rows),
    otherwise the slug is printed twice.
    """
    project = effective_project(rec)
    session = _short_session(rec, project)
    return _u16_truncate(f"{project} › {session}", 34)


def _bar_specs() -> tuple[dict | None, list[tuple[str, dict]]]:
    """Live (active rec, [(short label, rec), ...]) for the bar; dead panes drop."""
    live = {a.get("pane_id"): a for a in human_agents() if a.get("pane_id")}
    active = live.get(active_pane) if active_pane else None

    picked: list[dict] = []
    seen = {active_pane} if active_pane else set()
    for pane_id in recent_panes:
        if pane_id in seen:
            continue
        rec = live.get(pane_id)
        if rec is None:
            continue
        picked.append(rec)
        seen.add(pane_id)
        if len(picked) >= RECENT_BAR_SLOTS:
            break

    # Duplicate project › session names (unnamed harnesses) get the pane id so
    # a label always resolves to exactly one pane.
    bases = ([(active, _bar_short_identity(active))] if active else []) + [
        (rec, _bar_short_identity(rec)) for rec in picked
    ]
    counts = Counter(label for _rec, label in bases)
    labelled = [
        (label if counts[label] == 1 else f"{label} · {rec.get('pane_id')}", rec)
        for rec, label in bases
    ]
    active_item = (labelled[0][0], labelled[0][1]) if active else None
    recent_items = [(label, rec) for label, rec in labelled[1 if active else 0:]]
    return active_item, recent_items


def build_bar_markup() -> ReplyKeyboardMarkup | None:
    if not CHAT_ID:
        return None
    active_item, recent_items = _bar_specs()
    rows: list[list[str]] = []
    if active_item is not None:
        rows.append(["🎯 " + active_item[0]])
    for label, _rec in recent_items:
        rows.append(["🔄 " + label])
    # pf.10: the two drill-down shortcuts are static: 🚫 待办 jumps straight to
    # the blocked picker, 📁 项目 to the project list. Their own row keeps them
    # present even with no active pane and off the crowded three-key row.
    rows.append([BAR_PENDING_LABEL, BAR_PROJECT_LABEL])
    last = [BAR_SWITCH_LABEL, BAR_DASHBOARD_LABEL]
    if active_item is not None:
        last.append(BAR_UNFOCUS_LABEL)
    rows.append(last)
    if active_item is not None:
        placeholder = _u16_truncate("消息发往 " + agent_identity(active_item[1]), 63)
    else:
        placeholder = "先选 session，再直接发消息"
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


def _bar_signature(markup: ReplyKeyboardMarkup | None) -> str:
    if markup is None:
        return ""
    return json.dumps(
        {"k": [[btn.text for btn in row] for row in markup.keyboard],
         "p": markup.input_field_placeholder},
        ensure_ascii=False,
    )


def mark_bar_dirty():
    bar["dirty"] = True


async def refresh_bar(bot, force: bool = False):
    """Swap the persistent keyboard silently via a deleted placeholder message."""
    if not CHAT_ID or bot is None:
        bar["dirty"] = False
        return
    markup = build_bar_markup()
    sig = _bar_signature(markup)
    if not force and sig == bar["sig"]:
        bar["dirty"] = False
        return
    chat_id = int(CHAT_ID)
    try:
        sent = await bot.send_message(chat_id, BAR_PLACEHOLDER, reply_markup=markup)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
        except Exception:
            pass
        bar["sig"] = sig
        bar["last_push"] = time.time()
    except Exception as e:
        log.warning("failed to refresh session bar: %s", scrub(e))
    bar["dirty"] = False


def focus_pane(pane_id: str):
    """Make pane_id the sticky target and promote it in the recent list."""
    global active_pane, state_dirty
    active_pane = pane_id
    if pane_id in recent_panes:
        recent_panes.remove(pane_id)
    recent_panes.insert(0, pane_id)
    del recent_panes[RECENT_MAX:]
    bar["dirty"] = True
    state_dirty = True


def unfocus_pane():
    global active_pane, state_dirty
    if active_pane is not None:
        active_pane = None
        state_dirty = True
    bar["dirty"] = True


def bar_label_pane(text: str) -> tuple[str, str | None]:
    """Classify a tapped bottom-bar label: (kind, pane_id)."""
    active_item, recent_items = _bar_specs()
    active_label = "🎯 " + active_item[0] if active_item else None
    recent_map = {"🔄 " + label: rec.get("pane_id") for label, rec in recent_items}
    if text == BAR_SWITCH_LABEL:
        return "switch", None
    if text == BAR_PENDING_LABEL:
        return "pending", None
    if text == BAR_PROJECT_LABEL:
        return "project", None
    if text == BAR_DASHBOARD_LABEL:
        return "dashboard", None
    if text == BAR_UNFOCUS_LABEL:
        return "unfocus", None
    if active_label and text == active_label:
        return "active", active_pane
    if text in recent_map:
        return "recent", recent_map[text]
    return "", None


# --- /switch: two-line focus selector ---------------------------------------
#
# pf.4: the project lives inside every pane row (a ▸ header row scrolls away on
# a phone), hpc-<own project> coordinator names collapse to "coord" so the row
# is not twice the same slug, and the four status chips stay short enough for
# one phone-width row.
# pf.5: budgets are display *cells* (CJK/emoji = 2) -- u16 budgets let CJK
# titles render at double the button width.
# pf.8: one shared line could not hold topic + project + pane + session: the
# fixed tail (~18 cells) and project tag starved the title to ~11 cells, so the
# discriminating part of long CJK topics ("...续训对照 MTP" vs "...BF16 C4
# 评测交接") was always the part cut. The button is now TWO lines: the topic
# owns a whole line, and "project › pane · session @host" sits under it. Each
# line independently obeys the phone button width; on the meta line the project
# is the only thing that shrinks and it disappears (rather than clipping the
# identity tail) when it cannot fit.
#
# SWITCH_LABEL_CELLS is a per-LINE budget (Telegram clips any overflowing line
# itself, which would hide the pane/session tail).
SWITCH_PAGE_SIZE = 10             # pane rows per /switch page
SWITCH_LABEL_CELLS = 40           # half-width cells of one phone-button line
SWITCH_SESSION_CELLS = 18
SWITCH_HOST_CELLS = 10
SWITCH_FILTERS = ("all", "blocked", "working", "idle")
# Two CJK chars each: glyph + 3-char words still clip to "O…理" on a phone.
SWITCH_FILTER_LABELS = {"all": "全部", "blocked": "待办",
                        "working": "运行", "idle": "空闲"}

_HPC_COORD_SUFFIX_RE = re.compile(r"^(.+)-(\d+)$")


def _switch_cb(**kw) -> str:
    return json.dumps(kw, separators=(",", ":"))


def switch_matches(rec: dict, statusf: str, query: str) -> bool:
    status = rec.get("status", "unknown")
    if statusf == "blocked" and status != "blocked":
        return False
    if statusf == "working" and status != "working":
        return False
    if statusf == "idle" and status not in ("idle", "done", "unknown"):
        return False
    if query:
        haystack = " ".join((
            effective_project(rec), rec.get("title", ""), rec.get("label", ""),
            rec.get("agent", ""), rec.get("pane_id", ""),
        )).lower()
        if query.lower() not in haystack:
            return False
    return True


def _switch_panes(statusf: str, query: str) -> list[dict]:
    """Matching human panes, grouped by effective project.

    Iteration follows the status ranking (so the first group holds the most
    urgent panes) and panes stay grouped within each project. Only the first
    pane of a group on each page keeps the project tag (see
    build_switch_keyboard): a phone inline button fits ~40 display cells and
    the 15-cell slug cannot be repeated on every row.
    """
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for rec in sorted_agents(human_agents()):
        if not switch_matches(rec, statusf, query):
            continue
        groups.setdefault(effective_project(rec), []).append(rec)
    return [rec for recs in groups.values() for rec in recs]


def _short_session(rec: dict, project: str) -> str:
    """Session token for a compact row label.

    A coordinator registered as hpc-<slug> duplicates the project tag at the
    start of the row; collapse it to "coord" (coord-2/...). Registered names
    mirror Rust names.rs truncation: 24 chars standalone, 22 before the -N
    suffix. Anything else keeps its full (pre-capped) name.
    """
    session = (rec.get("label") or "").strip() or rec.get("agent") or "agent"
    if session.startswith("hpc-"):
        rest = session[4:]
        if rest in (project, project[:24]):
            return "coord"
        m = _HPC_COORD_SUFFIX_RE.match(rest)
        if m and m.group(1) in (project, project[:22]):
            return "coord" if m.group(2) == "1" else f"coord-{m.group(2)}"
    return session


def _pane_id_label(pane_id: str, budget: int) -> str:
    """Pane id sized for a button tail, middle-ellipsized with the suffix kept.

    herdr ids discriminate panes by their creation-counter tail (wT:p30 vs
    wT:p3J, ...-2606 vs ...-3604); a head-only truncation that drops the tail
    makes two long-id duplicates render identically. When the id fits it is
    returned verbatim.
    """
    if _cell_len(pane_id) <= budget:
        return pane_id
    keep = max(1, min(4, budget - 1))
    head_budget = budget - keep - 1            # one cell for the ellipsis
    if head_budget <= 0:
        return "…" + pane_id[-(budget - 1):]
    return _cell_truncate(pane_id[:-keep], head_budget) + pane_id[-keep:]


def _switch_pane_label(rec: dict) -> str:
    """Two-line phone button, each line within SWITCH_LABEL_CELLS display cells.

      line 1: glyph <topic title>            (full line; the title IS the
              conversation and the primary discriminator)
      line 2: <project> › <pane_id> · <session> @host
              (project is the only shrinkable part and drops entirely before
              the pane/session identity tail can be clipped)

    A title-less pane puts the project on line 1 (its sole readable cue) and
    keeps line 2 to pane · session so the slug is not printed twice.
    """
    project = effective_project(rec)
    glyph = STATUS_GLYPH.get(rec.get("status", "unknown"), "💤")
    session = _cell_truncate(_short_session(rec, project), SWITCH_SESSION_CELLS)
    host = rec.get("host") or "local"
    host_part = (
        f" @{_cell_truncate(host, SWITCH_HOST_CELLS)}" if host != "local" else ""
    )
    pane_id_raw = str(rec.get("pane_id") or "?")
    title = (rec.get("title") or "").strip()

    head = f"{glyph} "
    line1 = head + _cell_truncate(
        title or project, SWITCH_LABEL_CELLS - _cell_len(head)
    )

    # Reserve the session/host tail first; a long pane id then gives up its
    # MIDDLE, never the distinguishing suffix.
    tail_without_id = f" · {session}{host_part}"
    pane_id = _pane_id_label(
        pane_id_raw, max(6, SWITCH_LABEL_CELLS - _cell_len(tail_without_id))
    )
    tail = f"{pane_id}{tail_without_id}"
    if title:
        # " › " costs 3 cells; a project tag shorter than 4 cells is noise.
        room = SWITCH_LABEL_CELLS - _cell_len(tail) - 3
        if room >= 4:
            line2 = f"{_cell_truncate(project, room)} › {tail}"
        else:
            line2 = tail
    else:
        line2 = tail
    return f"{line1}\n{line2}"


def build_switch_keyboard(page: int = 0, statusf: str = "all", query: str = "") -> InlineKeyboardMarkup:
    if statusf not in SWITCH_FILTERS:
        statusf = "all"
    query = query[:24]
    panes = _switch_panes(statusf, query)
    page_count = max(1, (len(panes) + SWITCH_PAGE_SIZE - 1) // SWITCH_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    visible = panes[page * SWITCH_PAGE_SIZE:(page + 1) * SWITCH_PAGE_SIZE]

    # Every button carries its own meta line (project › pane · session): the
    # two-line layout gives the topic a whole line, so nothing needs sharing.
    keyboard = [
        [InlineKeyboardButton(
            _switch_pane_label(rec),
            callback_data=pane_callback_data("switch", rec["pane_id"]),
        )]
        for rec in visible
    ]

    keyboard.append([
        InlineKeyboardButton(
            ("● " if statusf == f else "○ ") + SWITCH_FILTER_LABELS[f],
            callback_data=_switch_cb(action="switch-filter", f=f, q=query),
        )
        for f in SWITCH_FILTERS
    ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "Previous",
            callback_data=_switch_cb(action="switch-menu", page=page - 1, f=statusf, q=query),
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "Next",
            callback_data=_switch_cb(action="switch-menu", page=page + 1, f=statusf, q=query),
        ))
    if nav:
        keyboard.append(nav)
    if query:
        keyboard.append([InlineKeyboardButton(
            f"✖ 清除搜索: {_u16_truncate(query, 40)}",
            callback_data=_switch_cb(action="switch-filter", f=statusf, q=""),
        )])
    return InlineKeyboardMarkup(keyboard)


FOCUS_VIEW_FOOTER = (
    "已聚焦：直接输入消息即发送到该 session，不需要 reply；再点一次该 session 可刷新预览。"
)


async def send_focus_view(bot, message, chat, rec: dict):
    """Focus a pane and open its one reusable transcript-preview message.

    pf.6: picking a session from /switch or the bottom bar used to post only a
    "已聚焦" one-liner, so the conversation preview was an extra 📖 Read tap away.
    Selection now renders the same transcript view as Read; repeated picks of the
    same pane edit that message in place (see show_pane_view) instead of
    burying the chat.
    """
    pane_id = rec["pane_id"]
    header = f"🎯 {pane_focus_identity(rec)} · 已聚焦"
    content, _is_raw = await read_agent(pane_id)
    return await show_pane_view(
        bot, chat.id, message, pane_id, header, content,
        footer=FOCUS_VIEW_FOOTER,
    )


# --- Project drill-down (pf.10) ---------------------------------------------
#
# A sibling picker to /switch: first choose a PROJECT (the herd here spans
# lagrange-system, learning, road-42, ...), then one of that project's human
# sessions. Projects are identified on the wire by their slug, re-resolved
# from the live snapshot at tap time exactly like pane tokens, so a stale slug
# answers "project no longer present" and offers the fresh list instead of
# acting on the wrong conversation. Counts cover human panes only -- thread
# workers never reach any number here.
PROJECT_PAGE_SIZE = SWITCH_PAGE_SIZE


def _project_rows() -> list[tuple[str, dict]]:
    """Projects with >=1 human pane: (slug, {blocked, working, panes}).

    Ordered blocked-count desc, then project name; the panes inside each
    project keep sorted_agents' status ranking so the neediest is first.
    """
    by_project: dict[str, dict] = {}
    for rec in sorted_agents(human_agents()):
        slug = effective_project(rec)
        row = by_project.setdefault(slug, {"blocked": 0, "working": 0, "panes": []})
        row["panes"].append(rec)
        if rec.get("status") == "blocked":
            row["blocked"] += 1
        elif rec.get("status") == "working":
            row["working"] += 1
    ordered = sorted(by_project.items(), key=lambda kv: (-kv[1]["blocked"], kv[0].lower()))
    return [(slug, row) for slug, row in ordered]


def _project_button_label(slug: str, row: dict) -> str:
    """`📁 <project> · 🚫n ▶️n`, zero counts omitted, capped to one button line."""
    counts = []
    if row["blocked"]:
        counts.append(f"🚫{row['blocked']}")
    if row["working"]:
        counts.append(f"▶️{row['working']}")
    label = "📁 " + slug
    if counts:
        label += " · " + " ".join(counts)
    return _cell_truncate(label, SWITCH_LABEL_CELLS)


# The callback must fit in 64 bytes: {"action":"proj-open","s":"<slug>"}
# leaves ~44 bytes for the slug. Real project slugs are short; a longer one is
# sent as a hash token and re-resolved from the live snapshot at tap time,
# exactly like pane tokens.
def _project_cb(slug: str, action: str, **extra) -> str:
    direct = _switch_cb(action=action, s=slug, **extra)
    if len(direct.encode()) <= 64:
        return direct
    token = hashlib.sha1(slug.encode()).hexdigest()[:12]
    return _switch_cb(action=action, pt=token, **extra)


def _resolve_project_key(data: dict) -> str | None:
    """Map a tap-time project key (slug or hash token) back to a live slug."""
    rows = _project_rows()
    slug = data.get("s")
    if isinstance(slug, str) and any(project == slug for project, _r in rows):
        return slug
    token = data.get("pt")
    if isinstance(token, str):
        for project, _r in rows:
            if hashlib.sha1(project.encode()).hexdigest()[:12] == token:
                return project
    return None


def build_project_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            _project_button_label(slug, row),
            callback_data=_project_cb(slug, "proj-open"),
        )]
        for slug, row in _project_rows()
    ]
    return InlineKeyboardMarkup(rows)


def _find_project(slug: str) -> dict | None:
    return next((row for project, row in _project_rows() if project == slug), None)


def build_project_sessions_keyboard(slug: str, page: int = 0) -> InlineKeyboardMarkup | None:
    """Second level: the project's human sessions in the /switch two-line style."""
    row = _find_project(slug)
    if row is None:
        return None
    panes = row["panes"]
    page_count = max(1, (len(panes) + PROJECT_PAGE_SIZE - 1) // PROJECT_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    visible = panes[page * PROJECT_PAGE_SIZE:(page + 1) * PROJECT_PAGE_SIZE]
    keyboard = [
        [InlineKeyboardButton(
            _switch_pane_label(rec),
            callback_data=pane_callback_data("switch", rec["pane_id"]),
        )]
        for rec in visible
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "Previous", callback_data=_project_cb(slug, "proj-page", page=page - 1),
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "Next", callback_data=_project_cb(slug, "proj-page", page=page + 1),
        ))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(
        "‹ 返回项目列表", callback_data=_switch_cb(action="proj-list")
    )])
    return InlineKeyboardMarkup(keyboard)


def receipt_keyboard(pane_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Read", callback_data=pane_callback_data("read", pane_id)),
        InlineKeyboardButton(
            "🔀 切换", callback_data=_switch_cb(action="switch-open")
        ),
    ]])


def sent_receipt(rec: dict, now: float) -> str:
    return (
        f"✅ 已发送 → <b>{_html_escape(agent_identity(rec))}</b> · {_hhmm(now)}"
    )


def prune_pane_state(now: float, live_panes: set[str]):
    """Drop bookkeeping for panes that have been absent past the grace window."""
    global state_dirty, active_pane
    for pane_id in [pid for pid, ts in pane_seen.items()
                    if pid not in live_panes and now - ts > PANE_STATE_GRACE]:
        pane_messages.pop(pane_id, None)
        pane_seen.pop(pane_id, None)
        done_at.pop(pane_id, None)
        prev_statuses.pop(pane_id, None)
        daily_stats.pop(pane_id, None)
        if pane_id in recent_panes:
            recent_panes.remove(pane_id)
        if active_pane == pane_id:
            active_pane = None
        state_dirty = True


# --- State persistence (survive launchd restarts, never holds content) ------

def load_state():
    global dashboard_msg_id, active_pane, recent_panes
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return
    mid = data.get("dashboard_msg_id")
    if isinstance(mid, int):
        dashboard_msg_id = mid
    now = time.time()
    for pane_id, msg_id in (data.get("pane_messages") or {}).items():
        if isinstance(pane_id, str) and isinstance(msg_id, int):
            pane_messages[pane_id] = msg_id
            pane_seen.setdefault(pane_id, now)
    for pane_id, ts in (data.get("done_at") or {}).items():
        if isinstance(ts, (int, float)) and now - float(ts) <= DASHBOARD_DONE_TTL:
            done_at[str(pane_id)] = float(ts)
    for pane_id, status in (data.get("statuses") or {}).items():
        if status in STATUS_ORDER:
            prev_statuses[str(pane_id)] = status
    focus = data.get("active_pane")
    if isinstance(focus, str) and focus:
        active_pane = focus
    recents = data.get("recent") or []
    if isinstance(recents, list):
        recent_panes = [pid for pid in recents if isinstance(pid, str)][:RECENT_MAX]


def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        live = {a.get("pane_id") for a in agents if a.get("pane_id")}
        data = {
            "dashboard_msg_id": dashboard_msg_id,
            "pane_messages": dict(list(pane_messages.items())[-PENDING_LIMIT:]),
            "done_at": {
                pid: ts for pid, ts in done_at.items()
                if time.time() - ts <= DASHBOARD_DONE_TTL
            },
            "statuses": {pid: st for pid, st in prev_statuses.items() if pid in live},
            "active_pane": active_pane,
            "recent": recent_panes[:RECENT_MAX],
        }
        tmp = STATE_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            os.close(fd)
            raise
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("failed to persist telegram state: %s", scrub(e))


# --- Bot commands ---

DASHBOARD_HELP = (
    "The live dashboard above refreshes on its own — long-press it and Pin so "
    "it stays at the top of the chat.\n\n"
    "Blocked sessions post one alert that turns into a ✅ one-liner when they "
    "finish; tap a row or use /read for the full transcript.\n\n"
    "The bar at the bottom focuses one session: /switch (or 📋 切换 session) "
    "picks it, and afterwards every plain message goes straight there — no "
    "reply needed. Replying to any session card still targets that card once."
)


async def _show_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE, repost: bool = False):
    if not CHAT_ID:
        await update.message.reply_text(
            f"herdr-remote bot\n\nYour chat id: {update.effective_chat.id}\n"
            "Set HERDR_TG_CHAT_ID to it to lock the bot to this chat."
        )
        return False
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        try:
            if repost:
                # Manual /dashboard (and the 📊 bar key): a fresh copy at the
                # bottom the user can find and pin; the old singleton is retired.
                await repost_dashboard(bot, update.effective_chat.id)
            else:
                await push_dashboard(bot, chat_id=update.effective_chat.id, force=True)
        except Exception as e:
            await update.message.reply_text(f"Dashboard update failed: {scrub(e)}")
            return False
    return True


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /start command — bring the singleton dashboard up to date."""
    if not authorized(update):
        return
    if await _show_dashboard(update, ctx):
        bot = getattr(ctx, "bot", None)
        if bot is not None:
            await refresh_bar(bot, force=True)
        await update.message.reply_text(DASHBOARD_HELP)


async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /dashboard — retire the old singleton and post a fresh copy at the bottom.

    No separate acknowledgement: the new dashboard message itself must be the
    last message in the chat, so it is immediately visible and ready to pin.
    """
    if not authorized(update):
        return
    await _show_dashboard(update, ctx, repost=True)


async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /switch [query] — project-tagged focus selector."""
    if not authorized(update):
        return
    query = " ".join(ctx.args or []).strip()
    if not relay_connected and not human_agents():
        await update.message.reply_text("Relay disconnected. Use /switch after it reconnects.")
        return
    flat = _switch_panes("all", query)
    if not flat:
        await update.message.reply_text(
            "没有匹配的人类 session（thread worker 已隐藏）。"
            if query else "没有人类 session 在线（thread worker 已隐藏）。"
        )
        return
    await update.message.reply_text(
        ("选择消息发送目标" + (f"（搜索: {_u16_truncate(query, 40)}）" if query else "")
         + "，可按状态过滤："),
        reply_markup=build_switch_keyboard(query=query),
    )


async def _reply_projects(message, caption: str):
    """Post (or re-post) the project list, or say there is nothing to list."""
    if not relay_connected and not human_agents():
        await message.reply_text("Relay disconnected. Use /switch after it reconnects.")
        return
    if not _project_rows():
        await message.reply_text("没有人类 session 在线（thread worker 已隐藏）。")
        return
    await message.reply_text(caption, reply_markup=build_project_keyboard())


async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the 📁 项目 bar key — pick a project, then a session in it."""
    if not authorized(update):
        return
    await _reply_projects(update.message, "选择项目：")


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /find <keyword> — /switch with a text filter."""
    if not authorized(update):
        return
    if not ctx.args:
        await update.message.reply_text("用法: /find <project/session/pane 关键词>")
        return
    await cmd_switch(update, ctx)


async def cmd_unfocus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /unfocus — clear the sticky target so plain text goes nowhere."""
    if not authorized(update):
        return
    if active_pane is None:
        await update.message.reply_text("当前没有聚焦的 session。")
        return
    unfocus_pane()
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        await refresh_bar(bot, force=True)
    await update.message.reply_text("已取消聚焦，之后的消息不会发送，直到你重新 /switch。")


# A /agents page must leave room for its "(page x/y)" caption and the pager
# row under Telegram's 4096 utf-16 cap; identities are themselves uncapped, so
# an over-long single line is truncated here rather than failing the send.
AGENTS_PAGE_U16 = 3800
# A single identity line is truncated with room for a re-emitted section
# header and newline on a page that starts mid-section.
AGENTS_PAGE_LINE_U16 = AGENTS_PAGE_U16 - 24
_AGENT_SECTIONS = (
    ("BLOCKED:", lambda a: a.get("status") == "blocked"),
    ("WORKING:", lambda a: a.get("status") == "working"),
    ("DONE:", lambda a: a.get("status") == "done"),
    ("IDLE:", lambda a: a.get("status") in ("idle", "unknown")),
)


def agents_pages() -> list[str]:
    """All human panes as sectioned text pages bounded to one Telegram message.

    One message used to truncate at 4096 chars, hiding every pane past the
    cut; pages keep every pane reachable through the inline pager.
    """
    panes = human_agents()
    pages: list[str] = []
    current: list[str] = []
    used = 0

    def push(line: str):
        nonlocal used
        current.append(line)
        used += _u16len(line) + 1

    for header, pred in _AGENT_SECTIONS:
        rows = sorted(
            (a for a in panes if pred(a)),
            key=lambda a: (effective_project(a).lower(), pane_descriptor(a).lower()),
        )
        if not rows:
            continue
        body = [
            f"  {_html_escape(_u16_truncate(agent_identity(a), AGENTS_PAGE_LINE_U16))}"
            for a in rows
        ]
        for line in [header, *body]:
            cost = _u16len(line) + 1
            if current and used + cost > AGENTS_PAGE_U16:
                pages.append("\n".join(current))
                current, used = [], 0
                # Re-emit the header on the new page so its rows are never
                # attached to the previous section.
                if line != header:
                    push(header)
            push(line)
    if current:
        pages.append("\n".join(current))
    return pages


def agents_page_text(page: int, pages: list[str]) -> str:
    if len(pages) > 1:
        return f"{pages[page]}\n\n<i>第 {page + 1}/{len(pages)} 页</i>"
    return pages[page]


def build_agents_pager(page: int, page_count: int) -> InlineKeyboardMarkup | None:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "Previous",
            callback_data=_switch_cb(action="agents-page", page=page - 1),
        ))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(
            "Next",
            callback_data=_switch_cb(action="agents-page", page=page + 1),
        ))
    return InlineKeyboardMarkup([nav]) if nav else None


async def cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /agents — list current human-facing agents with full identity."""
    pages = agents_pages()
    if not pages:
        await update.message.reply_text(
            "No human sessions connected (thread workers are hidden)."
            if relay_connected else "Not connected to relay."
        )
        return
    await update.message.reply_text(
        agents_page_text(0, pages),
        parse_mode="HTML",
        reply_markup=build_agents_pager(0, len(pages)),
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /status — show connection info."""
    b = len([a for a in agents if a.get("status") == "blocked"])
    w = len([a for a in agents if a.get("status") == "working"])
    d = len([a for a in agents if a.get("status") == "done"])
    i = len([a for a in agents if a.get("status") in ("idle", "unknown")])

    status = "Connected" if relay_connected else "Disconnected"
    focus = "none"
    if active_pane and (rec := find_agent(active_pane)) is not None:
        focus = agent_identity(rec)
    elif active_pane:
        focus = f"{active_pane} (offline)"
    text = (
        f"Relay: {RELAY_WS_SAFE}\n"
        f"Status: {status}\n"
        f"Focus: {focus}\n"
        f"Agents: {len(agents)} ({b} blocked, {w} working, {d} done, {i} idle)"
    )
    await update.message.reply_text(text)


async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /read [project] — read pane output."""
    args = ctx.args
    if not args:
        # Show agent picker
        if not agents:
            await update.message.reply_text("No agents. Use /agents to check.")
            return
        await update.message.reply_text("Read which agent?", reply_markup=build_agent_keyboard("read"))
        return

    # Find agent by project name
    query = " ".join(args).lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'. Use /agents to see list.")
        return

    content, _is_raw = await read_agent(match["pane_id"])
    await show_pane_view(ctx.bot, update.effective_chat.id, update.message,
                         match["pane_id"], agent_identity(match), content)


async def cmd_interrupt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /interrupt [project] — send Ctrl+C."""
    args = ctx.args
    if not args:
        if not agents:
            await update.message.reply_text("No agents.")
            return
        working = [a for a in agents if a.get("status") in ("working", "blocked")]
        if not working:
            await update.message.reply_text("No active agents to interrupt.")
            return
        await update.message.reply_text(
            "Interrupt which agent?",
            reply_markup=build_agent_keyboard("interrupt", agent_list=working),
        )
        return

    query = " ".join(args).lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'.")
        return

    try:
        await send_keys_to_relay(match["pane_id"], ["C-c"])
        await update.message.reply_text(f"Sent Ctrl+C to {match['project']}")
    except Exception as e:
        await update.message.reply_text(f"Failed: {scrub(e)}")


async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /send [project] [text] — send text + Enter to a pane."""
    args = ctx.args
    if not args:
        if not agents:
            await update.message.reply_text("No agents.")
            return
        await update.message.reply_text(
            "Send to which agent?\n(After selecting, reply with your text)",
            reply_markup=build_agent_keyboard("select_send"),
        )
        return

    query = args[0].lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'. Use /agents to see list.")
        return

    text = " ".join(args[1:])
    if not text:
        await send_reply_prompt(update.message, update.effective_chat, match)
        return

    try:
        await send_text_to_relay(match["pane_id"], text)
        await update.message.reply_text(f"Sent to {match['project']}: {text}")
    except Exception as e:
        await update.message.reply_text(f"Failed: {scrub(e)}")


async def cmd_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /reply [project] — show agent output then accept input."""
    args = ctx.args

    if not agents:
        await update.message.reply_text("No agents.")
        return

    if not args:
        await update.message.reply_text("Reply to which agent?", reply_markup=build_agent_keyboard("select_reply"))
        return

    query = " ".join(args).lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'.")
        return

    content, _is_raw = await read_agent(match["pane_id"])
    await send_reply_prompt(update.message, update.effective_chat, match, content)


async def cmd_trust(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /trust [project] — send 'trust, always allow' to a blocked agent."""
    args = ctx.args
    blocked = [a for a in agents if a.get("status") == "blocked"]

    if not blocked:
        await update.message.reply_text("No blocked agents.")
        return

    if not args:
        await update.message.reply_text(
            "Trust which agent?",
            reply_markup=build_agent_keyboard("trust", agent_list=blocked),
        )
        return

    query = " ".join(args).lower()
    match = next((a for a in blocked if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No blocked agent matching '{query}'.")
        return

    try:
        await send_to_relay(
            match["pane_id"],
            "trust, always allow",
            prompt_id=blocked_prompt_ids.get(match["pane_id"]),
        )
        await update.message.reply_text(f"Trusted {match['project']} (always allow)")
    except Exception as e:
        await update.message.reply_text(f"Failed: {scrub(e)}")


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /digest — show today's agent activity summary."""
    if not daily_stats:
        await update.message.reply_text("No activity recorded yet today.")
        return

    lines = ["Today's activity:\n"]
    sorted_agents = sorted(daily_stats.values(), key=lambda x: x.get("working_mins", 0), reverse=True)
    for s in sorted_agents:
        blocked = f", blocked {s['blocked_count']}x" if s.get("blocked_count") else ""
        mins = s.get("working_mins", 0)
        time_str = f"{mins}m" if mins < 60 else f"{mins//60}h{mins%60}m"
        lines.append(f"  {s['project']} ({s['agent']}): {time_str} working{blocked}")

    await update.message.reply_text("\n".join(lines))


# --- Callback handler (buttons) ---

async def deliver_choice(query, pane_id: str, label: str, deliver) -> bool:
    """Acknowledge a tapped choice at once, deliver it exactly once, then confirm or roll back.

    Telegram shows nothing for a tap until the bot edits or replies, and the relay plus two
    Telegram round trips take a second or two -- long enough to tap again. A second tap used
    to reach the relay while the first was in flight; by then the dialog had closed, so the
    duplicate "1" was typed into the agent's input line instead. So: the buttons come off the
    message BEFORE delivery (that is the feedback), a second tap during delivery is refused,
    and a failed delivery puts the buttons back so the user can retry. Returns True on
    success.
    """
    if pane_id in approval_in_flight:
        await query.message.reply_text(
            f"Still sending your previous choice -- wait for \"Sent\" before tapping again."
        )
        return False
    approval_in_flight.add(pane_id)
    previous_markup = query.message.reply_markup
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await deliver()
        approval_tokens.pop(pane_id, None)
    except Exception as e:
        try:
            await query.edit_message_reply_markup(reply_markup=previous_markup)
        except Exception:
            pass
        await query.message.reply_text(f"Failed: {scrub(e)}")
        return False
    finally:
        approval_in_flight.discard(pane_id)
    # The human is now interacting with this pane: make plain-text follow-ups
    # land here without a Telegram reply.
    focus_pane(pane_id)
    await query.message.reply_text(f"Sent: {label}")
    return True


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    if CHAT_ID and str(update.effective_chat.id) != CHAT_ID:
        await query.answer("Unauthorized")
        return
    await query.answer()

    if not relay_connected:
        await query.message.reply_text("Relay disconnected. Use /start after it reconnects.")
        return

    data = parse_callback_data(query.data)
    action = data.get("action", "approval")

    if action == "page":
        menu = data.get("menu", "select_reply")
        if not agents_for_action(menu):
            await query.message.reply_text("No eligible agents are available.")
            return
        await query.edit_message_reply_markup(
            reply_markup=build_agent_keyboard(menu, page=data.get("page", 0))
        )
        return

    if action == "noop":
        # Group-header buttons in the /switch selector do nothing.
        return

    if action in ("switch-menu", "switch-filter"):
        statusf = data.get("f", "all")
        query_text = data.get("q", "") or ""
        page = data.get("page", 0) if action == "switch-menu" else 0
        try:
            await query.edit_message_reply_markup(
                reply_markup=build_switch_keyboard(page=page, statusf=statusf, query=query_text)
            )
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
        return

    if action == "switch-open":
        await query.message.reply_text(
            "选择消息发送目标：", reply_markup=build_switch_keyboard()
        )
        return

    if action == "agents-page":
        pages = agents_pages()
        if not pages:
            await query.message.reply_text(
                "No human sessions connected (thread workers are hidden)."
            )
            return
        try:
            wanted = int(data.get("page", 0))
        except (TypeError, ValueError):
            wanted = 0
        page = min(max(wanted, 0), len(pages) - 1)
        await query.edit_message_text(
            text=agents_page_text(page, pages),
            parse_mode="HTML",
            reply_markup=build_agents_pager(page, len(pages)),
        )
        return

    if action == "proj-list":
        try:
            await query.edit_message_reply_markup(reply_markup=build_project_keyboard())
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
        return

    if action in ("proj-open", "proj-page"):
        slug = _resolve_project_key(data)
        page = 0
        if action == "proj-page":
            try:
                page = int(data.get("page", 0))
            except (TypeError, ValueError):
                page = 0
        if slug is None:
            # Stale slug/token: the project vanished since the list was drawn.
            # Re-resolve from the live snapshot and offer the fresh list.
            await _reply_projects(query.message, "该项目已不在当前列表中，重新选择：")
            return
        markup = build_project_sessions_keyboard(slug, page=page)
        if markup is None:
            await _reply_projects(query.message, "该项目已不在当前列表中，重新选择：")
            return
        try:
            await query.edit_message_reply_markup(reply_markup=markup)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
        return

    if action == "switch":
        pane_id = data.get("pane_id")
        selected = next(
            (agent for agent in human_agents() if agent.get("pane_id") == pane_id),
            None,
        )
        if selected is None:
            await query.message.reply_text(
                "That session is no longer available. Open the selector again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "📋 切换 session", callback_data=_switch_cb(action="switch-open")
                )]]),
            )
            return
        focus_pane(selected["pane_id"])
        bot = getattr(ctx, "bot", None)
        if bot is not None:
            await refresh_bar(bot, force=True)
        await send_focus_view(bot, query.message, update.effective_chat, selected)
        return

    selected_agent = None
    if action in {"read", "interrupt", "select_send", "select_reply", "trust", "approval"}:
        eligible_action = "trust" if action == "approval" else action
        selected_agent = next(
            (agent for agent in agents_for_action(eligible_action) if agent.get("pane_id") == data.get("pane_id")),
            None,
        )
        if selected_agent is None:
            await query.message.reply_text(
                "That agent is no longer available for this action. Refresh the list and choose another agent.",
                reply_markup=refresh_keyboard(eligible_action),
            )
            return

    if action == "read":
        content, _is_raw = await read_agent(data["pane_id"])
        await show_pane_view(ctx.bot, update.effective_chat.id, query.message,
                             data["pane_id"], agent_identity(selected_agent), content)
        return

    if action == "interrupt":
        try:
            await send_keys_to_relay(data["pane_id"], ["C-c"])
            await query.message.reply_text("Sent Ctrl+C")
        except Exception as e:
            await query.message.reply_text(f"Failed: {scrub(e)}")
        return

    if action == "select_send":
        await send_reply_prompt(query.message, update.effective_chat, selected_agent)
        return

    if action == "select_reply":
        content, _is_raw = await read_agent(data["pane_id"])
        await send_reply_prompt(query.message, update.effective_chat, selected_agent, content)
        return

    if action == "trust":
        try:
            await send_to_relay(
                data["pane_id"],
                "trust, always allow",
                prompt_id=blocked_prompt_ids.get(data["pane_id"]),
            )
            await query.message.reply_text(f"Trusted {selected_agent['project']} (always allow)")
        except Exception as e:
            await query.message.reply_text(f"Failed: {scrub(e)}")
        return

    if action != "approval":
        await query.message.reply_text("That action is no longer valid. Refresh the list and try again.")
        return

    pane_id = data["pane_id"]
    if not data.get("g") or data["g"] != approval_tokens.get(pane_id):
        await query.message.reply_text(
            "That approval belongs to an older prompt. Use the controls on the latest blocked notification."
        )
        return

    prompt_id = blocked_prompt_ids.get(pane_id)
    if not prompt_id:
        await query.message.reply_text(
            "That approval belongs to an older prompt. Use the controls on the latest blocked notification."
        )
        return

    option_index = data.get("i")
    if option_index is not None:
        try:
            option_index = int(option_index)
        except (TypeError, ValueError):
            await query.message.reply_text("That approval action is no longer supported. Use the latest notification.")
            return
        label = None
        if query.message and query.message.reply_markup:
            for row in query.message.reply_markup.inline_keyboard:
                for btn in row:
                    try:
                        button_data = parse_callback_data(btn.callback_data)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
                    if button_data.get("i") == str(option_index):
                        label = btn.text
                        break
                if label is not None:
                    break
        if not label:
            await query.message.reply_text("That approval action is no longer supported. Use the latest notification.")
            return
        await deliver_choice(
            query, pane_id, label, lambda: send_to_relay(pane_id, label, prompt_id=prompt_id)
        )
        return

    # Confirm a blocked agent's prompt by pressing the option number.
    # Sending the option *text* via `respond` does NOT work: the relay pastes it via
    # send-text, and Claude's TUI treats a pasted trailing newline as paste content,
    # not as Enter, so the prompt never gets confirmed. A real key press does.
    key = data.get("k")
    if key is None:
        await query.message.reply_text("That approval action is no longer supported. Use the latest notification.")
        return
    # Recover the pressed button's label for the confirmation (kept out of
    # callback_data to respect Telegram's 64-byte limit).
    label = f"option {key}"
    if query.message and query.message.reply_markup:
        for row in query.message.reply_markup.inline_keyboard:
            for btn in row:
                try:
                    if json.loads(btn.callback_data).get("k") == key:
                        label = btn.text
                except (ValueError, TypeError):
                    pass
    await deliver_choice(
        query, pane_id, label, lambda: send_keys_to_relay(pane_id, [key], prompt_id=prompt_id)
    )


# --- Free text reply ---

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Route free text: bottom-bar taps, a one-off reply target, or the focus pane."""
    if not authorized(update):
        return
    message = update.message
    if not message.text:
        return
    chat = update.effective_chat
    is_private = getattr(chat, "type", None) == "private"

    # Bottom-bar keys arrive as their button labels; act on them and never let
    # the label text reach a pane.
    if is_private:
        kind, bar_pane = bar_label_pane(message.text)
        if kind:
            await handle_bar_tap(update, ctx, kind, bar_pane)
            return

    target_id: str | None = None
    if message.reply_to_message:
        mapped = pending_pane(chat.id, message.reply_to_message.message_id)
        if mapped:
            target_id = mapped
            # Replying to a specific card both sends once and re-focuses it.
            focus_pane(mapped)

    if target_id is None and is_private and active_pane:
        target_id = active_pane

    if target_id is None:
        # Private chats must not swallow text silently: open the selector.
        # Group chats keep the legacy reply-only contract.
        if is_private:
            if not relay_connected:
                await message.reply_text(
                    "Relay disconnected. Use /start after it reconnects."
                )
            elif not human_agents():
                await message.reply_text(
                    "还没有聚焦的 session（thread worker 已隐藏）。"
                )
            else:
                await message.reply_text(
                    "先选择消息要发往的 session：",
                    reply_markup=build_switch_keyboard(),
                )
        return

    if not relay_connected:
        await message.reply_text("Relay disconnected. Use /start after it reconnects.")
        return
    rec = find_agent(target_id)
    if rec is None:
        if active_pane == target_id:
            unfocus_pane()
        bot = getattr(ctx, "bot", None)
        if bot is not None and is_private:
            await refresh_bar(bot, force=True)
        await message.reply_text(
            "That agent is no longer available. Choose another session:",
            reply_markup=build_switch_keyboard() if is_private else None,
        )
        return

    # No force-refresh here: focus changes (reply redirect / selector tap)
    # already mark the bar dirty and the loop swaps it within ~1s, so typing a
    # message must not spawn a send+deleted placeholder of its own.
    try:
        prompt_id = blocked_prompt_ids.get(target_id)
        if prompt_id:
            await send_to_relay(target_id, message.text, prompt_id=prompt_id)
        else:
            await send_text_to_relay(target_id, message.text)
        if is_private:
            await message.reply_text(
                sent_receipt(rec, time.time()),
                parse_mode="HTML",
                reply_markup=receipt_keyboard(target_id),
            )
        else:
            await message.reply_text("Sent")
    except Exception as e:
        await message.reply_text(f"Failed: {scrub(e)}")


async def handle_bar_tap(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                         kind: str, pane_id: str | None):
    """Dispatch a tapped persistent-bar button."""
    message = update.message
    if kind == "switch":
        await cmd_switch(update, ctx)
        return
    if kind == "pending":
        # Portal's 待办 shortcut: the /switch picker locked to blocked panes;
        # picking a row goes through the same focus + transcript preview.
        if not relay_connected and not human_agents():
            await message.reply_text("Relay disconnected. Use /switch after it reconnects.")
            return
        blocked = _switch_panes("blocked", "")
        if not blocked:
            await message.reply_text("当前没有待办。")
            return
        await message.reply_text(
            "待办 session，可切换过滤：", reply_markup=build_switch_keyboard(statusf="blocked")
        )
        return
    if kind == "project":
        await cmd_project(update, ctx)
        return
    if kind == "dashboard":
        await cmd_dashboard(update, ctx)
        return
    if kind == "unfocus":
        await cmd_unfocus(update, ctx)
        return
    if kind == "recent" and pane_id:
        rec = find_agent(pane_id)
        if rec is None:
            await message.reply_text(
                "That session is no longer available.",
                reply_markup=build_switch_keyboard(),
            )
            return
        focus_pane(pane_id)
        bot = getattr(ctx, "bot", None)
        if bot is not None:
            await refresh_bar(bot, force=True)
        await send_focus_view(bot, message, update.effective_chat, rec)
        return
    if kind == "active":
        rec = find_agent(pane_id) if pane_id else None
        if rec is None:
            await message.reply_text("聚焦的 session 已离线，重新选择：",
                                     reply_markup=build_switch_keyboard())
            unfocus_pane()
            return
        # Same preview surface as picking from /switch, without changing focus.
        await send_focus_view(
            getattr(ctx, "bot", None), message, update.effective_chat, rec
        )


# --- Blocked notification ---

TOOL_BUTTONS = [
    ("Yes (once)", "yes, single permission"),
    ("Trust (always)", "trust, always allow"),
    ("No", "no (tab to edit)"),
]

SUBAGENT_BUTTONS = [
    ("Approve all", "approve all pending"),
    ("Configure", "configure individually"),
    ("Cancel", "exit (cancel subagents)"),
]


NUMBERED_LABEL_MAX = 40


def numbered_button_label(number: int, option: str) -> str:
    """Button text for option N of a Claude-style numbered menu.

    The number is kept because it is literally the key the button presses. Claude appends
    hints after " · " ("switch to auto mode · auto mode handles these prompts for you"); the
    hint does not fit a phone-width button, the choice does.
    """
    text = f"{number}. {option.split(' · ')[0].strip()}"
    if len(text) > NUMBERED_LABEL_MAX:
        text = text[:NUMBERED_LABEL_MAX - 1] + "…"
    return text


def make_keyboard(
    pane_id: str,
    options: list[str] | None,
    generation: str | None = None,
    interaction: str | None = None,
) -> InlineKeyboardMarkup:
    if not options:
        return interaction_keyboard(pane_id)
    if interaction == "omp_question":
        buttons = [(opt, opt) for opt in options]
    elif interaction == "numbered":
        # Claude Code's 1..N menu: one button per option, pressing that option's number.
        buttons = [(numbered_button_label(i, opt), opt) for i, opt in enumerate(options, start=1)]
    elif "trust" in " ".join(options).lower():
        buttons = TOOL_BUTTONS
    elif "approve all" in " ".join(options).lower():
        buttons = SUBAGENT_BUTTONS
    else:
        buttons = [(opt.split(",")[0], opt) for opt in options]

    generation = generation or secrets.token_hex(4)
    if interaction == "omp_question":
        keyboard = [
            [InlineKeyboardButton(label, callback_data=pane_callback_data(
                "approval", pane_id, g=generation, i=str(i),
            ))]
            for i, (label, _resp) in enumerate(buttons)
        ]
        keyboard.append([InlineKeyboardButton(
            "Open output & reply",
            callback_data=pane_callback_data("select_reply", pane_id),
        )])
        return InlineKeyboardMarkup(keyboard)

    # Standard approval prompts require a real numeric key press.
    keyboard = [
        [InlineKeyboardButton(label, callback_data=pane_callback_data(
            "approval", pane_id, g=generation, k=str(i + 1)))]
        for i, (label, _resp) in enumerate(buttons)
    ]
    keyboard.append([InlineKeyboardButton(
        "Open output & reply",
        callback_data=pane_callback_data("select_reply", pane_id),
    )])
    return InlineKeyboardMarkup(keyboard)


def interaction_keyboard(pane_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Open output & reply",
            callback_data=pane_callback_data("select_reply", pane_id),
        )
    ]])


def blocked_text(identity: str, prompt: str, multi: bool) -> str:
    instruction = (
        "Use the web terminal or manual terminal controls to select multiple options."
        if multi
        else "Use an approval button, open the output, or reply to this notification."
    )
    snippet = _u16_truncate((prompt or "").strip() or "(no prompt text)", NOTIF_PROMPT_MAX)
    return (
        f"🚫 <b>{_html_escape(identity)}</b> BLOCKED\n\n"
        f"<pre>{_html_escape(snippet)}</pre>\n\n{instruction}"
    )


def resume_text(identity: str, now: float) -> str:
    return f"▶️ <b>{_html_escape(identity)}</b> working · {_hhmm(now)}"


def finished_text(identity: str, now: float) -> str:
    return f"✅ <b>{_html_escape(identity)}</b> finished · {_hhmm(now)}"


def resolved_keyboard(pane_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📖 Read", callback_data=pane_callback_data("read", pane_id)),
        InlineKeyboardButton("💬 Reply", callback_data=pane_callback_data("select_reply", pane_id)),
    ]])


async def edit_pane_message(bot, pane_id: str, text: str, markup: InlineKeyboardMarkup) -> bool:
    """Edit the pane's reusable notification in place.

    Returns False when there is no message to edit or Telegram refuses (deleted
    / too old), in which case the mapping is dropped so the caller can post a
    fresh message.
    """
    msg_id = pane_messages.get(pane_id)
    if msg_id is None:
        return False
    try:
        await bot.edit_message_text(
            chat_id=int(CHAT_ID), message_id=msg_id, text=text,
            parse_mode="HTML", reply_markup=markup,
        )
    except Exception as e:
        if "not modified" in str(e).lower():
            return True
        pane_messages.pop(pane_id, None)
        mark_state_dirty()
        return False
    return True


async def notify_blocked(
    app: Application,
    pane_id: str,
    agent: str,
    project: str,
    prompt: str,
    options: list[str] | None,
    prompt_id: str | None = None,
    interaction: str | None = None,
    multi: bool = False,
    generation: str | None = None,
):
    if not CHAT_ID:
        return
    identity = identity_for_blocked({
        "pane_id": pane_id, "agent": agent, "project": project,
    })
    text = blocked_text(identity, prompt, multi)
    generation = generation or secrets.token_hex(4)
    keyboard = make_keyboard(
        pane_id,
        [] if multi else options,
        generation=generation,
        interaction=interaction,
    )

    # Reuse the pane's existing notification across redraws, re-blocks and
    # resume cycles: one message per pane, edited in place. Only a refused edit
    # (message deleted / aged out) makes us post fresh.
    msg_id = pane_messages.get(pane_id)
    if msg_id is not None:
        try:
            await app.bot.edit_message_text(
                chat_id=int(CHAT_ID), message_id=msg_id, text=text,
                parse_mode="HTML", reply_markup=keyboard,
            )
        except Exception as e:
            if "not modified" in str(e).lower():
                approval_tokens[pane_id] = generation
                if prompt_id:
                    blocked_prompt_ids[pane_id] = prompt_id
                return
            pane_messages.pop(pane_id, None)
            mark_state_dirty()
            msg_id = None
    if msg_id is None:
        # Raises on transport failure; notify_blocked_safely owns the catch, and
        # the generation only lands in approval_tokens after success so a failed
        # send never invalidates the previous prompt's buttons.
        msg = await app.bot.send_message(
            chat_id=int(CHAT_ID), text=text, parse_mode="HTML", reply_markup=keyboard
        )
        pane_messages[pane_id] = msg.message_id
        mark_state_dirty()
        register_pending(int(CHAT_ID), msg.message_id, pane_id)
    approval_tokens[pane_id] = generation
    if prompt_id:
        blocked_prompt_ids[pane_id] = prompt_id


async def notify_blocked_safely(app: Application, msg: dict):
    pane_id = msg.get("pane_id")
    rec = find_agent(pane_id)
    # herdr-projects thread workers never page the human: their coordinator
    # owns the thread. Missing snapshot rows fail open (blocked can win the
    # race with the first snapshot after reconnect).
    if rec is not None and not pane_human(rec):
        return

    kwargs = dict(
        pane_id=pane_id,
        agent=msg.get("agent", "unknown"),
        project=msg.get("project", ""),
        prompt=msg.get("prompt", ""),
        options=msg.get("options"),
        prompt_id=msg.get("prompt_id"),
        interaction=msg.get("interaction"),
        multi=bool(msg.get("multi")),
    )
    if msg.get("update"):
        # Same blocked streak, re-hashed content: the prompt_id the relay will
        # demand has moved. Edit the existing message in place, keeping its
        # approval generation so its buttons stay valid. An update with no
        # delivered message is a stray (the connect preamble announces current
        # blocks with update=False), so ignore it rather than post a ghost card.
        if pane_messages.get(pane_id) is None:
            return
        try:
            await notify_blocked(
                app, generation=approval_tokens.get(pane_id), **kwargs
            )
        except Exception as e:
            log.warning("Failed to redraw blocked notification: %s", scrub(e))
        return
    try:
        await notify_blocked(app, **kwargs)
    except Exception as e:
        log.warning("Failed to send blocked notification: %s", scrub(e))


# --- Relay listener ---

async def track_agent_updates(app: Application, updated_agents: list[dict]):
    """Track status transitions for full snapshots and pane-level updates.

    pf.2: transitions reuse the pane's single notification message -- the
    blocked alert becomes a "working" one-liner and then a "finished"
    one-liner; the full transcript is one button tap away. Completion never
    posts a new message and never pushes the answer text: the dashboard is the
    only proactive surface for panes that never blocked.
    """
    if not CHAT_ID:
        return

    now = time.time()
    for agent_data in updated_agents:
        pane_id = agent_data.get("pane_id")
        if not pane_id or not update_is_human(agent_data):
            continue
        new_status = agent_data.get("status", "unknown")
        old_status = prev_statuses.get(pane_id)

        if pane_id not in daily_stats:
            daily_stats[pane_id] = {
                "agent": agent_data.get("agent", ""),
                "project": agent_data.get("project", ""),
                "blocked_count": 0,
                "working_mins": 0,
                "last_change": now,
            }
        stats = daily_stats[pane_id]
        if old_status == "working" and old_status != new_status:
            stats["working_mins"] += int((now - stats["last_change"]) / 60)
        if new_status == "blocked" and old_status != "blocked":
            stats["blocked_count"] += 1
        if old_status != new_status:
            stats["last_change"] = now
        if agent_data.get("status") and new_status != "blocked":
            approval_tokens.pop(pane_id, None)
            blocked_prompt_ids.pop(pane_id, None)

        if old_status and old_status != new_status:
            identity = agent_identity(agent_data)
            try:
                if new_status == "working" and old_status == "blocked":
                    # Approval resolved: the blocked alert collapses to a resume line.
                    await edit_pane_message(
                        app.bot, pane_id, resume_text(identity, now),
                        interaction_keyboard(pane_id),
                    )
                elif new_status in ("idle", "done") and old_status in ("working", "blocked"):
                    done_at[pane_id] = now
                    mark_state_dirty()
                    await edit_pane_message(
                        app.bot, pane_id, finished_text(identity, now),
                        resolved_keyboard(pane_id),
                    )
                if new_status in ("working", "blocked"):
                    done_at.pop(pane_id, None)
            except Exception as e:
                log.warning("Failed to update pane notification: %s", scrub(e))
        prev_statuses[pane_id] = new_status


def mark_state_dirty():
    global state_dirty
    state_dirty = True


async def relay_listener(app: Application):
    """Persistent WebSocket connection to relay."""
    import websockets
    global agents, relay_connected, prev_statuses

    while True:
        try:
            async with websockets.connect(RELAY_WS) as ws:
                relay_connected = True
                log.info(f"Connected to relay at {RELAY_WS_SAFE}")
                mark_dashboard_dirty()
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if msg.get("type") == "agents":
                        new_agents = msg.get("agents", [])
                        blocked_panes = {
                            agent.get("pane_id") for agent in new_agents
                            if agent.get("status") == "blocked"
                        }
                        for pane_id in list(approval_tokens):
                            if pane_id not in blocked_panes:
                                approval_tokens.pop(pane_id, None)
                                blocked_prompt_ids.pop(pane_id, None)
                        await track_agent_updates(app, new_agents)
                        agents = apply_agent_message(agents, msg)
                        now = time.time()
                        live_panes = set()
                        for a in agents:
                            pid = a.get("pane_id")
                            if pid:
                                pane_seen[pid] = now
                                live_panes.add(pid)
                        prune_pane_state(now, live_panes)
                        mark_dashboard_dirty()
                        mark_bar_dirty()

                    elif msg.get("type") == "agent_update":
                        updated_agent = msg.get("agent") or {}
                        if updated_agent.get("pane_id"):
                            await track_agent_updates(app, [updated_agent])
                            agents = apply_agent_message(agents, msg)
                            pane_seen[updated_agent["pane_id"]] = time.time()
                            mark_dashboard_dirty()
                            mark_bar_dirty()

                    elif msg.get("type") == "blocked":
                        await notify_blocked_safely(app, msg)
                        mark_dashboard_dirty()
                clear_relay_connection_state()
        except Exception as e:
            clear_relay_connection_state()
            log.warning(f"Relay connection lost: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


# --- Main ---

async def post_init(app: Application):
    """Register the typing-"/" command menu, scoped to the authorized chat.

    A failure here (network, unsupported scope) must never stop the bot from
    starting: the menu is a hint, not a requirement.
    """
    if not CHAT_ID:
        return
    try:
        await app.bot.set_my_commands(
            [BotCommand(command=cmd, description=desc) for cmd, desc in COMMAND_MENU],
            scope=BotCommandScopeChat(chat_id=int(CHAT_ID)),
        )
        log.info("command menu registered for chat %s", CHAT_ID)
    except Exception as e:
        log.warning("failed to register command menu: %s", scrub(e))


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    if CHAT_ID:
        auth_filter = filters.Chat(chat_id=int(CHAT_ID))
    else:
        auth_filter = filters.ALL  # No restriction in discovery mode

    app.add_handler(CommandHandler("start", cmd_start, filters=auth_filter))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard, filters=auth_filter))
    app.add_handler(CommandHandler("switch", cmd_switch, filters=auth_filter))
    app.add_handler(CommandHandler("project", cmd_project, filters=auth_filter))
    app.add_handler(CommandHandler("find", cmd_find, filters=auth_filter))
    app.add_handler(CommandHandler("unfocus", cmd_unfocus, filters=auth_filter))
    app.add_handler(CommandHandler("agents", cmd_agents, filters=auth_filter))
    app.add_handler(CommandHandler("status", cmd_status, filters=auth_filter))
    app.add_handler(CommandHandler("read", cmd_read, filters=auth_filter))
    app.add_handler(CommandHandler("reply", cmd_reply, filters=auth_filter))
    app.add_handler(CommandHandler("send", cmd_send, filters=auth_filter))
    app.add_handler(CommandHandler("trust", cmd_trust, filters=auth_filter))
    app.add_handler(CommandHandler("digest", cmd_digest, filters=auth_filter))
    app.add_handler(CommandHandler("interrupt", cmd_interrupt, filters=auth_filter))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & auth_filter, handle_text))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        load_state()
        async with app:
            await app.start()
            # The manual lifecycle (app.start + updater.start_polling) does NOT
            # run the builder's post_init -- only run_polling/run_webhook do --
            # so the command menu must be registered explicitly here.
            await post_init(app)
            await app.updater.start_polling()
            dashboard_task = asyncio.create_task(dashboard_loop(app))
            try:
                await relay_listener(app)
            finally:
                dashboard_task.cancel()

    loop.run_until_complete(run())


if __name__ == "__main__":
    main()
