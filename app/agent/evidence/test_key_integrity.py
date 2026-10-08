"""Tests for the evidence key file: how it is written, and what happens when it is damaged.

This was the worst defect found in the audit pass this file comes from, and the reason it was
worst is that it destroyed data silently. `_load_or_create_keys` treated *anything that is not
JSON* as the pre-M4.2 "the file IS the key" format, adopted those bytes as key material, and
wrote them back over the key file. Combined with a non-atomic `write_text` (truncate, then
write), a concurrent reader landing in the write window got partial JSON, adopted the fragment,
and overwrote the real key — after which every blob already in the evidence store was
permanently undecryptable and every audit entry's `content_digest` stopped verifying.

Run directly: `python3 -m agent.evidence.test_key_integrity`.
"""
from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from cryptography.fernet import Fernet

from .store import EvidenceKeyError, _load_or_create_keys, rotate_key

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    print("\n== creation is atomic and private ==")
    d = Path(tempfile.mkdtemp(prefix="evkey-"))
    kp = d / "key.bin"
    keys, version = _load_or_create_keys(kp)
    real = keys[version]
    check("a key is created", bool(real) and version in keys)
    check("the file is owner-only from the moment it exists",
          oct(kp.stat().st_mode & 0o777) == "0o600", oct(kp.stat().st_mode & 0o777))
    check("no temp file is left behind", not list(d.glob("*.tmp*")), str(list(d.glob('*.tmp*'))))
    again, _ = _load_or_create_keys(kp)
    check("loading again returns the same key, it does not mint a new one",
          again[version] == real)

    print("\n== a damaged key file is refused, never replaced ==")
    intact = kp.read_bytes()
    for name, content in [
        ("a truncated key document", intact[: len(intact) // 2]),
        ("an empty file", b""),
        ("random bytes", b"\x00\x01\x02not a key at all"),
        ("JSON that is not a key document", b'{"hello": "world"}'),
        ("a document whose 'current' names a missing version",
         json.dumps({"current": "v9", "keys": {"v1": real.decode()}}).encode()),
        ("a JSON scalar", b"42"),
    ]:
        kp.write_bytes(content)
        try:
            _load_or_create_keys(kp)
            check(f"{name} is refused", False, "it was accepted")
        except EvidenceKeyError as e:
            check(f"{name} is refused", True)
            check(f"and {name} is left on disk untouched", kp.read_bytes() == content,
                  f"file changed to {kp.read_bytes()[:40]!r}")
            check(f"and the reason says a new key is not a recovery ({name})",
                  "would not decrypt" in str(e) or "not a recovery" in str(e), str(e)[:100])

    print("\n== the legacy single-key format still migrates ==")
    d2 = Path(tempfile.mkdtemp(prefix="evkey-legacy-"))
    kp2 = d2 / "key.bin"
    legacy = Fernet.generate_key()
    kp2.write_bytes(legacy)
    migrated, mver = _load_or_create_keys(kp2)
    check("the legacy raw key is adopted, not discarded", migrated[mver] == legacy)
    check("and the file is now a key document",
          json.loads(kp2.read_text())["keys"][mver] == legacy.decode())
    check("the migrated file is owner-only too",
          oct(kp2.stat().st_mode & 0o777) == "0o600", oct(kp2.stat().st_mode & 0o777))
    reread, rver = _load_or_create_keys(kp2)
    check("re-reading the migrated document gives the same key", reread[rver] == legacy)

    # A legacy file with trailing whitespace is still a legacy key, not damage.
    d3 = Path(tempfile.mkdtemp(prefix="evkey-ws-"))
    kp3 = d3 / "key.bin"
    legacy3 = Fernet.generate_key()
    kp3.write_bytes(legacy3 + b"\n")
    k3, v3 = _load_or_create_keys(kp3)
    check("a trailing newline does not make a legacy key look like damage",
          k3[v3] == legacy3)

    print("\n== rotation keeps every old version, atomically ==")
    d4 = Path(tempfile.mkdtemp(prefix="evkey-rot-"))
    kp4 = d4 / "key.bin"
    first, fver = _load_or_create_keys(kp4)
    new_version = rotate_key(kp4)
    rotated, cur = _load_or_create_keys(kp4)
    check("rotation makes the new version current", cur == new_version and cur != fver)
    check("and keeps the old version so existing blobs stay decryptable",
          rotated.get(fver) == first[fver], f"{sorted(rotated)}")
    check("rotation leaves no temp file", not list(d4.glob("*.tmp*")))
    check("and the file is still owner-only",
          oct(kp4.stat().st_mode & 0o777) == "0o600", oct(kp4.stat().st_mode & 0o777))

    print("\n== concurrent loads of one key file never disagree ==")
    d5 = Path(tempfile.mkdtemp(prefix="evkey-race-"))
    kp5 = d5 / "key.bin"
    seen: set[bytes] = set()
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(24)

    def load():
        barrier.wait()
        try:
            k, v = _load_or_create_keys(kp5)
            with lock:
                seen.add(k[v])
        except Exception as e:  # noqa: BLE001 — any exception here is the finding
            with lock:
                errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=load) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("every concurrent loader agreed on one key", len(seen) == 1, f"{len(seen)} distinct keys")
    check("and none of them raised", not errors, f"{len(errors)}: {errors[:2]}")
    check("no temp files survive the race", not list(d5.glob("*.tmp*")))

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
