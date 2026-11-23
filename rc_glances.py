"""REGTeches RC - Glances as a source for the Stats tab.

Glances (https://nicolargo.github.io/glances) is a monitoring program that many people already run on their servers and PCs
(`glances -w` serves a web page and a REST API, by default on port 61208). When a server has "Use Glances for Stats" ticked
(Edit > Glances), the Stats tab reads that API instead of logging in over SSH. That works for machines RC has no SSH login
for, including Windows PCs, and it adds containers and temperature sensors. Only one read-only request (`/api/4/all`, or
`/api/3/all` on older Glances) is made per refresh. Sending signals to processes and the Services list need SSH and are not
available in this mode.

The functions here are pure (they turn Glances' JSON into the same "sample" the SSH collector produces) and are tested with
canned data and a small fake Glances server."""

import base64
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request

import rc_manager as core

log = core.log

DEFAULT_PORT = 61208
API_VERSIONS = (4, 3)


class GlancesError(Exception):
    """A readable reason Glances could not be used."""


def base_url(host):
    scheme = "https" if host.get("glances_https") else "http"
    port = core._port(host.get("glances_port"), DEFAULT_PORT)
    addr = host["address"]
    return f"{scheme}://[{addr}]:{port}" if ":" in addr and not addr.startswith("[") else f"{scheme}://{addr}:{port}"


def auth_header(host):
    pw = core.unprotect_password(host["glances_password_enc"]) if host.get("glances_password_enc") else ""
    user = (host.get("glances_user") or "").strip()
    if not pw and not user:
        return {}
    token = base64.b64encode(f"{user or 'glances'}:{pw}".encode("utf-8")).decode()
    return {"Authorization": "Basic " + token}


_CTX = []


def _https_context():
    """One TLS context for all https requests (building it loads the system certificate store, which is slow)."""
    if not _CTX:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE        # home machines use self-signed certificates
        _CTX.append(ctx)
    return _CTX[0]


