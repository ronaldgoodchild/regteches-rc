"""The web manual (docs/index.html) must hang together: every picture it shows exists, every in-page link has a target,
and the version it mentions is the program's.  Run:  python -m unittest discover -s tests"""

import os
import re
import sys
import tempfile
import unittest

os.environ["RC_DATA_DIR"] = tempfile.mkdtemp(prefix="rc_manual_")  # before rc_manager is imported
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import rc_manager as rc  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
PAGE = os.path.join(DOCS, "index.html")


def page_text():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(os.path.isfile(PAGE), "no docs folder in this copy")
class ManualPageTests(unittest.TestCase):
    def test_every_asset_it_uses_exists(self):
        text = page_text()
        names = set(re.findall(r'(?:src|href|poster)="(assets/[^"#?+]+)"', text))
        # the gallery is built by script from a list of picture names
        names |= {f"assets/{n}.png" for n in re.findall(r"\['(m\d\d_[a-z0-9_]+)'", text)}
        self.assertGreater(len(names), 20)
        missing = sorted(n for n in names if not os.path.isfile(os.path.join(DOCS, *n.split("/"))))
        self.assertEqual(missing, [], "pictures/video named in the manual but missing from docs/assets")

    def test_in_page_links_have_targets(self):
        text = page_text()
        ids = set(re.findall(r'\bid="([^"]+)"', text))
        links = set(re.findall(r'href="#([^"]+)"', text))
        self.assertEqual(sorted(l for l in links if l not in ids), [])

    def test_ids_are_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', page_text())
        self.assertEqual(sorted({i for i in ids if ids.count(i) > 1}), [])

    def test_names_the_current_version(self):
        text = page_text()
        self.assertIn(rc.APP_VERSION, text)
        stale = set(re.findall(r"\b2\.\d+\.\d+\b", text)) - {rc.APP_VERSION}
        self.assertEqual(sorted(stale), [], "the manual mentions another version number")

    def test_covers_every_top_level_menu(self):
        text = page_text()
        for menu in ("File", "Connect", "Servers", "Session", "View", "Tools", "Help"):
            self.assertRegex(text, rf"<h3>{menu}</h3>", f"no section for the {menu} menu")

    def test_no_private_details(self):
        text = page_text() + "".join(open(os.path.join(DOCS, f), encoding="utf-8", errors="ignore").read()
                                     for f in os.listdir(DOCS) if f.endswith((".html", ".css", ".js", ".svg")))
        self.assertNotRegex(text, r"192\.168\.8\.\d+")
        self.assertNotRegex(text, r"ronaldgoodchild(?!(/|\.github\.io/)(regteches-rc|scoop-regteches)\b)")


class HelpMenuTests(unittest.TestCase):
    def test_app_has_a_manual_opener(self):
        self.assertTrue(callable(getattr(rc.App, "open_manual", None)))
        self.assertTrue(rc.App.MANUAL_URL.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
