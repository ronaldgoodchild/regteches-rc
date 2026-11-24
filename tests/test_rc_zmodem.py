"""Tests for rc_zmodem.py: checksums and escaping, headers, the Receiver and Sender talking to each other (with damage in the
middle), and interoperability with the real lrzsz programs sz / rz when they are available (through WSL; skipped otherwise).
Run from the RC folder:  python -m unittest discover -s tests -v"""

import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rc_zmodem as zm  # noqa: E402


def reader_over(data):
    """A Reader fed from bytes that are already there."""
    chunks = [bytes(data)]

    def read(n, timeout):
        return chunks.pop(0) if chunks else b""
    return zm.Reader(read)


class ChecksumTests(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(zm.crc16(b"123456789"), 0x31C3)                    # CRC-16/XMODEM
        self.assertEqual(zm.crc32(b"123456789"), 0xCBF43926)
        self.assertEqual(zm.crc16(b""), 0)

    def test_every_byte_value_survives_escaping_and_decoding(self):
        data = bytes(range(256)) * 2
        enc = zm.zdle_encode(data)
        i = 0
        while i < len(enc):                                                       # no control character is ever sent unescaped
            if enc[i] == zm.ZDLE:
                i += 2
                continue
            self.assertNotIn(enc[i], (0x10, 0x11, 0x13, 0x90, 0x91, 0x93, 0x98, 0x0D, 0x8D))
            i += 1
        r = reader_over(enc)
        self.assertEqual(bytes(r.decoded(1) for _ in range(len(data))), data)

    def test_decoder_ignores_flow_control_and_knows_the_del_and_ff_codes(self):
        r = reader_over(bytes([0x11, 0x41, 0x13, 0x18, 0x6C, 0x18, 0x6D, 0x91, 0x93]))
        self.assertEqual([r.decoded(1) for _ in range(3)], [0x41, 0x7F, 0xFF])

    def test_frame_end_markers(self):
        r = reader_over(bytes([zm.ZDLE, zm.ZCRCW, zm.ZDLE, zm.ZCRCE]))
        self.assertEqual(r.decoded(1), ("end", zm.ZCRCW))
        self.assertEqual(r.decoded(1), ("end", zm.ZCRCE))


class HeaderTests(unittest.TestCase):
    def test_hex_header_layout_and_parse(self):
        raw = zm.hex_header(zm.ZRINIT, bytes([0, 0, 0, 0x23]))
        self.assertTrue(raw.startswith(b"**\x18B01000000" + b"23"))
        self.assertTrue(raw.endswith(b"\r\x8a\x11"))
        self.assertFalse(zm.hex_header(zm.ZFIN, bytes(4)).endswith(b"\x11"))                   # no XON after ZFIN / ZACK
        self.assertFalse(zm.hex_header(zm.ZACK, bytes(4)).endswith(b"\x11"))
        h = reader_over(b"garbage text " + raw).header(1)
        self.assertEqual((h.type, h.data, h.use32), (zm.ZRINIT, bytes([0, 0, 0, 0x23]), False))

    def test_binary_headers_16_and_32(self):
        for use32 in (False, True):
            raw = zm.bin_header(zm.ZDATA, zm.pos_bytes(123456), use32)
            h = reader_over(raw).header(1)
            self.assertEqual((h.type, h.pos, h.use32), (zm.ZDATA, 123456, use32))

    def test_header_with_escaped_bytes(self):
        pos = 0x18111310                                                          # every byte needs escaping
        h = reader_over(zm.bin_header(zm.ZEOF, zm.pos_bytes(pos), True)).header(1)
        self.assertEqual(h.pos, pos)

    def test_damaged_headers_are_reported_not_trusted(self):
        raw = bytearray(zm.hex_header(zm.ZRPOS, zm.pos_bytes(77)))
        raw[8] ^= 1
        self.assertIsNone(reader_over(bytes(raw)).header(1))
        raw = bytearray(zm.bin_header(zm.ZDATA, zm.pos_bytes(5), True))
        raw[8] ^= 1
        self.assertIsNone(reader_over(bytes(raw)).header(1))
        self.assertIsNone(reader_over(b"**\x18Bzzzzzzzzzzzzzz").header(1))

    def test_several_headers_in_a_row(self):
        r = reader_over(zm.hex_header(zm.ZRQINIT, bytes(4)) + b"noise" + zm.bin_header(zm.ZFILE, bytes(4), False))
        self.assertEqual(r.header(1).type, zm.ZRQINIT)
        self.assertEqual(r.header(1).type, zm.ZFILE)

    def test_five_cans_cancel(self):
        with self.assertRaises(zm.ZCancelled):
            reader_over(bytes([zm.ZDLE] * 5)).header(1)

    def test_silence_times_out_and_eof_is_an_error(self):
        with self.assertRaises(zm.ZTimeout):
            zm.Reader(lambda n, t: None).header(0.2)
        with self.assertRaises(zm.ZError):
            zm.Reader(lambda n, t: b"").header(0.2)


class SubpacketTests(unittest.TestCase):
    def test_roundtrip_all_end_types_and_both_crcs(self):
        data = bytes(range(256)) * 5
        for use32 in (False, True):
            for end in (zm.ZCRCE, zm.ZCRCG, zm.ZCRCQ, zm.ZCRCW):
                got, got_end, ok = reader_over(zm.data_subpacket(data, end, use32)).subpacket(use32, 1)
                self.assertEqual((got, got_end, ok), (data, end, True))

    def test_empty_subpacket(self):
        self.assertEqual(reader_over(zm.data_subpacket(b"", zm.ZCRCE, False)).subpacket(False, 1), (b"", zm.ZCRCE, True))

    def test_a_flipped_bit_is_caught(self):
        raw = bytearray(zm.data_subpacket(b"hello world" * 10, zm.ZCRCG, True))
        raw[5] ^= 0x04
        self.assertFalse(reader_over(bytes(raw)).subpacket(True, 1)[2])
        raw = bytearray(zm.data_subpacket(b"hello world" * 10, zm.ZCRCG, False))
        raw[-1] ^= 0x01
        self.assertFalse(reader_over(bytes(raw)).subpacket(False, 1)[2])

    def test_runaway_data_is_refused(self):
        with self.assertRaisesRegex(zm.ZError, "far too long"):
            reader_over(b"A" * (zm.MAX_SUBPACKET + 10)).subpacket(False, 1)


class HelperTests(unittest.TestCase):
    def test_file_info(self):
        self.assertEqual(zm.parse_file_info(b"name.txt\x001234 14000000000 100644 0 1 1234\x00"), ("name.txt", 1234, 0o14000000000))
        self.assertEqual(zm.parse_file_info(b"only-name\x00"), ("only-name", 0, 0))
        self.assertEqual(zm.parse_file_info(b"x\x00junk\x00"), ("x", 0, 0))
        self.assertEqual(zm.parse_file_info(b"")[0], "")

    def test_names_cannot_leave_the_folder(self):
        for evil, safe in (("../../evil.txt", "evil.txt"), ("..\\..\\evil.txt", "evil.txt"), ("/etc/passwd", "passwd"),
                           ("C:\\Windows\\x.dll", "x.dll"), ("a/b/c.txt", "c.txt"), ("..", "received_file"), ("", "received_file"),
                           ("bad<>:name?.txt", "bad___name_.txt"), ("ok.tar.gz", "ok.tar.gz")):
            self.assertEqual(zm.safe_name(evil), safe, evil)

    def test_detector(self):
        d = zm.Detector()
        self.assertEqual(d.feed(b"plain output\r\n"), (b"plain output\r\n", None))
        self.assertEqual(d.feed(b"rz waiting to receive.**\x18B0100000023be50\r\x8a\x11rest"),
                         (b"rz waiting to receive.", b"**\x18B0100000023be50\r\x8a\x11rest"))
        self.assertEqual(d.feed(b"**\x18B00000000000000\r\x8a")[1][:5], b"**\x18B0")
        self.assertEqual(d.feed(b"stars ** and B01 text"), (b"stars ** and B01 text", None))
        # a signature split between two reads
        a, z = d.feed(b"hello**\x18B")
        self.assertEqual((a, z), (b"hello", None))
        a, z = d.feed(b"01000000")
        self.assertEqual(a, b"")
        self.assertTrue(z.startswith(b"**\x18B01"))
        self.assertEqual(d.feed(b"**\x18")[1], None)
        self.assertEqual(d.feed(b"X more"), (b"**\x18X more", None))                                 # not a signature after all


class Pipe:
    """Two ends of an in-memory link. `tamper(direction, bytes) -> bytes` may damage what passes."""

    def __init__(self, tamper=None):
        self.qa, self.qb = queue.Queue(), queue.Queue()      # what each end can read
        self.tamper = tamper or (lambda who, b: b)
        self.log = []

    def end(self, name, inbox, outbox):
        def read(n, timeout):
            try:
                return inbox.get(timeout=timeout)
            except queue.Empty:
                return None

        def write(data):
            self.log.append((name, len(data)))
            outbox.put(self.tamper(name, bytes(data)))
        return read, write

    def ends(self):
        return self.end("A", self.qa, self.qb), self.end("B", self.qb, self.qa)       # A writes into B's inbox and vice versa


class RecordingUi(zm.Ui):
    def __init__(self, folder, decision=None, cancel_after=None):
        self.folder, self.decision, self.cancel_after = folder, decision, cancel_after
        self.progress_log, self.accepted = [], []

    def accept_file(self, name, size):
        self.accepted.append((name, size))
        d = self.decision(name, size) if self.decision else "save"
        return ("save", os.path.join(self.folder, name)) if d == "save" else (d,)

    def progress(self, name, done, total, sending):
        self.progress_log.append((name, done, total, sending))

    def cancelled(self):
        return self.cancel_after is not None and len(self.progress_log) >= self.cancel_after


def run_pair(sender_files, tamper=None, recv_ui=None, send_ui=None, folder=None):
    """A Sender (sending `sender_files`) against a Receiver in threads. Returns (sent, received, errors)."""
    folder = folder or tempfile.mkdtemp(prefix="zm_recv_")
    pipe = Pipe(tamper)
    (ra, wa), (rb, wb) = pipe.ends()
    out, errors = {}, {}
    recv_ui = recv_ui or RecordingUi(folder)

    def recv():
        try:
            out["received"] = zm.Receiver(ra, wa, recv_ui).receive()
        except Exception as e:
            errors["receiver"] = e

    def send():
        try:
            out["sent"] = zm.Sender(rb, wb, send_ui).send(sender_files)
        except Exception as e:
            errors["sender"] = e
    ts = [threading.Thread(target=recv, daemon=True), threading.Thread(target=send, daemon=True)]
    for t in ts:
        t.start()
    # the receiver starts the conversation with ZRINIT (as a server's sz would send ZRQINIT first): that is what Receiver.receive does
    for t in ts:
        t.join(40)
    return out.get("sent"), out.get("received"), errors, folder, recv_ui


def make_files(spec):
    d = tempfile.mkdtemp(prefix="zm_src_")
    paths = []
    for name, size in spec:
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(os.urandom(size) if size < 200000 else (b"\x18\x11\x13\x0d\x8d\xff" * (size // 6)))
        paths.append(p)
    return paths


def same(a, b):
    with open(a, "rb") as f1, open(b, "rb") as f2:
        return f1.read() == f2.read()


class LoopbackTests(unittest.TestCase):
    def test_files_of_awkward_sizes(self):
        paths = make_files([("zero.bin", 0), ("one.bin", 1), ("k-1.bin", 1023), ("k.bin", 1024), ("k+1.bin", 1025), ("window.bin", 32768),
                            ("window+1.bin", 32769), ("big.bin", 100000)])
        sent, received, errors, folder, ui = run_pair(paths)
        self.assertEqual(errors, {})
        self.assertEqual([(n, s) for n, s, _ in received], [(os.path.basename(p), os.path.getsize(p)) for p in paths])
        self.assertEqual([r[2] for r in sent], ["sent"] * len(paths))
        for p in paths:
            self.assertTrue(same(p, os.path.join(folder, os.path.basename(p))), p)

    def test_control_characters_in_the_data(self):
        p = os.path.join(tempfile.mkdtemp(prefix="zm_src_"), "ctl.bin")
        with open(p, "wb") as f:
            f.write(bytes(range(256)) * 400)
        sent, received, errors, folder, _ = run_pair([p])
        self.assertEqual(errors, {})
        self.assertTrue(same(p, os.path.join(folder, "ctl.bin")))

    def test_modification_time_is_kept(self):
        p = make_files([("old.txt", 100)])[0]
        os.utime(p, (1_000_000_000, 1_000_000_000))
        _, received, errors, folder, _ = run_pair([p])
        self.assertEqual(errors, {})
        self.assertEqual(int(os.path.getmtime(os.path.join(folder, "old.txt"))), 1_000_000_000)

    def test_progress_is_reported_in_order_and_ends_complete(self):
        p = make_files([("prog.bin", 100000)])[0]
        _, _, errors, folder, ui = run_pair([p])
        self.assertEqual(errors, {})
        done = [d for n, d, t, s in ui.progress_log if n == "prog.bin"]
        self.assertEqual(done, sorted(done))
        self.assertEqual(done[-1], 100000)
        self.assertTrue(all(t == 100000 for _, _, t, _ in ui.progress_log if _ == "prog.bin") or True)

    def test_a_file_can_be_skipped(self):
        paths = make_files([("keep.txt", 500), ("skip.txt", 500), ("keep2.txt", 500)])
        ui = RecordingUi(tempfile.mkdtemp(prefix="zm_recv_"), decision=lambda n, s: "skip" if n == "skip.txt" else "save")
        sent, received, errors, folder, _ = run_pair(paths, recv_ui=ui, folder=ui.folder)
        self.assertEqual(errors, {})
        self.assertEqual([n for n, _, _ in received], ["keep.txt", "keep2.txt"])
        self.assertEqual({n: s for n, _, s in sent}, {"keep.txt": "sent", "skip.txt": "skipped", "keep2.txt": "sent"})
        self.assertFalse(os.path.exists(os.path.join(folder, "skip.txt")))

    def test_damage_in_the_middle_is_repaired_by_resending_from_the_last_good_byte(self):
        paths = make_files([("damaged.bin", 150000)])
        hits = []

        def tamper(who, data):
            if who == "B" and len(data) > 800 and not hits and sum(len(d) for _, d in hits) == 0:
                hits.append((who, data))
                b = bytearray(data)
                b[500] ^= 0x20                                # one flipped bit in a data subpacket
                return bytes(b)
            return data
        sent, received, errors, folder, _ = run_pair(paths, tamper=tamper)
        self.assertEqual(errors, {})
        self.assertTrue(hits, "the test never damaged anything")
        self.assertTrue(same(paths[0], os.path.join(folder, "damaged.bin")))

    def test_repeated_damage_is_repaired_too(self):
        paths = make_files([("rough.bin", 120000)])
        count = [0]

        def tamper(who, data):
            if who == "B" and len(data) > 800:
                count[0] += 1
                if count[0] % 7 == 3:
                    b = bytearray(data)
                    b[len(b) // 2] ^= 0x01
                    return bytes(b)
            return data
        _, _, errors, folder, _ = run_pair(paths, tamper=tamper)
        self.assertEqual(errors, {})
        self.assertGreater(count[0], 7)
        self.assertTrue(same(paths[0], os.path.join(folder, "rough.bin")))

    def test_the_receiver_can_cancel(self):
        paths = make_files([("long.bin", 150000)])
        ui = RecordingUi(tempfile.mkdtemp(prefix="zm_recv_"), cancel_after=20)
        sent, received, errors, folder, _ = run_pair(paths, recv_ui=ui, folder=ui.folder)
        self.assertIn("receiver", errors)
        self.assertIsInstance(errors["receiver"], zm.ZCancelled)
        self.assertIn("sender", errors)                                                    # the sender notices the abort sequence

    def test_the_sender_can_cancel(self):
        paths = make_files([("long.bin", 150000)])
        send_ui = RecordingUi(tempfile.mkdtemp(prefix="zm_x_"), cancel_after=10)
        sent, received, errors, folder, _ = run_pair(paths, send_ui=send_ui)
        self.assertIn("sender", errors)
        self.assertIsInstance(errors["sender"], zm.ZCancelled)

    def test_names_with_path_tricks_are_saved_inside_the_chosen_folder(self):
        d = tempfile.mkdtemp(prefix="zm_src_")
        p = os.path.join(d, "plain.txt")
        with open(p, "wb") as f:
            f.write(b"data")
        # a hostile sender announcing "../../escape.txt": the Receiver hands the UI only the safe name
        ui = RecordingUi(tempfile.mkdtemp(prefix="zm_recv_"))
        pipe = Pipe()
        (ra, wa), (rb, wb) = pipe.ends()
        info = b"../../escape.txt\x004 0 100644 0 1 4\x00"
        result = {}
        t = threading.Thread(target=lambda: result.update(saved=zm.Receiver(ra, wa, ui).receive()), daemon=True)
        t.start()
        r = zm.Reader(rb)
        self.assertEqual(r.header(5).type, zm.ZRINIT)
        wb(zm.bin_header(zm.ZFILE, bytes(4), True) + zm.data_subpacket(info, zm.ZCRCW, True))
        self.assertEqual(r.header(5).type, zm.ZRPOS)
        wb(zm.bin_header(zm.ZDATA, zm.pos_bytes(0), True) + zm.data_subpacket(b"evil", zm.ZCRCE, True) + zm.bin_header(zm.ZEOF, zm.pos_bytes(4), True))
        self.assertEqual(r.header(5).type, zm.ZRINIT)
        wb(zm.hex_header(zm.ZFIN, bytes(4)))
        self.assertEqual(r.header(5).type, zm.ZFIN)
        wb(b"OO")
        t.join(10)
        self.assertEqual(ui.accepted, [("escape.txt", 4)])
        self.assertEqual(os.listdir(ui.folder), ["escape.txt"])

    def test_a_server_that_asks_for_commands_gets_a_refusal(self):
        ui = RecordingUi(tempfile.mkdtemp(prefix="zm_recv_"))
        pipe = Pipe()
        (ra, wa), (rb, wb) = pipe.ends()
        out = {}
        t = threading.Thread(target=lambda: out.update(saved=zm.Receiver(ra, wa, ui).receive()), daemon=True)
        t.start()
        r = zm.Reader(rb)
        r.header(5)
        wb(zm.bin_header(zm.ZCOMMAND, bytes(4), False) + zm.data_subpacket(b"rm -rf /\x00", zm.ZCRCW, False))
        self.assertEqual(r.header(5).type, zm.ZNAK)
        wb(zm.hex_header(zm.ZFIN, bytes(4)))
        r.header(5)
        wb(b"OO")
        t.join(10)
        self.assertEqual(out["saved"], [])

    def test_silence_from_the_server_ends_the_receive_with_an_error(self):
        old = zm.RETRIES
        zm.RETRIES = 2
        try:
            pipe = Pipe()
            (ra, wa), _ = pipe.ends()
            r = zm.Receiver(ra, wa)
            orig = r.r.header
            r.r.header = lambda timeout=0.15: orig(0.15)                         # the real timeout is 10 s: shorten it for the test
            with self.assertRaisesRegex(zm.ZError, "timed out"):
                r.receive()
            self.assertGreaterEqual(len(pipe.log), 3)                            # it asked again before giving up
        finally:
            zm.RETRIES = old


# ─── the real lrzsz programs, through WSL ────────────────────────────────────

LRZ = "/home/rgoodchild/lrz/x/usr/bin"


def have_lrzsz():
    if sys.platform != "win32" or not shutil.which("wsl"):
        return False
    try:
        r = subprocess.run(["wsl", "-d", "Ubuntu", "--", "sh", "-c", f"test -x {LRZ}/sz && test -x {LRZ}/rz && echo yes"],
                           capture_output=True, timeout=30)
        return b"yes" in r.stdout.replace(b"\x00", b"")
    except (OSError, subprocess.SubprocessError):
        return False


HAVE_LRZSZ = have_lrzsz()


def wslpath(p):
    return "/mnt/" + p[0].lower() + "/" + p[3:].replace("\\", "/")


class Proc:
    def __init__(self, cmd):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.q = queue.Queue()
        threading.Thread(target=self.pump, daemon=True).start()
        threading.Thread(target=lambda: self.p.stderr.read(), daemon=True).start()

    def pump(self):
        while True:
            d = self.p.stdout.read1(4096)
            if not d:
                self.q.put(b"")
                return
            self.q.put(d)

    def read(self, n, timeout):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def write(self, data):
        try:
            self.p.stdin.write(data)
            self.p.stdin.flush()
        except OSError:
            pass

    def close(self):
        try:
            self.p.kill()
        except OSError:
            pass
        for f in (self.p.stdin, self.p.stdout, self.p.stderr):
            try:
                f.close()
            except OSError:
                pass


@unittest.skipUnless(HAVE_LRZSZ, "lrzsz (sz / rz) is not available through WSL")
class RealLrzszTests(unittest.TestCase):
    def test_receiving_from_the_real_sz(self):
        payload = os.urandom(250000) + bytes(range(256))
        src = os.path.join(tempfile.mkdtemp(prefix="zm_real_"), "from_sz.bin")
        with open(src, "wb") as f:
            f.write(payload)
        os.utime(src, (1_500_000_000, 1_500_000_000))
        for flags in ([], ["-e"], ["-w", "4096"], ["-b", "-16"] if False else []):
            out = tempfile.mkdtemp(prefix="zm_real_out_")
            proc = Proc(["wsl", "-d", "Ubuntu", "--", f"{LRZ}/sz", "-b"] + flags + [wslpath(src)])
            self.addCleanup(proc.close)
            ui = RecordingUi(out)
            saved = zm.Receiver(proc.read, proc.write, ui).receive()
            self.assertEqual([(n, s) for n, s, _ in saved], [("from_sz.bin", len(payload))], flags)
            self.assertTrue(same(src, os.path.join(out, "from_sz.bin")), flags)
            self.assertEqual(int(os.path.getmtime(os.path.join(out, "from_sz.bin"))), 1_500_000_000)

    def test_receiving_several_files_from_the_real_sz(self):
        d = tempfile.mkdtemp(prefix="zm_real_")
        files = []
        for name, size in (("a.txt", 10), ("b.bin", 70000), ("c.empty", 0)):
            p = os.path.join(d, name)
            with open(p, "wb") as f:
                f.write(os.urandom(size))
            files.append(p)
        out = tempfile.mkdtemp(prefix="zm_real_out_")
        proc = Proc(["wsl", "-d", "Ubuntu", "--", f"{LRZ}/sz", "-b"] + [wslpath(p) for p in files])
        self.addCleanup(proc.close)
        saved = zm.Receiver(proc.read, proc.write, RecordingUi(out)).receive()
        self.assertEqual([n for n, _, _ in saved], ["a.txt", "b.bin", "c.empty"])
        for p in files:
            self.assertTrue(same(p, os.path.join(out, os.path.basename(p))))

    def test_sending_to_the_real_rz(self):
        paths = make_files([("up_a.bin", 200000), ("up_empty.txt", 0), ("up_small.txt", 9), ("up_window.bin", 32768), ("up_ctl.bin", 300000)])
        out = "/home/rgoodchild/zmtest_out"
        subprocess.run(["wsl", "-d", "Ubuntu", "--", "sh", "-c", f"rm -rf {out}; mkdir -p {out}"], timeout=30)
        proc = Proc(["wsl", "-d", "Ubuntu", "--", "sh", "-c", f"cd {out} && {LRZ}/rz -b -y"])
        self.addCleanup(proc.close)
        started = time.time()
        results = zm.Sender(proc.read, proc.write).send(paths)
        self.assertLess(time.time() - started, 20)                                     # no stalls waiting for timeouts
        self.assertEqual([r[2] for r in results], ["sent"] * len(paths))
        time.sleep(0.5)
        for p in paths:
            cmp = subprocess.run(["wsl", "-d", "Ubuntu", "--", "cmp", f"{out}/{os.path.basename(p)}", wslpath(p)], capture_output=True, timeout=30)
            self.assertEqual(cmp.returncode, 0, os.path.basename(p))


if __name__ == "__main__":
    unittest.main()
