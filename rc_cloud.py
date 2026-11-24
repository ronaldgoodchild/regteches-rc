"""REGTeches RC - WebDAV and S3 for the file manager (set "File transfer" to WebDAV, WebDAVS or S3 in Edit server > Connections).

* **WebDAV / WebDAVS** (Nextcloud, ownCloud, Synology / QNAP WebDAV, Apache, many NAS boxes): browse, upload, download, make
  folders, rename, copy, delete. Basic login with the server's username and password. Start folder: type it in the path box once
  (for Nextcloud: /remote.php/dav/files/<user>/); RC remembers it.
* **S3** (Amazon S3, MinIO, Wasabi, Backblaze B2, Ceph, many NAS boxes): the top level lists your buckets; folders are key prefixes.
  The username is the access key id and the password is the secret key. "IP address or DNS name" is the endpoint (s3.amazonaws.com,
  192.168.1.20 ...), "File port" 0 means 443 (choose "S3 (http)" for a server without TLS, e.g. MinIO on port 9000). The region is set on the Connections
  tab (blank = us-east-1). Path-style addresses are used, which every S3 server understands. Renaming a folder is not supported
  (S3 has no folders to rename); copying one works.

Like FTPS in RC, https certificates are not checked, so servers with self-signed certificates work.
Both classes offer the same methods as the SFTP / FTP backends (join, parent, home, listdir, stat, download, upload, mkdir, touch,
copy, ensure_dir, remove, rename, chmod, set_mtime, close). Signing and XML parsing are pure and tested without a server."""

import base64
import calendar
import hashlib
import hmac
import http.client
import os
import posixpath
import ssl
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit

import rc_manager as core

CLOUD_MODES = ("WebDAV", "WebDAVS", "S3", "S3 (http)")
CHUNK = 64 * 1024


class CloudError(OSError):
    """A readable server problem (shown to the user as is)."""


class CloudUnreachable(CloudError):
    """The server could not be reached at all (as opposed to answering with an error)."""


def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE        # home servers usually have self-signed certificates
    return ctx


class _Progress:
    """A file-like body that reports how much has been read (http.client reads it in blocks)."""

    def __init__(self, f, total, cb):
        self.f, self.total, self.cb, self.done = f, total, cb, 0

    def read(self, n=-1):
        data = self.f.read(CHUNK if n is None or n < 0 else min(n, CHUNK))
        self.done += len(data)
        if self.cb:
            self.cb(self.done, self.total)
        return data


class HttpBase:
    """One persistent HTTP(S) connection with a retry when the server closed it, used by both backends."""
    kind = "http"

    def __init__(self, host, port, tls, user, password, address=None):
        self.host, self.port, self.tls, self.user, self.password = host, int(port), bool(tls), user or "", password or ""
        self.address = address or host
        self.conn = None
        self.lock = threading.Lock()

    @staticmethod
    def join(base, name):
        return posixpath.join(base, name)

    @staticmethod
    def parent(path):
        return posixpath.dirname(path.rstrip("/")) or "/"

    def home(self):
        return "/"

    def _connect(self):
        if self.tls:
            self.conn = http.client.HTTPSConnection(self.address, self.port, timeout=30, context=_ctx())
        else:
            self.conn = http.client.HTTPConnection(self.address, self.port, timeout=30)

    def host_header(self):
        default = 443 if self.tls else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"

    def request(self, method, path, headers=None, body=None, expect=(200,), stream_to=None, progress=None, body_len=None):
        """Send one request. Returns (status, response headers, body bytes). `stream_to` is a binary file to write the response into."""
        headers = dict(headers or {})
        last = None
        for attempt in range(2):
            with self.lock:
                try:
                    if self.conn is None:
                        self._connect()
                    hdrs = self.prepare(method, path, dict(headers), body, body_len)
                    self.conn.request(method, self.target(path), body=body, headers=hdrs)
                    r = self.conn.getresponse()
                    data = b""
                    if stream_to is not None and r.status in expect:
                        total, done = int(r.getheader("Content-Length") or 0), 0
                        while True:
                            block = r.read(CHUNK)
                            if not block:
                                break
                            stream_to.write(block)
                            done += len(block)
                            if progress:
                                progress(done, total)
                    else:
                        data = r.read()
                    status, rh = r.status, {k.lower(): v for k, v in r.getheaders()}
                    if r.getheader("Connection", "").lower() == "close":
                        self.conn.close()
                        self.conn = None
                except (http.client.HTTPException, OSError) as e:
                    last = e
                    try:
                        if self.conn:
                            self.conn.close()
                    except Exception:
                        pass
                    self.conn = None
                    if isinstance(body, _Progress) or stream_to is not None or attempt == 1:
                        break                                    # a half-sent upload or half-written download is not repeated silently
                    continue
            if status not in expect:
                raise CloudError(self.explain(status, data, method, path))
            return status, rh, data
        raise CloudUnreachable(f"The server did not answer ({last}).")

    def explain(self, status, data, method, path):
        text = {401: "The server refused the login (wrong username or password).", 403: "Not allowed (403).",
                404: "Not found (404).", 405: "The server does not allow that here (405).", 409: "A parent folder is missing (409).",
                412: "Already exists (412).", 423: "Locked (423).", 507: "The server is full (507)."}.get(status, f"The server answered {status}.")
        return f"{text} [{method} {path}]"

    def close(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    # not available over HTTP
    def chmod(self, path, mode):
        raise OSError("This kind of server has no file permissions to change.")

    def set_mtime(self, path, mtime):
        pass


# ─── WebDAV ──────────────────────────────────────────────────────────────────

PROPFIND = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/>'
            '<d:getlastmodified/></d:prop></d:propfind>')


