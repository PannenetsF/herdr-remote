import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
import uuid


TELEGRAM_PATH = Path(__file__).resolve().parents[1] / "relay" / "herdr_telegram.py"


def _telegram_stubs():
    telegram = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class ForceReply:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class KeyboardButton:
        def __init__(self, text, **kwargs):
            self.text = text

    class ReplyKeyboardMarkup:
        def __init__(self, keyboard, **kwargs):
            # Real PTB coerces plain string rows into KeyboardButton rows.
            self.keyboard = [
                [b if hasattr(b, "text") else KeyboardButton(b) for b in row]
                for row in keyboard
            ]
            self.kwargs = kwargs
            self.input_field_placeholder = kwargs.get("input_field_placeholder")

    class BotCommand:
        def __init__(self, command, description, **kwargs):
            self.command = command
            self.description = description

    class BotCommandScopeChat:
        def __init__(self, chat_id, **kwargs):
            self.chat_id = chat_id

    telegram.Update = object
    telegram.ForceReply = ForceReply
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
    telegram.ReplyKeyboardMarkup = ReplyKeyboardMarkup
    telegram.BotCommand = BotCommand
    telegram.BotCommandScopeChat = BotCommandScopeChat

    ext = types.ModuleType("telegram.ext")
    ext.Application = object
    ext.CallbackQueryHandler = object
    ext.CommandHandler = object
    ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    ext.MessageHandler = object
    ext.filters = types.SimpleNamespace()
    return {"telegram": telegram, "telegram.ext": ext}


def loaded_telegram():
    module_name = f"herdr_telegram_test_{uuid.uuid4().hex}"
    with mock.patch.dict(os.environ, {"HERDR_TG_TOKEN": "test"}, clear=False), mock.patch.dict(
        sys.modules, _telegram_stubs(), clear=False
    ), mock.patch.object(sys, "path", [str(TELEGRAM_PATH.parent), *sys.path]):
        spec = importlib.util.spec_from_file_location(module_name, TELEGRAM_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=42)


def _rec(pane_id, *, project, status="idle", label="", title="",
         host="local", agent="claude", cwd=None):
    return {
        "pane_id": pane_id, "agent": agent, "label": label, "status": status,
        "project": project, "cwd": cwd or f"/work/{project}", "host": host, "title": title,
    }


def _thread_rec(pane_id, slug, n, status="working"):
    t = f"t-{n:04d}"
    return {
        "pane_id": pane_id, "agent": "claude", "label": f"hp-{slug}-{t}",
        "status": status, "project": t,
        "cwd": f"/Users/me/.herdr-projects/{slug}/threads/{t}",
        "host": "local", "title": "",
    }


class _FakeMessage:
    def __init__(self, chat_id=42, chat_type="private"):
        self.replies = []
        self.chat_id = chat_id
        self.chat = types.SimpleNamespace(id=chat_id, type=chat_type)
        self.reply_to_message = None
        self.text = ""

    async def reply_text(self, text, **kwargs):
        sent = types.SimpleNamespace(message_id=100 + len(self.replies))
        self.replies.append((text, kwargs, sent))
        return sent


class _FakeCallback:
    def __init__(self, data, message=None):
        self.data = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        self.message = message or _FakeMessage()
        self.answers = []
        self.edited_markup = None
        self.text_edits = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edited_markup = reply_markup

    async def edit_message_text(self, text, **kwargs):
        self.text_edits.append((text, kwargs))


class _FakeCtxBot:
    """Bot stand-in for callback flows: bar refresh + message deletes."""
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id, text, **kwargs):
        msg = types.SimpleNamespace(message_id=500 + len(self.sent))
        self.sent.append((chat_id, text, kwargs, msg))
        return msg

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _update(message=None, callback=None, chat_id=42, chat_type="private"):
    chat = types.SimpleNamespace(id=chat_id, type=chat_type)
    return types.SimpleNamespace(
        effective_chat=chat,
        message=message if message is not None else (callback.message if callback else _FakeMessage()),
        callback_query=callback,
    )


