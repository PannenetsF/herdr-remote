"""Strict primary-only Projects projection, exercised in the real web app.

Projects renders hp_role=coordinator and hp_role=session panes (plus legacy fail-open sessions
with no authoritative role). A hp_role=thread pane is NEVER its own card, regardless of
hp_audience or process status; human/stalled workers surface only as an aggregate follow-up
badge on the earliest live coordinator of the same (host, project). All panes keeps everything.
"""
import os
from pathlib import Path
import unittest


PAGE = (Path(__file__).resolve().parents[1] / "web" / "index.html").as_uri()
CHROME_CANDIDATES = [
    os.environ.get("HERDR_TEST_CHROME", ""),
    os.path.expanduser("~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"),
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
]
PHONE = {"width": 390, "height": 844}
T0 = 1_700_000_000_000


def _chrome():
    return next((path for path in CHROME_CANDIDATES if path and os.path.exists(path)), None)


try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

_shared = {}


def setUpModule():  # noqa: N802
    if sync_playwright is None or _chrome() is None:
        return
    _shared["playwright"] = sync_playwright().start()
    _shared["browser"] = _shared["playwright"].chromium.launch(executable_path=_chrome())


def tearDownModule():  # noqa: N802
    if "browser" in _shared:
        _shared["browser"].close()
        _shared["playwright"].stop()
    _shared.clear()


def agent(pane_id, role="", audience="", slug="", **extra):
    row = {
        "pane_id": pane_id, "agent": "claude", "label": pane_id, "status": "working",
        "cwd": "/work/alpha", "project": "alpha", "host": "local", "remote": None,
        "workspace_id": "wA", "tab_id": "wA:t1", "title": "", "focused": False,
        "scrollback": 0, "viewport_rows": 40, "has_session": True, "order": 0,
        "last_active_at": T0, "last_seen_at": T0,
        "hp_role": role, "hp_audience": audience, "hp_project": slug, "hp_group": "",
    }
    row.update(extra)
    return row


COORD = agent("wA:pC", "coordinator", slug="alpha", label="hpc-alpha", order=0)
SECOND = agent("wA:pD", "coordinator", slug="alpha", label="hpc-alpha-1", status="blocked",
               order=5)
SESSION = agent("wA:pS", "session", slug="alpha", label="manual-review", status="done",
                last_active_at=T0 + 100, last_seen_at=T0, order=6)
INTERNAL = agent("wA:pT", "thread", "internal", "alpha", label="hp-alpha-t-0001",
                 status="blocked", hp_group="alpha!1!1!t-0001", order=1)
STALLED = agent("wA:pU", "thread", "stalled", "alpha", label="hp-alpha-t-0002",
                status="blocked", hp_group="alpha!1!2!t-0002", order=2)
HUMAN = agent("wA:pV", "thread", "human", "alpha", label="hp-alpha-t-0003",
              status="done", hp_group="alpha!1!3!t-0003", order=3)
GUEST = agent("wA:pG", "guest", "internal", "alpha", status="blocked", order=7)
OTHER = agent("wA:pO", "other", "internal", status="blocked", order=8)
LEGACY = agent("wB:p1", label="plain-session", cwd="/work/plain", project="plain",
               workspace_id="wB", tab_id="wB:t1", order=0)
REMOTE = agent("wB:pC", "coordinator", slug="alpha", label="hpc-alpha", host="boe",
               remote="boe", workspace_id="wB", tab_id="wB:t1", order=1)
SNAPSHOT = {"type": "agents", "agents": [COORD, SECOND, SESSION, INTERNAL, STALLED, HUMAN,
                                               GUEST, OTHER, LEGACY, REMOTE],
            "spaces": {"workspaces": [], "tabs": []}, "panes": []}

