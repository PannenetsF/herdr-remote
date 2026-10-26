#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["python-telegram-bot>=21.0", "websockets>=14.0"]
# ///
"""herdr-remote Telegram bot — monitor and approve agents from Telegram."""
import asyncio, hashlib, json, os, logging, secrets
import re
from collections import Counter, OrderedDict

from telegram import ForceReply, Update, InlineKeyboardButton, InlineKeyboardMarkup
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

AGENT_PAGE_SIZE = 20
PENDING_LIMIT = 500
COMMAND_ACK_TIMEOUT = 20
PANE_READ_TIMEOUT = 20
STATUS_ORDER = {"blocked": 0, "working": 1, "done": 2, "idle": 3, "unknown": 3}
STATUS_LABELS = {"blocked": "BLOCKED", "working": "WORKING", "done": "DONE", "idle": "IDLE", "unknown": "IDLE"}
ACTION_CODES = {
    "read": "r",
    "interrupt": "i",
    "select_send": "s",
    "select_reply": "q",
    "trust": "t",
    "approval": "k",
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
FINISHED_EXCERPT_MAX = 2600  # utf-16 units budget for the finished excerpt
TG_MSG_MAX = 4096            # Telegram hard cap per message, in utf-16 units
_HISTORY_ROLE_LABELS = {"user": "you", "assistant": "agent", "note": "note"}


def _u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


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


async def last_assistant_excerpt(pane_id: str) -> str:
    """The agent's final message as HTML, for attaching to a finished notification."""
    history = await fetch_history(pane_id, limit=8)
    for turn in reversed(history.get("messages", [])):
        if turn.get("role") != "assistant":
            continue
        text = (turn.get("text") or "").strip()
        if text:
            return md_to_html(text, max_units=FINISHED_EXCERPT_MAX)
    return ""


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


def _code_block(content: str) -> str:
    # MarkdownV2 fenced block: inside ```...``` only backslash and backtick need escaping.
    escaped = content.replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{escaped}\n```"


_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def _md_escape(text: str) -> str:
    # Every MarkdownV2 special outside a code block must be backslash-escaped, or a project name
    # with a `-` or `.` in it breaks the whole message's parsing.
    return "".join("\\" + c if c in _MDV2_SPECIAL else c for c in text)


async def show_pane_view(bot, chat_id: int, message, pane_id: str, header: str, content: str):
    """One live-view message per pane, edited in place on every read.

    Repeated /read of the same agent used to post a fresh message each time, so a few commands
    buried the chat in output to scroll past. Editing the previous view instead keeps one message
    that refreshes -- closer to watching a terminal than reading a log. content arrives as
    pre-capped, balanced Telegram HTML (rendered transcript, or a <pre> raw-capture fallback),
    so it must never be tail-sliced here. Falls back to a new message if the old
    one is gone (deleted, or too old for Telegram to edit).
    """
    text = f"<b>{_html_escape(header)}</b>\n{content}"
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


def clear_relay_connection_state():
    global agents, relay_connected
    relay_connected = False
    agents = []
    approval_tokens.clear()
    blocked_prompt_ids.clear()


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
    if action == "interrupt":
        return [agent for agent in agents if agent.get("status") in ("working", "blocked")]
    if action == "trust":
        return [agent for agent in agents if agent.get("status") == "blocked"]
    return list(agents)


def sorted_agents(agent_list: list[dict]) -> list[dict]:
    return sorted(agent_list, key=lambda agent: (
        STATUS_ORDER.get(agent.get("status", "unknown"), 3),
        agent.get("project", "").lower(),
        agent.get("agent", "").lower(),
        agent.get("host", "local").lower(),
        agent.get("pane_id", ""),
    ))


def compact_identifier(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha1(value.encode()).hexdigest()[:12]
    return value[:limit - 15] + "..." + digest


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


def agent_button_labels(agent_list: list[dict]) -> list[str]:
    bases = []
    contexts = []
    for agent in agent_list:
        status = STATUS_LABELS.get(agent.get("status", "unknown"), "UNKNOWN")
        project = agent.get("project") or agent.get("cwd") or "unknown"
        name = agent.get("agent") or "agent"
        host = agent.get("host", "local")
        bases.append(f"[{status}] {project} ({name})")
        contexts.append(f" @{compact_identifier(host, 24)}" if host != "local" else "")

    provisional = [base[:max(1, 64 - len(context))] + context for base, context in zip(bases, contexts)]
    counts = Counter(provisional)
    labels = []
    for agent, base, context, candidate in zip(agent_list, bases, contexts, provisional):
        if counts[candidate] == 1:
            labels.append(candidate)
            continue
        pane_id = compact_identifier(agent.get("pane_id", "?"), 18)
        suffix = context + f" [{pane_id}]"
        labels.append(base[:max(1, 64 - len(suffix))] + suffix)
    unique_labels = []
    used = set()
    for label in labels:
        candidate = label
        ordinal = 2
        while candidate in used:
            marker = f" #{ordinal}"
            candidate = label[:64 - len(marker)] + marker
            ordinal += 1
        used.add(candidate)
        unique_labels.append(candidate)
    return unique_labels


def build_agent_keyboard(action: str, page: int = 0, agent_list: list[dict] | None = None) -> InlineKeyboardMarkup:
    ordered = sorted_agents(agents_for_action(action) if agent_list is None else agent_list)
    page_count = max(1, (len(ordered) + AGENT_PAGE_SIZE - 1) // AGENT_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * AGENT_PAGE_SIZE
    visible = ordered[start:start + AGENT_PAGE_SIZE]
    labels = agent_button_labels(ordered)[start:start + AGENT_PAGE_SIZE]
    keyboard = [[InlineKeyboardButton(
        label,
        callback_data=pane_callback_data(action, agent["pane_id"]),
    )] for agent, label in zip(visible, labels)]

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


def dashboard_text() -> str:
    if not relay_connected:
        text = "herdr-remote bot\n\nRelay disconnected. Use /status for connection details."
    elif not agents:
        text = "herdr-remote bot\n\nConnected to relay. No agents are running."
    else:
        blocked = sum(agent.get("status") == "blocked" for agent in agents)
        working = sum(agent.get("status") == "working" for agent in agents)
        done = sum(agent.get("status") == "done" for agent in agents)
        idle = len(agents) - blocked - working - done
        text = f"herdr-remote bot\n\nAgents: {len(agents)} ({blocked} blocked, {working} working, {done} done, {idle} idle)\nSelect an agent to read and reply."
    if not CHAT_ID:
        text += "\n\nChat ID: {chat_id}"
    return text


# --- Bot commands ---

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not authorized(update):
        return
    text = dashboard_text()
    if not CHAT_ID:
        text = text.format(chat_id=update.effective_chat.id)
    if relay_connected and agents:
        await update.message.reply_text(text, reply_markup=build_agent_keyboard("select_reply"))
        return
    await update.message.reply_text(text)


async def cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /agents — list current agents with status."""
    if not agents:
        await update.message.reply_text("No agents connected." if relay_connected else "Not connected to relay.")
        return

    blocked = [a for a in agents if a.get("status") == "blocked"]
    working = [a for a in agents if a.get("status") == "working"]
    done = [a for a in agents if a.get("status") == "done"]
    idle = [a for a in agents if a.get("status") in ("idle", "unknown")]

    lines = []
    if blocked:
        lines.append("BLOCKED:")
        for a in blocked:
            host = f" @{a['host']}" if a.get('host', 'local') != 'local' else ''
            lines.append(f"  {a['project']} ({a['agent']}){host}")
    if working:
        lines.append("WORKING:")
        for a in working:
            host = f" @{a['host']}" if a.get('host', 'local') != 'local' else ''
            lines.append(f"  {a['project']} ({a['agent']}){host}")
    if done:
        lines.append("DONE:")
        for a in done:
            host = f" @{a['host']}" if a.get('host', 'local') != 'local' else ''
            lines.append(f"  {a['project']} ({a['agent']}){host}")
    if idle:
        lines.append("IDLE:")
        for a in idle:
            host = f" @{a['host']}" if a.get('host', 'local') != 'local' else ''
            lines.append(f"  {a['project']} ({a['agent']}){host}")

    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /status — show connection info."""
    b = len([a for a in agents if a.get("status") == "blocked"])
    w = len([a for a in agents if a.get("status") == "working"])
    d = len([a for a in agents if a.get("status") == "done"])
    i = len([a for a in agents if a.get("status") in ("idle", "unknown")])

    status = "Connected" if relay_connected else "Disconnected"
    text = (
        f"Relay: {RELAY_WS_SAFE}\n"
        f"Status: {status}\n"
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

    import time
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
    """Send a Telegram reply to its associated agent pane."""
    if not authorized(update) or not update.message.reply_to_message:
        return
    pane_id = pending_pane(update.effective_chat.id, update.message.reply_to_message.message_id)
    if not pane_id:
        return
    if not relay_connected:
        await update.message.reply_text("Relay disconnected. Use /start after it reconnects.")
        return
    if find_agent(pane_id) is None:
        await update.message.reply_text(
            "That agent is no longer available. Refresh the list and choose another agent.",
            reply_markup=refresh_keyboard("select_reply"),
        )
        return

    try:
        prompt_id = blocked_prompt_ids.get(pane_id)
        if prompt_id:
            await send_to_relay(pane_id, update.message.text, prompt_id=prompt_id)
        else:
            await send_text_to_relay(pane_id, update.message.text)
        await update.message.reply_text("Sent")
    except Exception as e:
        await update.message.reply_text(f"Failed: {scrub(e)}")


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
):
    if not CHAT_ID:
        return
    instruction = (
        "Use the web terminal or manual terminal controls to select multiple options."
        if multi
        else "Use an approval button, open the output, or reply to this notification."
    )
    identity = identity_for_blocked({
        "pane_id": pane_id, "agent": agent, "project": project,
    })
    # Legacy Markdown mode: '*' and '`' are the only metacharacters this header
    # can contain from terminal titles, strip them rather than risk unbalanced
    # formatting swallowing the prompt.
    identity_md = identity.replace("`", "'").replace("*", " ")
    text = (
        f"*{identity_md}* BLOCKED\n\n```\n{prompt[:400]}\n```\n\n"
        f"{instruction}"
    )
    generation = secrets.token_hex(4)
    keyboard = make_keyboard(
        pane_id,
        [] if multi else options,
        generation=generation,
        interaction=interaction,
    )
    msg = await app.bot.send_message(
        chat_id=int(CHAT_ID), text=text, parse_mode="Markdown", reply_markup=keyboard
    )
    approval_tokens[pane_id] = generation
    if prompt_id:
        blocked_prompt_ids[pane_id] = prompt_id
    register_pending(int(CHAT_ID), msg.message_id, pane_id)


async def notify_blocked_safely(app: Application, msg: dict):
    if msg.get("update"):
        # Same blocked streak, re-hashed content: the prompt_id the relay will demand has
        # moved, so keep ours current or every tap after the redraw fails as "prompt changed".
        if msg.get("pane_id") in blocked_prompt_ids and msg.get("prompt_id"):
            blocked_prompt_ids[msg["pane_id"]] = msg["prompt_id"]
        return
    try:
        await notify_blocked(
            app,
            pane_id=msg["pane_id"],
            agent=msg.get("agent", "unknown"),
            project=msg.get("project", ""),
            prompt=msg.get("prompt", ""),
            options=msg.get("options"),
            prompt_id=msg.get("prompt_id"),
            interaction=msg.get("interaction"),
            multi=bool(msg.get("multi")),
        )
    except Exception as e:
        log.warning("Failed to send blocked notification: %s", scrub(e))


# --- Relay listener ---

async def track_agent_updates(app: Application, updated_agents: list[dict]):
    """Track status transitions for full snapshots and pane-level updates."""
    if not CHAT_ID:
        return

    import time
    now = time.time()
    for agent_data in updated_agents:
        pane_id = agent_data["pane_id"]
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

        if old_status and old_status != new_status and new_status in ("idle", "done") and old_status in ("working", "blocked"):
            try:
                excerpt = await last_assistant_excerpt(pane_id)
                # Rendered-transcript HTML: bold/lists/tables come through instead
                # of raw markdown source. Both branches use HTML consistently.
                if excerpt:
                    body = (
                        f"<b>{_html_escape(agent_identity(agent_data))}</b> finished:\n\n"
                        f"{excerpt}\n\n"
                        "Open the full output below, or reply to this notification to send a follow-up."
                    )
                else:
                    body = (
                        f"<b>{_html_escape(agent_identity(agent_data))}</b> finished.\n\n"
                        "Open the full output below, or reply to this notification to send a follow-up."
                    )
                msg = await app.bot.send_message(
                    chat_id=int(CHAT_ID), text=body,
                    parse_mode="HTML",
                    reply_markup=interaction_keyboard(pane_id),
                )
                register_pending(int(CHAT_ID), msg.message_id, pane_id)
            except Exception as e:
                log.warning("Failed to send completion notification: %s", scrub(e))
        prev_statuses[pane_id] = new_status


async def relay_listener(app: Application):
    """Persistent WebSocket connection to relay."""
    import websockets
    global agents, relay_connected, prev_statuses

    while True:
        try:
            async with websockets.connect(RELAY_WS) as ws:
                relay_connected = True
                log.info(f"Connected to relay at {RELAY_WS_SAFE}")
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

                    elif msg.get("type") == "agent_update":
                        updated_agent = msg.get("agent") or {}
                        if updated_agent.get("pane_id"):
                            await track_agent_updates(app, [updated_agent])
                            agents = apply_agent_message(agents, msg)

                    elif msg.get("type") == "blocked":
                        await notify_blocked_safely(app, msg)
                clear_relay_connection_state()
        except Exception as e:
            clear_relay_connection_state()
            log.warning(f"Relay connection lost: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


# --- Main ---

def main():
    app = Application.builder().token(TOKEN).build()
    if CHAT_ID:
        auth_filter = filters.Chat(chat_id=int(CHAT_ID))
    else:
        auth_filter = filters.ALL  # No restriction in discovery mode

    app.add_handler(CommandHandler("start", cmd_start, filters=auth_filter))
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
        async with app:
            await app.start()
            await app.updater.start_polling()
            await relay_listener(app)

    loop.run_until_complete(run())


if __name__ == "__main__":
    main()