def _get(url, headers, timeout):
    ctx = _https_context() if url.startswith("https:") else None
    req = urllib.request.Request(url, headers=dict(headers, **{"Accept": "application/json", "User-Agent": "RC"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise GlancesError("Glances wants a username and password (set them on the Glances tab).")
        if e.code == 404:
            raise GlancesError("That API version is not available.")
        raise GlancesError(f"Glances answered HTTP {e.code}.")
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.timeout) or "timed out" in str(reason):
            raise GlancesError("No answer (timed out). Is `glances -w` running, and is the port open in the firewall?")
        raise GlancesError(f"Could not connect ({reason}). Is `glances -w` running on that port?")
    except (socket.timeout, TimeoutError):
        raise GlancesError("No answer (timed out).")
    except (ConnectionError, OSError) as e:
        raise GlancesError(f"Could not connect ({e}).")
    except ValueError:
        raise GlancesError("That port answered, but not like Glances (the reply was not JSON).")


def detect(host, timeout=5):
    """Which API version this machine's Glances speaks. Returns (version, short description). Raises GlancesError."""
    headers, base, last = auth_header(host), base_url(host), None
    for v in API_VERSIONS:
        try:
            plugins = _get(f"{base}/api/{v}/pluginslist", headers, timeout)
        except GlancesError as e:
            last = e
            if "wants a username" in str(e) or "Could not connect" in str(e) or "No answer" in str(e):
                raise
            continue
        if not isinstance(plugins, list) or "cpu" not in plugins:
            raise GlancesError("That port answered, but it does not look like Glances.")
        try:
            system = _get(f"{base}/api/{v}/system", headers, timeout)
            name = (system.get("hostname") or "") if isinstance(system, dict) else ""
        except GlancesError:
            name = ""
        return v, (f"Glances API v{v}" + (f", host {name}" if name else ""))
    raise last or GlancesError("Glances was not found there.")


def fetch_all(host, version, timeout=8):
    data = _get(f"{base_url(host)}/api/{version}/all", auth_header(host), timeout)
    if not isinstance(data, dict) or "cpu" not in data:
        raise GlancesError("Glances answered with something unexpected.")
    return data


# ─── turning Glances' JSON into a Stats sample (pure) ────────────────────────

def parse_uptime(value):
    """'3 days, 4:05:06' / '4:05:06' / seconds -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"\s*(?:(\d+)\s+days?,\s*)?(\d+):(\d+):(\d+)", str(value or ""))
    if not m:
        return 0.0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _list(d, key):
    v = d.get(key)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _dict(d, key):
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def hottest(sensors):
    """(label, degrees C) of the hottest temperature sensor, or None."""
    temps = [(s.get("label", ""), _num(s.get("value"), None)) for s in sensors
             if s.get("unit") in ("C", "°C") and _num(s.get("value"), None) is not None and "fan" not in str(s.get("type", "")).lower()]
    temps = [t for t in temps if t[1] is not None and 0 < t[1] < 150]
    return max(temps, key=lambda t: t[1]) if temps else None


def sample_from_all(d, now=None):
    """The JSON of /api/N/all -> the same dict rc_monitor.parse_sample returns (plus cpu_pct / net_rates, which Glances has
    already worked out, and containers / sensors / glances extras)."""
    now = time.time() if now is None else now
    system, cpu, load = _dict(d, "system"), _dict(d, "cpu"), _dict(d, "load")
    mem, swap = _dict(d, "mem"), _dict(d, "memswap")
    percpu = _list(d, "percpu")
    cores = _int(load.get("cpucore") or cpu.get("cpucore") or len(percpu))
    cpu_pct = {"cpu": _num(cpu.get("total"))}
    for c in percpu:
        cpu_pct[f"cpu{_int(c.get('cpu_number'), len(cpu_pct) - 1)}"] = _num(c.get("total"))
    total = _int(mem.get("total"))
    avail = _int(mem.get("available"), total - _int(mem.get("used")))
    net, rates = {}, {}
    for n in _list(d, "network"):
        name = n.get("interface_name")
        if not name or name == "lo":
            continue
        net[name] = (_int(n.get("bytes_recv")), _int(n.get("bytes_sent")))
        if "bytes_recv_rate_per_sec" in n and "bytes_sent_rate_per_sec" in n:
            rates[name] = (_num(n["bytes_recv_rate_per_sec"]), _num(n["bytes_sent_rate_per_sec"]))
    disks = []
    for f in _list(d, "fs"):
        mount = f.get("mnt_point") or f.get("device_name")
        if not mount or str(f.get("fs_type", "")).lower() in ("tmpfs", "devtmpfs", "squashfs", "overlay"):
            continue
        size, used = _int(f.get("size")), _int(f.get("used"))
        disks.append({"mount": str(mount), "fs": str(f.get("device_name") or f.get("fs_type") or ""), "size": size, "used": used,
                      "avail": _int(f.get("free"), size - used), "pct": _int(f.get("percent"), round(100 * used / size) if size else 0)})
    procs = []
    for p in _list(d, "processlist"):
        pid = _int(p.get("pid"), -1)
        if pid < 0:
            continue
        mi = p.get("memory_info")
        rss = _int(mi.get("rss")) if isinstance(mi, dict) else (_int(mi[0]) if isinstance(mi, list) and mi else 0)
        cmd = p.get("cmdline")
        cmd = " ".join(str(c) for c in cmd) if isinstance(cmd, list) and cmd else str(p.get("name") or "")
        procs.append({"pid": pid, "user": str(p.get("username") or ""), "cpu": _num(p.get("cpu_percent")),
                      "mem": _num(p.get("memory_percent")), "rss": rss, "etime": "", "cmd": cmd or str(p.get("name") or ""),
                      "status": str(p.get("status") or ""), "threads": _int(p.get("num_threads"))})
    sensors = _list(d, "sensors")
    hot = hottest(sensors)
    os_name = str(system.get("os_name") or "")
    kernel = " ".join(x for x in (os_name, str(system.get("os_version") or "")) if x)
    return {"t": now,
            "os": {"kernel": kernel or str(system.get("platform") or ""), "hostname": str(system.get("hostname") or ""),
                   "pretty": str(system.get("hr_name") or system.get("linux_distro") or ""), "cores": cores},
            "uptime": parse_uptime(d.get("uptime")),
            "load": (_num(load.get("min1")), _num(load.get("min5")), _num(load.get("min15"))),
            "cpu": {}, "cpu_pct": cpu_pct,
            "mem": {"total": total, "used": _int(mem.get("used"), total - avail), "available": avail,
                    "swap_total": _int(swap.get("total")), "swap_used": _int(swap.get("used"))},
            "disks": sorted(disks, key=lambda x: x["mount"]), "net": net, "net_rates": rates,
            "procs": procs, "ports": [], "sensors": sensors, "hottest": hot,
            "containers": [c for c in _list(d, "containers")], "glances": {"system": system}}


def format_info(sample):
    """The System tab's text for a Glances sample."""
    o, g = sample["os"], sample["glances"]["system"]
    lines = ["SYSTEM", f"  Name:      {o['hostname']}", f"  System:    {o['pretty'] or o['kernel']}", f"  Platform:  {g.get('platform', '')} {g.get('os_name', '')} {g.get('os_version', '')}".rstrip(),
             f"  Cores:     {o['cores'] or '?'}", "", "NETWORK INTERFACES"]
    for name, (rx, tx) in sorted(sample["net"].items()):
        lines.append(f"  {name:<16} received {rx:>14,} bytes   sent {tx:>14,} bytes")
    if not sample["net"]:
        lines.append("  (none reported)")
    lines += ["", "SENSORS"]
    shown = [s for s in sample.get("sensors", []) if s.get("label")]
    for s in shown[:40]:
        lines.append(f"  {str(s.get('label')):<30} {s.get('value', '')} {s.get('unit', '')}   ({s.get('type', '')})")
    if not shown:
        lines.append("  (no sensors reported - they need Glances' sensors plugin and a supported machine)")
    lines += ["", "Source: Glances (read-only). Sending signals to processes and the Services list need an SSH connection."]
    return "\n".join(lines)
