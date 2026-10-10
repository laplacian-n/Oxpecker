"""Entry point: runs the Oxpecker web UI / API backend (agent/web/dev_server.py).

This launches `agent.web.dev_server`, NOT `agent.web.server`. The two FastAPI apps serve the same
static frontend, but only dev_server carries the security spine this project is built on — broker
mediation on every tool call, the hash-chained audit log, taint escalation and injection
screening. `agent.web.server` is the stripped early scaffold (no audit log, no taint, no injection
screening — three of the four §2.6.1 components a tier may never remove); running it in production
would serve the UI with those protections absent while the docs claim they are present. So the
entry point everything in deploy/ goes through must point here.

This module is a thin alias for `python -m agent.web.dev_server`: it delegates to that module's
`main()`, which is the ONLY launcher that initialises the model provider (`_llm`), loads persisted
state, and enforces the non-loopback bind-safety check. (The earlier version of this file called
`uvicorn.run("agent.web.dev_server:app", ...)` directly and never built `_llm`, so the UI came up
with no working model — launching through `main()` is what fixes that.) Every flag `dev_server`
accepts is accepted here, unchanged:

    python -m agent.run_web_ui --provider openrouter --model <id> --host 0.0.0.0 --port 8765
    python -m agent.run_web_ui                        # loopback, local llama-server at :8080

Loopback-only by default. For a non-loopback bind (`--host 0.0.0.0`, to accept Electron/browser
clients on the LAN) create an API key file (state/web_ui_api_key.txt) and set
AGENT_WEB_CORS_ORIGINS if a client loads the page from another origin — dev_server refuses an
unauthenticated non-loopback bind unless `--insecure-no-auth` is passed. For a GPU-less host, use
`--provider openrouter` with $OPENROUTER_API_KEY instead of a local llama-server (see
docs/DEPLOY_UBUNTU.md).
"""
from __future__ import annotations

from .web import dev_server


def main() -> None:
    # Delegate entirely: dev_server.main() parses sys.argv, so every flag passes straight through.
    dev_server.main()


if __name__ == "__main__":
    main()
