"""Tests for the network scanner's port handling and scan job (127.0.0.1 only).  Run:  python -m unittest discover -s tests"""

import os
import socket
import sys
import tempfile
import threading
import unittest

os.environ["RC_DATA_DIR"] = tempfile.mkdtemp(prefix="rc_scan_")  # before rc_manager is imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_netscan as scan  # noqa: E402


class ParsePortsTests(unittest.TestCase):
    def test_common_and_ranges(self):
        self.assertIn(22, scan.parse_ports("common"))
        self.assertEqual(scan.parse_ports("22,80,8000-8002"), [22, 80, 8000, 8001, 8002])

    def test_all_needs_a_single_machine(self):
        for word in ("all", "ALL", "full"):
            with self.assertRaises(ValueError) as cm:
                scan.parse_ports(word)
            self.assertIn("one machine", str(cm.exception))
        ports = scan.parse_ports("all", allow_all=True)
        self.assertEqual((len(ports), ports[0], ports[-1]), (65535, 1, 65535))

    def test_big_ranges_are_refused_but_point_to_all(self):
        with self.assertRaises(ValueError) as cm:
            scan.parse_ports("1-20000")
        self.assertIn("all", str(cm.exception))

    def test_bad_input(self):
        for bad in ("0", "70000", "22-21", "http"):
            with self.assertRaises(ValueError):
                scan.parse_ports(bad, allow_all=True)


class ScanJobTests(unittest.TestCase):
    def test_finds_an_open_port_and_calls_back(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        spare = socket.socket()                      # a port that was just free (the neighbour, port + 1, can belong to another test)
        spare.bind(("127.0.0.1", 0))
        closed = spare.getsockname()[1]
        spare.close()
        found, finished = {}, threading.Event()
        job = scan.ScanJob(["127.0.0.1"], [port, closed],
                           lambda ip, up, ports: found.update(ip=ip, up=up, ports=ports),
                           lambda d, t: None, lambda stopped: finished.set(), workers=4, timeout=1.0).start()
        self.assertTrue(finished.wait(20))
        srv.close()
        self.assertEqual((found["ip"], found["up"], found["ports"]), ("127.0.0.1", True, [port]))
        self.assertEqual(job.open["127.0.0.1"], [port])

    def test_stop_ends_early_and_keeps_what_was_found(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        finished, flags = threading.Event(), []
        job = scan.ScanJob(["127.0.0.1"], [port] + list(range(1, 3000)), lambda *a: None, lambda d, t: None,
                           lambda stopped: (flags.append(stopped), finished.set()), workers=2, timeout=0.2)
        job.stop()          # stop before it starts: nothing runs, nothing hangs
        job.start()
        self.assertTrue(finished.wait(20))
        srv.close()
        self.assertEqual(flags, [True])


if __name__ == "__main__":
    unittest.main()
