"""HMAC-SHA256 integrity signing for the skill library — a local-key integrity signature, not a
multi-party PKI trust chain (this is a single-operator self-hosted project; a real signing
authority would be a different, larger piece of infrastructure this project doesn't have a use
case for yet). What this DOES guarantee: a skill file cannot be silently modified (by a bug, a
target's output leaking into the wrong directory, or an operator mistake) between when it was
authored/signed and when it's loaded into a live engagement without that modification being
detected and the load failing closed — matching the project's existing fail-closed pattern
(`agent/broker/policy.py`, `agent/evidence/store.py`).
"""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from .. import config

SKILLS_KEY_PATH = config.STATE_DIR / "skills" / "signing_key.bin"


def _load_or_create_key(path: Path | None = None) -> bytes:
    # Resolved from the module namespace at call time, not bound as a default-argument value at
    # import time — a `path: Path = SKILLS_KEY_PATH` default would silently ignore a test's
    # `patch("agent.skills.signing.SKILLS_KEY_PATH", tmp_path)`, the exact bug already found and
    # fixed once this session in agent/internet/budget.py's ChannelBudgetTracker.
    resolved = path if path is not None else SKILLS_KEY_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists():
        return resolved.read_bytes()
    import secrets

    key = secrets.token_bytes(32)
    resolved.write_bytes(key)
    resolved.chmod(0o600)
    return key


def sign(content: bytes, key: bytes | None = None) -> str:
    key = key if key is not None else _load_or_create_key()
    return hmac.new(key, content, hashlib.sha256).hexdigest()


def verify(content: bytes, signature: str, key: bytes | None = None) -> bool:
    key = key if key is not None else _load_or_create_key()
    expected = hmac.new(key, content, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sig_path_for(skill_path: Path) -> Path:
    return skill_path.with_suffix(skill_path.suffix + ".sig")


def sign_file(skill_path: Path, key: bytes | None = None) -> Path:
    content = skill_path.read_bytes()
    sig = sign(content, key=key)
    sig_path = sig_path_for(skill_path)
    sig_path.write_text(sig)
    return sig_path


def verify_file(skill_path: Path, key: bytes | None = None) -> bool:
    sig_path = sig_path_for(skill_path)
    if not sig_path.exists():
        return False
    content = skill_path.read_bytes()
    signature = sig_path.read_text().strip()
    return verify(content, signature, key=key)
