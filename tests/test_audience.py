"""Human-audience gating for relay Web Push, including stalled upgrades."""
import asyncio
import copy
from unittest import mock
import unittest

from test_herdr_relay import loaded_relay


def pane(role="", audience="", **extra):
    row = {"pane_id": "w1:p1", "agent": "claude", "status": "blocked",
           "cwd": "/work/alpha", "project": "alpha", "host": "local", "remote": None,
           "label": "plain", "hp_role": role, "hp_audience": audience,
           "hp_project": "alpha" if role else "", "hp_group": ""}
    row.update(extra)
    return row


class AudienceProjectionTests(unittest.TestCase):
    def test_role_matrix_and_legacy_fail_open(self):
        with loaded_relay() as relay:
            self.assertTrue(relay.audience.pane_human(pane("coordinator")))
            self.assertTrue(relay.audience.pane_human(pane("session")))
            self.assertFalse(relay.audience.pane_human(pane("thread", "internal")))
            self.assertTrue(relay.audience.pane_human(pane("thread", "human")))
            self.assertTrue(relay.audience.pane_human(pane("thread", "stalled")))
            self.assertFalse(relay.audience.pane_human(pane("guest", "internal")))
            self.assertFalse(relay.audience.pane_human(pane("other", "internal")))
            self.assertTrue(relay.audience.pane_human(pane()))

    def test_registered_project_names_the_notification(self):
        with loaded_relay() as relay:
            rec = pane("session", project="threads", hp_project="nix-hm")
            self.assertEqual(relay.audience.effective_project(rec), "nix-hm")


class RelayAudiencePushTests(unittest.TestCase):
    def poll(self, relay, records, push):
        with mock.patch.object(relay, "refresh_remote_machines"), \
             mock.patch.object(relay, "get_all_panes", return_value=(copy.deepcopy(records), [])), \
             mock.patch.object(relay, "probe_idle_questions", return_value={}), \
             mock.patch.object(relay, "refresh_spaces", return_value={"workspaces": [], "tabs": []}), \
             mock.patch.object(relay, "read_pane", return_value="Deploy to prod?"), \
             mock.patch.object(relay, "broadcast", new=mock.AsyncMock()), \
             mock.patch.object(relay, "send_web_push", new=push):
            asyncio.run(relay._poll_once())

    def test_internal_thread_never_pushes_but_still_reaches_clients(self):
        with loaded_relay() as relay:
            push = mock.AsyncMock()
            sent = mock.AsyncMock()
            with mock.patch.object(relay, "refresh_remote_machines"), \
                 mock.patch.object(relay, "get_all_panes", return_value=([pane("thread", "internal")], [])), \
                 mock.patch.object(relay, "probe_idle_questions", return_value={}), \
                 mock.patch.object(relay, "refresh_spaces", return_value={}), \
                 mock.patch.object(relay, "read_pane", return_value="Allow internal tool?"), \
                 mock.patch.object(relay, "broadcast", new=sent), \
                 mock.patch.object(relay, "send_web_push", new=push):
                asyncio.run(relay._poll_once())
            push.assert_not_awaited()
            self.assertTrue(any(call.args[0].get("type") == "blocked" for call in sent.await_args_list))

    def test_primary_session_pushes(self):
        with loaded_relay() as relay:
            push = mock.AsyncMock()
            self.poll(relay, [pane("session")], push)
            self.assertEqual(push.await_count, 1)
            self.assertIn("alpha", push.await_args.kwargs["title"])

    def test_internal_to_stalled_upgrade_pushes_once_without_a_new_prompt(self):
        with loaded_relay() as relay:
            internal = pane("thread", "internal")
            stalled = pane("thread", "stalled")
            push = mock.AsyncMock()
            self.poll(relay, [internal], push)
            push.assert_not_awaited()

            self.poll(relay, [stalled], push)
            self.assertEqual(push.await_count, 1)
            self.assertIn("thread stalled", push.await_args.kwargs["title"])

            self.poll(relay, [stalled], push)
            self.assertEqual(push.await_count, 1, "a repaint is not another escalation")

    def test_human_thread_can_notify_even_when_its_process_is_done(self):
        with loaded_relay() as relay:
            rec = pane("thread", "human", status="done")
            self.assertTrue(relay.audience.pane_human(rec))

    def test_event_fast_path_uses_the_same_gate(self):
        with loaded_relay() as relay:
            internal = pane("thread", "internal")
            primary = pane("session", pane_id="w1:p2", label="manual")
            events = [
                {"type": "agent_event", "pane_id": rec["pane_id"], "agent": "claude",
                 "status": "blocked", "cwd": rec["cwd"], "project": rec["project"],
                 "host": "local"}
                for rec in (internal, primary)
            ]
            push = mock.AsyncMock()

            async def run():
                with mock.patch.object(relay, "get_all_panes", return_value=([internal, primary], [])), \
                     mock.patch.object(relay, "read_pane", return_value="Deploy to prod?"), \
                     mock.patch.object(relay, "broadcast", new=mock.AsyncMock()), \
                     mock.patch.object(relay, "send_web_push", new=push):
                    task = asyncio.create_task(relay.event_push())
                    try:
                        for event in events:
                            await relay.event_queue.put(event)
                        await asyncio.sleep(0.1)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task

            asyncio.run(run())
            self.assertEqual(push.await_count, 1)


if __name__ == "__main__":
    unittest.main()
