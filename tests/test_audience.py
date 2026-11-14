"""Human-audience gating for relay Web Push: primary-only rows, coalesced thread follow-ups."""
import asyncio
import copy
from unittest import mock
import unittest

from test_herdr_relay import loaded_relay


def pane(role="", audience="", **extra):
    row = {"pane_id": "w1:p1", "agent": "claude", "status": "blocked",
           "cwd": "/work/alpha", "project": "alpha", "host": "local", "remote": None,
           "label": "plain", "hp_role": role, "hp_audience": audience,
           "hp_project": "alpha" if role else "", "hp_group": "", "order": 0}
    row.update(extra)
    return row


COORD = pane("coordinator", pane_id="w1:pC", label="hpc-alpha", order=0)
INTERNAL = pane("thread", "internal", pane_id="w1:pT", hp_group="alpha!1!1!t-1", order=1)
STALLED = pane("thread", "stalled", pane_id="w1:pU", hp_group="alpha!1!2!t-2", order=2)
HUMAN = pane("thread", "human", pane_id="w1:pV", status="done",
             hp_group="alpha!1!3!t-3", order=3)


class AudienceProjectionTests(unittest.TestCase):
    def test_role_matrix_primary_only_and_legacy_fail_open(self):
        with loaded_relay() as relay:
            self.assertTrue(relay.audience.pane_human(pane("coordinator")))
            self.assertTrue(relay.audience.pane_human(pane("session")))
            # A worker thread is never a Projects row, regardless of audience or process status.
            self.assertFalse(relay.audience.pane_human(pane("thread", "internal")))
            self.assertFalse(relay.audience.pane_human(pane("thread", "human")))
            self.assertFalse(relay.audience.pane_human(pane("thread", "stalled")))
            self.assertFalse(relay.audience.pane_human(pane("guest", "internal")))
            self.assertFalse(relay.audience.pane_human(pane("other", "internal")))
            # No authoritative role: an ordinary session fails open and stays visible.
            self.assertTrue(relay.audience.pane_human(pane()))

    def test_registered_project_names_the_notification(self):
        with loaded_relay() as relay:
            rec = pane("session", project="threads", hp_project="nix-hm")
            self.assertEqual(relay.audience.effective_project(rec), "nix-hm")

    def test_followups_group_by_host_and_project(self):
        with loaded_relay() as relay:
            remote_coord = dict(COORD, host="boe", pane_id="w2:pC", hp_project="alpha")
            remote_stalled = dict(STALLED, host="boe", pane_id="w2:pU")
            grouped = relay.audience.project_followups([COORD, STALLED, HUMAN, INTERNAL,
                                                         remote_coord, remote_stalled])
            self.assertEqual(set(grouped), {"local|alpha", "boe|alpha"})
            self.assertEqual({t["pane_id"] for t in grouped["local|alpha"]}, {"w1:pU", "w1:pV"})

    def test_owner_is_the_earliest_live_coordinator_or_none(self):
        with loaded_relay() as relay:
            c2 = dict(COORD, pane_id="w1:pD", order=5)
            self.assertEqual(
                relay.audience.primary_coordinator([c2, COORD], "local|alpha")["pane_id"], "w1:pC")
            self.assertIsNone(relay.audience.primary_coordinator([STALLED], "local|alpha"))


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

    def test_escalated_threads_coalesce_into_one_coordinator_push(self):
        with loaded_relay() as relay:
            relay.last_push_project_followups.clear()
            push = mock.AsyncMock()
            with mock.patch.object(relay, "send_web_push", new=push):
                # First snapshot: one internal (ignored) + one stalled thread + the coordinator.
                asyncio.run(relay.reconcile_project_followups([COORD, INTERNAL, STALLED]))
                self.assertEqual(push.await_count, 1)
                kw = push.await_args.kwargs
                self.assertIn("1 thread follow-up", kw["title"])
                # Routes to the coordinator, never to the worker.
                self.assertTrue(kw["url"].endswith("pane=w1:pC"))
                # A repaint with no new escalation does not push again.
                asyncio.run(relay.reconcile_project_followups([COORD, INTERNAL, STALLED]))
                self.assertEqual(push.await_count, 1)
                # A second thread escalating fires exactly one more, now a count of two.
                asyncio.run(relay.reconcile_project_followups([COORD, INTERNAL, STALLED, HUMAN]))
                self.assertEqual(push.await_count, 2)
                self.assertIn("2 thread follow-ups", push.await_args.kwargs["title"])

    def test_escalated_thread_without_a_live_coordinator_does_not_push(self):
        with loaded_relay() as relay:
            relay.last_push_project_followups.clear()
            push = mock.AsyncMock()
            with mock.patch.object(relay, "send_web_push", new=push):
                asyncio.run(relay.reconcile_project_followups([STALLED, HUMAN]))
            push.assert_not_awaited()

    def test_event_fast_path_ignores_worker_and_keeps_primary_push(self):
        with loaded_relay() as relay:
            relay.last_push_project_followups.clear()
            # Push events carry no hp_* tokens (agent_update_message strips them); the poll-seeded
            # cache is what supplies the authoritative role through the merge in event_push.
            internal = pane("thread", "internal", label="hp-alpha-t-1",
                            cwd="/work/alpha/threads/t-1")
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
            # Only the primary session's block pushes; the worker produces nothing on its own.
            self.assertEqual(push.await_count, 1)
            self.assertTrue(push.await_args.kwargs["url"].endswith("pane=w1:p2"))


if __name__ == "__main__":
    unittest.main()
