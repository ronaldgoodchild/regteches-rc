"""Tests for rc_notes.py: storage, tags, searching, Markdown reading and the Notes tab (autosave, filters, server notes).
Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_notes_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_notes as nt  # noqa: E402


class DataDir(unittest.TestCase):
    def setUp(self):
        self._old = core.APP_DIR
        core.APP_DIR = tempfile.mkdtemp(prefix="rc_notesdata_")

    def tearDown(self):
        core.APP_DIR = self._old


def note(**kw):
    return nt.clean_note(dict({"title": "T", "body": "B", "tags": [], "server": "", "updated": 1}, **kw))


class StorageTests(DataDir):
    def test_round_trip_newest_first(self):
        nt.save_notes([note(id="a", updated=1), note(id="b", updated=3), note(id="c", updated=2)])
        self.assertEqual([n["id"] for n in nt.load_notes()], ["b", "c", "a"])

    def test_cleaning(self):
        n = nt.clean_note({"title": "  x  ", "body": 5, "tags": ["B", " b ", "c d", ""], "server": None, "updated": "junk"})
        self.assertEqual((n["title"], n["body"], n["tags"], n["server"], n["updated"]), ("x", "", ["b", "c d"], "", 0.0))
        self.assertTrue(n["id"])
        self.assertIsNone(nt.clean_note("junk"))
        self.assertEqual(nt.clean_note({"tags": "Net, rack 3 ; net"})["tags"], ["net", "rack 3"])

    def test_missing_odd_and_damaged_files(self):
        self.assertEqual(nt.load_notes(), [])
        with open(nt.notes_path(), "w") as f:
            f.write('[{"id": "a", "title": "ok"}, "junk", {"id": "a", "title": "dup"}, 5]')
        self.assertEqual([n["title"] for n in nt.load_notes()], ["ok"])
        with open(nt.notes_path(), "w") as f:
            f.write("{nope")
        self.assertEqual(nt.load_notes(), [])
        names = os.listdir(core.APP_DIR)
        self.assertTrue(any(n.startswith("notes.json.corrupt-") for n in names))
        self.assertNotIn("notes.json", names)

    def test_tags(self):
        self.assertEqual(nt.parse_tags("Network, rack 3 ,  network;;  "), ["network", "rack 3"])
        self.assertEqual(nt.parse_tags(None), [])
        self.assertEqual(len(nt.parse_tags(",".join(str(i) for i in range(50)))), 20)
        self.assertEqual(nt.all_tags([note(tags=["b", "a"]), note(tags=["a", "c"])]), ["a", "b", "c"])

    def test_titles(self):
        self.assertEqual(nt.display_title(note(title="Given")), "Given")
        self.assertEqual(nt.display_title(note(title="", body="\n\n## Heading line\nmore")), "Heading line")
        self.assertEqual(nt.display_title(note(title="", body="")), "(empty note)")
        self.assertEqual(len(nt.display_title(note(title="", body="x" * 200))), 60)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.notes = [note(id="1", title="Switch ports", body="Port 12 goes to the NAS", tags=["network", "rack 3"], server="h1"),
                      note(id="2", title="Plex fix", body="Restart the container", tags=["media"], server="h2"),
                      note(id="3", title="Loose note", body="nothing special", tags=[], server="")]

    def ids(self, **kw):
        return [n["id"] for n in nt.filter_notes(self.notes, **kw)]

    def test_search_words_all_must_match_anywhere(self):
        self.assertEqual(self.ids(text="nas port"), ["1"])
        self.assertEqual(self.ids(text="PLEX"), ["2"])
        self.assertEqual(self.ids(text="rack"), ["1"])                      # tags are searched too
        self.assertEqual(self.ids(text="plex nas"), [])
        self.assertEqual(self.ids(text="  "), ["1", "2", "3"])

    def test_tag_and_server_filters(self):
        self.assertEqual(self.ids(tag="media"), ["2"])
        self.assertEqual(self.ids(server="h1"), ["1"])
        self.assertEqual(self.ids(server=nt.NO_SERVER), ["3"])
        self.assertEqual(self.ids(server=nt.ALL), ["1", "2", "3"])
        self.assertEqual(self.ids(text="note", tag="", server=nt.NO_SERVER), ["3"])
        self.assertEqual(self.ids(tag="network", server="h2"), [])


class MarkdownTests(unittest.TestCase):
    def test_blocks(self):
        body = "# Title\n## Sub\n### Small\nplain **bold** text\n- one\n* two\n\n```\ncode line\n# not a heading\n```\nlast"
        self.assertEqual(nt.md_blocks(body), [
            ("h1", "Title"), ("h2", "Sub"), ("h3", "Small"), ("p", "plain **bold** text"), ("li", "one"), ("li", "two"), ("blank", ""),
            ("code", "code line"), ("code", "# not a heading"), ("p", "last")])
        self.assertEqual(nt.md_blocks(""), [])
        self.assertEqual(nt.md_blocks("#nospace"), [("p", "#nospace")])
        self.assertEqual(nt.md_blocks("a\r\nb"), [("p", "a"), ("p", "b")])

    def test_inline(self):
        self.assertEqual(nt.inline_spans("a **b** `c` d"), [("a ", ""), ("b", "bold"), (" ", ""), ("c", "code"), (" d", "")])
        self.assertEqual(nt.inline_spans("no marks"), [("no marks", "")])
        self.assertEqual(nt.inline_spans("**unclosed"), [("**unclosed", "")])
        self.assertEqual(nt.inline_spans("see *UPS* note"), [("see ", ""), ("UPS", "italic"), (" note", "")])
        self.assertEqual(nt.inline_spans("2 * 3 * 4"), [("2 * 3 * 4", "")])                   # a multiplication sign is not emphasis
        self.assertEqual(nt.inline_spans(""), [])


class FakeApp:
    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.hosts = [{"id": "h1", "name": "nas", "address": "10.0.0.2"}, {"id": "h2", "name": "plex", "address": "10.0.0.3"}]


class TabTests(DataDir):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1000x600")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        super().setUp()
        self.app = FakeApp(self.root)
        p = mock.patch.object(nt.messagebox, "askyesno", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.app.nb.destroy()
        super().tearDown()

    def tab(self):
        t = nt.NotesTab(self.app)
        self.app.nb.add(t, text="n")
        self.root.update()
        return t

    def type_into(self, t, title="", body="", tags=""):
        t.title_var.set(title)
        t.tags_var.set(tags)
        t.body.delete("1.0", "end")
        t.body.insert("1.0", body)

    def test_new_note_typed_and_saved(self):
        t = self.tab()
        t.new()
        self.type_into(t, "Rack layout", "U1 router\nU2 switch", "network, Rack 3")
        self.assertTrue(t.flush())
        saved = nt.load_notes()
        self.assertEqual((saved[0]["title"], saved[0]["body"], saved[0]["tags"]), ("Rack layout", "U1 router\nU2 switch", ["network", "rack 3"]))
        self.assertEqual(t.listbox.get(0), "Rack layout   [network, rack 3]")
        self.assertIn("Saved", t.status.get())
        self.assertEqual(t.tag_box.cget("values"), ("(all)", "network", "rack 3"))

    def test_autosave_happens_by_itself(self):
        t = self.tab()
        t.AUTOSAVE_MS = 50
        t.new()
        t.title_var.set("typed")                                           # no explicit save
        end = time.time() + 3
        while time.time() < end and not nt.load_notes():
            self.root.update()
            time.sleep(0.02)
        self.assertEqual(nt.load_notes()[0]["title"], "typed")

    def test_body_edits_trigger_autosave(self):
        t = self.tab()
        t.AUTOSAVE_MS = 50
        t.new()
        t.body.insert("1.0", "some body text")
        end = time.time() + 3
        while time.time() < end and not nt.load_notes():
            self.root.update()
            time.sleep(0.02)
        self.assertEqual(nt.load_notes()[0]["body"], "some body text")

    def test_an_untouched_new_note_is_never_saved(self):
        t = self.tab()
        t.new()
        t.new()
        t.close()
        self.assertEqual(nt.load_notes(), [])

    def test_switching_notes_saves_the_one_you_leave(self):
        nt.save_notes([note(id="a", title="First", body="one", updated=2), note(id="b", title="Second", body="two", updated=1)])
        t = self.tab()
        t.listbox.selection_set(0)
        t.pick()
        t.body.insert("end", " edited")
        t.listbox.selection_clear(0, "end")
        t.listbox.selection_set(1)
        t.pick()
        self.assertEqual(t.title_var.get(), "Second")
        self.assertEqual({n["id"]: n["body"] for n in nt.load_notes()}["a"], "one edited")

    def test_search_tag_and_server_filters_in_the_list(self):
        nt.save_notes([note(id="1", title="Switch", tags=["network"], server="h1", updated=3), note(id="2", title="Plex", tags=["media"], server="h2", updated=2),
                       note(id="3", title="Loose", updated=1)])
        t = self.tab()
        self.assertEqual(t.listbox.size(), 3)
        t.search.set("plex")
        self.assertEqual([t.listbox.get(i) for i in range(t.listbox.size())], ["Plex   [media]"])
        t.search.set("")
        t.tag_var.set("network")
        t.refill()
        self.assertEqual(t.listbox.size(), 1)
        t.tag_var.set("(all)")
        t.server_var.set("plex")
        t.refill()
        self.assertEqual(t.listbox.get(0), "Plex   [media]")
        t.server_var.set(nt.NO_SERVER)
        t.refill()
        self.assertEqual(t.listbox.get(0), "Loose")

    def test_attaching_to_a_server(self):
        t = self.tab()
        t.new()
        self.type_into(t, "NAS notes", "body")
        t.attach_var.set("nas")
        t.flush()
        self.assertEqual(nt.load_notes()[0]["server"], "h1")
        t.attach_var.set(nt.NO_SERVER)
        t.flush()
        self.assertEqual(nt.load_notes()[0]["server"], "")

    def test_focus_server_shows_its_notes_or_starts_a_new_one(self):
        nt.save_notes([note(id="1", title="Plex fix", body="x", server="h2")])
        t = self.tab()
        t.focus_server("h2")
        self.assertEqual(t.title_var.get(), "Plex fix")
        t.focus_server("h1")                                               # nas has none: a new note is started for it
        self.assertEqual(t.attach_var.get(), "nas")
        self.assertEqual(t.title_var.get(), "")
        self.type_into(t, "NAS", "first note")
        t.flush()
        self.assertEqual({n["title"]: n["server"] for n in nt.load_notes()}, {"Plex fix": "h2", "NAS": "h1"})

    def test_delete_asks_and_removes(self):
        nt.save_notes([note(id="1", title="Gone", body="x")])
        t = self.tab()
        t.listbox.selection_set(0)
        t.pick()
        with mock.patch.object(nt.messagebox, "askyesno", return_value=False):
            t.delete()
        self.assertEqual(len(nt.load_notes()), 1)
        t.delete()
        self.assertEqual(nt.load_notes(), [])
        self.assertEqual(t.listbox.size(), 0)
        self.assertIsNone(t.cur)

    def test_preview_shows_formatted_text_and_back(self):
        t = self.tab()
        t.new()
        self.type_into(t, "Doc", "# Big\nsome **bold** and `code`\n- item")
        t.preview.set(True)
        t.show_mode()
        self.root.update()
        text = t.view.get("1.0", "end")
        self.assertIn("Big", text)
        self.assertIn("bold", text)
        self.assertIn("•  item", text)
        self.assertNotIn("**", text)
        self.assertTrue(t.view.winfo_ismapped())
        self.assertFalse(t.body.winfo_ismapped())
        bold = t.view.tag_ranges("bold")
        self.assertTrue(bold and t.view.get(bold[0], bold[1]) == "bold")
        t.preview.set(False)
        t.show_mode()
        self.root.update()
        self.assertTrue(t.body.winfo_ismapped())
        self.assertIn("**bold**", t.body.get("1.0", "end"))

    def test_closing_the_tab_saves_pending_edits(self):
        t = self.tab()
        t.new()
        self.type_into(t, "Late", "typed just before closing")
        t.close()
        self.assertEqual(nt.load_notes()[0]["title"], "Late")


class BackupTests(DataDir):
    def test_notes_travel_in_a_backup_and_follow_matched_servers(self):
        import rc_backup as bk
        old = {k: getattr(core, k) for k in ("HOSTS_FILE", "SNIPPETS_FILE", "WORKSPACES_FILE", "IDENTITIES_FILE", "PROXIES_FILE", "KNOWN_HOSTS")}
        try:
            for name, fn in (("HOSTS_FILE", "hosts.json"), ("SNIPPETS_FILE", "snippets.json"), ("WORKSPACES_FILE", "workspaces.json"),
                             ("IDENTITIES_FILE", "identities.json"), ("PROXIES_FILE", "proxies.json"), ("KNOWN_HOSTS", "known_hosts")):
                setattr(core, name, os.path.join(core.APP_DIR, fn))
            core._LIST_CACHE.clear()
            nt.save_notes([note(id="n1", title="About the NAS", body="x", server="their-id"), note(id="n2", title="Loose", body="y")])
            bundle, _ = bk.make_bundle([])
            self.assertEqual(len(bundle["notes"]), 2)
            blob = bk.encrypt(bundle, "pw")
            os.remove(nt.notes_path())
            mine = core.sanitize_host({"id": "local-id", "name": "nas", "address": "10.0.0.2"})
            theirs = core.sanitize_host({"id": "their-id", "name": "nas", "address": "10.0.0.2"})
            bundle = bk.decrypt(blob, "pw")
            bundle["hosts"] = [bk.portable_host(theirs)[0]]
            _, report = bk.apply_bundle(bundle, [mine], replace=False)
            self.assertEqual(report["notes"], (2, 0, 0))
            got = {n["id"]: n for n in nt.load_notes()}
            self.assertEqual(got["n1"]["server"], "local-id")                 # attached to the same server on this PC
            self.assertIn("Notes: 2 added", bk.describe(report))
            _, report = bk.apply_bundle(bundle, [mine], replace=False)
            self.assertEqual(report["notes"], (0, 0, 2))
        finally:
            for k, v in old.items():
                setattr(core, k, v)
            core._LIST_CACHE.clear()


if __name__ == "__main__":
    unittest.main()
