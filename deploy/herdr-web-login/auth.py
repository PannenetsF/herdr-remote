#!/usr/bin/env python3
"""Small session-auth sidecar for the Caddy edge; stdlib only."""
import hashlib
import hmac
import http.cookies
import http.server
import ipaddress
import json
import os
import secrets
import threading
import time
import urllib.parse

PASSWORD_HASH = bytes.fromhex(os.environ["HERDR_LOGIN_PASSWORD_SHA256"])
USERNAME = os.environ.get("HERDR_LOGIN_USERNAME", "fanyun")
COOKIE_NAME = "herdr_web_session"
SESSION_TTL = 30 * 24 * 60 * 60
SESSION_KEY = secrets.token_bytes(32)
MAX_BODY = 4096
WINDOW = 10 * 60
MAX_FAILURES = 10
ATTEMPTS = {}
ATTEMPTS_LOCK = threading.Lock()

PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#101512">
  <title>Sign in · Herdr</title>
  <style>
    :root { color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #101512; color: #e8ede9; }
    main { width: 100%; max-width: 360px; }
    .mark { font: 600 28px ui-monospace, SFMono-Regular, Menlo, monospace; margin-bottom: 12px; }
    h1 { font-size: 22px; font-weight: 600; margin: 0 0 8px; }
    p { color: #a8b4ac; font-size: 14px; line-height: 1.6; margin: 0 0 28px; }
    form { display: grid; gap: 18px; }
    label { display: grid; gap: 8px; font-size: 13px; color: #bdc8c0; }
    input { font: inherit; font-size: 16px; width: 100%; padding: 12px; color: #edf5ef; background: #1a231d; border: 1px solid #405047; border-radius: 7px; }
    input:focus { outline: 2px solid #a7d4b3; outline-offset: 2px; }
    button { font: inherit; font-weight: 600; padding: 12px; color: #102015; background: #b4e3bf; border: 0; border-radius: 7px; cursor: pointer; margin-top: 4px; }
    .error { color: #ffb4a8; padding: 10px 12px; background: #3c2421; border-radius: 7px; }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <main>
    <div class="mark">↳ herdr</div>
    <h1>Sign in to your workspace</h1>
    <p>Use your personal web password to continue.</p>
    <form method="post" action="/login" autocomplete="on">
      <input type="hidden" name="next" value="__NEXT__">
      <label>Username
        <input name="username" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus>
      </label>
      <label>Password
        <input name="password" type="password" autocomplete="current-password" required>
      </label>
      <p class="error" role="alert" __ERROR_HIDDEN__>The username or password is incorrect.</p>
      <button type="submit">Sign in</button>
    </form>
  </main>
</body>
</html>"""


def login_page(next_path="/", error=False):
    # next_path is a local absolute path and cannot contain markup after validation.
    return (PAGE.replace("__NEXT__", next_path)
            .replace("__ERROR_HIDDEN__", "" if error else "hidden")).encode()


def sign(username, expires):
    payload = f"{username}:{expires}".encode()
    return f"{expires}.{hmac.new(SESSION_KEY, payload, hashlib.sha256).hexdigest()}"


def session_valid(cookie):
    try:
        expires_text, signature = cookie.split(".", 1)
        expires = int(expires_text)
        if expires < int(time.time()):
            return False
        return hmac.compare_digest(signature, sign(USERNAME, expires).split(".", 1)[1])
    except (ValueError, TypeError):
        return False


def session_cookie(headers):
    try:
        cookies = http.cookies.SimpleCookie(headers.get("Cookie", ""))
        return session_valid(cookies[COOKIE_NAME].value)
    except (KeyError, http.cookies.CookieError):
        return False


def request_host(headers):
    """Bare host (no port) from the Host header; Caddy preserves the original vhost."""
    raw = (headers.get("Host") or "").strip()
    if not raw:
        return ""
    # Drop an IPv6 literal's brackets and any trailing port.
    if raw.startswith("["):
        return raw[1:].split("]", 1)[0]
    return raw.split(":", 1)[0]


def plain_http_ip_origin(headers):
    """True only for a direct, literal-IP HTTP origin (e.g. http://[2001:db8::1]/).

    The DNS domain stays HTTPS and keeps `Secure`. A bare IP cannot get a public CA certificate,
    so its explicit HTTP origin needs a non-Secure cookie or the browser will never send it back;
    HttpOnly + SameSite=Lax and the signed/HMAC value still protect it. Hostnames are never
    downgraded, so a plain-HTTP Host header on the domain cannot strip Secure.
    """
    host = request_host(headers)
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Loopback is a browser secure context even over HTTP, so keep Secure there. Only a remote
    # literal IP reached over plain HTTP needs the Secure attribute dropped.
    if addr.is_loopback:
        return False
    proto = (headers.get("X-Forwarded-Proto", "") or "").strip().lower()
    return proto != "https"


def session_cookie_attrs(headers):
    if plain_http_ip_origin(headers):
        return "Path=/; HttpOnly; SameSite=Lax"
    return "Path=/; HttpOnly; Secure; SameSite=Lax"


def allow_attempt(address):
    now = time.monotonic()
    with ATTEMPTS_LOCK:
        recent = [t for t in ATTEMPTS.get(address, []) if now - t < WINDOW]
        if len(recent) >= MAX_FAILURES:
            ATTEMPTS[address] = recent
            return False
        recent.append(now)
        ATTEMPTS[address] = recent
        if len(ATTEMPTS) > 1024:
            cutoff = now - WINDOW
            for key in list(ATTEMPTS):
                ATTEMPTS[key] = [t for t in ATTEMPTS[key] if t > cutoff]
                if not ATTEMPTS[key]:
                    del ATTEMPTS[key]
        return True


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HerdrLogin"

    def log_message(self, format, *args):
        # Never log query strings, form bodies, cookies, or credentials.
        print(f"{self.address_string()} {format % args}", flush=True)

    def reply(self, status, body=b"", content_type="text/plain; charset=utf-8", extra=()):
        # Login bodies are bounded and never pipelined: a rejected request must not leave
        # unread bytes for BaseHTTPRequestHandler to interpret as another request.
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location, extra=()):
        self.reply(303, extra=(("Location", location), *extra))

    def safe_location(self, value):
        if not value.startswith("/") or value.startswith("//") or "\\" in value:
            return "/"
        if any(ord(char) < 32 or char in "\"'<>" for char in value):
            return "/"
        return value

    def safe_next(self, query):
        value = urllib.parse.parse_qs(query).get("next", ["/"])[0]
        return self.safe_location(value)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            self.reply(200, b"ok\n")
            return
        if path == "/verify":
            if session_cookie(self.headers):
                self.reply(200)
            else:
                # Caddy forward_auth copies this redirect to the original request,
                # including WebSocket handshakes that lack the session cookie.
                original = self.safe_location(self.headers.get("X-Forwarded-Uri", "/"))
                self.redirect("/login?" + urllib.parse.urlencode({"next": original}))
            return
        if path == "/login":
            self.reply(200, login_page(self.safe_next(query)), "text/html; charset=utf-8")
            return
        if path == "/logout":
            self.redirect("/login", (("Set-Cookie",
                f"{COOKIE_NAME}=; {session_cookie_attrs(self.headers)}; Max-Age=0"),))
            return
        self.reply(404, b"not found\n")

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path != "/login":
            self.reply(404, b"not found\n")
            return
        next_path = self.safe_next(query)
        if not allow_attempt(self.client_address[0]):
            self.reply(429, b"Too many sign-in attempts. Try again in 10 minutes.\n", extra=(("Retry-After", "600"),))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self.reply(400, b"invalid request\n")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self.reply(415, b"unsupported form encoding\n")
            return
        try:
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=4)
            username = form.get("username", [""])[0]
            password = form.get("password", [""])[0].encode()
        except (UnicodeDecodeError, ValueError):
            self.reply(400, b"invalid form\n")
            return
        valid_user = hmac.compare_digest(username.encode(), USERNAME.encode())
        valid_password = hmac.compare_digest(hashlib.sha256(password).digest(), PASSWORD_HASH)
        password = b""
        if not (valid_user and valid_password):
            self.reply(401, login_page(next_path, error=True), "text/html; charset=utf-8")
            return
        with ATTEMPTS_LOCK:
            ATTEMPTS.pop(self.client_address[0], None)
        expires = int(time.time()) + SESSION_TTL
        cookie = f"{COOKIE_NAME}={sign(USERNAME, expires)}; {session_cookie_attrs(self.headers)}; Max-Age={SESSION_TTL}"
        self.redirect(next_path, (("Set-Cookie", cookie),))


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8377), Handler)
    server.daemon_threads = True
    server.serve_forever()