def _flat(markup):
    return [b for row in markup.inline_keyboard for b in row]


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    REQUIRED = ("switch", "dashboard", "agents", "read", "trust",
                "interrupt", "unfocus", "status", "digest")

    async def test_post_init_registers_scoped_chinese_menu(self):
        tg = loaded_telegram()
        tg.CHAT_ID = "42"

        class CmdBot:
            def __init__(self):
                self.calls = []

            async def set_my_commands(self, commands, scope=None):
                self.calls.append((commands, scope))

        bot = CmdBot()
        await tg.post_init(types.SimpleNamespace(bot=bot))
        self.assertEqual(len(bot.calls), 1)
        commands, scope = bot.calls[0]
        names = [c.command for c in commands]
        for name in self.REQUIRED:
            self.assertIn(name, names)
        # Descriptions are short Chinese hints.
        for cmd in commands:
            self.assertTrue(cmd.description.strip())
            self.assertTrue(any("一" <= ch <= "鿿" for ch in cmd.description))
        self.assertEqual(scope.chat_id, 42)

    async def test_failure_to_register_never_crashes_startup(self):
        tg = loaded_telegram()
        tg.CHAT_ID = "42"

        class BadBot:
            async def set_my_commands(self, commands, scope=None):
                raise RuntimeError("network down")

        await tg.post_init(types.SimpleNamespace(bot=BadBot()))  # must not raise

    async def test_discovery_mode_registers_nothing(self):
        tg = loaded_telegram()
        tg.CHAT_ID = ""

        class CmdBot:
            async def set_my_commands(self, commands, scope=None):
                raise AssertionError("must not register without a locked chat")

        await tg.post_init(types.SimpleNamespace(bot=CmdBot()))


class PendingBarShortcutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tg = loaded_telegram()
        self.tg.CHAT_ID = "42"
        self.tg.relay_connected = True
        self.tg.agents = [
            _rec("wT:pB", project="nix-hm", status="blocked", label="hpc-nix-hm",
                 title="stuck on a prompt"),
            _rec("wT:pA", project="lagrange", status="working", label="hpc-lagrange",
                 title="training run"),
            _thread_rec("wT:pT", "lagrange", 7, status="blocked"),
        ]

    async def _tap(self, label):
        tg = self.tg
        message = _FakeMessage()
        message.text = label
        with mock.patch.object(tg, "send_text_to_relay", mock.AsyncMock()) as send_text:
            await tg.handle_text(_update(message=message), types.SimpleNamespace(bot=_FakeCtxBot()))
        send_text.assert_not_awaited()
        return message

    async def test_non_empty_shows_blocked_picker_only(self):
        message = await self._tap(self.tg.BAR_PENDING_LABEL)
        text, kwargs, _sent = message.replies[0]
        self.assertIn("待办", text)
        pane_buttons = [
            b for b in _flat(kwargs["reply_markup"])
            if self.tg.parse_callback_data(b.callback_data).get("action") == "switch"
        ]
        self.assertEqual(len(pane_buttons), 1)
        self.assertEqual(
            self.tg.parse_callback_data(pane_buttons[0].callback_data)["pane_id"], "wT:pB"
        )
        # The blocked filter chip is the lit one.
        chips = [
            b for b in _flat(kwargs["reply_markup"])
            if self.tg.parse_callback_data(b.callback_data).get("action") == "switch-filter"
        ]
        self.assertTrue(any(b.text.startswith("●") and "待办" in b.text for b in chips))

    async def test_empty_answers_briefly(self):
        self.tg.agents = [a for a in self.tg.agents if a.get("status") != "blocked"]
        message = await self._tap(self.tg.BAR_PENDING_LABEL)
        self.assertEqual(message.replies[0][0], "当前没有待办。")


class ProjectDrillDownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tg = loaded_telegram()
        self.tg.CHAT_ID = "42"
        self.tg.relay_connected = True
        self.tg.agents = [
            # zebra: 2 blocked -> sorts first
            _rec("wT:p1", project="zebra", status="blocked", title="z block 1"),
            _rec("wT:p2", project="zebra", status="blocked", title="z block 2"),
            # alpha: 1 blocked, 1 working; plus a hidden thread worker (blocked)
            _rec("wT:p3", project="alpha", status="blocked", title="a block"),
            _rec("wT:p4", project="alpha", status="working", title="a run"),
            _thread_rec("wT:p9", "alpha", 1, status="blocked"),
            # mid: no blocked, 1 working + 1 idle -> only ▶️ count
            _rec("wT:p5", project="mid", status="working", title="m run"),
            _rec("wT:p6", project="mid", status="idle", title="m idle"),
        ]

    def test_project_list_order_counts_and_human_only(self):
        tg = self.tg
        buttons = _flat(tg.build_project_keyboard())
        labels = [b.text for b in buttons]
        self.assertEqual(len(buttons), 3)  # thread worker adds no project row
        # blocked-count desc, then name
        self.assertTrue(labels[0].startswith("📁 zebra"))
        self.assertTrue(labels[1].startswith("📁 alpha"))
        self.assertTrue(labels[2].startswith("📁 mid"))
        self.assertIn("🚫2", labels[0])
        self.assertIn("🚫1", labels[1])
        self.assertIn("▶️1", labels[1])
        self.assertNotIn("🚫", labels[2])
        self.assertIn("▶️1", labels[2])
        for b in buttons:
            self.assertLessEqual(len(b.callback_data.encode()), 64)

    def test_session_level_lists_only_that_projects_human_panes(self):
        tg = self.tg
        markup = tg.build_project_sessions_keyboard("alpha")
        pane_buttons = [
            b for b in _flat(markup)
            if tg.parse_callback_data(b.callback_data).get("action") == "switch"
        ]
        pane_ids = {tg.parse_callback_data(b.callback_data)["pane_id"] for b in pane_buttons}
        self.assertEqual(pane_ids, {"wT:p3", "wT:p4"})  # thread pane wT:p9 absent
        # The existing two-line style: topic line then project meta line.
        self.assertTrue(all("\n" in b.text for b in pane_buttons))
        actions = {tg.json.loads(b.callback_data).get("action") for b in _flat(markup)}
        self.assertIn("proj-list", actions)

    def test_back_button_returns_to_project_list(self):
        tg = self.tg
        markup = tg.build_project_sessions_keyboard("alpha")
        back = next(b for b in _flat(markup)
                    if tg.json.loads(b.callback_data).get("action") == "proj-list")
        self.assertIn("返回项目列表", back.text)

    def test_unknown_slug_has_no_keyboard(self):
        self.assertIsNone(self.tg.build_project_sessions_keyboard("ghost"))

    def test_pagination_for_many_sessions(self):
        tg = self.tg
        tg.agents = [
            _rec(f"wT:p{i:02d}", project="big", status="idle", title=f"session {i}")
            for i in range(tg.PROJECT_PAGE_SIZE + 3)
        ]
        first = tg.build_project_sessions_keyboard("big", page=0)
        pane_buttons = [
            b for b in _flat(first)
            if tg.parse_callback_data(b.callback_data).get("action") == "switch"
        ]
        self.assertEqual(len(pane_buttons), tg.PROJECT_PAGE_SIZE)
        nxt = next(b for b in _flat(first)
                   if tg.json.loads(b.callback_data).get("action") == "proj-page")
        self.assertEqual(nxt.text, "Next")
        second = tg.build_project_sessions_keyboard(
            "big", page=tg.json.loads(nxt.callback_data)["page"]
        )
        pane_buttons = [
            b for b in _flat(second)
            if tg.parse_callback_data(b.callback_data).get("action") == "switch"
        ]
        self.assertEqual(len(pane_buttons), 3)

    def test_long_slug_uses_hash_token_that_resolves_back(self):
        tg = self.tg
        long_slug = "a-very-long-project-slug-that-would-overflow-the-callback-budget-xyz"
        tg.agents = [_rec("wT:p1", project=long_slug, status="idle")]
        button = _flat(tg.build_project_keyboard())[0]
        raw = tg.json.loads(button.callback_data)
        self.assertLessEqual(len(button.callback_data.encode()), 64)
        self.assertNotIn("s", raw)
        self.assertEqual(tg._resolve_project_key(raw), long_slug)

    async def test_stale_slug_tap_offers_fresh_list(self):
        tg = self.tg
        cb = _FakeCallback({"action": "proj-open", "s": "ghost"})
        await tg.handle_callback(_update(callback=cb), types.SimpleNamespace(bot=_FakeCtxBot()))
        # Stale slug: no markup edit and a fresh project list is posted.
        self.assertIsNone(cb.edited_markup)
        self.assertTrue(cb.message.replies)
        caption, kwargs, _sent = cb.message.replies[0]
        self.assertIn("不在当前列表", caption)
        live = [tg.json.loads(b.callback_data).get("s")
                for b in _flat(kwargs["reply_markup"])]
        self.assertIn("zebra", live)

    async def test_project_then_session_tap_focuses_and_previews(self):
        tg = self.tg
        tg.read_views.clear()
        markup = tg.build_project_sessions_keyboard("alpha")
        target = next(
            b for b in _flat(markup)
            if tg.parse_callback_data(b.callback_data).get("pane_id") == "wT:p3"
        )
        cb = _FakeCallback(target.callback_data)
        with mock.patch.object(tg, "read_agent",
                               mock.AsyncMock(return_value=("<b>x</b>\n预览", False))):
            await tg.handle_callback(_update(callback=cb), types.SimpleNamespace(bot=_FakeCtxBot()))
        self.assertEqual(tg.active_pane, "wT:p3")
        text, _kwargs, _sent = cb.message.replies[0][0], cb.message.replies[0][1], None
        self.assertIn("已聚焦", cb.message.replies[0][0])

    async def test_bar_project_key_opens_list(self):
        tg = self.tg
        message = _FakeMessage()
        message.text = tg.BAR_PROJECT_LABEL
        await tg.handle_text(_update(message=message), types.SimpleNamespace(bot=_FakeCtxBot()))
        _caption, kwargs, _sent = message.replies[0]
        slugs = [tg.json.loads(b.callback_data).get("s")
                 for b in _flat(kwargs["reply_markup"])]
        self.assertEqual(slugs, ["zebra", "alpha", "mid"])