def parse_multistatus(xml_bytes):
    """PROPFIND answer -> [(href path (decoded), is_dir, size, mtime)] in the server's order."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise CloudError("The server's answer was not a WebDAV listing.")
    ns = "{DAV:}"
    out = []
    for resp in root.iter(ns + "response"):
        href = resp.findtext(ns + "href") or ""
        path = unquote(urlsplit(href).path or href)
        is_dir, size, mtime = False, 0, 0
        for ps in resp.iter(ns + "prop"):
            if ps.find(ns + "resourcetype/" + ns + "collection") is not None:
                is_dir = True
            length = ps.findtext(ns + "getcontentlength")
            if length and length.strip().isdigit():
                size = int(length)
            lm = ps.findtext(ns + "getlastmodified")
            if lm:
                try:
                    mtime = parsedate_to_datetime(lm).timestamp()
                except (TypeError, ValueError):
                    pass
            break
        out.append((path, is_dir, 0 if is_dir else size, mtime))
    return out


class DavBackend(HttpBase):
    kind = "webdav"

    def prepare(self, method, path, headers, body, body_len):
        if self.user:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode("utf-8")).decode()
        headers["Host"] = self.host_header()
        if body_len is not None:
            headers["Content-Length"] = str(body_len)
        elif isinstance(body, (bytes, bytearray)):
            headers["Content-Length"] = str(len(body))
        elif body is None and method in ("PUT", "POST", "PROPFIND", "MKCOL"):
            headers["Content-Length"] = "0"
        return headers

    def target(self, path):
        return quote(path if path.startswith("/") else "/" + path, safe="/~@:+,;=()!$&'*-._")

    def _propfind(self, path, depth):
        _s, _h, data = self.request("PROPFIND", path, {"Depth": str(depth), "Content-Type": "application/xml"}, PROPFIND.encode(), expect=(207, 200))
        return parse_multistatus(data)

    def listdir(self, path):
        base = path.rstrip("/") or "/"
        out = []
        for href, is_dir, size, mtime in self._propfind(path.rstrip("/") + "/" if path != "/" else "/", 1):
            h = href.rstrip("/") or "/"
            if h == base:
                continue                                          # the folder itself
            out.append((posixpath.basename(h), is_dir, size, mtime))
        return out

    def stat(self, path):
        try:
            entries = self._propfind(path, 0)
        except CloudError as e:
            if "404" in str(e):
                return None
            raise
        if not entries:
            return None
        _h, is_dir, size, mtime = entries[0]
        return is_dir, size, mtime, 0o755 if is_dir else 0o644

    def download(self, remote, local, progress=None):
        with open(local, "wb") as f:
            self.request("GET", remote, expect=(200,), stream_to=f, progress=progress)

    def upload(self, local, remote, progress=None):
        total = os.path.getsize(local)
        with open(local, "rb") as f:
            body = _Progress(f, total, progress)
            self.request("PUT", remote, {"Content-Type": "application/octet-stream"}, body, expect=(200, 201, 204), body_len=total)

    def mkdir(self, path):
        self.request("MKCOL", path, expect=(201, 200))

    def touch(self, path):
        self.request("PUT", path, body=b"", expect=(200, 201, 204))

    def ensure_dir(self, path):
        cur = ""
        for part in [p for p in path.split("/") if p]:
            cur += "/" + part
            if self.stat(cur) is None:
                self.mkdir(cur)

    def remove(self, path, is_dir):
        self.request("DELETE", path + ("/" if is_dir and not path.endswith("/") else ""), expect=(200, 204, 202, 404))

    def _dest(self, path):
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{self.host_header()}{self.target(path)}"

    def rename(self, old, new):
        self.request("MOVE", old, {"Destination": self._dest(new), "Overwrite": "F"}, expect=(201, 204))

    def copy(self, src, dst, is_dir):
        self.request("COPY", src, {"Destination": self._dest(dst), "Overwrite": "F", "Depth": "infinity"}, expect=(201, 204))


# ─── S3 ──────────────────────────────────────────────────────────────────────

def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def s3_quote(s):
    return quote(s, safe="-_.~")


def sigv4_headers(method, path, query, headers, payload_hash, access, secret, region, service, amzdate):
    """AWS Signature Version 4. `headers` must already contain host and x-amz-date (lower-case names); returns the Authorization
    header value. `path` is the already-encoded URI path, `query` a dict. (Checked against Amazon's published test vector.)"""
    canon_q = "&".join(f"{s3_quote(k)}={s3_quote(v)}" for k, v in sorted(query.items()))
    names = sorted(headers)
    canon_h = "".join(f"{n}:{' '.join(str(headers[n]).split())}\n" for n in names)
    signed = ";".join(names)
    canonical = "\n".join([method, path, canon_q, canon_h, signed, payload_hash])
    day = amzdate[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode("utf-8"), day), region), service), "aws4_request")
    sig = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={sig}"


