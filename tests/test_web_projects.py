"""Coordinator/session-centred project projection, exercised in the real web app."""
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
        "scrollback": 0, "viewport_rows": 40, "has_session": True,
        "last_active_at": T0, "last_seen_at": T0,
        "hp_role": role, "hp_audience": audience, "hp_project": slug, "hp_group": "",
    }
    row.update(extra)
    return row


COORD = agent("wA:pC", "coordinator", slug="alpha", label="hpc-alpha")
SECOND = agent("wA:pD", "coordinator", slug="alpha", label="hpc-alpha-1", status="blocked")
SESSION = agent("wA:pS", "session", slug="alpha", label="manual-review", status="done",
                last_active_at=T0 + 100, last_seen_at=T0)
INTERNAL = agent("wA:pT", "thread", "internal", "alpha", label="hp-alpha-t-0001",
                 status="blocked", hp_group="alpha!1!1!t-0001")
STALLED = agent("wA:pU", "thread", "stalled", "alpha", label="hp-alpha-t-0002",
                status="blocked", hp_group="alpha!1!2!t-0002")
HUMAN = agent("wA:pV", "thread", "human", "alpha", label="hp-alpha-t-0003",
              status="done", hp_group="alpha!1!3!t-0003")
GUEST = agent("wA:pG", "guest", "internal", "alpha", status="blocked")
OTHER = agent("wA:pO", "other", "internal", status="blocked")
LEGACY = agent("wB:p1", label="plain-session", cwd="/work/plain", project="plain")
REMOTE = agent("wB:pC", "coordinator", slug="alpha", label="hpc-alpha", host="boe",
               remote="boe", workspace_id="wB", tab_id="wB:t1")
SNAPSHOT = {"type": "agents", "agents": [COORD, SECOND, SESSION, INTERNAL, STALLED, HUMAN,
                                               GUEST, OTHER, LEGACY, REMOTE],
            "spaces": {"workspaces": [], "tabs": []}, "panes": []}


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

    def setUp(self):
        self.page.evaluate("""s => {
          ws = {readyState: 1, send: () => {}};
          hidePanel(); hideTerminal();
          activeWorkspace = 'projects'; activeTab = null; herdView = 'projects';
          localStorage.setItem('herdr_herd_view', 'projects');
          timeline = []; rawTimeline = []; prevStatuses = {}; previousAttention = {};
          agentsSnapshotReady = false; pendingPaneRequest = null;
          window.__cues = []; window.cue = name => window.__cues.push(name);
          recentOpen = true; recentDir = 'newest';
          handleMessage(s);
          timeline = []; rawTimeline = []; window.__cues = [];
        }""", SNAPSHOT)

    def rows(self):
        return self.page.eval_on_selector_all("#agents .agent", """els => els.map(e => ({
          source: e.dataset.sourcePaneId, target: e.dataset.paneId,
          bucket: e.dataset.bucket, title: e.innerText}))""")

    def test_projects_keep_coordinators_and_sessions_not_internal_workers(self):
        rows = self.rows()
        self.assertEqual({r["source"] for r in rows},
                         {"wA:pC", "wA:pD", "wA:pS", "wA:pU", "wA:pV", "wB:p1", "wB:pC"})
        self.assertNotIn("wA:pT", {r["source"] for r in rows})
        self.assertNotIn("wA:pG", {r["source"] for r in rows})
        self.assertNotIn("wA:pO", {r["source"] for r in rows})
        self.assertEqual(self.page.locator('[data-source-pane-id="wA:pC"]').count(), 1)
        self.assertEqual(self.page.locator('[data-source-pane-id="wA:pD"]').count(), 1)

    def test_human_thread_is_attention_and_returns_to_its_coordinator(self):
        row = next(r for r in self.rows() if r["source"] == "wA:pV")
        self.assertEqual(row["bucket"], "needs")
        self.assertEqual(row["target"], "wA:pC")
        section = self.page.evaluate("""() => {
          let el = document.querySelector('[data-source-pane-id="wA:pV"]');
          while (el && !el.classList.contains('section-header')) el = el.previousElementSibling;
          return el && el.querySelector('.sec-label').textContent;
        }""")
        self.assertEqual(section, "Needs you")
        self.page.locator('[data-source-pane-id="wA:pV"]').click()
        self.assertEqual(self.page.evaluate("activePane"), "wA:pC")

    def test_stalled_thread_is_the_deliberate_direct_worker_route(self):
        row = next(r for r in self.rows() if r["source"] == "wA:pU")
        self.assertEqual(row["target"], "wA:pU")
        self.page.locator('[data-source-pane-id="wA:pU"]').click()
        self.assertEqual(self.page.evaluate("activePane"), "wA:pU")

    def test_registered_slug_groups_and_host_disambiguates_same_slug(self):
        headings = self.page.locator("#agents .project-heading").all_inner_texts()
        self.assertTrue(any(h.startswith("alpha") for h in headings))
        self.assertTrue(any("alpha @boe" in h for h in headings))
        self.assertNotIn("threads", " ".join(headings))

    def test_all_panes_restores_every_agent_and_the_legacy_raw_herd(self):
        self.page.get_by_role("tab", name="All panes").click()
        self.assertEqual(self.page.locator("#agents .agent").count(), len(SNAPSHOT["agents"]))
        self.assertEqual(self.page.locator('[data-source-pane-id="wA:pT"]').count(), 1)
        self.assertEqual(self.page.get_by_role("tab", name="All panes").get_attribute("aria-selected"), "true")

    def test_internal_workers_make_no_default_timeline_or_sound(self):
        self.page.evaluate("""s => {
          const internal = JSON.parse(JSON.stringify(s));
          internal.agents.find(a => a.pane_id === 'wA:pU').hp_audience = 'internal';
          handleMessage(internal);
          handleMessage({type: 'blocked', pane_id: 'wA:pT', agent: 'claude', project: 't-0001',
                         host: 'local', prompt: 'Allow?', prompt_id: 'internal', update: false});
          // The audience upgrade arrives only in a snapshot: prompt_id and blocked status stay put.
          handleMessage(s);
          handleMessage({type: 'blocked', pane_id: 'wA:pU', agent: 'claude', project: 't-0002',
                         host: 'local', prompt: 'Allow?', prompt_id: 'stalled', update: true});
        }""", SNAPSHOT)
        self.assertEqual(self.page.evaluate("window.__cues"), ["chime"])
        self.assertEqual(self.page.evaluate("timeline.map(e => e.label)"), ["hp-alpha-t-0002"])
        self.assertIn("hp-alpha-t-0001", self.page.evaluate("rawTimeline.map(e => e.label)"))

    def test_sparse_update_does_not_reveal_an_internal_worker(self):
        update = dict(INTERNAL)
        update.pop("hp_role")
        update.pop("hp_audience")
        update.pop("hp_project")
        update["status"] = "working"
        self.page.evaluate("u => handleMessage({type: 'agent_update', agent: u})", update)
        self.assertEqual(self.page.locator('[data-source-pane-id="wA:pT"]').count(), 0)

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
        self.assertEqual(self.page.locator("#agents .agent").count(), len(SNAPSHOT["agents"]))

    def test_projects_fit_a_phone_without_horizontal_page_scroll(self):
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertEqual(self.page.get_by_role("tab", name="Projects").get_attribute("aria-selected"), "true")
