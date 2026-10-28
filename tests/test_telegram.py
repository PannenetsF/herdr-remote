#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["python-telegram-bot>=21.0", "websockets>=14.0"]
# ///
import asyncio
import ast
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))
os.environ.setdefault("HERDR_TG_TOKEN", "test-token")

tg = importlib.import_module("herdr_telegram")


def make_agents(count, *, status="idle", project="project"):
    return [
        {
            "pane_id": f"w{i}:p1",
            "agent": "opencode",
            "status": status,
            "project": project,
            "cwd": f"/work/{project}/{i}",
            "host": "local",
        }
        for i in range(count)
    ]


class FakeMessage:
    def __init__(self, chat_id=42, chat_type="private", message_id=10):
        self.replies = []
        self.message_id = message_id
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.reply_markup = None
        self.reply_to_message = None
        self.text = ""

    async def reply_text(self, text, **kwargs):
        sent = SimpleNamespace(message_id=self.message_id * 100 + len(self.replies), chat_id=self.chat_id)
        self.replies.append((text, kwargs, sent))
        return sent


class FakeCallback:
    def __init__(self, data, chat_id=42, chat_type="private", message_id=10):
        self.data = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        self.message = FakeMessage(chat_id, chat_type, message_id)
        self.answers = []
        self.edited_markup = None
        self.edit_calls = 0

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edit_calls += 1
        self.edited_markup = reply_markup


class FakeBot:
    def __init__(self, fail=False, not_modified=False):
        self.sent = []
        self.edits = []
        self.fail = fail
        self.not_modified = not_modified

    async def send_message(self, chat_id, text, **kwargs):
        message = SimpleNamespace(message_id=500 + len(self.sent), chat_id=chat_id)
        self.sent.append((chat_id, text, kwargs, message))
        return message

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append((chat_id, message_id, text, kwargs))
        if self.not_modified:
            raise Exception("Message is not modified")
        if self.fail:
            raise Exception("message to edit not found")

    async def delete_message(self, chat_id, message_id):
        self.deleted = getattr(self, "deleted", [])
        self.deleted.append((chat_id, message_id))


class FakeRelayConnection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return json.dumps(next(self.responses))

    def __aiter__(self):
        async def empty_messages():
            if False:
                yield None
        return empty_messages()


def make_update(chat_id=42, chat_type="private", callback=None, message=None):
    if callback is not None:
        callback.message.chat_id = chat_id
        callback.message.chat = SimpleNamespace(id=chat_id, type=chat_type)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        message=message or FakeMessage(chat_id, chat_type),
        callback_query=callback,
    )


def make_context(args=None, bot=None):
    return SimpleNamespace(args=args or [], bot=bot or FakeBot())


def make_active_approval_keyboard(pane_id, options):
    markup = tg.make_keyboard(pane_id, options)
    generation = json.loads(markup.inline_keyboard[0][0].callback_data)["g"]
    tg.approval_tokens[pane_id] = generation
    tg.blocked_prompt_ids[pane_id] = "prompt-123"
    return markup


def reset_focus_state():
    tg.active_pane = None
    tg.recent_panes.clear()
    tg.bar.update(dirty=False, last_push=0.0, sig=None)


class TelegramDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_chat_id = tg.CHAT_ID
        self.old_dashboard_msg_id = tg.dashboard_msg_id
        tg.CHAT_ID = "42"
        tg.agents = []
        tg.relay_connected = False
        tg.pending.clear()
        tg.approval_tokens.clear()
        tg.blocked_prompt_ids.clear()
        tg.prev_statuses.clear()
        tg.daily_stats.clear()
        tg.read_views.clear()
        tg.pane_messages.clear()
        tg.pane_seen.clear()
        tg.done_at.clear()
        reset_focus_state()
        tg.dashboard_msg_id = None
        tg.dash.update(dirty=False, last_push=0.0, last_text=None, last_sig=None)

    def tearDown(self):
        tg.CHAT_ID = self.old_chat_id
        tg.dashboard_msg_id = self.old_dashboard_msg_id
        tg.agents = []
        tg.relay_connected = False
        tg.approval_tokens.clear()
        tg.prev_statuses.clear()
        tg.daily_stats.clear()
        tg.pane_messages.clear()
        tg.pane_seen.clear()
        tg.done_at.clear()
        reset_focus_state()

    async def test_start_rejects_unauthorized_chat(self):
        update = make_update(chat_id=7)

        await tg.cmd_start(update, SimpleNamespace(args=[]))

        self.assertEqual(update.message.replies, [])

    async def test_start_preserves_chat_discovery_mode(self):
        tg.CHAT_ID = ""
        update = make_update(chat_id=-123)

        await tg.cmd_start(update, SimpleNamespace(args=[]))

        self.assertIn("-123", update.message.replies[0][0])
        self.assertIn("HERDR_TG_CHAT_ID", update.message.replies[0][0])

    async def test_start_refreshes_dashboard_without_posting_disconnected_card(self):
        bot = FakeBot()
        disconnected = make_update()
        await tg.cmd_start(disconnected, SimpleNamespace(args=[], bot=bot))
        # No dashboard while relay is down; only the (immediately deleted)
        # persistent-bar placeholder goes out, so nothing visible remains.
        visible = [s for s in bot.sent if s[1] != tg.BAR_PLACEHOLDER]
        self.assertEqual(visible, [])
        self.assertTrue(bot.deleted)
        self.assertIn("dashboard", disconnected.message.replies[0][0].lower())

        tg.relay_connected = True
        empty = make_update()
        await tg.cmd_start(empty, SimpleNamespace(args=[], bot=bot))
        # Connected with no panes: the dashboard gets posted to the chat directly.
        posted = [s for s in bot.sent if s[1] != tg.BAR_PLACEHOLDER]
        self.assertEqual(len(posted), 1)
        self.assertIn("no human sessions", posted[0][1].lower())

    async def test_start_dashboard_offers_picker_for_sixteen_agent_herd(self):
        tg.relay_connected = True
        tg.agents = make_agents(16)
        bot = FakeBot()
        update = make_update()

        await tg.cmd_start(update, SimpleNamespace(args=[], bot=bot))

        # Dashboard collapses 16 idle panes to a count and exposes the picker.
        posted = [s for s in bot.sent if s[1] != tg.BAR_PLACEHOLDER]
        _chat, text, kwargs, _msg = posted[0]
        self.assertIn("16 panes", text)
        self.assertIn("💤 16 idle", text)
        dash_buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual([b.text for b in dash_buttons], ["📋 All panes"])

        # The picker behind it still lists all 16 (one page, select_reply).
        callback = FakeCallback(dash_buttons[0].callback_data)
        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())
        markup = callback.edited_markup
        agent_buttons = [row[0] for row in markup.inline_keyboard if row[0].text not in ("Previous", "Next")]
        self.assertEqual(len(agent_buttons), 16)
        self.assertTrue(all(tg.parse_callback_data(b.callback_data)["action"] == "select_reply" for b in agent_buttons))

    def test_labels_sort_status_and_disambiguate_duplicate_agents(self):
        agent_list = [
            *make_agents(2, status="idle", project="same"),
            *make_agents(1, status="working", project="work"),
            *make_agents(1, status="blocked", project="blocked"),
        ]
        agent_list[-1]["host"] = "remote.example"

        markup = tg.build_agent_keyboard("read", agent_list=agent_list)
        labels = [row[0].text for row in markup.inline_keyboard]

        self.assertTrue(labels[0].startswith("[BLOCKED]"))
        self.assertTrue(labels[1].startswith("[WORKING]"))
        self.assertIn("remote.example", labels[0])
        duplicate_labels = [label for label in labels if "same" in label]
        self.assertEqual(len(set(duplicate_labels)), 2)
        self.assertTrue(all("w" in label and ":p1" in label for label in duplicate_labels))

    def test_large_keyboard_paginates_without_omitting_agents(self):
        agent_list = make_agents(tg.AGENT_PAGE_SIZE + 5)
        tg.agents = agent_list

        first = tg.build_agent_keyboard("read", page=0, agent_list=agent_list)
        second = tg.build_agent_keyboard("read", page=1, agent_list=agent_list)
        first_ids = [tg.parse_callback_data(row[0].callback_data)["pane_id"] for row in first.inline_keyboard[:-1]]
        second_ids = [tg.parse_callback_data(row[0].callback_data)["pane_id"] for row in second.inline_keyboard[:-1]]

        self.assertEqual(len(first_ids), tg.AGENT_PAGE_SIZE)
        self.assertEqual(len(second_ids), 5)
        self.assertEqual(len(set(first_ids + second_ids)), tg.AGENT_PAGE_SIZE + 5)

    def test_long_labels_remain_unique_and_preserve_remote_host(self):
        long_prefix = "project-" + "x" * 70
        agent_list = make_agents(3)
        agent_list[0]["project"] = long_prefix + "-one"
        agent_list[1]["project"] = long_prefix + "-two"
        agent_list[2]["project"] = long_prefix + "-three"
        agent_list[2]["host"] = "remote-" + "host" * 30 + ".example"

        markup = tg.build_agent_keyboard("read", agent_list=agent_list)
        labels = [row[0].text for row in markup.inline_keyboard]

        self.assertEqual(len(set(labels)), 3)
        self.assertTrue(any("@remote-" in label for label in labels))
        self.assertTrue(all(len(label) <= 64 for label in labels))

    def test_compacted_pane_hash_collision_still_has_unique_labels(self):
        agent_list = make_agents(2, project="same")
        agent_list[0]["pane_id"] = "sameprefx-very-long-pane-id-2606"
        agent_list[1]["pane_id"] = "sameprefx-very-long-pane-id-3604"

        markup = tg.build_agent_keyboard("read", agent_list=agent_list)
        labels = [row[0].text for row in markup.inline_keyboard]

        self.assertEqual(len(set(labels)), 2)
        self.assertTrue(all(len(label) <= 64 for label in labels))

    def test_all_pane_callbacks_fit_telegram_byte_limit(self):
        pane_id = "pane-" + "кирилица" * 100
        tg.agents = make_agents(1)
        tg.agents[0]["pane_id"] = pane_id
        markups = [
            tg.build_agent_keyboard(action)
            for action in ("read", "interrupt", "select_send", "select_reply", "trust")
        ]
        markups.extend([tg.make_keyboard(pane_id, None), tg.interaction_keyboard(pane_id)])

        callbacks = [
            button.callback_data
            for markup in markups
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertTrue(all(len(callback.encode()) <= 64 for callback in callbacks))

    async def test_read_and_filtered_trust_pickers_do_not_truncate(self):
        tg.agents = make_agents(16)
        read_update = make_update()
        await tg.cmd_read(read_update, SimpleNamespace(args=[]))
        self.assertEqual(len(read_update.message.replies[0][1]["reply_markup"].inline_keyboard), 16)

        tg.agents = make_agents(12, status="blocked") + make_agents(3, status="idle")
        trust_update = make_update()
        await tg.cmd_trust(trust_update, SimpleNamespace(args=[]))
        self.assertEqual(len(trust_update.message.replies[0][1]["reply_markup"].inline_keyboard), 12)

    async def test_repeated_read_edits_one_message_instead_of_stacking(self):
        # The whole point: /read the same agent twice and the second refreshes the first message
        # rather than posting a wall to scroll through.
        tg.agents = make_agents(1, project="api-v2.0")   # specials must not break MarkdownV2
        update = make_update()
        ctx = make_context(args=["api"])
        with patch.object(tg, "read_pane", AsyncMock(side_effect=["first out", "second out"])):
            await tg.cmd_read(update, ctx)   # posts the view
            await tg.cmd_read(update, ctx)   # edits it

        self.assertEqual(len(update.message.replies), 1)          # one message, not two
        self.assertEqual(len(ctx.bot.edits), 1)                    # second read edited it
        self.assertIn("second out", ctx.bot.edits[0][2])
        # pf.1 renders HTML: '>' is escaped, dots in project names stay literal.
        self.assertIn("api-v2.0 &gt; w0:p1 &gt; opencode", ctx.bot.edits[0][2])

    async def test_unchanged_output_does_not_repost(self):
        # Identical output -> Telegram "not modified": keep the view, post nothing new.
        tg.agents = make_agents(1)
        update = make_update()
        ctx = make_context(args=["project"], bot=FakeBot(not_modified=True))
        with patch.object(tg, "read_pane", AsyncMock(side_effect=["same", "same"])):
            await tg.cmd_read(update, ctx)
            await tg.cmd_read(update, ctx)

        self.assertEqual(len(update.message.replies), 1)

    async def test_read_reposts_when_the_view_message_is_gone(self):
        # If the old message can't be edited (deleted / too old), fall back to a fresh one.
        tg.agents = make_agents(1)
        update = make_update()
        ctx = make_context(args=["project"], bot=FakeBot(fail=True))
        with patch.object(tg, "read_pane", AsyncMock(side_effect=["one", "two"])):
            await tg.cmd_read(update, ctx)
            await tg.cmd_read(update, ctx)

        self.assertEqual(len(update.message.replies), 2)

    async def test_direct_read_send_and_reply_forms_preserve_behavior(self):
        tg.agents = make_agents(1)
        read_update = make_update()
        send_update = make_update()
        reply_update = make_update()

        with (
            patch.object(tg, "read_pane", AsyncMock(side_effect=["read output", "reply output"])),
            patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text,
        ):
            await tg.cmd_read(read_update, make_context(args=["project"]))
            await tg.cmd_send(send_update, SimpleNamespace(args=["project", "hello"]))
            await tg.cmd_reply(reply_update, SimpleNamespace(args=["project"]))

        self.assertIn("read output", read_update.message.replies[0][0])
        send_text.assert_awaited_once_with("w0:p1", "hello")
        self.assertIn("Sent to project", send_update.message.replies[0][0])
        reply_text, reply_kwargs, reply_message = reply_update.message.replies[0]
        self.assertIn("reply output", reply_text)
        self.assertIsInstance(reply_kwargs["reply_markup"], tg.ForceReply)
        self.assertEqual(tg.pending_pane(42, reply_message.message_id), "w0:p1")

    async def test_send_and_reply_pickers_target_the_expected_actions(self):
        tg.agents = make_agents(1)
        send_update = make_update()
        reply_update = make_update()

        await tg.cmd_send(send_update, SimpleNamespace(args=[]))
        await tg.cmd_reply(reply_update, SimpleNamespace(args=[]))

        send_button = send_update.message.replies[0][1]["reply_markup"].inline_keyboard[0][0]
        reply_button = reply_update.message.replies[0][1]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(tg.parse_callback_data(send_button.callback_data)["action"], "select_send")
        self.assertEqual(tg.parse_callback_data(reply_button.callback_data)["action"], "select_reply")

    async def test_done_agent_is_listed_but_finish_is_dashboard_only_without_alert(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="done", project="completed")
        update = make_update()

        await tg.cmd_agents(update, SimpleNamespace(args=[]))

        self.assertIn("DONE:", update.message.replies[0][0])
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "working"
        now = tg.time.time()
        await tg.track_agent_updates(app, tg.agents)
        # pf.2: no proactive completion message and no answer text; dashboard only.
        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.edits, [])
        self.assertIn("w0:p1", tg.done_at)
        self.assertLessEqual(now, tg.done_at["w0:p1"])
        # The finished pane is what the dashboard renders as a recent ✅ line.
        dashboard = tg.render_dashboard(tg.done_at["w0:p1"])
        self.assertIn("✅", dashboard)
        self.assertIn("completed", dashboard)

    async def test_blocked_alert_is_reused_through_resume_and_finish(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked", project="completed")
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)

        await tg.notify_blocked(
            app, "w0:p1", "opencode", "completed", "Allow tool?",
            ["yes, single permission", "trust, always allow", "no (tab to edit)"],
            prompt_id="prompt-1",
        )
        self.assertEqual(len(bot.sent), 1)
        message_id = bot.sent[0][3].message_id
        self.assertEqual(tg.pane_messages["w0:p1"], message_id)
        self.assertEqual(tg.pending_pane(42, message_id), "w0:p1")
        # Snapshot tracking runs before the blocked event in the real listener.
        tg.prev_statuses["w0:p1"] = "blocked"

        # Approval resolves it: the same message is edited into a resume line.
        tg.agents = make_agents(1, status="working", project="completed")
        await tg.track_agent_updates(app, tg.agents)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(bot.edits[0][1], message_id)
        self.assertIn("working", bot.edits[0][2])

        # Finishing edits the same message again into a ✅ one-liner with buttons.
        tg.agents = make_agents(1, status="done", project="completed")
        await tg.track_agent_updates(app, tg.agents)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 2)
        chat_id, edited_id, text, kwargs = bot.edits[1]
        self.assertEqual(edited_id, message_id)
        self.assertIn("finished", text)
        labels = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(labels, ["📖 Read", "💬 Reply"])
        actions = [tg.parse_callback_data(b.callback_data)["action"]
                   for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(actions, ["read", "select_reply"])

        # Re-blocking edits the same card once more, never posts a second alert.
        tg.agents = make_agents(1, status="blocked", project="completed")
        await tg.notify_blocked_safely(app, {
            "pane_id": "w0:p1", "agent": "opencode", "project": "completed",
            "prompt": "Allow again?", "options": ["yes", "no"], "prompt_id": "prompt-2",
        })
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 3)
        self.assertEqual(tg.blocked_prompt_ids["w0:p1"], "prompt-2")

    async def test_page_callback_rebuilds_from_latest_cache(self):
        tg.relay_connected = True
        tg.agents = make_agents(tg.AGENT_PAGE_SIZE + 5)
        callback = FakeCallback({"action": "page", "menu": "read", "page": 1})

        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        self.assertEqual(len(callback.edited_markup.inline_keyboard), 6)

    async def test_dashboard_selection_creates_private_reply_prompt(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        button = tg.build_agent_keyboard("select_reply").inline_keyboard[0][0]
        callback = FakeCallback(button.callback_data)

        with patch.object(tg, "read_agent", AsyncMock(return_value=("recent output", False))):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        text, kwargs, sent = callback.message.replies[0]
        self.assertIn("recent output", text)
        self.assertIsInstance(kwargs["reply_markup"], tg.ForceReply)
        # pf.1 identity header: project > pane id > session.
        self.assertTrue(kwargs["reply_markup"].input_field_placeholder.startswith("Reply to project >"))
        self.assertEqual(tg.pending_pane(42, sent.message_id), "w0:p1")

    async def test_stale_selection_offers_refresh(self):
        tg.relay_connected = True
        callback = FakeCallback({"action": "select_reply", "pane_id": "gone:pane"})

        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        text, kwargs, _ = callback.message.replies[0]
        self.assertIn("no longer available", text.lower())
        self.assertIn("reply_markup", kwargs)

    async def test_disconnected_callback_cannot_use_stale_agent_cache(self):
        tg.agents = make_agents(1)
        callback = FakeCallback({"action": "select_reply", "pane_id": "w0:p1"})

        with patch.object(tg, "read_pane", AsyncMock()) as read_pane:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        read_pane.assert_not_awaited()
        self.assertIn("disconnected", callback.message.replies[0][0].lower())

    async def test_callback_rechecks_command_eligibility(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="idle")
        callback = FakeCallback({"action": "trust", "pane_id": "w0:p1"})

        with patch.object(tg, "send_to_relay", AsyncMock()) as send_to_relay:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_to_relay.assert_not_awaited()
        self.assertIn("no longer available", callback.message.replies[0][0].lower())

    async def test_stale_approval_cannot_type_into_unblocked_agent(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        markup = make_active_approval_keyboard("w0:p1", ["yes", "no"])
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup
        tg.agents[0]["status"] = "working"

        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_keys.assert_not_awaited()
        self.assertIn("no longer available", callback.message.replies[0][0].lower())
        self.assertEqual(callback.edit_calls, 0)

    async def test_approval_transport_failure_preserves_controls(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        markup = make_active_approval_keyboard("w0:p1", ["yes", "no"])
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup

        with patch.object(tg, "send_keys_to_relay", AsyncMock(side_effect=OSError("offline"))):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        self.assertIn("failed", callback.message.replies[0][0].lower())
        # Buttons come off on the tap and go back on failure: two edits, original markup restored.
        self.assertEqual(callback.edit_calls, 2)
        self.assertIs(callback.edited_markup, markup)
        self.assertIn("w0:p1", tg.approval_tokens)

    async def test_approval_sends_key_and_removes_controls_on_success(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        markup = make_active_approval_keyboard("w0:p1", ["yes", "no"])
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup

        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_keys.assert_awaited_once_with("w0:p1", ["1"], prompt_id="prompt-123")
        self.assertEqual(callback.edit_calls, 1)
        self.assertIn("Sent: yes", callback.message.replies[0][0])
        self.assertNotIn("w0:p1", tg.approval_tokens)

    async def test_approval_from_previous_blocked_prompt_is_rejected(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        old_markup = make_active_approval_keyboard("w0:p1", ["old yes", "old no"])
        make_active_approval_keyboard("w0:p1", ["new yes", "new no"])
        callback = FakeCallback(old_markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = old_markup

        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_keys.assert_not_awaited()
        self.assertIn("older prompt", callback.message.replies[0][0].lower())
        self.assertEqual(callback.edit_calls, 0)

    async def test_legacy_approval_is_rejected_without_discarding_controls(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        make_active_approval_keyboard("w0:p1", ["yes", "no"])
        callback = FakeCallback({"pane_id": "w0:p1", "k": "1"})

        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_keys.assert_not_awaited()
        self.assertIn("older prompt", callback.message.replies[0][0].lower())
        self.assertEqual(callback.edit_calls, 0)

    async def test_send_keys_requires_positive_relay_acknowledgement(self):
        accepted = FakeRelayConnection([
            {"type": "agents", "agents": []},
            {"type": "command_result", "command": "send_keys", "ok": True, "request_id": "request-123"},
        ])
        with patch("websockets.connect", return_value=accepted), patch.object(
            tg.secrets, "token_hex", return_value="request-123"
        ):
            await tg.send_keys_to_relay("w0:p1", ["1"])
        self.assertEqual(accepted.sent, [{
            "type": "send_keys",
            "pane_id": "w0:p1",
            "keys": ["1"],
            "request_id": "request-123",
        }])

        rejected = FakeRelayConnection([{"type": "error", "message": "keys contain disallowed values"}])
        with patch("websockets.connect", return_value=rejected):
            with self.assertRaisesRegex(RuntimeError, "disallowed"):
                await tg.send_keys_to_relay("w0:p1", ["1"])

    async def test_read_pane_skips_arbitrarily_many_unrelated_messages(self):
        # The connect preamble (sessions, agents, one `blocked` per blocked
        # agent) can put any number of messages ahead of pane_content. A
        # fixed-count skip budget breaks once there are enough blocked
        # agents; the skip must be bounded by time, not by a message count.
        preamble = [{"type": "sessions", "sources": []}, {"type": "agents", "agents": []}]
        preamble += [{"type": "blocked", "pane_id": f"w{i}:p1"} for i in range(8)]
        preamble.append({"type": "pane_content", "content": "hello from pane"})
        connection = FakeRelayConnection(preamble)

        with patch("websockets.connect", return_value=connection):
            content = await tg.read_pane("w0:p1")

        self.assertEqual(content, "hello from pane")

    def test_relay_allows_numeric_approval_keys_and_acknowledges_them(self):
        relay_path = ROOT / "relay" / "herdr_relay.py"
        source = relay_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        safe_keys = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SAFE_KEYS" for target in node.targets)
        )
        values = eval(compile(ast.Expression(safe_keys), str(relay_path), "eval"), {"range": range})
        self.assertTrue({"1", "2", "3"}.issubset(values))
        self.assertIn('"command": "send_keys"', source)
        self.assertIn('"ok": True', source)
        self.assertIn("if result.returncode != 0:", source)

    async def test_omp_choice_uses_question_response_with_label_and_prompt_id(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.agents[0]["agent"] = "omp"
        tg.blocked_prompt_ids["w0:p1"] = "prompt-123"
        markup = tg.make_keyboard(
            "w0:p1",
            ["Red", "Blue"],
            generation="generation",
            interaction="omp_question",
        )
        tg.approval_tokens["w0:p1"] = "generation"
        callback = FakeCallback(markup.inline_keyboard[1][0].callback_data)
        callback.message.reply_markup = markup

        with (
            patch.object(tg, "send_to_relay", AsyncMock()) as send_to_relay,
            patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys,
        ):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_to_relay.assert_awaited_once_with("w0:p1", "Blue", prompt_id="prompt-123")
        send_keys.assert_not_awaited()

    def test_relay_disconnect_clears_approval_generations(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.approval_tokens["w0:p1"] = "generation"
        tg.blocked_prompt_ids["w0:p1"] = "prompt"

        tg.clear_relay_connection_state()

        self.assertFalse(tg.relay_connected)
        self.assertEqual(tg.agents, [])
        self.assertEqual(tg.approval_tokens, {})
        self.assertEqual(tg.blocked_prompt_ids, {})

    async def test_failed_blocked_notification_preserves_previous_generation(self):
        tg.approval_tokens["w0:p1"] = "previous"
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=OSError("offline"))))

        with self.assertRaisesRegex(OSError, "offline"):
            await tg.notify_blocked(app, "w0:p1", "opencode", "project", "prompt", ["yes", "no"])

        self.assertEqual(tg.approval_tokens["w0:p1"], "previous")

    async def test_blocked_notification_failure_does_not_disconnect_relay(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.approval_tokens["w0:p1"] = "previous"
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=OSError("offline"))))

        await tg.notify_blocked_safely(app, {
            "pane_id": "w0:p1",
            "agent": "opencode",
            "project": "project",
            "prompt": "prompt",
            "options": ["yes", "no"],
        })

        self.assertTrue(tg.relay_connected)
        self.assertEqual(tg.agents[0]["pane_id"], "w0:p1")
        self.assertEqual(tg.approval_tokens["w0:p1"], "previous")

    async def test_graceful_relay_close_clears_connection_state(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.approval_tokens["w0:p1"] = "generation"
        calls = 0

        def connect(_url):
            nonlocal calls
            calls += 1
            if calls == 1:
                return FakeRelayConnection([])
            self.assertFalse(tg.relay_connected)
            self.assertEqual(tg.agents, [])
            self.assertEqual(tg.approval_tokens, {})
            raise KeyboardInterrupt

        with patch("websockets.connect", side_effect=connect):
            with self.assertRaises(KeyboardInterrupt):
                await tg.relay_listener(SimpleNamespace())

    async def test_callback_rejects_unauthorized_chat(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        callback = FakeCallback({"action": "select_reply", "pane_id": "w0:p1"})

        await tg.handle_callback(make_update(chat_id=7, callback=callback), SimpleNamespace())

        self.assertEqual(callback.answers, ["Unauthorized"])
        self.assertEqual(callback.message.replies, [])

    def test_pending_registry_is_chat_scoped_and_bounded(self):
        tg.register_pending(42, 10, "local:pane")
        tg.register_pending(43, 10, "other:pane")
        self.assertEqual(tg.pending_pane(42, 10), "local:pane")
        self.assertEqual(tg.pending_pane(43, 10), "other:pane")

        for message_id in range(tg.PENDING_LIMIT + 2):
            tg.register_pending(42, 1000 + message_id, f"pane:{message_id}")
        self.assertEqual(len(tg.pending), tg.PENDING_LIMIT)
        self.assertIsNone(tg.pending_pane(42, 1000))

    async def test_blocked_notification_keeps_approvals_and_adds_interaction(self):
        bot = FakeBot()
        await tg.notify_blocked(
            SimpleNamespace(bot=bot),
            pane_id="w0:p1",
            agent="opencode",
            project="blocked-project",
            prompt="Allow tool?",
            options=["yes, single permission", "trust, always allow", "no (tab to edit)"],
        )

        chat_id, text, kwargs, sent = bot.sent[0]
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual([json.loads(row[0].callback_data)["k"] for row in rows[:3]], ["1", "2", "3"])
        self.assertEqual(rows[-1][0].text, "Open output & reply")
        self.assertIn("reply to this notification", text)
        self.assertEqual(tg.pending_pane(chat_id, sent.message_id), "w0:p1")

    async def test_group_prompts_keep_independent_pane_mappings(self):
        tg.relay_connected = True
        tg.agents = make_agents(2)
        first = FakeCallback({"action": "select_reply", "pane_id": "w0:p1"}, chat_type="group", message_id=10)
        second = FakeCallback({"action": "select_reply", "pane_id": "w1:p1"}, chat_type="group", message_id=11)

        with patch.object(tg, "read_pane", AsyncMock(return_value="output")):
            await tg.handle_callback(make_update(chat_type="group", callback=first), SimpleNamespace())
            await tg.handle_callback(make_update(chat_type="group", callback=second), SimpleNamespace())

        first_text, first_kwargs, first_sent = first.message.replies[0]
        second_text, second_kwargs, second_sent = second.message.replies[0]
        self.assertNotIn("reply_markup", first_kwargs)
        self.assertNotIn("reply_markup", second_kwargs)
        self.assertIn("Reply to this message", first_text)
        self.assertEqual(tg.pending_pane(42, first_sent.message_id), "w0:p1")
        self.assertEqual(tg.pending_pane(42, second_sent.message_id), "w1:p1")

        first_reply = FakeMessage(chat_type="group")
        first_reply.reply_to_message = SimpleNamespace(message_id=first_sent.message_id)
        first_reply.text = "first response"
        second_reply = FakeMessage(chat_type="group")
        second_reply.reply_to_message = SimpleNamespace(message_id=second_sent.message_id)
        second_reply.text = "second response"
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(chat_type="group", message=first_reply), SimpleNamespace())
            await tg.handle_text(make_update(chat_type="group", message=second_reply), SimpleNamespace())
        self.assertEqual(
            send_text.await_args_list,
            [unittest.mock.call("w0:p1", "first response"), unittest.mock.call("w1:p1", "second response")],
        )

    async def test_native_notification_reply_routes_to_associated_pane(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        tg.register_pending(42, 77, "w0:p1")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "follow up"

        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), SimpleNamespace())

        send_text.assert_awaited_once_with("w0:p1", "follow up")
        # pf.3 receipt carries the full identity and Read/switch controls, and
        # replying to the card also focuses that pane for later plain messages.
        receipt, receipt_kwargs = message.replies[0][0], message.replies[0][1]
        self.assertIn("已发送", receipt)
        self.assertIn("project &gt; w0:p1 &gt; opencode", receipt)
        self.assertEqual(tg.active_pane, "w0:p1")
        self.assertEqual(tg.recent_panes[0], "w0:p1")
        receipt_actions = [
            tg.parse_callback_data(b.callback_data)["action"]
            for row in receipt_kwargs["reply_markup"].inline_keyboard for b in row
        ]
        self.assertEqual(receipt_actions, ["read", "switch-open"])

    async def test_blocked_notification_reply_uses_prompt_response(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.blocked_prompt_ids["w0:p1"] = "prompt-123"
        tg.register_pending(42, 77, "w0:p1")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "custom answer"

        with (
            patch.object(tg, "send_to_relay", AsyncMock()) as send_response,
            patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text,
        ):
            await tg.handle_text(make_update(message=message), SimpleNamespace())

        send_response.assert_awaited_once_with(
            "w0:p1", "custom answer", prompt_id="prompt-123"
        )
        send_text.assert_not_awaited()

    async def test_mapped_reply_rejects_disconnected_and_stale_panes(self):
        tg.register_pending(42, 77, "w0:p1")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "follow up"

        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), SimpleNamespace())
            send_text.assert_not_awaited()
        self.assertIn("disconnected", message.replies[0][0].lower())

        tg.relay_connected = True
        message.replies.clear()
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), SimpleNamespace())
            send_text.assert_not_awaited()
        self.assertIn("no longer available", message.replies[0][0].lower())

    async def test_unauthorized_native_reply_is_ignored(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        tg.register_pending(7, 77, "w0:p1")
        message = FakeMessage(chat_id=7)
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "not allowed"

        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(chat_id=7, message=message), SimpleNamespace())

        send_text.assert_not_awaited()
        self.assertEqual(message.replies, [])

    async def test_duplicate_cross_host_pane_identity_fails_closed(self):
        tg.relay_connected = True
        tg.agents = make_agents(2)
        tg.agents[1]["pane_id"] = tg.agents[0]["pane_id"]
        tg.agents[1]["host"] = "remote.example"
        button = tg.build_agent_keyboard("select_reply").inline_keyboard[0][0]
        callback = FakeCallback(button.callback_data)

        with patch.object(tg, "read_pane", AsyncMock()) as read_pane:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        read_pane.assert_not_awaited()
        self.assertIn("no longer available", callback.message.replies[0][0].lower())

    async def test_oversized_native_reply_is_rejected_before_relay_send(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        tg.register_pending(42, 77, "w0:p1")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "x" * 10001

        await tg.handle_text(make_update(message=message), SimpleNamespace())

        self.assertIn("1-10000", message.replies[0][0])



class NumberedMenuKeyboardTests(unittest.TestCase):
    """A Claude-style numbered menu becomes one button per option, each pressing its number."""

    OPTIONS = [
        "Yes",
        "Yes, and always allow access to /tmp from this project",
        "Yes, and switch to auto mode · auto mode handles these prompts for you",
        "No",
    ]

    def test_buttons_press_their_option_number(self):
        markup = tg.make_keyboard("pane-1", self.OPTIONS, interaction="numbered")
        rows = markup.inline_keyboard
        self.assertEqual(len(rows), 5)  # four options + "Open output & reply"
        keys = [json.loads(row[0].callback_data)["k"] for row in rows[:4]]
        self.assertEqual(keys, ["1", "2", "3", "4"])
        self.assertEqual(rows[4][0].text, "Open output & reply")

    def test_labels_keep_number_drop_hint_and_fit_a_phone(self):
        markup = tg.make_keyboard("pane-1", self.OPTIONS, interaction="numbered")
        labels = [row[0].text for row in markup.inline_keyboard[:4]]
        self.assertEqual(labels[0], "1. Yes")
        self.assertEqual(labels[3], "4. No")
        self.assertTrue(labels[2].startswith("3. Yes, and switch to auto mode"))
        self.assertNotIn("·", labels[2])
        self.assertTrue(all(len(label) <= tg.NUMBERED_LABEL_MAX for label in labels))
        self.assertTrue(labels[1].endswith("…"))

    def test_plain_prompt_without_numbered_interaction_is_unchanged(self):
        markup = tg.make_keyboard("pane-1", ["Yes", "No"])
        labels = [row[0].text for row in markup.inline_keyboard[:2]]
        self.assertEqual(labels, ["Yes", "No"])



class ApprovalDoubleTapTests(unittest.IsolatedAsyncioTestCase):
    """A tap is acknowledged at once, delivered once, and never duplicated by a second tap."""

    def setUp(self):
        self.old_chat_id = tg.CHAT_ID
        tg.CHAT_ID = "42"
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.pending.clear()
        tg.approval_tokens.clear()
        tg.blocked_prompt_ids.clear()
        tg.approval_in_flight.clear()
        tg.pane_messages.clear()
        tg.done_at.clear()
        reset_focus_state()

    def tearDown(self):
        tg.CHAT_ID = self.old_chat_id
        tg.approval_in_flight.clear()
        reset_focus_state()

    def _tap(self, markup):
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup
        return callback

    async def test_buttons_come_off_before_delivery(self):
        callback = self._tap(make_active_approval_keyboard("w0:p1", ["yes", "no"]))
        seen = {}

        async def deliver(*args, **kwargs):
            seen["edits"] = callback.edit_calls
            seen["markup"] = callback.edited_markup

        with patch.object(tg, "send_keys_to_relay", AsyncMock(side_effect=deliver)):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        self.assertEqual(seen["edits"], 1)
        self.assertIsNone(seen["markup"])
        self.assertEqual(callback.edit_calls, 1)
        self.assertIn("Sent: yes", callback.message.replies[0][0])
        self.assertNotIn("w0:p1", tg.approval_tokens)
        self.assertNotIn("w0:p1", tg.approval_in_flight)

    async def test_second_tap_during_delivery_is_refused_not_resent(self):
        markup = make_active_approval_keyboard("w0:p1", ["yes", "no"])
        first_tap, second_tap = self._tap(markup), self._tap(markup)
        gate = asyncio.Event()

        async def deliver(*args, **kwargs):
            await gate.wait()

        send_keys = AsyncMock(side_effect=deliver)
        with patch.object(tg, "send_keys_to_relay", send_keys):
            first = asyncio.create_task(
                tg.handle_callback(make_update(callback=first_tap), SimpleNamespace())
            )
            await asyncio.sleep(0)  # first tap reaches the relay call and parks on the gate
            await tg.handle_callback(make_update(callback=second_tap), SimpleNamespace())
            gate.set()
            await first

        self.assertEqual(send_keys.await_count, 1)
        self.assertIn("wait", second_tap.message.replies[0][0].lower())
        self.assertEqual(second_tap.edit_calls, 0)
        self.assertIn("Sent: yes", first_tap.message.replies[0][0])
        self.assertNotIn("w0:p1", tg.approval_in_flight)

    async def test_tap_after_success_hits_the_older_prompt_guard(self):
        markup = make_active_approval_keyboard("w0:p1", ["yes", "no"])
        first_tap, second_tap = self._tap(markup), self._tap(markup)
        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=first_tap), SimpleNamespace())
            await tg.handle_callback(make_update(callback=second_tap), SimpleNamespace())

        self.assertEqual(send_keys.await_count, 1)
        self.assertIn("older prompt", second_tap.message.replies[0][0].lower())

    async def test_blocked_update_redraws_existing_card_without_new_message(self):
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        # A delivered alert exists for w0:p1.
        await tg.notify_blocked_safely(app, {
            "type": "blocked", "pane_id": "w0:p1", "agent": "opencode",
            "project": "project", "prompt": "Allow?", "options": ["yes", "no"],
            "prompt_id": "old",
        })
        self.assertEqual(len(bot.sent), 1)
        message_id = bot.sent[0][3].message_id
        generation = tg.approval_tokens["w0:p1"]

        # Re-hashed prompt redraws that one message and moves the prompt id;
        # its approval generation is preserved so the card's buttons stay valid.
        await tg.notify_blocked_safely(app, {
            "type": "blocked", "pane_id": "w0:p1", "prompt_id": "new",
            "prompt": "Allow (updated)?", "options": ["yes", "no"], "update": True,
        })
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(bot.edits[0][1], message_id)
        self.assertIn("Allow (updated)?", bot.edits[0][2])
        self.assertEqual(tg.blocked_prompt_ids["w0:p1"], "new")
        self.assertEqual(tg.approval_tokens["w0:p1"], generation)

        # An update for a pane that was never announced is a stray: no ghost card.
        await tg.notify_blocked_safely(app, {
            "type": "blocked", "pane_id": "w0:p2", "prompt_id": "x", "update": True,
        })
        self.assertNotIn("w0:p2", tg.blocked_prompt_ids)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.edits), 1)