def split_s3_path(path):
    """'/bucket/dir/file' -> ('bucket', 'dir/file'); '/' -> ('', '')."""
    parts = [p for p in path.split("/") if p]
    return (parts[0], "/".join(parts[1:])) if parts else ("", "")


def parse_s3_time(text):
    try:
        return calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def _tag(el):
    return el.tag.rsplit("}", 1)[-1]


def parse_s3_list(xml_bytes):
    """ListBuckets / ListObjectsV2 answer -> dict(items=[(name, is_dir, size, mtime)], next=token or '', truncated=bool)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise CloudError("The server's answer was not an S3 listing.")
    items, token, truncated = [], "", False
    for el in root.iter():
        t = _tag(el)
        kids = {_tag(k): (k.text or "") for k in el}
        if t == "Bucket" and "Name" in kids:
            items.append((kids["Name"], True, 0, parse_s3_time(kids.get("CreationDate", ""))))
        elif t == "Contents" and "Key" in kids:
            items.append((kids["Key"], False, int(kids.get("Size") or 0), parse_s3_time(kids.get("LastModified", ""))))
        elif t == "CommonPrefixes" and "Prefix" in kids:
            items.append((kids["Prefix"], True, 0, 0))
        elif t == "NextContinuationToken":
            token = el.text or ""
        elif t == "IsTruncated":
            truncated = (el.text or "").strip().lower() == "true"
    return {"items": items, "next": token, "truncated": truncated}


class S3Backend(HttpBase):
    kind = "s3"

    def __init__(self, host, port, tls, access, secret, region="", address=None):
        super().__init__(host, port, tls, access, secret, address)
        self.region = (region or "us-east-1").strip()

    def target(self, path):
        return path

    def prepare(self, method, path, headers, body, body_len):
        return headers

    def s3(self, method, bucket, key="", query=None, headers=None, body=None, expect=(200,), body_len=None, stream_to=None, progress=None, payload=None):
        """One signed S3 request."""
        query = dict(query or {})
        path = "/" + s3_quote(bucket) + ("/" + quote(key, safe="/-_.~") if key else "") if bucket else "/"
        amzdate = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if payload is None:
            payload = hashlib.sha256(body or b"").hexdigest() if isinstance(body, (bytes, bytearray)) or body is None else "UNSIGNED-PAYLOAD"
        h = {k.lower(): v for k, v in (headers or {}).items()}
        h.update({"host": self.host_header(), "x-amz-date": amzdate, "x-amz-content-sha256": payload})
        auth = sigv4_headers(method, path, query, h, payload, self.user, self.password, self.region, "s3", amzdate)
        send = {k: v for k, v in h.items() if k != "host"}
        send["Authorization"] = auth
        if body_len is not None:
            send["Content-Length"] = str(body_len)
        elif isinstance(body, (bytes, bytearray)):
            send["Content-Length"] = str(len(body))
        elif body is None and method in ("PUT", "POST"):
            send["Content-Length"] = "0"
        qs = "&".join(f"{s3_quote(k)}={s3_quote(v)}" for k, v in sorted(query.items()))
        send["Host"] = h["host"]
        return self.request(method, path + ("?" + qs if qs else ""), send, body, expect=expect, stream_to=stream_to, progress=progress, body_len=body_len)

    def explain(self, status, data, method, path):
        try:
            root = ET.fromstring(data)
            code = next((el.text for el in root.iter() if _tag(el) == "Code"), "")
            msg = next((el.text for el in root.iter() if _tag(el) == "Message"), "")
            if code:
                return f"{code}: {msg} [{method} {path.split('?')[0]}]"
        except ET.ParseError:
            pass
        return super().explain(status, data, method, path)

    def _list_all(self, bucket, prefix, delimiter="/"):
        items, token = [], ""
        while True:
            q = {"list-type": "2", "prefix": prefix, "delimiter": delimiter} if delimiter else {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            _s, _h, data = self.s3("GET", bucket, "", q)
            res = parse_s3_list(data)
            items += res["items"]
            if not (res["truncated"] and res["next"]):
                return items
            token = res["next"]

    def listdir(self, path):
        bucket, key = split_s3_path(path)
        if not bucket:
            _s, _h, data = self.s3("GET", "")
            return parse_s3_list(data)["items"]
        prefix = key + "/" if key else ""
        out = []
        for name, is_dir, size, mtime in self._list_all(bucket, prefix):
            short = name[len(prefix):].rstrip("/")
            if short and not (not is_dir and name == prefix):          # skip the folder's own placeholder object
                out.append((short, is_dir, size, mtime))
        return out

    def stat(self, path):
        bucket, key = split_s3_path(path)
        try:
            if not bucket:
                return True, 0, 0, 0o755
            if not key:
                self.s3("HEAD", bucket, expect=(200,))
                return True, 0, 0, 0o755
            try:
                _s, h, _d = self.s3("HEAD", bucket, key, expect=(200,))
                mtime = parsedate_to_datetime(h["last-modified"]).timestamp() if h.get("last-modified") else 0
                return False, int(h.get("content-length") or 0), mtime, 0o644
            except CloudError as e:
                if "404" not in str(e) and "NoSuchKey" not in str(e):
                    raise
            if self._list_all(bucket, key.rstrip("/") + "/")[:1]:
                return True, 0, 0, 0o755
            return None
        except CloudError as e:
            if "404" in str(e) or "NoSuchBucket" in str(e):
                return None
            raise

    def download(self, remote, local, progress=None):
        bucket, key = split_s3_path(remote)
        if not key:
            raise OSError("Choose a file inside a bucket.")
        with open(local, "wb") as f:
            self.s3("GET", bucket, key, expect=(200,), stream_to=f, progress=progress)

    def upload(self, local, remote, progress=None):
        bucket, key = split_s3_path(remote)
        if not key:
            raise OSError("Files go inside a bucket: open a bucket first.")
        total = os.path.getsize(local)
        with open(local, "rb") as f:
            payload = hashlib.sha256(f.read()).hexdigest() if not self.tls and total < 64 * 1024 * 1024 else "UNSIGNED-PAYLOAD"
            f.seek(0)
            self.s3("PUT", bucket, key, body=_Progress(f, total, progress), expect=(200,), body_len=total, payload=payload)

    def mkdir(self, path):
        bucket, key = split_s3_path(path)
        if not bucket:
            raise OSError("Open a bucket (or type its name in the path box) first.")
        if not key:
            self.s3("PUT", bucket, expect=(200,))                       # a new bucket
        else:
            self.s3("PUT", bucket, key.rstrip("/") + "/", body=b"", expect=(200,))

    def touch(self, path):
        bucket, key = split_s3_path(path)
        if not key:
            raise OSError("Files go inside a bucket.")
        self.s3("PUT", bucket, key, body=b"", expect=(200,))

    def ensure_dir(self, path):
        pass                                                           # prefixes exist as soon as an object is put under them

    def remove(self, path, is_dir):
        bucket, key = split_s3_path(path)
        if not bucket:
            raise OSError("Nothing to delete here.")
        if not key:
            self.s3("DELETE", bucket, expect=(204, 200))                # an empty bucket
        elif is_dir:
            self.s3("DELETE", bucket, key.rstrip("/") + "/", expect=(204, 200))
        else:
            self.s3("DELETE", bucket, key, expect=(204, 200))

    def _copy_object(self, sb, sk, db, dk):
        src = "/" + s3_quote(sb) + "/" + quote(sk, safe="/-_.~")
        self.s3("PUT", db, dk, headers={"x-amz-copy-source": src}, body=b"", expect=(200,))

    def copy(self, src, dst, is_dir):
        sb, sk = split_s3_path(src)
        db, dk = split_s3_path(dst)
        if not sk or not dk:
            raise OSError("Copy files and folders inside a bucket (buckets themselves cannot be copied).")
        if not is_dir:
            self._copy_object(sb, sk, db, dk)
            return
        prefix = sk.rstrip("/") + "/"
        found = False
        for name, d, _size, _mt in self._list_all(sb, prefix, delimiter=""):
            found = True
            self._copy_object(sb, name, db, dk.rstrip("/") + "/" + name[len(prefix):])
        if not found:
            self.s3("PUT", db, dk.rstrip("/") + "/", body=b"", expect=(200,))

    def rename(self, old, new):
        sb, sk = split_s3_path(old)
        db, dk = split_s3_path(new)
        if not sk or not dk:
            raise OSError("Buckets cannot be renamed.")
        st = self.stat(old)
        if st and st[0]:
            raise OSError("S3 cannot rename a folder (it only has key prefixes). Copy it, then delete the old one.")
        self._copy_object(sb, sk, db, dk)
        self.s3("DELETE", sb, sk, expect=(204, 200))


# ─── opening ─────────────────────────────────────────────────────────────────

def open_backend(host, port, user, password):
    """The backend for a server whose File transfer is WebDAV, WebDAVS or S3. Checks the login by reading the top folder."""
    mode = host.get("file_mode")
    addr = core.resolve_address(host, port)
    if mode in ("S3", "S3 (http)"):
        be = S3Backend(host["address"], port, mode == "S3", user, password, host.get("s3_region", ""), address=addr)
    elif mode in ("WebDAV", "WebDAVS"):
        be = DavBackend(host["address"], port, mode == "WebDAVS", user, password, address=addr)
    else:
        raise ValueError("not a WebDAV / S3 server")
    try:
        be.listdir("/")
    except CloudUnreachable:
        be.close()
        raise
    except CloudError as e:
        # Only a refused login stops the connection: a server may forbid listing its top level while the folders below work.
        if not any(w in str(e) for w in ("refused the login", "SignatureDoesNotMatch", "InvalidAccessKeyId")):
            return be
        be.close()
        raise
    except Exception:
        be.close()
        raise
    return be
