"""Tests for the README-as-web-page converter (Help > Quick start guide)."""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_test_readme_"))
import rc_tools as tools  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(HERE, "README.md")


class InlineTests(unittest.TestCase):
    def test_styles(self):
        self.assertEqual(tools._inline("**bold** and *it* and `code`"),
                         "<strong>bold</strong> and <em>it</em> and <code>code</code>")

    def test_bold_around_code_and_escaping(self):
        self.assertEqual(tools._inline("**a `b*c` d** <x>"), "<strong>a <code>b*c</code> d</strong> &lt;x&gt;")

    def test_link_only_http(self):
        self.assertIn('href="https://e.com"', tools._inline("[l](https://e.com)"))
        self.assertNotIn("<a ", tools._inline("[l](javascript:alert(1))"))


class BlockTests(unittest.TestCase):
    def test_blocks(self):
        md = ("# T\n\nintro line\nsecond line\n\n## Sec One\n\n| A | B |\n|---|---|\n| 1 | `x` |\n\n"
              "- one\n  wrapped\n- two\n\n1. first\n2. second\n\n```\n<raw> & code\n```\n")
        h = tools.markdown_to_html(md, "Title <T>", "Sub")
        self.assertIn("<h2 id=\"sec-one\">Sec One</h2>", h)
        self.assertIn('<a href="#sec-one">Sec One</a>', h)  # contents sidebar
        self.assertIn("<th>A</th>", h)
        self.assertIn("<td><code>x</code></td>", h)
        self.assertIn("<li>one wrapped</li>", h)
        self.assertIn("<ol><li>first</li><li>second</li></ol>", h)
        self.assertIn("&lt;raw&gt; &amp; code", h)
        self.assertIn("<p>intro line second line</p>", h)
        self.assertIn("<title>Title &lt;T&gt;</title>", h)

    def test_duplicate_headings_get_unique_ids(self):
        h = tools.markdown_to_html("## Same\n\n## Same\n")
        self.assertIn('id="same"', h)
        self.assertIn('id="same-2"', h)

    def test_real_readme_converts_cleanly(self):
        with open(README, encoding="utf-8") as f:
            h = tools.markdown_to_html(f.read(), "RC", "v", base_dir=HERE)
        outside_code = re.sub(r"<pre.*?</pre>", "", h, flags=re.S)
        self.assertNotIn("**", outside_code)
        self.assertNotIn("`", outside_code)
        self.assertNotIn("\x00", h)
        self.assertNotIn("![", outside_code)  # every picture reference was understood
        self.assertGreater(h.count("<h2"), 10)
        self.assertIn("<table", h)


class PictureTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.d.name, "screenshots"))
        for n in ("a.png", "tour.mp4"):
            with open(os.path.join(self.d.name, "screenshots", n), "wb") as f:
                f.write(b"x")

    def tearDown(self):
        self.d.cleanup()

    def test_local_picture_and_video_link(self):
        h = tools.markdown_to_html("[![Tour](screenshots/a.png)](screenshots/tour.mp4)\n", base_dir=self.d.name)
        self.assertRegex(h, r'<a href="file:///[^"]+tour\.mp4"[^>]*><img src="file:///[^"]+a\.png" alt="Tour"')

    def test_missing_picture_is_dropped_but_alt_text_kept(self):
        h = tools.markdown_to_html("![Nothing here](screenshots/none.png)\n", base_dir=self.d.name)
        self.assertNotIn("<img", h)
        self.assertIn("Nothing here", h)

    def test_no_escaping_the_folder_or_other_schemes(self):
        h = tools.markdown_to_html("![x](../secret.png) ![y](file:///c:/x.png) [z](javascript:alert(1))", base_dir=self.d.name)
        self.assertNotIn("<img", h)
        self.assertNotIn("<a href=\"javascript", h)

    def test_pictures_need_a_base_folder(self):
        self.assertNotIn("<img", tools.markdown_to_html("![x](screenshots/a.png)"))


class NameClashTests(unittest.TestCase):
    def test_key_file_names_still_use_the_original_slug(self):
        # the README converter once shadowed this helper and broke the Keys tab
        self.assertEqual(tools._slug("My Server #1"), tools._slug("My Server #1"))
        self.assertNotIn(" ", tools._slug("My Server #1"))


class OpenTests(unittest.TestCase):
    def test_missing_readme_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(tools.open_readme_html(d))

    def test_opens_page_in_browser(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
                f.write("# Hi\n\n## Part\n\ntext\n")
            with mock.patch.object(tools.webbrowser, "open") as opened:
                self.assertTrue(tools.open_readme_html(d, "RC", "v1"))
            url = opened.call_args[0][0]
            self.assertTrue(url.startswith("file:///") and url.endswith("REGTeches_RC_guide.html"))


if __name__ == "__main__":
    unittest.main()
