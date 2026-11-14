#!/usr/bin/env python3
"""One-time migration of the edge Caddyfile from HTTP Basic auth to the form-login sidecar.

This is deliberately guarded: it refuses to touch anything unless the Caddyfile still contains the
exact Basic-auth block and the expected reverse_proxy target, so re-running it after the cutover
(when ``basic_auth`` is already gone) aborts rather than double-editing the live config.

Machine-specific values come from the environment; no secret is embedded.

    HERDR_EDGE_HOST      ssh target, ideally a ~/.ssh/config alias (default: ec2-herdr-tunnel)
    HERDR_CADDYFILE      remote Caddyfile path (default: /home/tiger/caddy-bin/Caddyfile)
    HERDR_CADDY_BIN      remote caddy binary (default: /home/tiger/caddy-bin/caddy)
    HERDR_CADDY_UNIT     systemd unit for the edge Caddy (default: herdr-web-relay)
"""
import os
import subprocess
import sys

HOST = ["ssh", os.environ.get("HERDR_EDGE_HOST", "ec2-herdr-tunnel")]
CADDYFILE = os.environ.get("HERDR_CADDYFILE", "/home/tiger/caddy-bin/Caddyfile")
CADDY_BIN = os.environ.get("HERDR_CADDY_BIN", "/home/tiger/caddy-bin/caddy")
CADDY_UNIT = os.environ.get("HERDR_CADDY_UNIT", "herdr-web-relay")

OLD_BASIC = "\t\tbasic_auth {"
PROXY_LINE = "\t\treverse_proxy 127.0.0.1:8376 {"


def main():
    old = subprocess.check_output(HOST + [f"cat {CADDYFILE}"], text=True)
    if OLD_BASIC not in old or PROXY_LINE not in old:
        raise SystemExit(
            "Unexpected Caddyfile (no Basic-auth block or no reverse_proxy target); "
            "refusing to overwrite it. If the form-login migration already ran, this is expected."
        )
    start = old.index(OLD_BASIC)
    end = old.index(PROXY_LINE, start)
    new = old[:start] + old[end:]
    new = new.replace(
        "\thandle {\n",
        "\thandle /login* {\n\t\treverse_proxy 127.0.0.1:8377\n\t}\n"
        "\thandle /logout {\n\t\treverse_proxy 127.0.0.1:8377\n\t}\n"
        "\thandle {\n\t\tforward_auth 127.0.0.1:8377 {\n\t\t\turi /verify\n\t\t}\n",
    )
    subprocess.run(HOST + [f"cp {CADDYFILE} {CADDYFILE}.before-form-login"], check=True)
    subprocess.run(HOST + ["umask 077; cat > %s.new" % CADDYFILE],
                   input=new, text=True, check=True)
    subprocess.run(HOST + [
        f"{CADDY_BIN} validate --config {CADDYFILE}.new --adapter caddyfile && "
        f"mv {CADDYFILE}.new {CADDYFILE} && "
        f"chmod 600 {CADDYFILE} && sudo systemctl restart {CADDY_UNIT} && "
        f"systemctl is-active {CADDY_UNIT} herdr-web-login",
    ], check=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode or 1)
