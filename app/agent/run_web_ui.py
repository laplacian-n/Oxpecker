"""Entry point: runs the Oxpecker web UI / API backend (agent/web/server.py).

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
        "agent.web.server:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
