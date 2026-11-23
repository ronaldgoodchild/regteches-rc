"""REGTeches RC - Zmodem file transfer inside a terminal tab (the `rz` and `sz` commands).

On a server, `sz file` sends a file and `rz` waits to receive one. When the SSH terminal sees the Zmodem start-up signature in the
output it takes over the connection for the duration of the transfer (showing progress in the status bar), then hands it back
to the terminal. Received files are saved in a folder you choose (asked the first time, remembered); for `rz` RC asks which
files to send. Right-click > "Upload files with Zmodem (rz)" types `rz -be` for you.

This is a pure-Python implementation of the protocol (hex / binary16 / binary32 headers, CRC16 and CRC32 data subpackets,
escaping, resume and error recovery by ZRPOS) written against lrzsz 0.13.1, the program that provides rz/sz on most servers.
Everything here works on two callables, read(n, timeout) and write(bytes), so it is tested without a network."""

import os
import re
import struct
import time
import zlib

# frame types
ZRQINIT, ZRINIT, ZSINIT, ZACK, ZFILE, ZSKIP, ZNAK, ZABORT, ZFIN, ZRPOS, ZDATA, ZEOF, ZFERR, ZCRC, ZCHALLENGE, ZCOMPL, ZCAN = range(17)
ZFREECNT, ZCOMMAND, ZSTDERR = 17, 18, 19
NAMES = {0: "ZRQINIT", 1: "ZRINIT", 2: "ZSINIT", 3: "ZACK", 4: "ZFILE", 5: "ZSKIP", 6: "ZNAK", 7: "ZABORT", 8: "ZFIN", 9: "ZRPOS",
         10: "ZDATA", 11: "ZEOF", 12: "ZFERR", 13: "ZCRC", 16: "ZCAN", 18: "ZCOMMAND"}
ZPAD, ZDLE = 0x2A, 0x18
ZBIN, ZHEX, ZBIN32 = 0x41, 0x42, 0x43
ZCRCE, ZCRCG, ZCRCQ, ZCRCW = 0x68, 0x69, 0x6A, 0x6B
# ZRINIT capability flags (ZF0)
CANFDX, CANOVIO, CANBRK, CANFC32, ESCCTL = 0x01, 0x02, 0x04, 0x20, 0x40

SUBPACKET = 1024            # bytes per data subpacket
WINDOW = 32 * 1024          # bytes sent before the sender waits for an acknowledgement
HEADER_TIMEOUT = 10.0
RETRIES = 6
MAX_SUBPACKET = 65536

SIGNATURE = re.compile(rb"\*\*\x18B0[01]")
_SIG_PREFIXES = [b"**\x18", b"**\x18B", b"**\x18B0"]


class ZError(Exception):
    """The transfer failed (the message is for the user)."""


class ZCancelled(ZError):
    """Either side cancelled."""


class ZTimeout(ZError):
    pass


# ─── checksums and escaping ──────────────────────────────────────────────────

def _make_crc16_table():
    table = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        table.append(c)
    return table


_CRC16 = _make_crc16_table()


def crc16(data, crc=0):
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16[(crc >> 8) ^ b]
    return crc


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


_ESCAPE = {0x18, 0x10, 0x11, 0x13, 0x90, 0x91, 0x93, 0x98, 0x0D, 0x8D}
_ESC_TABLE = [bytes([ZDLE, b ^ 0x40]) if b in _ESCAPE else bytes([b]) for b in range(256)]


def zdle_encode(data):
    return b"".join(_ESC_TABLE[b] for b in data)


def hex_header(ftype, data4):
    body = bytes([ftype]) + bytes(data4)
    crc = crc16(body)
    out = b"**" + bytes([ZDLE]) + b"B" + (body + bytes([crc >> 8, crc & 255])).hex().encode("ascii") + b"\r\x8a"
    return out + (b"" if ftype in (ZFIN, ZACK) else b"\x11")


