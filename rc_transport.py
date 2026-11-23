"""REGTeches RC - the two non-SSH terminal connections: Telnet and Serial (COM port).

Both present the small slice of the paramiko channel API that the terminal tab uses (recv, send, resize_pty,
exit_status_ready, close), so rc_terminal.TerminalTab can drive any of the three the same way.

* Telnet is written here (Python 3.13 dropped telnetlib). It answers the usual option negotiation: it agrees to send
  the terminal type and window size and to suppress go-ahead, accepts the server's echo, and refuses everything
  else. Bytes are cleaned of telnet commands before the terminal sees them. It goes through the server's proxy if
  it has one.
* Serial uses the `pyserial` package (pip install pyserial). The setting is one line, for example
  `COM3, 115200, 8N1, rtscts`.
"""

import re
import socket
import threading

import rc_manager as core

log = core.log

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 1, 3, 24, 31
TTYPE_IS, TTYPE_SEND = 0, 1


class TelnetChannel:
    """A connected socket speaking Telnet. recv() returns clean terminal bytes (b'' once the connection is over)."""

    def __init__(self, sock, cols=80, rows=24, ttype="xterm-256color"):
        self.sock, self.cols, self.rows, self.ttype = sock, cols, rows, ttype
        self.lock = threading.Lock()
        self.closed = self.peer_closed = False
        self.naws = False
        self.will, self.do = set(), set()         # options we already agreed to / asked for (so repeats are not answered twice)
        self.state, self.cmd, self.sb, self.after_cr = 0, 0, bytearray(), False
        self.pending = bytearray()

    # ── reading ──
    def recv(self, n=8192):
        while not self.pending:
            if self.closed:
                return b""
            try:
                data = self.sock.recv(n)
            except OSError:
                data = b""
            if not data:
                self.peer_closed = not self.closed     # the server hung up (as opposed to us closing)
                self.closed = True
                return b""
            self.feed(data)
        out = bytes(self.pending[:n])
        del self.pending[:n]
        return out

    def feed(self, data):
        for b in data:
            st = self.state
            if st == 0:
                if b == IAC:
                    self.state = 1
                elif self.after_cr and b == 0:
                    self.after_cr = False             # CR NUL is how Telnet writes a bare carriage return
                else:
                    self.after_cr = b == 13
                    self.pending.append(b)
            elif st == 1:
                if b == IAC:
                    self.pending.append(IAC)
                    self.state = 0
                elif b in (DO, DONT, WILL, WONT):
                    self.cmd, self.state = b, 2
                elif b == SB:
                    self.sb.clear()
                    self.state = 3
                else:
                    self.state = 0                      # NOP, GA, ... nothing to do
            elif st == 2:
                self.negotiate(self.cmd, b)
                self.state = 0
            elif st == 3:
                if b == IAC:
                    self.state = 4
                else:
                    self.sb.append(b)
            else:                                       # st == 4: IAC inside a sub-negotiation
                if b == SE:
                    self.subnegotiate(bytes(self.sb))
                    self.state = 0
                elif b == IAC:
                    self.sb.append(IAC)
                    self.state = 3
                else:
                    self.state = 0

    # ── option negotiation ──
    def raw(self, data):
        with self.lock:
            if not self.closed:
                self.sock.sendall(data)

    def negotiate(self, cmd, opt):
        try:
            if cmd == DO:
                if opt in (OPT_TTYPE, OPT_NAWS, OPT_SGA):
                    if opt not in self.will:
                        self.will.add(opt)
                        self.raw(bytes([IAC, WILL, opt]))
                        if opt == OPT_NAWS:
                            self.naws = True
                            self.send_size()
                else:
                    self.raw(bytes([IAC, WONT, opt]))
            elif cmd == DONT:
                if opt in self.will:
                    self.will.discard(opt)
                    self.raw(bytes([IAC, WONT, opt]))
                    if opt == OPT_NAWS:
                        self.naws = False
            elif cmd == WILL:
                if opt in (OPT_ECHO, OPT_SGA):
                    if opt not in self.do:
                        self.do.add(opt)
                        self.raw(bytes([IAC, DO, opt]))
                else:
                    self.raw(bytes([IAC, DONT, opt]))
            elif cmd == WONT:
                if opt in self.do:
                    self.do.discard(opt)
                    self.raw(bytes([IAC, DONT, opt]))
        except OSError:
            pass

    def subnegotiate(self, sb):
        if len(sb) >= 2 and sb[0] == OPT_TTYPE and sb[1] == TTYPE_SEND:
            try:
                self.raw(bytes([IAC, SB, OPT_TTYPE, TTYPE_IS]) + self.ttype.encode("ascii", "replace") + bytes([IAC, SE]))
            except OSError:
                pass

    def send_size(self):
        def esc(n):
            return bytes([n >> 8 & 255, n & 255]).replace(b"\xff", b"\xff\xff")
        try:
            self.raw(bytes([IAC, SB, OPT_NAWS]) + esc(self.cols) + esc(self.rows) + bytes([IAC, SE]))
        except OSError:
            pass

    # ── writing and the rest of the channel API ──
    def send(self, s):
        data = s.encode("utf-8", "replace") if isinstance(s, str) else bytes(s)
        data = data.replace(b"\xff", b"\xff\xff").replace(b"\r", b"\r\n")      # Enter is CR LF on the wire
        self.raw(data)

    def resize_pty(self, width, height):
        self.cols, self.rows = int(width), int(height)
        if self.naws:
            self.send_size()

    def exit_status_ready(self):
        return self.peer_closed

    def close(self):
        if self.closed:
            return
        self.closed = True
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except OSError:
                pass


