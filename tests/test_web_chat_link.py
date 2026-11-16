"""The Chat link in the terminal header: which panes offer it, and where it points.

The pane-level Chat view is not this app's: it is the herdr-web-ui plugin, a separate front end
that reads the same pane. It runs once per host, so a link is only meaningful for panes on the
host that instance owns -- a pane id from another host does not exist there. Shells have no
transcript at all. Those two rules are what these tests pin down, in a real browser at desktop
and phone widths.
"""
import os
from pathlib import Path
import unittest


PAGE = (Path(__file__).resolve().parents[1] / "web" / "index.html").as_uri()

CHROME_CANDIDATES = [
    os.environ.get("HERDR_TEST_CHROME", ""),
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    os.path.expanduser("~/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"),
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
]

PHONE = {"width": 390, "height": 844}


def _chrome():
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    return None


try:  # pragma: no cover - environment probe
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


_shared = {}


def setUpModule():  # noqa: N802 - unittest's own name
    if sync_playwright is None or _chrome() is None:
        return
    _shared["playwright"] = sync_playwright().start()
    _shared["browser"] = _shared["playwright"].chromium.launch(executable_path=_chrome())


def tearDownModule():  # noqa: N802 - unittest's own name
    if "browser" in _shared:
        _shared["browser"].close()
        _shared["playwright"].stop()
    _shared.clear()


class ChatLinkTest(unittest.TestCase):
    def _page(self, viewport=None):
        if "browser" not in _shared:
            self.skipTest("playwright or a chromium build is unavailable")
        page = _shared["browser"].new_page(viewport=viewport)
        page.set_default_timeout(15000)
        page.goto(PAGE)
        page.wait_for_function("typeof openTerminal === 'function'")
        return page

    def _open_agent(self, page, pane_id, host):
        page.evaluate(
            """([paneId, host]) => {
              agents = [{pane_id: paneId, host, agent: 'claude', project: 'demo',
                         cwd: '/tmp/demo', label: 'demo', status: 'idle', has_session: true}];
              shellPanes = [];
              openTerminal(paneId);
            }""",
            [pane_id, host],
        )

    def test_local_pane_links_to_the_chat_view(self):
        page = self._page()
        self._open_agent(page, "w1:p1", "local")
        link = page.locator("#chatLink")
        self.assertTrue(link.is_visible())
        href = link.get_attribute("href")
        self.assertIn("?pane=w1%3Ap1", href)
        self.assertIn("view=chat", href)
        self.assertTrue(href.startswith("http"))
        page.close()

    def test_other_host_pane_has_no_link(self):
        page = self._page()
        self._open_agent(page, "w2:p1", "mac-herdr")
        self.assertFalse(page.locator("#chatLink").is_visible())
        page.close()

    def test_shell_pane_has_no_link(self):
        page = self._page()
        page.evaluate(
            """() => {
              agents = [];
              shellPanes = [{pane_id: 'w3:p1', host: 'local', project: 'demo', label: 'shell'}];
              openTerminal('w3:p1');
            }"""
        )
        self.assertFalse(page.locator("#chatLink").is_visible())
        page.close()

    def test_link_is_tappable_at_phone_width(self):
        page = self._page(PHONE)
        self._open_agent(page, "w1:p2", "local")
        link = page.locator("#chatLink")
        self.assertTrue(link.is_visible())
        box = link.bounding_box()
        self.assertGreater(box["width"], 0)
        self.assertGreaterEqual(box["x"], 0)
        self.assertLessEqual(box["x"] + box["width"], PHONE["width"])
        page.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
