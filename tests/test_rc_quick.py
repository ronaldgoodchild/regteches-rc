"""Tests for rc_quick.py: command history (and what it refuses to record), the post-login script, the multi-line paste
check and the Quick commands panel. No server is contacted.  Run:  python -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

os.environ.setdefault("RC_DATA_DIR", tempfile.mkdtemp(prefix="rc_quick_test_"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as core  # noqa: E402
import rc_quick as quick  # noqa: E402
import rc_terminal as term  # noqa: E402


class DataDir(unittest.TestCase):
    """Every test gets its own data folder (history.json, snippets.json)."""

    def setUp(self):
        self._old = (core.APP_DIR, core.SNIPPETS_FILE)
        d = tempfile.mkdtemp(prefix="rc_quickdata_")
        core.APP_DIR, core.SNIPPETS_FILE = d, os.path.join(d, "snippets.json")
        self.dir = d

    def tearDown(self):
        core.APP_DIR, core.SNIPPETS_FILE = self._old


class ParseTests(unittest.TestCase):
    def test_steps_comments_and_empty_wait(self):
        steps = quick.parse_login_script("""
            // become root
            $ => sudo -i
            assword => ${password}

            => echo hello => world
        """)
        self.assertEqual([(s["wait"], s["send"]) for s in steps],
                         [("\\$", "sudo -i"), ("assword", "${password}"), ("", "echo hello => world")])

    def test_a_lone_dollar_means_the_dollar_prompt(self):
        steps = quick.parse_login_script("$ => sudo -i\nbox:~$ => ls")
        self.assertEqual([s["wait"] for s in steps], ["\\$", "box:~$"])
        sent = []
        clock = FakeClock()
        r = quick.ExpectRunner(steps[:1], sent.append, clock.after, clock.cancel, lambda m: None)
        r.start()
        r.feed("Last login: today\r\n")                           # no prompt yet: a bare $ pattern would have fired here
        self.assertEqual(sent, [])
        r.feed("ron@box:~$ ")
        self.assertEqual(sent, ["sudo -i\r"])

    def test_errors_name_the_line(self):
        with self.assertRaisesRegex(ValueError, "Line 2: .*wait-for-text => text"):
            quick.parse_login_script("a => b\nnot a step")
        with self.assertRaisesRegex(ValueError, "Line 1: .*not a valid pattern"):
            quick.parse_login_script("([ => x")
        with self.assertRaisesRegex(ValueError, "At most"):
            quick.parse_login_script("\n".join("x => y" for _ in range(quick.MAX_STEPS + 1)))
        self.assertEqual(quick.parse_login_script(""), [])
        self.assertEqual(quick.parse_login_script(None), [])

    def test_substitute(self):
        self.assertEqual(quick.substitute("${user}@${host}:${password}", "ron", "10.0.0.1", "pw"), "ron@10.0.0.1:pw")
        self.assertEqual(quick.substitute("${password}", "ron", "h", None), "")


class FakeClock:
    def __init__(self):
        self.jobs, self.n = {}, 0

    def after(self, ms, fn):
        self.n += 1
        self.jobs[self.n] = (ms, fn)
        return self.n

    def cancel(self, j):
        self.jobs.pop(j, None)

    def run(self, max_ms):
        """Run every pending job due within max_ms, soonest first."""
        for j, (ms, fn) in sorted(self.jobs.items(), key=lambda kv: kv[1][0]):
            if ms <= max_ms and j in self.jobs:
                del self.jobs[j]
                fn()


class RunnerTests(unittest.TestCase):
    def make(self, text, timeout=10):
        self.sent, self.notes, self.clock = [], [], FakeClock()
        r = quick.ExpectRunner(quick.parse_login_script(text), self.sent.append, self.clock.after, self.clock.cancel,
                               self.notes.append, subst=lambda t: quick.substitute(t, "ron", "box", "s3cret"), timeout=timeout)
        r.start()
        return r

    def test_waits_then_sends_in_order(self):
        r = self.make("\\$ => sudo -i\nassword => ${password}\n# => cd /var/log")
        r.feed("Welcome\r\nron@box:~$ ")
        self.assertEqual(self.sent, ["sudo -i\r"])
        r.feed("[sudo] password for ron: ")
        self.assertEqual(self.sent, ["sudo -i\r", "s3cret\r"])
        r.feed("root@box:~# ")
        self.assertEqual(self.sent[-1], "cd /var/log\r")
        self.assertEqual(self.notes, ["login script finished"])
        r.feed("more output $ # password")
        self.assertEqual(len(self.sent), 3)                       # finished: nothing more is ever sent

    def test_ansi_colours_do_not_hide_the_prompt(self):
        r = self.make("box:~\\$ => ls")
        r.feed("\x1b[32mbox\x1b[0m:\x1b[34m~\x1b[0m$ ")
        self.assertEqual(self.sent, ["ls\r"])

    def test_match_is_case_insensitive_and_buffered_across_chunks(self):
        r = self.make("password => x")
        r.feed("Pass")
        self.assertEqual(self.sent, [])
        r.feed("WORD: ")
        self.assertEqual(self.sent, ["x\r"])

    def test_an_old_prompt_is_not_matched_again(self):
        r = self.make("\\$ => one\n\\$ => two")
        r.feed("ron@box:~$ ")
        self.assertEqual(self.sent, ["one\r"])
        r.feed("")                                                 # the old '$' was cleared with the first match
        self.assertEqual(self.sent, ["one\r"])
        r.feed("again$ ")
        self.assertEqual(self.sent, ["one\r", "two\r"])

    def test_empty_wait_sends_after_a_short_pause(self):
        self.make("=> echo hi")
        self.assertEqual(self.sent, [])
        self.clock.run(500)
        self.assertEqual(self.sent, ["echo hi\r"])

    def test_timeout_stops_and_says_why(self):
        r = self.make("never => x\nother => y", timeout=10)
        r.feed("nothing useful")
        self.clock.run(10000)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.notes), 1)
        self.assertIn("“never” did not appear within 10 s", self.notes[0])
        r.feed("never")
        self.assertEqual(self.sent, [])                            # stopped for good

    def test_stop_cancels_everything(self):
        r = self.make("a => b")
        r.stop()
        r.feed("a")
        self.clock.run(99999)
        self.assertEqual((self.sent, self.notes), ([], []))


class RecorderTests(unittest.TestCase):
    def make(self, prompt="ron@box:~$ ", alt=False):
        self.saved = []
        self.prompt, self.alt = prompt, alt
        return quick.HistoryRecorder(lambda: self.prompt, lambda: self.alt, save=self.saved.append)

    def type(self, rec, text):
        for ch in text:
            rec.typed(ch)

    def test_plain_typing_and_backspace(self):
        r = self.make()
        self.type(r, "ls -lx")
        r.typed("\x7f")
        self.type(r, "a\r")
        self.assertEqual(self.saved, ["ls -la"])

    def test_arrow_tab_and_ctrl_keys_make_a_line_untrustworthy(self):
        for key in ("\x1b[A", "\t", "\x03", "\x12", "\x1bf"):
            r = self.make()
            self.type(r, "ec")
            r.typed(key)
            r.typed("\r")
            self.assertEqual(self.saved, [], repr(key))
        r = self.make()
        self.type(r, "ok\r")                                       # the next line starts clean again
        self.assertEqual(self.saved, ["ok"])

    def test_not_recorded(self):
        for prompt in ("[sudo] password for ron: ", "Enter passphrase for key '/home/ron/.ssh/id': ", "Verification code: ",
                       "Password:", "OTP ", "Enter PIN: "):
            r = self.make(prompt=prompt)
            self.type(r, "hunter2\r")
            self.assertEqual(self.saved, [], prompt)
        r = self.make(alt=True)
        self.type(r, ":wq\r")
        self.assertEqual(self.saved, [])
        r = self.make()
        self.type(r, " secret command\r")                          # leading space = "do not remember"
        self.type(r, "\r")
        self.type(r, "   \r")
        self.type(r, "x" * (quick.MAX_LINE + 1) + "\r")
        self.assertEqual(self.saved, [])

    def test_a_normal_prompt_with_a_hash_or_dollar_is_fine(self):
        r = self.make(prompt="root@box:/var/log# ")
        self.type(r, "tail syslog\r")
        self.assertEqual(self.saved, ["tail syslog"])

    def test_paste_of_several_lines(self):
        r = self.make()
        r.typed("uptime\rdf -h\rwho")
        self.assertEqual(self.saved, ["uptime", "df -h"])
        r.typed("\r")
        self.assertEqual(self.saved[-1], "who")

    def test_windows_newlines_in_a_paste(self):
        r = self.make()
        r.typed("a\r\nb\r\n")
        self.assertEqual(self.saved, ["a", "b"])


class StoreTests(DataDir):
    def test_add_merges_duplicates_and_orders_by_recency(self):
        quick.add_history("one", now=1)
        quick.add_history("two", now=2)
        quick.add_history("one", now=3)
        items = quick.load_store()["items"]
        self.assertEqual([(i["cmd"], i["count"]) for i in items], [("one", 2), ("two", 1)])

    def test_cap(self):
        for i in range(quick.MAX_HISTORY + 20):
            quick.add_history(f"cmd{i}", now=i)
        items = quick.load_store()["items"]
        self.assertEqual(len(items), quick.MAX_HISTORY)
        self.assertEqual(items[0]["cmd"], f"cmd{quick.MAX_HISTORY + 19}")

    def test_recording_can_be_switched_off(self):
        store = quick.load_store()
        store["record"] = False
        quick.save_store(store)
        self.assertFalse(quick.add_history("secretish"))
        self.assertEqual(quick.load_store()["items"], [])

    def test_damaged_or_odd_files_do_not_crash(self):
        with open(quick.history_path(), "w") as f:
            f.write("{nope")
        self.assertEqual(quick.load_store(), {"record": True, "confirm_paste": True, "items": []})
        with open(quick.history_path(), "w") as f:
            f.write('{"record": false, "items": [{"cmd": "ok", "count": "x", "last": null}, {"cmd": 5}, "junk", {"cmd": "  "}]}')
        s = quick.load_store()
        self.assertEqual((s["record"], [(i["cmd"], i["count"]) for i in s["items"]]), (False, [("ok", 1)]))


class FakeApp:
    all_terminals = core.App.all_terminals
    cur_tab = core.App.cur_tab
    broadcast_peers = core.App.broadcast_peers
    broadcast_changed = core.App.broadcast_changed
    refresh_tab_title = core.App.refresh_tab_title
    tab_title_base = core.App.tab_title_base

    def __init__(self, root):
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.status = tk.StringVar()
        self.snippets_opened = 0

    def post(self, fn):
        fn()

    def open_snippets(self):
        self.snippets_opened += 1

    def confirm_broadcast(self):
        return True


class TabTests(DataDir):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        cls.root.geometry("1100x650")
        term.TerminalTab.AUTO_CONNECT = False

    @classmethod
    def tearDownClass(cls):
        term.TerminalTab.AUTO_CONNECT = True
        cls.root.destroy()

    def setUp(self):
        super().setUp()
        self.app = FakeApp(self.root)
        self.asked = []
        self.answer = True
        p = mock.patch.object(term.messagebox, "askyesno", side_effect=lambda *a, **k: (self.asked.append(a[1]), self.answer)[1])
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(quick.messagebox, "askyesno", side_effect=lambda *a, **k: (self.asked.append(a[1]), self.answer)[1])
        p2.start()
        self.addCleanup(p2.stop)

    def tearDown(self):
        for t in self.app.all_terminals():
            t.close()
        self.app.nb.destroy()
        super().tearDown()

    def tab(self, **host):
        h = dict({"address": "10.0.0.5", "name": "box"}, **host)
        t = term.TerminalTab(self.app, h, "ron", "s3cret")
        self.app.nb.add(t, text="t")
        self.root.update()
        t.apply_size()
        self.sent = []
        t.send = self.sent.append
        return t

    def screen_text(self, t):
        t.render()
        return t.t.get("1.0", "end")

    # history through the real key path
    def test_typed_commands_land_in_history(self):
        t = self.tab()
        t.feed("ron@box:~$ ")
        for ch in "uptime":
            t.send_user(ch)
        t.send_user("\r")
        self.assertEqual([i["cmd"] for i in quick.load_store()["items"]], ["uptime"])

    def test_password_typed_at_a_prompt_is_not_recorded(self):
        t = self.tab()
        t.feed("[sudo] password for ron: ")
        for ch in "hunter2":
            t.send_user(ch)
        t.send_user("\r")
        self.assertEqual(quick.load_store()["items"], [])
        self.assertEqual("".join(self.sent), "hunter2\r")                    # but it is still sent, of course

    def test_terminal_replies_and_broadcast_copies_are_not_history(self):
        a, b = self.tab(), self.tab()
        got = []
        b.send = got.append
        a.bcast_var.set(True)
        b.bcast_var.set(True)
        a.feed("$ ")
        for ch in "ls":
            a.send_user(ch)
        a.send_user("\r")
        self.assertEqual("".join(got), "ls\r")
        self.assertEqual([i["cmd"] for i in quick.load_store()["items"]], ["ls"])      # recorded once, at the source
        a.send("\x1b[0n")
        self.assertEqual(len(quick.load_store()["items"]), 1)

    def test_bracketed_paste_is_recorded_as_typed_text(self):
        t = self.tab()
        t.feed("\x1b[?2004h$ ")
        self.root.clipboard_clear()
        self.root.clipboard_append("df -h")
        t.paste()
        t.send_user("\r")
        self.assertEqual([i["cmd"] for i in quick.load_store()["items"]], ["df -h"])

    # multi-line paste
    def test_multiline_paste_asks_first(self):
        t = self.tab()
        self.root.clipboard_clear()
        self.root.clipboard_append("one\ntwo\nthree\n")
        self.answer = False
        t.paste()
        self.assertEqual(self.sent, [])
        self.assertIn("3 lines", self.asked[-1])
        self.answer = True
        t.paste()
        self.assertEqual(self.sent, ["one\rtwo\rthree\r"])

    def test_one_line_or_switched_off_paste_does_not_ask(self):
        t = self.tab()
        self.root.clipboard_clear()
        self.root.clipboard_append("single line\n")
        t.paste()
        self.assertEqual((self.asked, self.sent), ([], ["single line\r"]))
        store = quick.load_store()
        store["confirm_paste"] = False
        quick.save_store(store)
        self.root.clipboard_clear()
        self.root.clipboard_append("a\nb")
        t.paste()
        self.assertEqual(self.asked, [])
        self.assertEqual(self.sent[-1], "a\rb")

    def test_paste_question_mentions_broadcast_targets(self):
        a, b = self.tab(), self.tab()
        a.bcast_var.set(True)
        b.bcast_var.set(True)
        self.root.clipboard_clear()
        self.root.clipboard_append("x\ny")
        self.answer = False
        a.paste()
        self.assertIn("1 other terminal", self.asked[-1])

    # login script
    def test_login_script_runs_after_connect_and_hides_the_password_from_the_log(self):
        t = self.tab(login_script="\\$ => sudo -i\nassword => ${password}\n# => cd /var/log")
        t.connected(t.generation)
        self.assertIsNotNone(t.expect)
        t.remote_data("Linux box 6.1\r\nron@box:~$ ")
        self.assertEqual(self.sent, ["sudo -i\r"])
        with self.assertLogs("rc", level="INFO") as cm:
            t.remote_data("[sudo] password for ron: ")
            t.remote_data("root@box:~# ")
        self.assertEqual(self.sent, ["sudo -i\r", "s3cret\r", "cd /var/log\r"])
        self.assertNotIn("s3cret", "\n".join(cm.output))
        self.assertIn("login script finished", self.screen_text(t))

    def test_login_script_starts_again_after_a_reconnect_and_stops_on_close(self):
        t = self.tab(login_script="\\$ => whoami")
        t.connected(t.generation)
        first = t.expect
        t.remote_data("$ ")
        t.connected(t.generation)
        self.assertIsNot(t.expect, first)
        t.remote_data("$ ")
        self.assertEqual(self.sent, ["whoami\r", "whoami\r"])
        t.connected(t.generation)
        runner = t.expect
        t.close()
        self.assertTrue(runner.done)

    def test_bad_script_is_reported_not_run(self):
        t = self.tab(login_script="([ => x")
        t.connected(t.generation)
        self.assertIsNone(t.expect)
        self.assertIn("login script not run", self.screen_text(t))

    def test_no_script_means_no_runner(self):
        t = self.tab()
        t.connected(t.generation)
        self.assertIsNone(t.expect)

    # panel
    def test_panel_shows_groups_search_and_uses_each_mode(self):
        core.save_snippets([{"id": "a", "name": "Disk", "command": "df -h", "confirm": False},
                            {"id": "b", "name": "Restart nginx", "command": "systemctl restart nginx", "confirm": True, "group": "Web"},
                            {"id": "c", "name": "Two lines", "command": "uptime\nwho", "confirm": False, "group": "Web"}])
        t = self.tab()
        t.toggle_panel()
        self.root.update()
        self.assertTrue(t.panel.winfo_ismapped())
        tree = t.panel.q_tree
        groups = [tree.item(g, "text") for g in tree.get_children()]
        self.assertEqual(groups, ["General  (1)", "Web  (2)"])
        t.panel.q_search.set("nginx")
        self.assertEqual([tree.item(c, "text") for g in tree.get_children() for c in tree.get_children(g)], ["Restart nginx"])
        t.panel.q_search.set("")
        # paste: typed without Enter
        tree.selection_set("s0")
        t.panel.q_do("paste")
        self.assertEqual(self.sent, ["df -h"])
        # run: with Enter; asks first when the command says so
        t.panel.q_do("run")
        self.assertEqual(self.sent[-1], "df -h\r")
        tree.selection_set("s1")
        self.answer = False
        t.panel.q_do("run")
        self.assertEqual(self.sent[-1], "df -h\r")                  # declined: nothing new
        self.answer = True
        t.panel.q_do("run")
        self.assertEqual(self.sent[-1], "systemctl restart nginx\r")
        # copy
        tree.selection_set("s0")
        t.panel.q_do("copy")
        self.assertEqual(t.clipboard_get(), "df -h")
        # several lines pasted through the quick panel also get the multi-line check
        tree.selection_set("s2")
        self.sent.clear()
        t.panel.q_do("paste")
        self.assertEqual(self.sent, ["uptime\rwho"])
        self.assertIn("2 lines", self.asked[-1])
        # hide again
        t.toggle_panel()
        self.root.update()
        self.assertFalse(t.panel.winfo_ismapped())

    def test_history_tab(self):
        quick.add_history("ls -la", now=1)
        quick.add_history("df -h", now=2)
        t = self.tab()
        t.toggle_panel()
        t.panel.book.select(1)
        self.root.update()
        h = t.panel.h_tree
        self.assertEqual([h.item(i, "text") for i in h.get_children()], ["df -h", "ls -la"])
        t.panel.h_search.set("ls")
        self.assertEqual([h.item(i, "text") for i in h.get_children()], ["ls -la"])
        t.panel.h_search.set("")
        h.selection_set("h0")
        t.panel.h_do("paste")
        self.assertEqual(self.sent[-1], "df -h")
        t.history.buf = []                                           # (the pasted text was not run: forget it)
        t.panel.h_do("run")
        self.assertEqual(self.sent[-1], "df -h\r")
        # delete one
        h.selection_set("h1")
        t.panel.h_delete()
        self.assertEqual([i["cmd"] for i in quick.load_store()["items"]], ["df -h"])
        # clear (asks)
        self.answer = False
        t.panel.h_clear()
        self.assertEqual(len(quick.load_store()["items"]), 1)
        self.answer = True
        t.panel.h_clear()
        self.assertEqual(quick.load_store()["items"], [])

    def test_history_checkbox_switches_recording(self):
        t = self.tab()
        t.toggle_panel()
        t.panel.record.set(False)
        t.panel.set_record()
        t.feed("$ ")
        t.send_user("x")
        t.send_user("\r")
        self.assertEqual(quick.load_store()["items"], [])
        self.assertFalse(quick.load_store()["record"])

    def test_save_history_entry_as_quick_command(self):
        quick.add_history("journalctl -xe", now=1)
        t = self.tab()
        t.toggle_panel()
        t.panel.book.select(1)
        self.root.update()
        t.panel.h_tree.selection_set("h0")
        with mock.patch.object(quick.simpledialog, "askstring", return_value="Show journal"):
            t.panel.h_save()
        snip = [s for s in core.load_snippets() if s["name"] == "Show journal"][0]
        self.assertEqual((snip["command"], snip["group"]), ("journalctl -xe", "From history"))
        self.assertEqual(t.panel.book.index(t.panel.book.select()), 0)

    def test_panel_refreshes_history_after_enter(self):
        t = self.tab()
        t.toggle_panel()
        t.panel.book.select(1)
        self.root.update()
        t.feed("$ ")
        for ch in "whoami":
            t.send_user(ch)
        t.send_user("\r")
        end = __import__("time").time() + 1.0
        while __import__("time").time() < end:
            self.root.update()
        self.assertEqual([t.panel.h_tree.item(i, "text") for i in t.panel.h_tree.get_children()], ["whoami"])

    def test_snippets_menu_groups(self):
        core.save_snippets([{"id": "a", "name": "Disk", "command": "df -h"},
                            {"id": "b", "name": "Restart", "command": "x", "group": "Web"}])
        t = self.tab()
        t.fill_snippet_menu()
        menu = t.snip_btn.nametowidget(t.snip_btn.cget("menu"))
        kinds = [(menu.type(i), menu.entrycget(i, "label")) for i in range(menu.index("end") + 1)]
        self.assertEqual(kinds, [("command", "Disk"), ("cascade", "Web")])


class EditorTests(DataDir):
    def test_editor_validates_the_login_script(self):
        try:
            root = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("no display available")
        try:
            class App(FakeApp):
                hosts = []
                editors = {}
                editor_done = lambda *a: None
                open_identities = open_proxies = lambda self: None
            app = App(root)
            host = dict(core.NEW_HOST, id="h", name="x", address="10.0.0.1", login_script="\\$ => sudo -i")
            ed = core.HostEditor(app, host, [], "h")
            self.assertEqual(ed.script.get("1.0", "end").strip(), "\\$ => sudo -i")
            h, errors, _ = ed.collect()
            self.assertEqual(h["login_script"], "\\$ => sudo -i")
            self.assertFalse([e for e in errors if "After login" in e])
            ed.script.delete("1.0", "end")
            ed.script.insert("1.0", "([ => x")
            h, errors, _ = ed.collect()
            self.assertTrue([e for e in errors if e.startswith("After login: Line 1")])
        finally:
            root.destroy()

    def test_login_script_survives_sanitising(self):
        self.assertEqual(core.sanitize_host({"address": "x.lan", "login_script": "a => b"})["login_script"], "a => b")
        self.assertEqual(core.sanitize_host({"address": "x.lan"})["login_script"], "")
        self.assertEqual(core.sanitize_host({"address": "x.lan", "login_script": None})["login_script"], "")


if __name__ == "__main__":
    unittest.main()
