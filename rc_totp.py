"""REGTeches RC - authenticator codes for two-step SSH logins (Edit server > Login > Authenticator secret).

Servers that use Google Authenticator / `pam_google_authenticator` (or any TOTP, RFC 6238) ask for a 6-digit code after the password.
With the secret saved for a server, RC types the current code for you in two places:
  * during the login itself (SSH "keyboard-interactive": a password prompt gets the password, a code prompt gets the code), and
  * later, when a terminal shows a code prompt such as "Verification code:" (sudo or a second step), once per prompt.
Login scripts can also use ${totp}. The secret is stored like a saved password (encrypted for your Windows account only) and is never
shown again. Anyone who has the secret can make your codes, so it is treated exactly like a password in backups and sync.
Everything here is pure and tested without a server except `authenticate`, which needs an SSH transport."""

import base64
import binascii
import hashlib
import hmac
import re
import struct
import time
from urllib.parse import parse_qs, urlparse

STEP = 30
DIGITS = 6

# "Verification code:", "Authenticator code:", "One-time password:", "OTP:", "2FA code:", "Token code:" ... at the end of the output
CODE_PROMPT = re.compile(r"(verification code|authenticator|one.?time|\botp\b|\b2fa\b|two.?factor|token code|security code|mfa code)[^:?\n]{0,30}[:?]\s*$", re.I)


def clean_secret(text):
    """What the user pasted -> a normalised base32 secret. Accepts the plain key (with spaces), or an otpauth://totp/... link.
    Raises ValueError with a readable message."""
    t = (text or "").strip()
    if t.lower().startswith("otpauth://"):
        q = parse_qs(urlparse(t).query)
        t = (q.get("secret") or [""])[0]
    t = re.sub(r"[\s-]+", "", t).upper().rstrip("=")
    if not t:
        raise ValueError("The authenticator secret is empty.")
    if not re.fullmatch(r"[A-Z2-7]+", t):
        raise ValueError("The authenticator secret should be letters A-Z and digits 2-7 (the key shown when you set up the authenticator).")
    if len(t) < 16:
        raise ValueError("That secret is too short to be an authenticator key.")
    try:
        base64.b32decode(t + "=" * (-len(t) % 8))
    except (binascii.Error, ValueError):
        raise ValueError("That is not a valid authenticator secret.")
    return t


def code(secret, now=None, digits=DIGITS, step=STEP):
    """The TOTP code (RFC 6238, HMAC-SHA1) for `secret` at time `now`."""
    key = base64.b32decode(clean_secret(secret) + "=" * (-len(clean_secret(secret)) % 8))
    counter = int((time.time() if now is None else now) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def seconds_left(now=None, step=STEP):
    return step - int((time.time() if now is None else now) % step)


def fresh_code(secret, now=None):
    """A code that will still be valid when it arrives: if the current one expires within 2 seconds, wait for the next."""
    now = time.time() if now is None else now
    if seconds_left(now) <= 2:
        return code(secret, now + 3)
    return code(secret, now)


def is_code_prompt(text):
    """Does the end of this terminal output look like a code prompt?"""
    lines = (text or "")[-300:].replace(chr(13), chr(10)).rstrip(" " + chr(10)).splitlines()
    return bool(lines and CODE_PROMPT.search(lines[-1]))


def make_handler(password, secret, now=None):
    """The callback for paramiko's keyboard-interactive login: answers each prompt with the password or the current code."""
    def handler(title, instructions, prompts):
        out = []
        for text, _echo in prompts:
            if CODE_PROMPT.search(text.strip() + " ") or re.search(r"\bcode\b", text, re.I):
                out.append(fresh_code(secret, now))
            elif re.search(r"pass", text, re.I):
                out.append(password or "")
            else:
                out.append("")
        return out
    return handler


def authenticate(transport, user, password, secret, now=None):
    """Log in on an open (not yet authenticated) paramiko Transport: keyboard-interactive with the password and the code; if the server
    wants the password by itself first, that is sent the normal way. Raises paramiko.AuthenticationException when refused."""
    import paramiko
    handler = make_handler(password, secret, now)
    try:
        remaining = transport.auth_interactive(user, handler)
    except paramiko.BadAuthenticationType as e:
        remaining = None
        allowed = list(e.allowed_types or [])
        if "password" in allowed and password:
            rest = transport.auth_password(user, password)
            if transport.is_authenticated():
                return
            if rest and "keyboard-interactive" in rest:
                remaining = transport.auth_interactive(user, handler)
            elif not rest:
                return
            else:
                raise paramiko.AuthenticationException("The server did not offer a second step I can answer.")
        else:
            raise
    while not transport.is_authenticated() and remaining:
        if "keyboard-interactive" in remaining:
            remaining = transport.auth_interactive(user, handler)
        elif "password" in remaining and password:
            remaining = transport.auth_password(user, password)
        else:
            break
    if not transport.is_authenticated():
        raise paramiko.AuthenticationException("The server refused the password or the authenticator code.")