def bin_header(ftype, data4, use32=False):
    body = bytes([ftype]) + bytes(data4)
    if use32:
        return bytes([ZPAD, ZDLE, ZBIN32]) + zdle_encode(body + struct.pack("<I", crc32(body)))
    crc = crc16(body)
    return bytes([ZPAD, ZDLE, ZBIN]) + zdle_encode(body + bytes([crc >> 8, crc & 255]))


def data_subpacket(data, end, use32=False):
    if use32:
        tail = struct.pack("<I", crc32(bytes(data) + bytes([end])))
    else:
        c = crc16(bytes(data) + bytes([end]))
        tail = bytes([c >> 8, c & 255])
    return zdle_encode(data) + bytes([ZDLE, end]) + zdle_encode(tail)


def pos_bytes(n):
    return struct.pack("<I", n & 0xFFFFFFFF)


def pos_value(data4):
    return struct.unpack("<I", bytes(data4))[0]


# ─── reading ─────────────────────────────────────────────────────────────────

class Header:
    def __init__(self, ftype, data, use32):
        self.type, self.data, self.use32 = ftype, bytes(data), use32

    @property
    def pos(self):
        return pos_value(self.data)

    def __repr__(self):
        return f"<{NAMES.get(self.type, self.type)} {self.data.hex()}>"


class Reader:
    """Pulls bytes from read(n, timeout) -> bytes | None (timeout) | b'' (closed) and decodes Zmodem's escaping."""

    def __init__(self, read):
        self.read, self.buf, self.cans = read, bytearray(), 0

    def raw(self, timeout):
        end = time.time() + timeout
        while not self.buf:
            left = end - time.time()
            if left <= 0:
                raise ZTimeout("the other side stopped answering")
            chunk = self.read(4096, left)
            if chunk is None:
                continue
            if chunk == b"":
                raise ZError("the connection closed during the transfer")
            self.buf += chunk
        b = self.buf.pop(0)
        self.cans = self.cans + 1 if b == ZDLE else 0
        if self.cans >= 5:
            self.buf.clear()
            raise ZCancelled("the other side cancelled the transfer")
        return b

    def decoded(self, timeout):
        """One decoded byte (int), or ('end', marker) for a ZDLE + frame-end marker."""
        while True:
            b = self.raw(timeout)
            if b in (0x11, 0x13, 0x91, 0x93):
                continue                                      # flow-control characters are never data
            if b != ZDLE:
                return b
            c = self.raw(timeout)
            if ZCRCE <= c <= ZCRCW:
                return ("end", c)
            if c == 0x6C:
                return 0x7F
            if c == 0x6D:
                return 0xFF
            if c == ZDLE:                                     # a cancel sequence in progress: counted in raw()
                continue
            return c ^ 0x40

    def byte(self, timeout):
        b = self.decoded(timeout)
        if isinstance(b, tuple):
            raise ZError("unexpected end-of-frame marker inside a header")
        return b

    def header(self, timeout=HEADER_TIMEOUT):
        """The next header, or None if one arrived damaged (the caller asks again). Text before it is skipped."""
        end = time.time() + timeout
        while True:
            b = self.raw(max(0.05, end - time.time()))
            if b != ZPAD:
                continue
            b = self.raw(max(0.05, end - time.time()))
            while b == ZPAD:
                b = self.raw(max(0.05, end - time.time()))
            if b != ZDLE:
                continue
            fmt = self.raw(max(0.05, end - time.time()))
            if fmt == ZHEX:
                digits = bytes(self.raw(timeout) for _ in range(14))
                try:
                    body = bytes.fromhex(digits.decode("ascii"))
                except ValueError:
                    return None
                if crc16(body[:5]) != (body[5] << 8 | body[6]):
                    return None
                return Header(body[0], body[1:5], False)
            if fmt in (ZBIN, ZBIN32):
                n = 5 + (4 if fmt == ZBIN32 else 2)
                body = bytes(self.byte(timeout) for _ in range(n))
                if fmt == ZBIN32:
                    ok = struct.unpack("<I", body[5:])[0] == crc32(body[:5])
                else:
                    ok = crc16(body[:5]) == (body[5] << 8 | body[6])
                return Header(body[0], body[1:5], fmt == ZBIN32) if ok else None

    def subpacket(self, use32, timeout=HEADER_TIMEOUT):
        """(data, end_marker, crc_ok)."""
        data = bytearray()
        while True:
            b = self.decoded(timeout)
            if isinstance(b, tuple):
                end = b[1]
                break
            data.append(b)
            if len(data) > MAX_SUBPACKET:
                raise ZError("a data block was far too long (the stream is out of step)")
        n = 4 if use32 else 2
        crc = bytes(self.byte(timeout) for _ in range(n))
        if use32:
            ok = struct.unpack("<I", crc)[0] == crc32(bytes(data) + bytes([end]))
        else:
            ok = (crc[0] << 8 | crc[1]) == crc16(bytes(data) + bytes([end]))
        return bytes(data), end, ok


