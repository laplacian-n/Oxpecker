"""HTTP client for the memory service — Phase 2's remote drop-in for session.py's local store.

Same public surface as SessionStore (session_id, append, load_all, load_as_messages) so
loop.py can be handed either one with minimal branching. The key behavioral difference is
concurrency: append() carries the last-seen version and raises ConcurrentModificationError on
a 409, rather than silently overwriting — "two clients cannot drive the same session at once
without an explicit handoff" (Phase-2 exit criterion) means a conflict must surface, not merge
automatically.
"""
from __future__ import annotations

import time
import uuid

import requests

from . import config


class ConcurrentModificationError(RuntimeError):
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(
            f"session was modified by another device (server is at version {current_version}, "
            "we expected our last-known version) — refresh and retry, don't auto-merge"
        )


class MemoryServiceError(RuntimeError):
    pass


class RemoteSessionStore:
    def __init__(
        self,
        session_id: str,
        device_id: str,
        token: str,
        base_url: str = config.MEMORY_SERVICE_URL,
        verify: str | bool = str(config.MEMORY_SERVICE_CERT_PATH),
    ):
        self.session_id = session_id
        self.device_id = device_id
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self._session.headers["X-Device-Id"] = device_id
        self._session.verify = verify
        self._version = self._fetch_version()

    @staticmethod
    def new_session_id() -> str:
        return str(uuid.uuid4())

    def _fetch_version(self) -> int:
        resp = self._session.get(
            f"{self.base_url}/sessions/{self.session_id}", timeout=config.REQUEST_TIMEOUT_S
        )
        if resp.status_code == 404:
            return 0  # new session, server creates it lazily on first append
        if resp.status_code != 200:
            raise MemoryServiceError(f"GET session -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()["version"]

    def refresh(self) -> int:
        """Explicit handoff step: pull the current server version before retrying a conflict."""
        self._version = self._fetch_version()
        return self._version

    def append(self, role: str, content: str, extra: dict | None = None) -> dict:
        item = {"role": role, "content": content}
        if extra:
            item["metadata"] = extra
        resp = self._session.post(
            f"{self.base_url}/sessions/{self.session_id}/items",
            json={"items": [item], "expected_version": self._version},
            timeout=config.REQUEST_TIMEOUT_S,
        )
        if resp.status_code == 409:
            current = resp.json()["detail"]["current_version"]
            raise ConcurrentModificationError(current)
        if resp.status_code != 200:
            raise MemoryServiceError(f"POST items -> HTTP {resp.status_code}: {resp.text[:300]}")
        self._version = resp.json()["version"]
        return {"role": role, "content": content, "session_id": self.session_id}

    def load_all(self) -> list[dict]:
        resp = self._session.get(
            f"{self.base_url}/sessions/{self.session_id}/items", timeout=config.REQUEST_TIMEOUT_S
        )
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise MemoryServiceError(f"GET items -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()["items"]

    def load_as_messages(self) -> list[dict]:
        messages = []
        for record in self.load_all():
            msg = {"role": record["role"], "content": record["content"]}
            metadata = record.get("metadata") or {}
            msg.update(metadata)  # tool_call_id / tool_calls / name, when present
            messages.append(msg)
        return messages
