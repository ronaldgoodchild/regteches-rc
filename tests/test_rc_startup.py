"""RC must open its main window on this Python/Tk (a start-up crash is invisible to the other tests)."""

import os
import sys
import tempfile
import tkinter as tk
import unittest

os.environ["RC_DATA_DIR"] = tempfile.mkdtemp(prefix="rc_start_")  # before rc_manager is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_manager as rc  # noqa: E402


class StartupTests(unittest.TestCase):
    def test_main_window_opens(self):
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no display")
        try:
            root.withdraw()
            app = rc.App(root)
            root.update()
            self.assertTrue(app.nb.winfo_exists())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