def _rec(pane_id, *, label="", status="idle", project=None, cwd=None,
         title="", host="local", agent="claude"):
    return {
        "pane_id": pane_id,
        "agent": agent,
        "label": label,
        "status": status,
        "project": project if project is not None else (cwd or "").rsplit("/", 1)[-1],
        "cwd": cwd or f"/work/{pane_id}",
        "title": title,
        "host": host,
    }


def coord_rec(pane_id, slug, *, status="working", n=None, host="local"):
    return _rec(
        pane_id, label=f"hpc-{slug}" + (f"-{n}" if n else ""),
        status=status, project=slug, cwd=f"/Users/me/Documents/{slug}", host=host,
        title=f"{slug} work",
    )


def thread_rec(pane_id, slug, n, *, status="working", host="local"):
    t = f"t-{n:04d}"
    return _rec(
        pane_id, label=f"hp-{slug}-{t}", status=status, project=t,
        cwd=f"/Users/me/.herdr-projects/{slug}/threads/{t}", host=host,
    )


class AudienceGateTests(unittest.TestCase):
    def test_registry_name_patterns(self):
        self.assertTrue(tg.pane_human(coord_rec("a", "lagrange")))
        self.assertTrue(tg.pane_human(coord_rec("a", "lagrange", n=2)))
        self.assertFalse(tg.pane_human(thread_rec("a", "lagrange", 9)))

    def test_thread_cwd_markers_hide_unnamed_workers(self):
        # Registered-name marker wins over cwd (coordinator inside a thread worktree).
        coord = _rec("a", label="hpc-x", status="blocked", project="x",
                     cwd="/repo/worktrees/wt1/hp-x-t-3-checkout")
        self.assertTrue(tg.pane_human(coord))
        # Unnamed pane inside a thread worktree is a worker.
        worker = _rec("b", label="", cwd="/repo/worktrees/wt1/hp-x-t-3-checkout")
        self.assertFalse(tg.pane_human(worker))
        # Bare t-NNNN basename without cwd marker still hides.
        self.assertFalse(tg.pane_human(_rec("c", label="", project="t-17", cwd="/")))

    def test_ordinary_and_remote_sessions_stay_visible(self):
        self.assertTrue(tg.pane_human(_rec("a", label="", cwd="/work/foo", project="foo")))
        self.assertTrue(tg.pane_human(coord_rec("a", "lg", host="boe-fyq")))

    def test_agents_for_action_excludes_thread_workers(self):
        tg.agents = [
            coord_rec("w:1", "alpha", status="blocked"),
            thread_rec("w:2", "alpha", 3, status="blocked"),
            coord_rec("w:3", "beta", status="working"),
            thread_rec("w:4", "beta", 4, status="working"),
            _rec("w:5", label="", status="working", project="gamma", cwd="/work/gamma"),
        ]
        self.assertEqual({a["pane_id"] for a in tg.agents_for_action("trust")}, {"w:1"})
        self.assertEqual(
            {a["pane_id"] for a in tg.agents_for_action("interrupt")},
            {"w:1", "w:3", "w:5"},
        )
        self.assertEqual({a["pane_id"] for a in tg.human_agents()}, {"w:1", "w:3", "w:5"})


class ThreadSilenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_chat_id = tg.CHAT_ID
        tg.CHAT_ID = "42"
        tg.agents = []
        tg.approval_tokens.clear()
        tg.blocked_prompt_ids.clear()
        tg.prev_statuses.clear()
        tg.done_at.clear()
        tg.pane_messages.clear()
        reset_focus_state()

    def tearDown(self):
        tg.CHAT_ID = self.old_chat_id
        tg.agents = []
        reset_focus_state()

    async def test_thread_blocked_event_is_swallowed(self):
        tg.agents = [thread_rec("w:1", "alpha", 9, status="blocked")]
        bot = FakeBot()
        await tg.notify_blocked_safely(SimpleNamespace(bot=bot), {
            "pane_id": "w:1", "agent": "claude", "project": "t-0009",
            "prompt": "Allow?", "options": ["yes", "no"], "prompt_id": "p1",
        })
        self.assertEqual(bot.sent, [])
        self.assertNotIn("w:1", tg.approval_tokens)
        self.assertNotIn("w:1", tg.blocked_prompt_ids)

    async def test_thread_completion_makes_no_noise(self):
        tg.agents = [thread_rec("w:1", "alpha", 9, status="done")]
        tg.prev_statuses["w:1"] = "working"
        bot = FakeBot()
        await tg.track_agent_updates(SimpleNamespace(bot=bot), tg.agents)
        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.edits, [])
        self.assertNotIn("w:1", tg.done_at)


