"""Phase 6 — self-hosted out-of-band (OOB) interaction catcher, correlation-token-based
validation. **Not source-IP scope-gating** — this is the integration prompt's own non-negotiable
correction: a resolver, proxy, or CDN may be the actual network caller reaching this listener,
not the target itself, so an interaction is proven genuine by knowing the unguessable token
issued for it, never by which IP address made the request. Source IP is still recorded, purely
as metadata for the operator to look at — it is never consulted by `is_valid_interaction()` or
`wait_for_interaction()`.

Bound to 127.0.0.1 only, by construction (`_HOST` below, no parameter to change it): this
project's lab targets (Juice Shop, DVWA) are themselves loopback-bound Docker containers, so a
listener only needs to be reachable from there — nothing about this milestone needs public
internet exposure the way a real red-team OOB service would, keeping it inside this project's
existing lab-only posture rather than opening a new one.

stdlib-only (`http.server`) — no new dependency, no host-wide install.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import config

_HOST = "127.0.0.1"
INTERACTIONS_DIR = config.STATE_DIR / "oob" / "interactions"


class _Handler(BaseHTTPRequestHandler):
    server: "OOBServer"  # type: ignore[assignment]

    def _handle(self, method: str) -> None:
        token = self.path.strip("/").split("/")[-1] if self.path.strip("/") else ""
        known = self.server.known_tokens()  # type: ignore[attr-defined]
        if token in known:
            self.server.record_interaction(  # type: ignore[attr-defined]
                token,
                {
                    "method": method,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "source_addr": self.client_address[0],  # recorded, never used to validate
                    "received_at": time.time(),
                },
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):  # silence default stderr access logging
        pass


class OOBServer:
    def __init__(self, interactions_dir: Path | None = None):
        self._dir = interactions_dir if interactions_dir is not None else INTERACTIONS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._tokens: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> int:
        self._httpd = ThreadingHTTPServer((_HOST, 0), _Handler)
        self._httpd.record_interaction = self._record_interaction  # type: ignore[attr-defined]
        self._httpd.known_tokens = self._known_tokens  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self._httpd.server_address[1]

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def issue_token(self) -> tuple[str, str]:
        token = secrets.token_hex(16)
        with self._lock:
            self._tokens.add(token)
        (self._dir / f"{token}.json").write_text(json.dumps([]))
        return token, f"http://{_HOST}:{self.port}/oob/{token}"

    def _known_tokens(self) -> set[str]:
        with self._lock:
            return set(self._tokens)

    def _record_interaction(self, token: str, interaction: dict) -> None:
        path = self._dir / f"{token}.json"
        try:
            existing = json.loads(path.read_text()) if path.exists() else []
        except json.JSONDecodeError:
            existing = []
        existing.append(interaction)
        path.write_text(json.dumps(existing))

    def get_interactions(self, token: str) -> list[dict]:
        path = self._dir / f"{token}.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return []

    def is_valid_interaction(self, token: str) -> bool:
        """True iff at least one interaction was recorded for this exact token. This is the
        entire trust decision — no source-IP check anywhere in this path."""
        return len(self.get_interactions(token)) > 0

    def wait_for_interaction(self, token: str, timeout_s: float, poll_interval_s: float = 0.1) -> dict | None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            interactions = self.get_interactions(token)
            if interactions:
                return interactions[0]
            time.sleep(poll_interval_s)
        return None