class BarMarkupStaticKeysTests(unittest.TestCase):
    def test_new_static_keys_own_row_and_never_drop(self):
        tg = loaded_telegram()
        tg.CHAT_ID = "42"
        tg.agents = []
        # The stub now mimics PTB and wraps row strings in KeyboardButton.
        rows = tg.build_bar_markup().keyboard
        # With no active pane the bar is the two static rows only.
        self.assertEqual(
            [[b.text for b in row] for row in rows],
            [[tg.BAR_PENDING_LABEL, tg.BAR_PROJECT_LABEL],
             [tg.BAR_SWITCH_LABEL, tg.BAR_DASHBOARD_LABEL]],
        )
        # With an active pane the live rows come first and ✖ joins the last row;
        # the two new static keys survive unchanged.
        tg.agents = [_rec("wT:p1", project="x", status="blocked")]
        tg.focus_pane("wT:p1")
        rows = tg.build_bar_markup().keyboard
        flat = [b.text for row in rows for b in row]
        self.assertIn(tg.BAR_PENDING_LABEL, flat)
        self.assertIn(tg.BAR_PROJECT_LABEL, flat)
        self.assertIn(tg.BAR_UNFOCUS_LABEL, flat)
        self.assertEqual(
            [b.text for b in rows[-2]], [tg.BAR_PENDING_LABEL, tg.BAR_PROJECT_LABEL]
        )


class AgentsPaginationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tg = loaded_telegram()
        self.tg.CHAT_ID = "42"
        self.tg.relay_connected = True

    def test_every_pane_reachable_and_pages_fit_telegram_limit(self):
        tg = self.tg
        # ~110+ identity lines at 3800 u16/page forces multiple pages.
        tg.agents = [
            _rec(f"w{i % 4}:p{i}", project=f"proj-{i % 7}", status="idle",
                 label="", title="")
            for i in range(320)
        ]
        pages = tg.agents_pages()
        self.assertGreater(len(pages), 1)
        reachable = "\n".join(pages)
        for i in range(320):
            self.assertIn(f"w{i % 4}:p{i}", reachable)
        for index, page in enumerate(pages):
            self.assertLessEqual(
                tg._u16len(tg.agents_page_text(index, pages)), tg.TG_MSG_MAX
            )
        # Status sections survive: all idle here.
        self.assertIn("IDLE:", pages[0])
        self.assertEqual(tg.build_agents_pager(0, 1), None)
        pager = tg.build_agents_pager(0, len(pages))
        self.assertEqual([b.text for b in _flat(pager)], ["Next"])

    def test_section_order_blocked_first(self):
        tg = self.tg
        tg.agents = [
            _rec("w:1", project="a", status="idle"),
            _rec("w:2", project="b", status="blocked"),
        ]
        text = tg.agents_pages()[0]
        self.assertLess(text.index("BLOCKED:"), text.index("IDLE:"))

    async def test_command_sends_first_page_with_pager_and_html(self):
        tg = self.tg
        tg.agents = [
            _rec(f"w{i % 4}:p{i}", project=f"proj-{i % 7}", status="idle")
            for i in range(320)
        ]
        message = _FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=42, type="private"),
            message=message,
        )
        await tg.cmd_agents(update, types.SimpleNamespace(args=[], bot=_FakeCtxBot()))
        text, kwargs, _sent = message.replies[0]
        self.assertEqual(kwargs.get("parse_mode"), "HTML")
        self.assertIn(f"1/{len(tg.agents_pages())}", text)
        self.assertTrue(kwargs.get("reply_markup"))

    async def test_pager_callback_edits_to_requested_page(self):
        tg = self.tg
        tg.agents = [
            _rec(f"w{i % 4}:p{i}", project=f"proj-{i % 7}", status="idle")
            for i in range(320)
        ]
        pages = tg.agents_pages()
        cb = _FakeCallback({"action": "agents-page", "page": 1})
        await tg.handle_callback(_update(callback=cb), types.SimpleNamespace(bot=_FakeCtxBot()))
        edited, kwargs = cb.text_edits[0]
        self.assertIn("2/" + str(len(pages)), edited)
        self.assertIn("reply_markup", kwargs)

    async def test_empty_herd_gets_hint_not_pager(self):
        tg = self.tg
        message = _FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=42, type="private"),
            message=message,
        )
        await tg.cmd_agents(update, types.SimpleNamespace(args=[], bot=_FakeCtxBot()))
        self.assertIn("No human sessions", message.replies[0][0])
        self.assertNotIn("reply_markup", message.replies[0][1])


class TelegramQuestionTests(unittest.TestCase):
    def test_multi_select_notification_only_offers_safe_output_link(self):
        telegram = loaded_telegram()
        telegram.CHAT_ID = "1"
        bot = _Bot()
        app = types.SimpleNamespace(bot=bot)

        asyncio.run(
            telegram.notify_blocked(
                app,
                pane_id="pane-1",
                agent="omp",
                project="demo",
                prompt="Which capabilities?",
                options=["Color", "Font"],
                multi=True,
            )
        )

        markup = bot.sent[0]["reply_markup"]
        self.assertEqual(
            [button.text for row in markup.inline_keyboard for button in row],
            ["Open output & reply"],
        )
        self.assertIn("web terminal or manual terminal controls", bot.sent[0]["text"])
        self.assertEqual(telegram.pending[(1, 42)], "pane-1")


if __name__ == "__main__":
    unittest.main()
