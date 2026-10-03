"""Per-device credentials for the memory service (§6 "Simple LAN auth").

Deliberately not shared with llama-server's --api-key: separate credential, separate audience,
separate revocation lifecycle. Tokens are stored hashed (never plaintext) — the raw token is
shown exactly once, at creation/rotation time, same pattern as a GitHub PAT.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from .. import config


def _now() -> float:
    return time.time()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceStore:
    def __init__(self, path: Path = config.MEMORY_SERVICE_DEVICES_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def add(self, device_id: str, ttl_s: int = config.DEVICE_TOKEN_DEFAULT_TTL_S) -> str:
        data = self._read()
        raw_token = secrets.token_urlsafe(32)
        data[device_id] = {
            "token_hash": _hash(raw_token),
            "created_at": _now(),
            "expires_at": _now() + ttl_s,
            "revoked": False,
        }
        self._write(data)
        return raw_token

    def rotate(self, device_id: str, ttl_s: int = config.DEVICE_TOKEN_DEFAULT_TTL_S) -> str:
        return self.add(device_id, ttl_s)  # add() overwrites; old token stops verifying

    def revoke(self, device_id: str) -> bool:
        data = self._read()
        if device_id not in data:
            return False
        data[device_id]["revoked"] = True
        self._write(data)
        return True

    def verify(self, device_id: str, raw_token: str) -> tuple[bool, str]:
        data = self._read()
        record = data.get(device_id)
        if record is None:
            return False, "unknown device"
        if record["revoked"]:
            return False, "revoked"
        if _now() > record["expires_at"]:
            return False, "expired"
        if not secrets.compare_digest(_hash(raw_token), record["token_hash"]):
            return False, "bad token"
        return True, "ok"

    def list_devices(self) -> list[dict]:
        data = self._read()
        return [
            {
                "device_id": k,
                "created_at": v["created_at"],
                "expires_at": v["expires_at"],
                "revoked": v["revoked"],
            }
            for k, v in data.items()
        ]
