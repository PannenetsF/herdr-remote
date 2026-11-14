#!/usr/bin/env bash
set -euo pipefail

# Install or refresh the HTML login sidecar on the edge host.
#
# Everything machine-specific arrives through environment variables; no secret is written into
# this script. The only credential is the login password, which is generated once into a 0600
# local file and shipped to the edge as a salt-less SHA-256 hash -- the plaintext never lands in
# the remote working tree (it is read, hashed, then removed).
#
#   HERDR_EDGE_HOST            ssh target, ideally a ~/.ssh/config alias (default: ec2-herdr-tunnel)
#   HERDR_EDGE_DIR             directory under the remote home for the sidecar (default: herdr-web-auth)
#   HERDR_LOGIN_PASSWORD_FILE  local 0600 file holding the plaintext password
#   HERDR_LOGIN_USERNAME       expected login username (default: fanyun)
#
# Re-runnable: an existing password file is kept, auth.py is re-copied, and the unit is re-installed
# and restarted. The edge sidecar binds 127.0.0.1:8377 and is reached only through Caddy.

EDGE="${HERDR_EDGE_HOST:-ec2-herdr-tunnel}"
REMOTE_DIR="${HERDR_EDGE_DIR:-herdr-web-auth}"
CREDENTIAL_FILE="${HERDR_LOGIN_PASSWORD_FILE:-$HOME/.config/herdr-remote/web-login-password}"
USERNAME="${HERDR_LOGIN_USERNAME:-fanyun}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$REMOTE_DIR" in
  *[!A-Za-z0-9._-]* | "") echo "HERDR_EDGE_DIR must be a single safe path component: $REMOTE_DIR" >&2; exit 2;;
esac

# Generate the password once, locally, with the file mode fixed at creation.
if [ ! -s "$CREDENTIAL_FILE" ]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$CREDENTIAL_FILE")
fi
chmod 600 "$CREDENTIAL_FILE"

# Discover the remote identity and home rather than hardcoding a username or path.
REMOTE_HOME="$(ssh "$EDGE" 'printf "%s" "$HOME"')"
REMOTE_PATH="$REMOTE_HOME/$REMOTE_DIR"
ssh "$EDGE" "mkdir -p $(printf '%q' "$REMOTE_PATH")"
scp -q "$SCRIPT_DIR/auth.py" "$EDGE:$REMOTE_PATH/auth.py"

# Stream the plaintext straight into a 0600 remote temp file; it never sits in a command argument.
ssh "$EDGE" "umask 077; cat > $(printf '%q' "$REMOTE_PATH")/.new-password" < "$CREDENTIAL_FILE"

# All values arrive as positional args after `bash -s --`, so nothing is interpolated into a shell
# string and a path containing spaces would still be handled correctly.
ssh "$EDGE" bash -s -- "$REMOTE_PATH" "$USERNAME" <<'REMOTE'
set -euo pipefail
remote_path=$1
username=$2
umask 077
python3 -c 'import hashlib, os, pathlib, sys
p = pathlib.Path(sys.argv[1])
password = p.joinpath(".new-password").read_bytes().strip()
p.joinpath("login.env").write_text(
    "HERDR_LOGIN_PASSWORD_SHA256=" + hashlib.sha256(password).hexdigest() + "\n"
    + "HERDR_LOGIN_USERNAME=" + sys.argv[2] + "\n")' "$remote_path" "$username"
rm "$remote_path/.new-password"
chmod 600 "$remote_path/login.env"
unit="$(mktemp)"
cat > "$unit" <<EOF
[Unit]
Description=Form login sidecar for herdr web relay
After=network.target

[Service]
Type=simple
User=$(id -un)
EnvironmentFile=$remote_path/login.env
ExecStart=/usr/bin/python3 -I $remote_path/auth.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
RestrictAddressFamilies=AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$unit" /etc/systemd/system/herdr-web-login.service
rm "$unit"
sudo systemctl daemon-reload
sudo systemctl enable --now herdr-web-login
systemctl is-active herdr-web-login
curl -sS -o /dev/null -w 'login=%{http_code}\n' http://127.0.0.1:8377/login
curl -sS -o /dev/null -w 'verify=%{http_code}\n' http://127.0.0.1:8377/verify
REMOTE
