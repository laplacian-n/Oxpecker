"""Entry point: runs the Oxpecker web UI / API backend (agent/web/dev_server.py).

This launches `agent.web.dev_server`, NOT `agent.web.server`. The two FastAPI apps serve the same
static frontend, but only dev_server carries the security spine this project is built on — broker
mediation on every tool call, the hash-chained audit log, taint escalation and injection
screening. `agent.web.server` is the stripped early scaffold (no audit log, no taint, no injection
screening — three of the four §2.6.1 components a tier may never remove); running it in production
would serve the UI with those protections absent while the docs claim they are present. So the
entry point everything in deploy/ goes through must point here. (server.py is retired once
dev_server is fully graph-backed; until then it stays only for its own tests.)

Loopback-only by default. Pass --host 0.0.0.0 to accept connections from Electron clients on
the LAN — when doing so, also set AGENT_WEB_CORS_ORIGINS and create an API key file
(state/web_ui_api_key.txt).
"""
from __future__ import annotations

import argparse

import uvicorn

from . import config


def main() -> None:
    parser = argparse.ArgumentParser(description="Oxpecker Agent API server")
    parser.add_argument("--host", default=config.WEB_UI_HOST,
                        help=f"bind address (default: {config.WEB_UI_HOST})")
    parser.add_argument("--port", type=int, default=config.WEB_UI_PORT,
                        help=f"bind port (default: {config.WEB_UI_PORT})")
    parser.add_argument("--cors", nargs="*", default=None,
                        help="allowed CORS origins (overrides AGENT_WEB_CORS_ORIGINS)")
    args = parser.parse_args()

    if args.cors is not None:
        config.WEB_UI_CORS_ORIGINS = args.cors

    if args.host != "127.0.0.1" and not config.WEB_UI_API_KEY_FILE.exists():
        print("[!] Binding to a non-loopback address without an API key.")
        print(f"    Create one:  echo 'your-secret' > {config.WEB_UI_API_KEY_FILE}")
        print("    Continuing without auth — only safe on a trusted network.\n")

    uvicorn.run(
        "agent.web.dev_server:app",  # the security-complete app — see the module docstring
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