# ─── shared pieces ───────────────────────────────────────────────────────────

class Ui:
    """What the transfer needs from the program around it. The default does the simplest sensible thing (for tests)."""

    def accept_file(self, name, size):
        """-> ('save', full path) | ('skip',) | ('cancel',)"""
        return ("save", name)

    def progress(self, name, done, total, sending):
        pass

    def cancelled(self):
        return False


def abort_bytes():
    return bytes([ZDLE] * 8 + [0x08] * 8)


def parse_file_info(raw):
    """b'name\\0size mtime mode ...\\0' -> (name, size, mtime)."""
    name, _, rest = raw.partition(b"\x00")
    fields = rest.split(b"\x00")[0].split()
    size = int(fields[0]) if fields and fields[0].isdigit() else 0
    try:
        mtime = int(fields[1], 8) if len(fields) > 1 else 0
    except ValueError:
        mtime = 0
    return name.decode("utf-8", "replace"), size, mtime


def safe_name(name):
    """The file name part only (the sender must not be able to write outside the folder you chose)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)
    return base if base not in ("", ".", "..") else "received_file"


# ─── receiving (the server ran sz) ───────────────────────────────────────────

class Receiver:
    def __init__(self, read, write, ui=None, log=None):
        self.r, self.write, self.ui = Reader(read), write, ui or Ui()
        self.log = log or (lambda m: None)
        self.use32 = True

    def zrinit(self):
        flags = CANFDX | CANOVIO | CANFC32
        self.write(hex_header(ZRINIT, bytes([0, 0, 0, flags])))

    def receive(self):
        """Run the transfer. Returns [(name, size, saved path)] for the files that arrived."""
        saved, f, pos, name, size, mtime, path = [], None, 0, "", 0, 0, ""
        self.zrinit()
        bad = 0
        try:
            while True:
                if self.ui.cancelled():
                    self.write(abort_bytes())
                    raise ZCancelled("you cancelled the transfer")
                try:
                    h = self.r.header()
                except ZTimeout:
                    bad += 1
                    if bad > RETRIES:
                        raise ZError("the server stopped sending (timed out)")
                    self.write(hex_header(ZRPOS, pos_bytes(pos)) if f else hex_header(ZRINIT, bytes([0, 0, 0, CANFDX | CANOVIO | CANFC32])))
                    continue
                if h is None:
                    bad += 1
                    if bad > RETRIES * 4:
                        raise ZError("too many damaged blocks")
                    self.write(hex_header(ZRPOS, pos_bytes(pos)) if f else hex_header(ZNAK, bytes(4)))
                    continue
                bad = 0
                if h.type == ZRQINIT:
                    self.zrinit()
                elif h.type == ZSINIT:
                    self.r.subpacket(h.use32)                              # attention string: not used
                    self.write(hex_header(ZACK, bytes(4)))
                elif h.type == ZFILE:
                    info, _end, ok = self.r.subpacket(h.use32)
                    if not ok:
                        self.write(hex_header(ZNAK, bytes(4)))
                        continue
                    name, size, mtime = parse_file_info(info)
                    decision = self.ui.accept_file(safe_name(name), size)
                    if decision[0] == "cancel":
                        self.write(abort_bytes())
                        raise ZCancelled("you cancelled the transfer")
                    if decision[0] == "skip":
                        self.write(hex_header(ZSKIP, bytes(4)))
                        continue
                    path = decision[1]
                    f = open(path, "wb")
                    pos = 0
                    self.write(hex_header(ZRPOS, pos_bytes(0)))
                elif h.type == ZDATA:
                    if f is None:
                        self.zrinit()
                        continue
                    if h.pos != pos:
                        self.write(hex_header(ZRPOS, pos_bytes(pos)))
                        continue
                    while True:
                        try:
                            data, end, ok = self.r.subpacket(h.use32)
                        except ZError as e:
                            if isinstance(e, ZCancelled):
                                raise
                            self.write(hex_header(ZRPOS, pos_bytes(pos)))
                            break
                        if not ok:
                            self.write(hex_header(ZRPOS, pos_bytes(pos)))
                            break
                        f.write(data)
                        pos += len(data)
                        self.ui.progress(safe_name(name), pos, size, False)
                        if self.ui.cancelled():
                            self.write(abort_bytes())
                            raise ZCancelled("you cancelled the transfer")
                        if end in (ZCRCQ, ZCRCW):
                            self.write(hex_header(ZACK, pos_bytes(pos)))
                        if end in (ZCRCE, ZCRCW):
                            break
                elif h.type == ZEOF:
                    if f is None:
                        self.zrinit()
                        continue
                    if h.pos != pos:
                        self.write(hex_header(ZRPOS, pos_bytes(pos)))
                        continue
                    f.close()
                    f = None
                    if mtime:
                        try:
                            os.utime(path, (mtime, mtime))
                        except OSError:
                            pass
                    saved.append((safe_name(name), pos, path))
                    self.ui.progress(safe_name(name), pos, max(size, pos), False)
                    self.zrinit()
                elif h.type == ZFIN:
                    self.write(hex_header(ZFIN, bytes(4)))
                    try:
                        self.r.raw(1.0)
                        self.r.raw(0.3)                                    # the closing "OO"
                    except ZError:
                        pass
                    return saved
                elif h.type in (ZABORT, ZCAN, ZFERR):
                    raise ZError("the server ended the transfer")
                elif h.type == ZCOMMAND:
                    self.r.subpacket(h.use32)
                    self.write(hex_header(ZNAK, bytes(4)))                 # never run commands a server asks for
                else:
                    self.log(f"ignored {h!r}")
        finally:
            if f is not None:
                f.close()


# ─── sending (the server ran rz) ─────────────────────────────────────────────

class Sender:
    def __init__(self, read, write, ui=None, log=None):
        self.r, self.write, self.ui = Reader(read), write, ui or Ui()
        self.log = log or (lambda m: None)
        self.use32 = False

    def wait_for(self, wanted, resend=None):
        """The next header whose type is in `wanted` (others, like a repeated ZRINIT, are ignored)."""
        bad = 0
        while True:
            if self.ui.cancelled():
                self.write(abort_bytes())
                raise ZCancelled("you cancelled the transfer")
            try:
                h = self.r.header()
            except ZTimeout:
                h = None
            if h is None:
                bad += 1
                if bad > RETRIES:
                    raise ZError("the server stopped answering (timed out)")
                if resend:
                    self.write(resend)
                continue
            bad = 0
            if h.type in wanted:
                return h
            if h.type in (ZABORT, ZCAN, ZFERR):
                raise ZError("the server ended the transfer")
            if h.type == ZNAK and resend:
                self.write(resend)

    def send(self, paths, first=None):
        """Send the files. `first` may be a ZRINIT header already read. Returns [(name, size, 'sent' | 'skipped')]."""
        h = first or self.wait_for((ZRINIT,))
        if h.type != ZRINIT:
            h = self.wait_for((ZRINIT,))
        self.use32 = bool(h.data[3] & CANFC32)
        results = []
        for path in paths:
            results.append(self.send_file(path))
        self.write(hex_header(ZFIN, bytes(4)))
        try:
            self.wait_for((ZFIN,), resend=hex_header(ZFIN, bytes(4)))
        finally:
            self.write(b"OO")
        return results

    def send_file(self, path):
        size = os.path.getsize(path)
        mtime = int(os.path.getmtime(path))
        name = os.path.basename(path)
        info = name.encode("utf-8", "replace") + b"\x00" + f"{size} {mtime:o} 100644 0 1 {size}".encode("ascii") + b"\x00"
        zfile = bin_header(ZFILE, bytes(4), self.use32) + data_subpacket(info, ZCRCW, self.use32)
        self.write(zfile)
        h = self.wait_for((ZRPOS, ZSKIP, ZCRC), resend=zfile)
        while h.type == ZCRC:                                               # the receiver wants a checksum of the file first
            with open(path, "rb") as f:
                value = crc32(f.read())
            self.write(hex_header(ZCRC, pos_bytes(value)))
            h = self.wait_for((ZRPOS, ZSKIP, ZCRC), resend=zfile)
        if h.type == ZSKIP:
            self.ui.progress(name, size, size, True)
            return (name, size, "skipped")
        pos = min(h.pos, size)
        with open(path, "rb") as f:
            while True:
                f.seek(pos)
                self.write(bin_header(ZDATA, pos_bytes(pos), self.use32))
                resync = False
                since_ack = 0
                while True:
                    chunk = f.read(SUBPACKET)
                    eof = len(chunk) < SUBPACKET or f.tell() >= size
                    end = ZCRCE if eof else (ZCRCQ if since_ack + len(chunk) >= WINDOW else ZCRCG)    # Q: acknowledge, then the frame goes on
                    self.write(data_subpacket(chunk, end, self.use32))
                    pos += len(chunk)
                    since_ack += len(chunk)
                    self.ui.progress(name, pos, size, True)
                    if self.ui.cancelled():
                        self.write(abort_bytes())
                        raise ZCancelled("you cancelled the transfer")
                    if end == ZCRCQ:
                        a = self.wait_for((ZACK, ZRPOS))
                        if a.type == ZRPOS:
                            pos, resync = min(a.pos, size), True
                            break
                        since_ack = 0
                    if eof:
                        break
                if resync:
                    continue
                self.write(bin_header(ZEOF, pos_bytes(size), self.use32))
                a = self.wait_for((ZRINIT, ZRPOS), resend=bin_header(ZEOF, pos_bytes(size), self.use32))
                if a.type == ZRPOS:
                    pos = min(a.pos, size)
                    continue
                return (name, size, "sent")


# ─── spotting a transfer in the terminal's output ────────────────────────────

class Detector:
    """Splits the byte stream into 'ordinary output' and 'a Zmodem transfer starts here'."""

    def __init__(self):
        self.tail = b""

    def feed(self, data):
        """-> (bytes to show, bytes that begin a Zmodem session or None)."""
        buf = self.tail + data
        self.tail = b""
        m = SIGNATURE.search(buf)
        if m:
            return buf[:m.start()], buf[m.start():]
        for p in _SIG_PREFIXES:                              # a signature split between two reads: hold the start back
            if buf.endswith(p):
                self.tail = p
                return buf[:-len(p)], None
        return buf, None