class DashboardRenderTests(unittest.TestCase):
    def setUp(self):
        self.old_chat_id = tg.CHAT_ID
        self.old_dashboard_msg_id = tg.dashboard_msg_id
        tg.CHAT_ID = "42"
        tg.agents = []
        tg.relay_connected = True
        tg.done_at.clear()
        tg.dashboard_msg_id = None
        tg.dash.update(dirty=False, last_push=0.0, last_text=None, last_sig=None)

    def tearDown(self):
        tg.CHAT_ID = self.old_chat_id
        tg.dashboard_msg_id = self.old_dashboard_msg_id
        tg.agents = []
        tg.relay_connected = False
        tg.done_at.clear()

    def test_groups_by_project_with_status_glyphs_and_hidden_workers(self):
        now = tg.time.time()
        tg.agents = [
            coord_rec("w:1", "alpha", status="working"),
            coord_rec("w:2", "alpha", status="blocked"),
            thread_rec("w:3", "alpha", 9, status="blocked"),
            coord_rec("w:4", "zeta", status="done"),
            _rec("w:5", label="", status="idle", project="zeta", cwd="/work/zeta"),
        ]
        tg.done_at["w:4"] = now - 60
        text = tg.render_dashboard(now)

        # Blocked project sorts before the working-only/idle ones.
        blocked_at = text.index("alpha")
        self.assertLess(blocked_at, text.index("zeta"))
        self.assertIn("🚫", text)
        self.assertIn("▶️", text)
        self.assertIn("✅", text)
        self.assertIn("💤 1 idle", text)
        # Thread pane never appears by name or id.
        self.assertNotIn("hp-alpha-t-0009", text)
        self.assertNotIn("w:3", text)
        # Identity pieces reach the line: pane descriptor and coordinator name.
        self.assertIn("hpc-alpha", text)

        markup = tg.build_dashboard_keyboard(now)
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertTrue(buttons[0].text.startswith("🚫"))
        self.assertEqual(tg.parse_callback_data(buttons[0].callback_data)["pane_id"], "w:2")
        self.assertEqual(buttons[-1].text, "📋 All panes")

    def test_stale_done_collapses_into_idle_count(self):
        now = tg.time.time()
        tg.agents = [coord_rec("w:4", "zeta", status="done")]
        tg.done_at["w:4"] = now - tg.DASHBOARD_DONE_TTL - 1
        text = tg.render_dashboard(now)
        self.assertNotIn("✅", text)
        self.assertIn("💤 1 idle", text)

    def test_clock_change_alone_does_not_change_signature(self):
        tg.agents = [coord_rec("w:1", "alpha", status="working")]
        a = tg._text_signature(tg.render_dashboard(1_900_000_000))
        b = tg._text_signature(tg.render_dashboard(1_900_000_300))
        self.assertEqual(a, b)


class DashboardPushTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_chat_id = tg.CHAT_ID
        self.old_dashboard_msg_id = tg.dashboard_msg_id
        tg.CHAT_ID = "42"
        tg.agents = []
        tg.relay_connected = True
        tg.done_at.clear()
        tg.pane_messages.clear()
        reset_focus_state()
        tg.dashboard_msg_id = None
        tg.dash.update(dirty=False, last_push=0.0, last_text=None, last_sig=None)

    def tearDown(self):
        tg.CHAT_ID = self.old_chat_id
        tg.dashboard_msg_id = self.old_dashboard_msg_id
        tg.agents = []
        tg.relay_connected = False
        tg.done_at.clear()
        tg.pane_messages.clear()
        reset_focus_state()

    async def test_posts_then_edits_and_reposts_after_delete(self):
        tg.agents = [coord_rec("w:1", "alpha", status="working")]
        bot = FakeBot()

        await tg.push_dashboard(bot)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(tg.dashboard_msg_id, bot.sent[0][3].message_id)

        # Identical content within the refresh window: no edit.
        await tg.push_dashboard(bot)
        self.assertEqual(bot.edits, [])

        # Force: the same message is edited in place.
        await tg.push_dashboard(bot, force=True)
        self.assertEqual(len(bot.edits), 1)
        self.assertEqual(bot.edits[0][1], tg.dashboard_msg_id)

        # Telegram refuses the edit (user deleted it / aged out): repost fresh.
        tg.agents = [coord_rec("w:1", "alpha", status="blocked")]

        class EditFailBot(FakeBot):
            async def edit_message_text(self, chat_id, message_id, text, **kwargs):
                self.edits.append((chat_id, message_id, text, kwargs))
                raise Exception("message to edit not found")

        fail_bot = EditFailBot()
        await tg.push_dashboard(fail_bot, force=True)
        self.assertEqual(len(fail_bot.sent), 1)
        self.assertEqual(tg.dashboard_msg_id, fail_bot.sent[0][3].message_id)


