"""pf.23 project-first Projects layout, exercised in the real web app.

Projects is one compact row PER (host, hp_project), ordered by project urgency, with sessions in an
inline accordion. The pf.22 rule is unchanged: only coordinator/session (and legacy fail-open) panes
are members; no hp_role=thread, guest or other pane ever becomes a row, and human/stalled workers are
an aggregate follow-up count on the owning coordinator. All panes keeps the raw triage list.
"""
import os
from pathlib import Path
import unittest


PAGE = (Path(__file__).resolve().parents[1] / "web" / "index.html").as_uri()
SHOTS = Path("/tmp/pf23-test-shots")
CHROME_CANDIDATES = [
    os.environ.get("HERDR_TEST_CHROME", ""),
    os.path.expanduser("~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"),
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
]
PHONE = {"width": 390, "height": 844}
T0 = 1_700_000_000_000
THREAD_IDS = {"wA:pT", "wA:pU", "wA:pV", "wA:pG", "wA:pO"}


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


COORD = agent("wA:pC", "coordinator", slug="alpha", label="hpc-alpha", order=0,
              title="Reviewing the deploy plan")
SECOND = agent("wA:pD", "coordinator", slug="alpha", label="hpc-alpha-1", status="blocked",
               order=5, title="Merging the queue backlog",
               last_active_at=T0 + 400, last_seen_at=T0)
SESSION = agent("wA:pS", "session", slug="alpha", label="manual-review", status="done",
                title="Hand-started incident check", order=6,
                last_active_at=T0 + 100, last_seen_at=T0)
INTERNAL = agent("wA:pT", "thread", "internal", "alpha", label="hp-alpha-t-0001",
                 status="blocked", hp_group="alpha!1!1!t-0001", order=1)
STALLED = agent("wA:pU", "thread", "stalled", "alpha", label="hp-alpha-t-0002",
                status="blocked", hp_group="alpha!1!2!t-0002", order=2)
HUMAN = agent("wA:pV", "thread", "human", "alpha", label="hp-alpha-t-0003",
              status="done", hp_group="alpha!1!3!t-0003", order=3)
GUEST = agent("wA:pG", "guest", "internal", "alpha", status="blocked", order=7)
OTHER = agent("wA:pO", "other", "internal", status="blocked", order=8)
LEGACY = agent("wB:p1", label="plain-session", cwd="/work/plain", project="plain",
               workspace_id="wB", tab_id="wB:t1", title="Idle maintenance shell",
               status="idle", last_active_at=T0 - 5000, last_seen_at=T0 - 4000)
REMOTE = agent("wB:pC", "coordinator", slug="alpha", label="hpc-alpha", host="boe",
               remote="boe", workspace_id="wB", tab_id="wB:t1", order=1,
               title="Remote coordinator standup")
SNAPSHOT = {"type": "agents", "agents": [COORD, SECOND, SESSION, INTERNAL, STALLED, HUMAN,
                                               GUEST, OTHER, LEGACY, REMOTE],
            "spaces": {"workspaces": [], "tabs": []}, "panes": []}