PROJECT_IDS = {"wA:pC", "wA:pD", "wA:pS", "wB:p1", "wB:pC"}
THREAD_IDS = {"wA:pT", "wA:pU", "wA:pV", "wA:pG", "wA:pO"}


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
@unittest.skipIf(_chrome() is None, "no chromium build available")
class WebProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = _shared["browser"].new_page(viewport=PHONE)
        cls.page.goto(PAGE)

    @classmethod
    def tearDownClass(cls):
        cls.page.close()

    def load(self, snapshot=SNAPSHOT):
        # Fresh attention ledgers, so each test observes the first-snapshot behaviour.
        self.page.evaluate("""s => {
          ws = {readyState: 1, send: () => {}};
          hidePanel(); hideTerminal();
          activeWorkspace = 'projects'; activeTab = null; herdView = 'projects';
          localStorage.setItem('herdr_herd_view', 'projects');
          timeline = []; rawTimeline = []; prevStatuses = {};
          previousAttention = {}; previousFollowups = new Map();
          agentsSnapshotReady = false; pendingPaneRequest = null;
          window.__cues = []; window.cue = name => window.__cues.push(name);
          recentOpen = true; recentDir = 'newest';
          handleMessage(s);
        }""", snapshot)

    def setUp(self):
        self.load()

    def rows(self):
        return self.page.eval_on_selector_all("#agents .agent", """els => els.map(e => ({
          source: e.dataset.sourcePaneId, target: e.dataset.paneId,
          bucket: e.dataset.bucket, title: e.innerText}))""")

    def test_projects_show_only_primary_sessions_and_legacy_fail_open(self):
        sources = {r["source"] for r in self.rows()}
        self.assertEqual(sources, PROJECT_IDS)
        # No worker thread, guest or other pane ever gets a Projects card.
        self.assertEqual(sources & THREAD_IDS, set())
        for pane_id in THREAD_IDS:
            self.assertEqual(self.page.locator(f'[data-source-pane-id="{pane_id}"]').count(), 0)

    def test_escalated_workers_badge_once_on_the_earliest_coordinator(self):
        badges = self.page.locator("#agents .follow-badge")
        self.assertEqual(badges.count(), 1)
        owner = self.page.locator('[data-source-pane-id="wA:pC"]')
        self.assertEqual(owner.locator(".follow-badge").count(), 1)
        self.assertEqual(owner.locator(".follow-badge").inner_text(), "2 follow-ups")
        self.assertEqual(
            owner.locator(".follow-badge").get_attribute("aria-label"), "2 thread follow-ups")
        # The second coordinator stays its own card and never duplicates the aggregate.
        second = self.page.locator('[data-source-pane-id="wA:pD"]')
        self.assertEqual(second.locator(".follow-badge").count(), 0)
        # The remote coordinator owns no remote threads, so it carries no badge.
        self.assertEqual(
            self.page.locator('[data-source-pane-id="wB:pC"] .follow-badge').count(), 0)

    def test_aggregate_puts_the_coordinator_in_needs_you_without_faking_its_status(self):
        owner = next(r for r in self.rows() if r["source"] == "wA:pC")
        self.assertEqual(owner["bucket"], "needs")
        # The underlying process is still working: the badge, not a fake block, drives the bucket.
        self.assertEqual(self.page.evaluate(
            "agents.find(a => a.pane_id === 'wA:pC').status"), "working")
        section = self.page.evaluate("""() => {
          let el = document.querySelector('[data-source-pane-id="wA:pC"]');
          while (el && !el.classList.contains('section-header')) el = el.previousElementSibling;
          return el && el.querySelector('.sec-label').textContent;
        }""")
        self.assertEqual(section, "Needs you")
        # The card's second line names the aggregate rather than pretending to be a prompt.
        self.assertIn("2 thread follow-ups", owner["title"])

    def test_coordinator_card_opens_the_coordinator_itself(self):
        row = self.page.locator('[data-source-pane-id="wA:pC"]')
        self.assertEqual(row.get_attribute("data-pane-id"), "wA:pC")
        row.click()
        self.assertEqual(self.page.evaluate("activePane"), "wA:pC")

    def test_two_coordinators_remain_two_cards_and_each_keeps_its_own_bucket(self):
        rows = {r["source"]: r for r in self.rows()}
        self.assertIn("wA:pC", rows)
        self.assertIn("wA:pD", rows)
        self.assertEqual(rows["wA:pD"]["bucket"], "needs")  # genuinely process-blocked
        self.assertEqual(rows["wA:pS"]["bucket"], "ready")  # done while unseen

    def test_threads_without_a_live_coordinator_get_no_card_and_no_alert(self):
        orphaned = {
            "type": "agents",
            "agents": [SESSION, INTERNAL, STALLED, HUMAN, GUEST, OTHER, LEGACY, REMOTE],
            "spaces": {"workspaces": [], "tabs": []}, "panes": [],
        }
        self.load(orphaned)
        sources = {r["source"] for r in self.rows()}
        self.assertNotIn("wA:pU", sources)
        self.assertNotIn("wA:pV", sources)
        self.assertEqual(self.page.locator(".follow-badge").count(), 0)
        # Nothing primary is blocked and the orphan aggregate cannot chime.
        self.assertEqual(self.page.evaluate("window.__cues"), [])
        # The workers are not lost: the raw stream still reaches them.
        self.page.get_by_role("tab", name="All panes").click()
        for pane_id in ("wA:pU", "wA:pV"):
            self.assertEqual(
                self.page.locator(f'[data-source-pane-id="{pane_id}"]').count(), 1)

    def test_a_coordinator_appearing_after_the_escalation_still_fires_once(self):
        orphaned = {
            "type": "agents",
            "agents": [SESSION, STALLED, HUMAN],
            "spaces": {"workspaces": [], "tabs": []}, "panes": [],
        }
        self.load(orphaned)
        self.assertEqual(self.page.locator(".follow-badge").count(), 0)
        self.assertEqual(self.page.evaluate("window.__cues"), [])
        # Coordinator comes online in a later snapshot: the aggregate now fires exactly once.
        self.page.evaluate("s => handleMessage(s)", SNAPSHOT)
        self.assertEqual(self.page.evaluate("window.__cues"), ["chime", "chime"])
        owner = self.page.locator('[data-source-pane-id="wA:pC"]')
        self.assertEqual(owner.locator(".follow-badge").inner_text(), "2 follow-ups")
        # A further repaint does not re-fire.
        self.page.evaluate("s => handleMessage(s)", SNAPSHOT)
        self.assertEqual(self.page.evaluate("window.__cues"), ["chime", "chime"])

    def test_registered_slug_groups_and_host_disambiguates_same_slug(self):
        headings = self.page.locator("#agents .project-heading").all_inner_texts()
        self.assertTrue(any(h.startswith("alpha") for h in headings))
        self.assertTrue(any("alpha @boe" in h for h in headings))
        self.assertNotIn("threads", " ".join(headings))

    def test_all_panes_restores_every_agent_including_every_worker(self):
        self.page.get_by_role("tab", name="All panes").click()
        self.assertEqual(self.page.locator("#agents .agent").count(), len(SNAPSHOT["agents"]))
        for pane_id in THREAD_IDS:
            self.assertEqual(
                self.page.locator(f'[data-source-pane-id="{pane_id}"]').count(), 1)
        self.assertEqual(
            self.page.get_by_role("tab", name="All panes").get_attribute("aria-selected"), "true")

    def test_first_snapshot_chimes_once_per_project_and_keeps_threads_out_of_timeline(self):
        # setUp's load() is the first snapshot: one block chime (SECOND) + one aggregate chime.
        self.assertEqual(sorted(self.page.evaluate("window.__cues")), ["chime", "chime"])
        labels = self.page.evaluate("timeline.map(e => e.label)")
        self.assertEqual(set(labels), {"hpc-alpha", "hpc-alpha-1"})
        self.assertFalse(any("t-000" in label for label in labels))
        aggregate = self.page.evaluate(
            "timeline.find(e => e.followups && e.followups === 2)")
        self.assertIsNotNone(aggregate)
        self.assertEqual(aggregate["label"], "hpc-alpha")
        # A repaint with the same escalations chimes and records nothing new.
        before = self.page.evaluate("timeline.length")
        self.page.evaluate("s => handleMessage(s)", SNAPSHOT)
        self.assertEqual(self.page.evaluate("timeline.length"), before)
        self.assertEqual(sorted(self.page.evaluate("window.__cues")), ["chime", "chime"])

    def test_worker_events_only_ever_reach_the_raw_feed(self):
        # Re-arm with every thread internal: no project escalation exists yet. The blocked
        # coordinator stays, so its own attention chime is already counted in the baseline.
        internal = {
            "type": "agents",
            "agents": [SECOND, dict(STALLED, hp_audience="internal"),
                       dict(HUMAN, hp_audience="internal"), INTERNAL, COORD],
            "spaces": {"workspaces": [], "tabs": []}, "panes": [],
        }
        self.load(internal)
        cues_before = len(self.page.evaluate("window.__cues"))
        self.assertEqual(self.page.evaluate("timeline.map(e => e.label)"), ["hpc-alpha-1"])
        self.page.evaluate("""() => {
          handleMessage({type: 'blocked', pane_id: 'wA:pT', agent: 'claude', project: 't-0001',
                         host: 'local', prompt: 'Allow?', prompt_id: 'internal', update: false});
          handleMessage({type: 'blocked', pane_id: 'wA:pU', agent: 'claude', project: 't-0002',
                         host: 'local', prompt: 'Allow?', prompt_id: 'stalled', update: true});
        }""")
        # No new sound: a worker block is never an attention event.
        self.assertEqual(len(self.page.evaluate("window.__cues")), cues_before)
        labels = self.page.evaluate("timeline.map(e => e.label)")
        self.assertEqual(labels, ["hpc-alpha-1"])
        self.assertIn("hp-alpha-t-0001",
                      self.page.evaluate("rawTimeline.map(e => e.label)"))
        # When the project later escalates, exactly one aggregate chime lands on the coordinator.
        self.page.evaluate("s => handleMessage(s)", SNAPSHOT)
        self.assertEqual(len(self.page.evaluate("window.__cues")), cues_before + 1)
        labels = self.page.evaluate("timeline.map(e => e.label)")
        self.assertIn("hpc-alpha", labels)
        self.assertFalse(any("t-000" in label for label in labels))

    def test_sparse_update_does_not_reveal_an_internal_worker(self):
        update = dict(INTERNAL)
        update.pop("hp_role")
        update.pop("hp_audience")
        update.pop("hp_project")
        update["status"] = "working"
        self.page.evaluate("u => handleMessage({type: 'agent_update', agent: u})", update)
        self.assertEqual(
            self.page.locator('[data-source-pane-id="wA:pT"]').count(), 0)

    def test_push_deep_link_waits_for_a_snapshot_and_opens_the_requested_pane(self):
        self.page.evaluate("""() => {
          hideTerminal(); agentsSnapshotReady = false;
          requestPane('wA:pD');
        }""")
        self.assertIsNone(self.page.evaluate("activePane"))
        self.page.evaluate("s => handleMessage(s)", SNAPSHOT)
        self.assertEqual(self.page.evaluate("activePane"), "wA:pD")
        self.assertIsNone(self.page.evaluate("pendingPaneRequest"))

    def test_the_projection_choice_survives_a_reload(self):
        self.page.evaluate("setHerdView('panes')")
        self.page.reload()
        self.assertIsNone(self.page.evaluate("activeWorkspace"))
        self.page.evaluate("""s => {
          ws = {readyState: 1, send: () => {}};
          handleMessage(s);
        }""", SNAPSHOT)
        self.assertEqual(
            self.page.locator("#agents .agent").count(), len(SNAPSHOT["agents"]))

    def test_projects_fit_a_phone_without_horizontal_page_scroll(self):
        self.assertTrue(
            self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertEqual(
            self.page.get_by_role("tab", name="Projects").get_attribute("aria-selected"), "true")


if __name__ == "__main__":
    unittest.main()