def open_telnet(host, cols=80, rows=24, timeout=12):
    """Connect to host['telnet_port'] (default 23), through the server's proxy if it has one."""
    port = core._port(host.get("telnet_port"), 23)
    px = core.proxy_for(host)
    if px:
        sock = core.proxy_connect(px, host["address"], port, timeout)
    else:
        addr = core.resolve_address(host, port) if host.get("alt_addresses") else host["address"]
        sock = socket.create_connection((addr, port), timeout)
    sock.settimeout(None)
    return TelnetChannel(sock, cols, rows)


# ─── Serial ──────────────────────────────────────────────────────────────────

_FRAME = re.compile(r"([5-8])([NEOMS])(1|1\.5|2)$", re.I)


def parse_serial_spec(text, baud=0):
    """'COM3' or 'COM3, 115200, 8N1, rtscts' -> settings dict. `baud` (the connection's Port box) is used when the text has no
    number. Raises ValueError with a message for the user."""
    parts = [p for p in re.split(r"[,;\s]+", (text or "").strip()) if p]
    if not parts:
        raise ValueError("Enter the COM port, for example COM3 or COM3, 115200, 8N1.")
    spec = {"port": parts[0], "baudrate": int(baud) if baud else 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
            "xonxoff": False, "rtscts": False}
    for p in parts[1:]:
        m = _FRAME.match(p)
        if p.isdigit():
            if not 50 <= int(p) <= 4000000:
                raise ValueError(f"{p} is not a sensible speed (baud rate).")
            spec["baudrate"] = int(p)
        elif m:
            spec["bytesize"], spec["parity"], spec["stopbits"] = int(m.group(1)), m.group(2).upper(), float(m.group(3)) if m.group(3) == "1.5" else int(m.group(3))
        elif p.lower() in ("rtscts", "xonxoff"):
            spec[p.lower()] = True
        else:
            raise ValueError(f"Don't understand “{p}”. Use a speed (115200), a frame (8N1) or rtscts / xonxoff.")
    return spec


def list_serial_ports():
    """[(device, description)] of the serial ports on this PC (empty without pyserial)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return sorted(((p.device, p.description or "") for p in list_ports.comports()), key=lambda d: (len(d[0]), d[0]))


class SerialChannel:
    def __init__(self, ser):
        self.ser = ser
        self.closed = False

    def recv(self, n=8192):
        import serial
        while not self.closed:
            try:
                data = self.ser.read(max(1, min(n, self.ser.in_waiting or 1)))
            except (serial.SerialException, OSError, ValueError):
                return b""                              # unplugged, or closed under us
            if data:
                return data
        return b""

    def send(self, s):
        data = s.encode("utf-8", "replace") if isinstance(s, str) else bytes(s)
        self.ser.write(data)

    def resize_pty(self, width, height):
        pass                                            # a cable has no window size

    def exit_status_ready(self):
        return False                                    # an unplugged cable is a lost connection, not a log-out

    def close(self):
        self.closed = True
        try:
            self.ser.close()
        except Exception:
            pass


def open_serial(spec_text, baud=0):
    try:
        import serial
    except ImportError:
        raise RuntimeError("Serial needs the 'pyserial' package. Run:  pip install pyserial")
    spec = parse_serial_spec(spec_text, baud)
    parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD, "M": serial.PARITY_MARK,
              "S": serial.PARITY_SPACE}[spec["parity"]]
    stop = {1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2: serial.STOPBITS_TWO}[spec["stopbits"]]
    try:
        ser = serial.serial_for_url(spec["port"], baudrate=spec["baudrate"], bytesize=spec["bytesize"], parity=parity,
                                    stopbits=stop, xonxoff=spec["xonxoff"], rtscts=spec["rtscts"], timeout=0.2)
    except (serial.SerialException, OSError, ValueError) as e:
        ports = ", ".join(f"{d} ({desc})" if desc else d for d, desc in list_serial_ports()) or "none found"
        raise RuntimeError(f"Could not open {spec['port']}: {e}\nSerial ports on this PC: {ports}")
    return SerialChannel(ser)