# Project keys in rendered urgency order.
ALPHA_LOCAL = "local|alpha"
ALPHA_BOE = "boe|alpha"
PLAIN_LOCAL = "local|plain"


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
@unittest.skipIf(_chrome() is None, "no chromium build available")
class WebProjectLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = _shared["browser"].new_page(viewport=PHONE)
        cls.page.goto(PAGE)

    @classmethod
    def tearDownClass(cls):
        cls.page.close()

    def load(self, snapshot=SNAPSHOT):
        self.page.evaluate("""s => {
          ws = {readyState: 1, send: () => {}};
          hidePanel(); hideTerminal();
          activeWorkspace = 'projects'; activeTab = null; herdView = 'projects';
          localStorage.setItem('herdr_herd_view', 'projects');
          localStorage.removeItem('herdr_project_expanded');
          projectExpansion = null; seenProjectKeys.clear();
          timeline = []; rawTimeline = []; prevStatuses = {};
          previousAttention = {}; previousFollowups = new Map();
          agentsSnapshotReady = false; pendingPaneRequest = null;
          window.__cues = []; window.cue = name => window.__cues.push(name);
          handleMessage(s);
        }""", snapshot)

    def setUp(self):
        self.load()

    def groups(self):
        return self.page.eval_on_selector_all(
            ".project-group",
            """els => els.map(e => ({
              key: e.dataset.projectKey, bucket: e.dataset.bucket,
              expanded: e.classList.contains('expanded'),
              slug: e.querySelector('.project-slug').textContent,
              meta: [...e.querySelectorAll('.project-meta-chip')].map(c => c.textContent),
              host: e.querySelector('.project-meta-chip.is-host')?.textContent || '',
              badge: e.querySelector('.project-count')?.textContent || '',
              members: [...e.querySelectorAll('.project-session')].map(li => ({
                id: li.dataset.paneId, role: li.querySelector('.ps-role').textContent,
                state: li.querySelector('.ps-state').textContent,
                title: li.querySelector('.ps-title').textContent,
                followups: li.querySelector('.ps-followups')?.textContent || '',
              })),
            }))""")

    def test_one_row_per_project_and_not_a_single_thread_card(self):
        groups = self.groups()
        self.assertEqual([g["key"] for g in groups], [ALPHA_LOCAL, ALPHA_BOE, PLAIN_LOCAL])
        # A project split across status sections would render twice; each key appears exactly once.
        keys = [g["key"] for g in groups]
        self.assertEqual(len(keys), len(set(keys)))
        # No worker/guest/other pane id survives anywhere in the Projects surface.
        rendered = set(self.page.eval_on_selector_all(
            "[data-source-pane-id]", "els => els.map(e => e.dataset.sourcePaneId)"))
        self.assertEqual(rendered & THREAD_IDS, set())
        # The global status sections are gone from Projects.
        self.assertEqual(self.page.locator(".section-header").count(), 0)
        self.assertEqual(self.page.locator(".project-heading").count(), 0)

    def test_the_full_slug_is_the_primary_text_never_a_truncated_hpc_prefix(self):
        slugs = self.page.locator(".project-slug").all_inner_texts()
        self.assertIn("alpha", slugs)
        self.assertIn("plain", slugs)
        self.assertFalse(any(s.startswith("hpc-") for s in slugs))

    def test_projects_are_ordered_by_urgency_and_default_to_attention_expanded(self):
        groups = self.groups()
        self.assertEqual([g["bucket"] for g in groups], ["needs", "working", "recent"])
        # Implicit default: the first/attention project open, quiet projects collapsed.
        self.assertTrue(groups[0]["expanded"])
        self.assertFalse(groups[1]["expanded"])
        self.assertFalse(groups[2]["expanded"])

    def test_meta_counts_are_honest_and_the_aggregate_badge_sits_on_one_coordinator(self):
        alpha = self.groups()[0]
        self.assertEqual(alpha["badge"], "2 follow-ups")
        self.assertIn("2 coordinators", alpha["meta"])
        self.assertIn("1 session", alpha["meta"])
        self.assertIn("1 blocked", alpha["meta"])
        self.assertIn("1 ready", alpha["meta"])
        self.assertIn("1 active", alpha["meta"])
        # Exactly one member row carries the quiet follow-up pill, and it is the earliest coordinator.
        with_pill = [m for m in alpha["members"] if m["followups"]]
        self.assertEqual([m["id"] for m in with_pill], ["wA:pC"])
        self.assertEqual(with_pill[0]["followups"], "2 follow-ups")
        self.assertEqual([m["id"] for m in alpha["members"]], ["wA:pC", "wA:pD", "wA:pS"])

    def test_expansion_reveals_distinct_sessions_that_open_their_own_pane(self):
        # The boe project starts collapsed: its panel is hidden.
        boe = self.page.locator(f'.project-group[data-project-key="{ALPHA_BOE}"]')
        self.assertEqual(boe.locator(".project-row").get_attribute("aria-expanded"), "false")
        self.assertFalse(boe.locator(".project-sessions").is_visible())
        boe.locator(".project-row").click()
        self.assertEqual(boe.locator(".project-row").get_attribute("aria-expanded"), "true")
        self.assertTrue(boe.locator(".project-session").first.is_visible())
        # One distinct row per primary pane; coordinators are never merged.
        self.assertEqual(boe.locator(".project-session").count(), 1)
        boe.locator(".project-session").click()
        self.assertEqual(self.page.evaluate("activePane"), "wB:pC")

    def test_expansion_is_persisted_per_browser(self):
        boe = self.page.locator(f'.project-group[data-project-key="{ALPHA_BOE}"]')
        boe.locator(".project-row").click()
        stored = self.page.evaluate("JSON.parse(localStorage.herdr_project_expanded)")
        self.assertIn(ALPHA_BOE, stored)
        self.page.reload()
        self.page.goto(PAGE)
        # Reload WITHOUT the setUp reset, which deliberately clears the store: here the persisted
        # choice must survive. Re-seed only the socket and view, leaving herdr_project_expanded.
        self.page.evaluate("""s => {
          ws = {readyState: 1, send: () => {}};
          hidePanel(); hideTerminal();
          activeWorkspace = 'projects'; activeTab = null; herdView = 'projects';
          seenProjectKeys.clear();
          agentsSnapshotReady = false; pendingPaneRequest = null;
          handleMessage(s);
        }""", SNAPSHOT)
        # The stored choice survives the reload: boe stays open.
        self.assertTrue(
            self.page.locator(f'.project-group[data-project-key="{ALPHA_BOE}"]')
            .evaluate("e => e.classList.contains('expanded')"))

    def test_host_is_a_group_level_marker_not_repeated_on_member_rows(self):
        boe = self.groups()[1]
        self.assertEqual(boe["host"], "@boe")
        local_hosts = self.page.locator(
            f'.project-group[data-project-key="{ALPHA_LOCAL}"] .project-meta-chip.is-host').count()
        self.assertEqual(local_hosts, 0)
        # Member rows never carry a host string of their own.
        self.assertEqual(self.page.locator(".project-session .project-host").count(), 0)
        self.assertEqual(self.page.locator(".project-session .is-host").count(), 0)

    def test_no_nested_interactive_controls_inside_the_toggle_row(self):
        # The whole project row is the one button; chevron/dot/badge are non-interactive spans and
        # session rows are siblings in the panel, not children of the toggle.
        offenders = self.page.eval_on_selector_all(
            ".project-row",
            "els => els.flatMap(e => [...e.querySelectorAll('button,[role=button],a')].map(x => x.className))")
        self.assertEqual(offenders, [])
        toggles = self.page.locator(".project-row[aria-expanded][aria-controls]")
        self.assertEqual(toggles.count(), 3)

    def test_orphan_threads_get_no_group_badge_or_alert(self):
        orphaned = {"type": "agents",
                    "agents": [SESSION, INTERNAL, STALLED, HUMAN, GUEST, OTHER],
                    "spaces": {"workspaces": [], "tabs": []}, "panes": []}
        self.load(orphaned)
        # The session's project exists, but carries no FOLLOW-UP badge: no live coordinator owns the
        # escalated workers. The session's own ready state may still show its honest "1 ready" pill.
        groups = self.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            self.page.locator(".project-count.is-followups").count(), 0)
        self.assertEqual(
            self.page.locator(".ps-followups").count(), 0)
        self.assertEqual(self.page.evaluate("window.__cues"), [])
        rendered = set(self.page.eval_on_selector_all(
            "[data-source-pane-id]", "els => els.map(e => e.dataset.sourcePaneId)"))
        self.assertEqual(rendered & THREAD_IDS, set())

    def test_deep_link_opens_the_pane_and_expands_its_project(self):
        # boe starts collapsed; a notification deep link must still land on the pane.
        self.page.evaluate("""() => {
          hideTerminal(); agentsSnapshotReady = false;
          requestPane('wB:pC');
        }""")
        self.assertIsNone(self.page.evaluate("activePane"))
        self.page.evaluate("s => handleMessage(s)", SNAPSHOT)
        self.assertEqual(self.page.evaluate("activePane"), "wB:pC")
        self.assertTrue(
            self.page.locator(f'.project-group[data-project-key="{ALPHA_BOE}"]')
            .evaluate("e => e.classList.contains('expanded')"))

    def test_all_panes_keeps_the_raw_triage_list_with_every_worker(self):
        self.page.get_by_role("tab", name="All panes").click()
        self.assertEqual(self.page.locator("#agents .agent").count(), len(SNAPSHOT["agents"]))
        for pane_id in THREAD_IDS:
            self.assertEqual(
                self.page.locator(f'[data-source-pane-id="{pane_id}"]').count(), 1)
        # The legacy global sections are intact in the raw view.
        self.assertGreater(self.page.locator(".section-header").count(), 0)
        self.assertEqual(
            self.page.get_by_role("tab", name="All panes").get_attribute("aria-selected"), "true")

    def test_attention_semantics_survive_the_redesign(self):
        # One block chime (the genuinely blocked second coordinator) + one aggregate chime.
        self.assertEqual(sorted(self.page.evaluate("window.__cues")), ["chime", "chime"])
        labels = self.page.evaluate("timeline.map(e => e.label)")
        self.assertEqual(set(labels), {"hpc-alpha", "hpc-alpha-1"})
        self.assertFalse(any("t-000" in label for label in labels))
        aggregate = self.page.evaluate(
            "timeline.find(e => e.followups && e.followups === 2)")
        self.assertEqual(aggregate["label"], "hpc-alpha")
        # A repaint fires nothing new.
        before = self.page.evaluate("timeline.length")
        self.page.evaluate("s => handleMessage(s)", SNAPSHOT)
        self.assertEqual(self.page.evaluate("timeline.length"), before)
        self.assertEqual(sorted(self.page.evaluate("window.__cues")), ["chime", "chime"])

    def test_worker_events_only_reach_the_raw_feed(self):
        internal = {"type": "agents",
                    "agents": [SECOND, dict(STALLED, hp_audience="internal"),
                               dict(HUMAN, hp_audience="internal"), INTERNAL, COORD],
                    "spaces": {"workspaces": [], "tabs": []}, "panes": []}
        self.load(internal)
        cues_before = len(self.page.evaluate("window.__cues"))
        self.page.evaluate("""() => {
          handleMessage({type: 'blocked', pane_id: 'wA:pT', agent: 'claude', project: 't-0001',
                         host: 'local', prompt: 'Allow?', prompt_id: 'internal', update: false});
        }""")
        self.assertEqual(len(self.page.evaluate("window.__cues")), cues_before)
        self.assertIn("hp-alpha-t-0001",
                      self.page.evaluate("rawTimeline.map(e => e.label)"))
        self.assertNotIn("hp-alpha-t-0001",
                         self.page.evaluate("timeline.map(e => e.label)"))

    def test_sparse_worker_update_creates_no_project_surface(self):
        update = dict(INTERNAL)
        update.pop("hp_role")
        update.pop("hp_audience")
        update.pop("hp_project")
        self.page.evaluate("u => handleMessage({type: 'agent_update', agent: u})", update)
        self.assertEqual(
            self.page.locator('[data-source-pane-id="wA:pT"]').count(), 0)

    def test_empty_state_explains_the_two_views(self):
        only_workers = {"type": "agents",
                        "agents": [INTERNAL, STALLED, HUMAN, GUEST, OTHER],
                        "spaces": {"workspaces": [], "tabs": []}, "panes": []}
        self.load(only_workers)
        text = self.page.locator(".empty").inner_text()
        self.assertIn("Projects", text)
        self.assertIn("All panes", text)
        self.assertEqual(self.page.locator(".project-group").count(), 0)

    def test_projection_choice_survives_a_reload(self):
        self.page.evaluate("setHerdView('panes')")
        self.page.reload()
        self.page.goto(PAGE)
        self.page.evaluate("""s => {
          ws = {readyState: 1, send: () => {}};
          handleMessage(s);
        }""", SNAPSHOT)
        self.assertEqual(
            self.page.locator("#agents .agent").count(), len(SNAPSHOT["agents"]))

    # ---- geometry: phone and two desktop widths -------------------------------------------

    def _geometry_page(self, width):
        page = _shared["browser"].new_page(viewport={"width": width, "height": 900})
        page.goto(PAGE)
        page.evaluate("""s => {
          ws = {readyState: 1, send: () => {}};
          activeWorkspace = 'projects'; herdView = 'projects';
          localStorage.removeItem('herdr_project_expanded');
          projectExpansion = null; seenProjectKeys.clear();
          handleMessage(s);
        }""", SNAPSHOT)
        page.wait_for_selector(".project-list")
        return page

    def _assert_geometry(self, page, width):
        geo = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('.project-row')].map(r => {
            const b = r.getBoundingClientRect();
            return {h: Math.round(b.height), w: Math.round(b.width)};
          });
          const sessions = [...document.querySelectorAll('.project-session')]
            .map(r => Math.round(r.getBoundingClientRect().height)).filter(h => h > 0);
          const boxes = [...document.querySelectorAll('.project-row, .project-session')]
            .map(e => e.getBoundingClientRect());
          const overlap = boxes.some((a, i) => boxes.some((b, j) => i !== j
            && a.top < b.bottom && b.top < a.bottom && a.left < b.right && b.left < a.right));
          const list = document.querySelector('.project-list').getBoundingClientRect();
          return {rows, sessions, overlap, scrollWidth: document.documentElement.scrollWidth,
                  innerWidth, listWidth: Math.round(list.width),
                  listLeft: Math.round(list.left), listRight: Math.round(list.right)};
        }""")
        self.assertLessEqual(geo["scrollWidth"], width)
        self.assertFalse(geo["overlap"])
        self.assertTrue(all(r["h"] >= 40 for r in geo["rows"]))
        self.assertTrue(all(h >= 40 for h in geo["sessions"]))
        return geo

    def test_mobile_geometry(self):
        page = self._geometry_page(390)
        try:
            geo = self._assert_geometry(page, 390)
            self.assertTrue(all(r["w"] <= 366 for r in geo["rows"]))
        finally:
            page.close()

    def test_desktop_geometry_and_readable_width_at_1280_and_1440(self):
        for width in (1280, 1440):
            page = self._geometry_page(width)
            try:
                geo = self._assert_geometry(page, width)
                # The list is a single readable column, never wider than 760px, centred.
                self.assertLessEqual(geo["listWidth"], 760)
                self.assertAlmostEqual(geo["listLeft"], width - geo["listRight"], delta=2)
            finally:
                page.close()

    def test_collapsed_rows_are_substantially_more_compact_than_cards(self):
        page = self._geometry_page(390)
        try:
            page.click('[data-project-action="collapse"]')
            # The compactness is structural: a collapsed group is ONE toggle row no matter how many
            # members it holds, with every session panel hidden. Meta chips may wrap (the brief
            # permits two-line rows), so the assertion is the visible child count, not a pixel cap.
            for group in page.locator(".project-group").all():
                self.assertEqual(group.locator(".project-row:visible").count(), 1)
                self.assertEqual(group.locator(".project-session:visible").count(), 0)
                # One wrapped row stays a single compact block, never a stack of member cards.
                self.assertLessEqual(group.bounding_box()["height"], 120)
            collapsed = page.eval_on_selector_all(
                ".project-group", "els => els.map(e => e.getBoundingClientRect().height)")
            page.click('[data-project-action="expand"]')
            expanded = page.eval_on_selector_all(
                ".project-group", "els => els.map(e => e.getBoundingClientRect().height)")
            self.assertGreater(sum(expanded), sum(collapsed))
        finally:
            page.close()

    def test_captures_screenshots_for_review(self):
        SHOTS.mkdir(exist_ok=True)
        for name, width in (("mobile", 390), ("desktop", 1280)):
            page = self._geometry_page(width)
            try:
                page.screenshot(path=str(SHOTS / f"pf23-{name}-collapsed.png"), full_page=True)
                page.click('[data-project-action="expand"]')
                page.screenshot(path=str(SHOTS / f"pf23-{name}-expanded.png"), full_page=True)
            finally:
                page.close()
        self.assertEqual(len(list(SHOTS.glob("pf23-*.png"))), 4)


if __name__ == "__main__":
    unittest.main()