class FocusAndBarTests(unittest.IsolatedAsyncioTestCase):
    """pf.3: sticky focus pane, persistent bar, /switch selector."""

    def setUp(self):
        self.old_chat_id = tg.CHAT_ID
        tg.CHAT_ID = "42"
        tg.agents = []
        tg.relay_connected = True
        tg.pending.clear()
        tg.approval_tokens.clear()
        tg.blocked_prompt_ids.clear()
        tg.prev_statuses.clear()
        tg.pane_messages.clear()
        tg.done_at.clear()
        reset_focus_state()

    def tearDown(self):
        tg.CHAT_ID = self.old_chat_id
        tg.agents = []
        tg.relay_connected = False
        reset_focus_state()

    def _herd(self):
        # two projects, a thread worker that must never appear, an unnamed session
        tg.agents = [
            coord_rec("wT:pA", "lagrange", status="working"),
            coord_rec("wT:pB", "nix-hm", status="blocked"),
            thread_rec("wT:pC", "lagrange", 3, status="blocked"),
            _rec("wT:pD", label="", status="idle", project="dot-nvim",
                 cwd="/work/dot-nvim", title="nvim tweaks", agent="claude"),
        ]

    def _flatten(self, markup):
        return [b for row in markup.inline_keyboard for b in row]

    def _bar_texts(self, markup):
        # Real PTB reply-keyboard rows hold KeyboardButton objects, not strings.
        return [b.text for row in markup.keyboard for b in row]

    # --- bar markup -------------------------------------------------------

    def test_bar_without_focus_has_only_static_keys(self):
        self._herd()
        markup = tg.build_bar_markup()
        flat = self._bar_texts(markup)
        self.assertEqual(flat, [tg.BAR_SWITCH_LABEL, tg.BAR_DASHBOARD_LABEL])
        self.assertIn("先选", markup.input_field_placeholder)

    def test_bar_active_and_recents_hide_workers_and_dead_panes(self):
        self._herd()
        tg.focus_pane("wT:pA")
        tg.focus_pane("wT:pB")
        active_item, recent_items = tg._bar_specs()
        self.assertEqual(active_item[1]["pane_id"], "wT:pB")
        self.assertEqual([r["pane_id"] for _l, r in recent_items], ["wT:pA"])
        markup = tg.build_bar_markup()
        flat = self._bar_texts(markup)
        self.assertTrue(flat[0].startswith("🎯"))
        self.assertTrue(any(b.startswith("🔄") for b in flat))
        self.assertIn(tg.BAR_UNFOCUS_LABEL, flat)
        self.assertIn("消息发往", markup.input_field_placeholder)

        # A thread-worker id that snuck into focus/recent (never via the bar,
        # but be defensive) renders as no active row and never as a recent key.
        tg.focus_pane("wT:pC")
        active_item, recent_items = tg._bar_specs()
        self.assertIsNone(active_item)
        self.assertTrue(all(r["pane_id"] != "wT:pC" for _l, r in recent_items))
        flat = self._bar_texts(tg.build_bar_markup())
        self.assertNotIn("hp-lagrange-t-0003", " ".join(flat))

    def test_dead_active_pane_renders_without_active_row(self):
        self._herd()
        tg.active_pane = "gone:pane"
        tg.recent_panes = ["gone:pane", "wT:pA"]
        active_item, recent_items = tg._bar_specs()
        self.assertIsNone(active_item)
        self.assertEqual([r["pane_id"] for _l, r in recent_items], ["wT:pA"])

    def test_duplicate_identities_get_pane_id_suffix(self):
        tg.agents = [
            _rec("w:1", label="", status="idle", project="same", cwd="/work/same", agent="claude"),
            _rec("w:2", label="", status="idle", project="same", cwd="/work/same", agent="claude"),
        ]
        tg.focus_pane("w:1")
        tg.focus_pane("w:2")
        markup = tg.build_bar_markup()
        flat = self._bar_texts(markup)
        self.assertEqual(len([b for b in flat if "w:1" in b]), 1)
        self.assertEqual(len([b for b in flat if "w:2" in b]), 1)

    async def test_refresh_bar_silently_sends_and_deletes_placeholder(self):
        self._herd()
        tg.focus_pane("wT:pA")
        bot = FakeBot()
        await tg.refresh_bar(bot)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0][1], tg.BAR_PLACEHOLDER)
        self.assertEqual(bot.deleted, [(42, bot.sent[0][3].message_id)])
        # Signature-gated: a second refresh does nothing.
        await tg.refresh_bar(bot)
        self.assertEqual(len(bot.sent), 1)
        # Focus change forces a new keyboard.
        tg.focus_pane("wT:pB")
        await tg.refresh_bar(bot, force=True)
        self.assertEqual(len(bot.sent), 2)

    async def test_refresh_bar_is_noop_without_chat_lock(self):
        tg.CHAT_ID = ""
        bot = FakeBot()
        await tg.refresh_bar(bot)
        self.assertEqual(bot.sent, [])

    def test_placeholder_is_the_invisible_char_telegram_accepts(self):
        # Bot API rejects bodies made solely of U+200B/U+2800/U+1160/U+3164/
        # nbsp/ZWNJ/ZWNBSP ("text must be non-empty"); U+2060 WORD JOINER is
        # the one invisible char that survives the trim (verified 2026-10).
        self.assertEqual(tg.BAR_PLACEHOLDER, "⁠")

    # --- free text routing ------------------------------------------------

    async def test_plain_text_without_focus_opens_selector_sends_nothing(self):
        self._herd()
        message = FakeMessage()
        message.text = "hello any session"
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), make_context(bot=FakeBot()))
        send_text.assert_not_awaited()
        _text, kwargs = message.replies[0][0], message.replies[0][1]
        buttons = self._flatten(kwargs["reply_markup"])
        projects = {b.text for b in buttons if b.text.startswith("▸")}
        self.assertIn("▸ lagrange", projects)
        self.assertIn("▸ nix-hm", projects)

    async def test_plain_text_routes_to_focus_with_identity_receipt(self):
        self._herd()
        tg.focus_pane("wT:pA")
        message = FakeMessage()
        message.text = "继续"
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), make_context(bot=FakeBot()))
        send_text.assert_awaited_once_with("wT:pA", "继续")
        receipt, kwargs = message.replies[0][0], message.replies[0][1]
        self.assertIn("lagrange", receipt)
        self.assertIn("hpc-lagrange", receipt)
        self.assertIn("wT:pA", receipt)
        actions = [tg.parse_callback_data(b.callback_data)["action"]
                   for b in self._flatten(kwargs["reply_markup"])]
        self.assertEqual(actions, ["read", "switch-open"])

    async def test_blocked_focus_uses_prompt_response_path(self):
        self._herd()
        tg.focus_pane("wT:pB")
        tg.blocked_prompt_ids["wT:pB"] = "prompt-9"
        message = FakeMessage()
        message.text = "yes"
        with (
            patch.object(tg, "send_to_relay", AsyncMock()) as send_response,
            patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text,
        ):
            await tg.handle_text(make_update(message=message), make_context(bot=FakeBot()))
        send_response.assert_awaited_once_with("wT:pB", "yes", prompt_id="prompt-9")
        send_text.assert_not_awaited()

    async def test_reply_to_card_overrides_focus_and_redirects(self):
        self._herd()
        tg.focus_pane("wT:pA")
        tg.register_pending(42, 77, "wT:pB")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "answer on B"
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), make_context(bot=FakeBot()))
        send_text.assert_awaited_once_with("wT:pB", "answer on B")
        self.assertEqual(tg.active_pane, "wT:pB")
        self.assertEqual(tg.recent_panes[0], "wT:pB")

    async def test_stale_focus_is_cleared_and_selector_offered(self):
        self._herd()
        tg.focus_pane("wT:pA")
        tg.agents = [a for a in tg.agents if a["pane_id"] != "wT:pA"]
        message = FakeMessage()
        message.text = "anyone there"
        bot = FakeBot()
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), make_context(bot=bot))
        send_text.assert_not_awaited()
        self.assertIsNone(tg.active_pane)
        self.assertIn("no longer available", message.replies[0][0].lower())
        self.assertTrue(message.replies[0][1].get("reply_markup"))

    async def test_group_chat_keeps_reply_only_contract(self):
        self._herd()
        tg.focus_pane("wT:pA")
        message = FakeMessage(chat_type="group")
        message.text = "ungrouped text"
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(chat_type="group", message=message),
                                 make_context(bot=FakeBot()))
        send_text.assert_not_awaited()
        self.assertEqual(message.replies, [])

    # --- bar label taps ---------------------------------------------------

    async def test_bar_label_taps_never_reach_a_pane(self):
        self._herd()
        tg.focus_pane("wT:pA")
        tg.recent_panes = ["wT:pB", "wT:pA"]
        _active, recents = tg._bar_specs()
        recent_label = "🔄 " + recents[0][0]
        # exact active label from the current markup
        active_label = [t for t in self._bar_texts(tg.build_bar_markup())
                        if t.startswith("🎯")][0]
        labels = (tg.BAR_SWITCH_LABEL, tg.BAR_DASHBOARD_LABEL,
                  active_label, recent_label, tg.BAR_UNFOCUS_LABEL)
        tapped = []
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            for label in labels:
                message = FakeMessage()
                message.text = label
                tapped.append((label, message))
                await tg.handle_text(make_update(message=message), make_context(bot=FakeBot()))
        send_text.assert_not_awaited()
        # Every tap produced some feedback, none routed text to a pane.
        self.assertTrue(all(m.replies for _label, m in tapped))
        self.assertTrue(any("dashboard" in r[0].lower()
                            for _l, m in tapped for r in m.replies))

    async def test_unfocus_tap_clears_active(self):
        self._herd()
        tg.focus_pane("wT:pA")
        message = FakeMessage()
        message.text = tg.BAR_UNFOCUS_LABEL
        await tg.handle_text(make_update(message=message), make_context(bot=FakeBot()))
        self.assertIsNone(tg.active_pane)
        self.assertIn("取消", message.replies[0][0])

    async def test_recent_tap_refocuses_and_prompts(self):
        self._herd()
        tg.focus_pane("wT:pA")
        tg.focus_pane("wT:pB")
        # A is now a recent key; find its exact label.
        _active, recents = tg._bar_specs()
        label = "🔄 " + recents[0][0]
        message = FakeMessage()
        message.text = label
        await tg.handle_text(make_update(message=message), make_context(bot=FakeBot()))
        self.assertEqual(tg.active_pane, recents[0][1]["pane_id"])
        self.assertIn("已聚焦", message.replies[0][0])
        self.assertIsInstance(message.replies[0][1]["reply_markup"], tg.ForceReply)

    # --- /switch selector --------------------------------------------------

    async def test_switch_selection_focuses_and_followup_text_routes(self):
        self._herd()
        button = tg.build_switch_keyboard().inline_keyboard
        # find the lagrange coordinator pane row (skip headers)
        pane_buttons = [row[0] for row in button
                        if tg.parse_callback_data(row[0].callback_data).get("action") == "switch"]
        target = next(b for b in pane_buttons
                      if tg.parse_callback_data(b.callback_data)["pane_id"] == "wT:pA")
        callback = FakeCallback(target.callback_data)
        await tg.handle_callback(make_update(callback=callback), make_context(bot=FakeBot()))

        self.assertEqual(tg.active_pane, "wT:pA")
        self.assertEqual(tg.recent_panes[0], "wT:pA")
        self.assertIn("已聚焦", callback.message.replies[0][0])

        follow = FakeMessage()
        follow.text = "do the thing"
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=follow), make_context(bot=FakeBot()))
        send_text.assert_awaited_once_with("wT:pA", "do the thing")

    def test_switch_keyboard_groups_and_filters(self):
        self._herd()
        markup = tg.build_switch_keyboard()
        flat = self._flatten(markup)
        headers = [b.text for b in flat if b.text.startswith("▸")]
        self.assertIn("▸ lagrange", headers)
        self.assertIn("▸ nix-hm", headers)
        # worker pane absent
        self.assertNotIn("wT:pC", [tg.parse_callback_data(b.callback_data).get("pane_id")
                                    for b in flat])

        blocked = tg.build_switch_keyboard(statusf="blocked")
        bflat = self._flatten(blocked)
        pane_ids = {tg.parse_callback_data(b.callback_data).get("pane_id")
                    for b in bflat if tg.parse_callback_data(b.callback_data).get("action") == "switch"}
        self.assertEqual(pane_ids, {"wT:pB"})

        found = tg.build_switch_keyboard(query="nvim")
        ids = {tg.parse_callback_data(b.callback_data).get("pane_id")
               for b in self._flatten(found)
               if tg.parse_callback_data(b.callback_data).get("action") == "switch"}
        self.assertEqual(ids, {"wT:pD"})

    def test_switch_filters_paginate_and_all_callbacks_fit_bytes(self):
        tg.agents = [
            coord_rec(f"w:p{i:02d}", f"proj-{i % 4}", status=("blocked" if i % 3 == 0 else "working"))
            for i in range(tg.SWITCH_PAGE_SIZE * 2 + 3)
        ]
        first = tg.build_switch_keyboard(page=0)
        second = tg.build_switch_keyboard(page=1, query="proj")
        for markup in (first, second):
            for b in self._flatten(markup):
                self.assertLessEqual(len(b.callback_data.encode()), 64, b.callback_data)
        # header+pane rows within page budget on page 0
        pane_rows = [b for b in self._flatten(first)
                     if tg.parse_callback_data(b.callback_data).get("action") == "switch"]
        self.assertLessEqual(len(first.inline_keyboard), tg.SWITCH_PAGE_SIZE + 3)
        self.assertTrue(pane_rows)
        # Next button exists and carries page 1
        nav = next(b for b in self._flatten(first) if b.text == "Next")
        self.assertEqual(json.loads(nav.callback_data)["page"], 1)

    async def test_switch_menu_and_filter_callbacks_rebuild_keyboard(self):
        self._herd()
        nav = tg._switch_cb(action="switch-menu", page=0, f="blocked", q="")
        callback = FakeCallback(nav)
        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())
        ids = {tg.parse_callback_data(b.callback_data).get("pane_id")
               for b in self._flatten(callback.edited_markup)
               if tg.parse_callback_data(b.callback_data).get("action") == "switch"}
        self.assertEqual(ids, {"wT:pB"})

    async def test_group_header_noop_callback_does_nothing(self):
        callback = FakeCallback({"action": "noop"})
        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())
        self.assertEqual(callback.message.replies, [])

    async def test_switch_open_receipt_button_posts_selector(self):
        self._herd()
        callback = FakeCallback({"action": "switch-open"})
        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())
        text, kwargs, _sent = callback.message.replies[0]
        self.assertIn("选择", text)
        self.assertTrue(kwargs["reply_markup"].inline_keyboard)

    async def test_stale_switch_pane_reports_offline(self):
        self._herd()
        button = tg.pane_callback_data("switch", "gone:p1")
        callback = FakeCallback(button)
        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())
        self.assertIn("no longer available", callback.message.replies[0][0].lower())

    async def test_find_command_requires_keyword(self):
        update = make_update()
        await tg.cmd_find(update, make_context(args=[]))
        self.assertIn("用法", update.message.replies[0][0])

    # --- pruning / recent bookkeeping -------------------------------------

    def test_recent_list_is_mru_bounded(self):
        ids = [f"w:p{i}" for i in range(tg.RECENT_MAX + 3)]
        for pid in ids:
            tg.focus_pane(pid)
        self.assertEqual(len(tg.recent_panes), tg.RECENT_MAX)
        self.assertEqual(tg.recent_panes[0], ids[-1])
        # re-focusing an old pane moves it to front without duplication
        tg.focus_pane(ids[2])
        self.assertEqual(tg.recent_panes[0], ids[2])
        self.assertEqual(tg.recent_panes.count(ids[2]), 1)

    def test_prune_clears_active_and_recent_entries(self):
        tg.focus_pane("w:dead")
        tg.recent_panes.append("w:alive")
        now = tg.time.time()
        tg.pane_seen["w:dead"] = now - tg.PANE_STATE_GRACE - 10
        tg.pane_seen["w:alive"] = now
        tg.prune_pane_state(now, {"w:alive"})
        self.assertIsNone(tg.active_pane)
        self.assertNotIn("w:dead", tg.recent_panes)
        self.assertIn("w:alive", tg.recent_panes)


class StatePersistenceTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.old_state = tg.STATE_FILE
        tg.STATE_FILE = self.tmp.name
        self.old_dashboard = tg.dashboard_msg_id
        tg.dashboard_msg_id = None

    def tearDown(self):
        tg.STATE_FILE = self.old_state
        tg.dashboard_msg_id = self.old_dashboard
        tg.pane_messages.clear()
        tg.done_at.clear()
        tg.prev_statuses.clear()
        reset_focus_state()
        for path in (self.tmp.name, self.tmp.name + ".tmp"):
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_round_trip_dashboard_and_pane_maps(self):
        now = tg.time.time()
        tg.pane_messages.clear()
        tg.done_at.clear()
        tg.prev_statuses.clear()
        tg.pane_messages["w:1"] = 77
        tg.done_at["w:1"] = now - 10
        tg.prev_statuses["w:1"] = "done"
        tg.dashboard_msg_id = 99
        tg.agents = [_rec("w:1", label="hpc-x", status="done", project="x", cwd="/d/x")]

        tg.save_state()
        self.assertEqual(oct(os.stat(self.tmp.name).st_mode & 0o777), "0o600")
        # A fresh process loads the same dashboard and message mappings.
        tg.dashboard_msg_id = None
        tg.pane_messages.clear()
        tg.done_at.clear()
        tg.prev_statuses.clear()
        tg.load_state()
        self.assertEqual(tg.dashboard_msg_id, 99)
        self.assertEqual(tg.pane_messages, {"w:1": 77})
        self.assertEqual(tg.prev_statuses, {"w:1": "done"})
        self.assertIn("w:1", tg.done_at)


if __name__ == "__main__":
    unittest.main()
