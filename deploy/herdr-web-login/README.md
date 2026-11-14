# herdr web login — edge form-auth sidecar

A small, dependency-free HTTP service that puts an HTML sign-in page and a session cookie in front
of the herdr relay at the public edge. Caddy's `forward_auth` delegates every request (HTTP **and**
WebSocket) to this sidecar; only requests carrying a valid signed cookie reach the relay.

## Why this exists

A bare relay behind a public entry needs authentication, but Basic auth gives a browser a native
modal with no logout, no password-manager-friendly form, and no per-origin cookie policy. This
sidecar provides a real login form, an HMAC-signed session cookie, rate-limited guessing, and an
origin-aware `Secure` attribute so the same service works behind both an HTTPS domain and a
direct, literal-IP HTTP origin.

## Components

| file | role |
|---|---|
| `auth.py` | the sidecar. Python 3 stdlib only; binds `127.0.0.1:8377`. Run with `python3 -I auth.py`. |
| `test_auth.py` | 11 stdlib `unittest` cases; no network or credentials required beyond a test hash in the file. |
| `deploy-sidecar.sh` | installs/refreshes `auth.py`, hashes the password on the edge, and installs the systemd unit. |
| `deploy-caddy.py` | one-time, guarded migration of the edge Caddyfile from Basic auth to `forward_auth`. |

## Request path

```
browser ──► Caddy :80 (herdr-web-relay)
              /login*, /logout  ── reverse_proxy ──► sidecar 127.0.0.1:8377
              everything else  ── forward_auth /verify (sidecar)
                                   └─ 200: reverse_proxy ──► ssh -R 127.0.0.1:8376 ──► relay 127.0.0.1:8375
                                   └─ 303: redirect to /login (including unauthenticated WS handshakes)
```

The relay stays on loopback and is reached only through the reverse tunnel. Caddy injects the
relay's bearer token on the proxy hop; the sidecar never sees it and does not store it.

## Credentials and cookie policy

- The sidecar reads only a **SHA-256 hash** from `HERDR_LOGIN_PASSWORD_SHA256` and an optional
  `HERDR_LOGIN_USERNAME`. Both arrive through an `EnvironmentFile` that is `0600` on the edge;
  the plaintext password is generated once locally, shipped, hashed, then deleted on the edge.
- The session cookie `herdr_web_session` is `expires.HMAC(username:expires)` with a process-local
  random key, and carries `HttpOnly; SameSite=Lax`, plus `Secure` everywhere **except** a remote,
  literal-IP origin reached over plain HTTP. A bare IP cannot get a public CA certificate, so that
  one origin needs a non-Secure cookie or the browser would never return it; hostnames (the HTTPS
  domain) always keep `Secure`, so a plain-HTTP `Host` header on the domain cannot strip it.
  Loopback keeps `Secure` (it is a browser secure context even over HTTP).
- Sessions last 30 days. Failed sign-ins are capped at 10 per source IP per 10 minutes (HTTP 429).
- The sidecar never logs query strings, form bodies, cookies or credentials.

## Deploy

Configure an ssh alias for the edge (the default target is `ec2-herdr-tunnel`), then:

```sh
# First install / later refresh the sidecar and its unit.
HERDR_EDGE_HOST=ec2-herdr-tunnel ./deploy-sidecar.sh

# One-time Caddy cutover; refuses unless the old Basic-auth block is still present.
HERDR_EDGE_HOST=ec2-herdr-tunnel ./deploy-caddy.py
```

Override the remote paths/unit with `HERDR_CADDYFILE`, `HERDR_CADDY_BIN`, `HERDR_CADDY_UNIT` and
the local password location with `HERDR_LOGIN_PASSWORD_FILE` if your layout differs. No value in
this directory is a secret; nothing here should be edited to embed one.

## Health checks

```sh
ssh "$HERDR_EDGE_HOST" '
  systemctl is-active herdr-web-login herdr-web-relay
  curl -sS -o /dev/null -w "login=%{http_code}\n"  http://127.0.0.1:8377/login    # 200
  curl -sS -o /dev/null -w "verify=%{http_code}\n" http://127.0.0.1:8377/verify   # 303 (no cookie)
'
curl -sS -o /dev/null -w "healthz=%{http_code}\n" "https://<domain>/healthz"       # 200
```

A correct login returns `303` with a `Set-Cookie`; a wrong password returns the form with `401`
and no cookie.

## Rollback

- Caddy: the migration copies the prior config to `Caddyfile.before-form-login` on the edge.
  Restore it, `caddy validate`, then `systemctl restart herdr-web-relay`.
- Sidecar: `sudo systemctl disable --now herdr-web-login` and remove the unit; with Caddy rolled
  back to Basic auth the sidecar is no longer referenced.
- Password: rotate by replacing the local password file and re-running `deploy-sidecar.sh`; the
  hash is regenerated and the unit restarted. Existing cookies are invalidated by a sidecar
  restart because the HMAC key is process-local and regenerated each start.

## Tests

```sh
python3 -m unittest test_auth -v
```

Covers the form fields, wrong-password handling, the secure cookie on the domain, the literal-IP
`Secure` downgrade (and that the signature still authenticates), loopback keeping `Secure`,
tampered/expired cookies, external-redirect rejection, logout, and the rate limit.
