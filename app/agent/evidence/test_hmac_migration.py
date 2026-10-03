"""ADR-0005 tests: content addressing is HMAC-SHA256 (not plaintext SHA-256), and
migrate_to_hmac() correctly re-addresses legacy plaintext-SHA-256 blobs without losing data or
breaking the audit/evidence digest cross-reference. Run directly:
`python3 -m agent.evidence.test_hmac_migration`.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from .store import DEFAULT_KEY_VERSION, EvidenceStore, compute_digest

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def _make_store(tmp: Path) -> EvidenceStore:
    return EvidenceStore(
        evidence_dir=tmp / "evidence",
        index_path=tmp / "index.jsonl",
        access_log_path=tmp / "access.jsonl",
        key_path=tmp / "key.json",
    )


def _write_legacy_blob(store: EvidenceStore, data: bytes, *, action_id: str, session_id: str, source: str) -> str:
    """Simulates a pre-ADR-0005 put(): plaintext-SHA-256 address, everything else the same."""
    import time as _time

    legacy_digest = hashlib.sha256(data).hexdigest()
    blob_path = store._blob_path(legacy_digest)
    fernet = Fernet(store._keys[store._current_key_version])
    blob_path.write_bytes(fernet.encrypt(data))
    record = {
        "digest": legacy_digest, "action_id": action_id, "session_id": session_id, "source": source,
        "retention_class": "standard", "sensitivity": "normal", "content_type": "application/octet-stream",
        "size_bytes": len(data), "key_version": store._current_key_version, "stored_at": _time.time(),
        "deleted_at": None, "deletion_reason": None,
    }
    with store.index_path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return legacy_digest


def main() -> int:
    print("== New puts are HMAC-addressed, not plaintext-SHA-256 ==")
    tmp = Path(tempfile.mkdtemp(prefix="hmac-migration-test-"))
    store = _make_store(tmp)
    data = b"some real evidence content"
    digest = store.put(data, action_id="a1", session_id="s1", source="http_recon")
    legacy_would_be = hashlib.sha256(data).hexdigest()
    check("stored digest is NOT the plaintext SHA-256", digest != legacy_would_be)
    check(
        "stored digest IS the HMAC-SHA256 under the current key",
        digest == compute_digest(data, store._keys[store._current_key_version]),
    )
    check("get() still round-trips correctly under the new scheme", store.get(digest) == data)

    print("\n== migrate_to_hmac() re-addresses a legacy plaintext-SHA-256 blob ==")
    tmp2 = Path(tempfile.mkdtemp(prefix="hmac-migration-test2-"))
    store2 = _make_store(tmp2)
    legacy_data = b"pre-ADR-0005 evidence blob"
    legacy_digest = _write_legacy_blob(store2, legacy_data, action_id="a2", session_id="s2", source="port_discovery")
    check("legacy blob is readable via the old plaintext digest before migration", store2.get(legacy_digest) == legacy_data)

    migrated, problems = store2.migrate_to_hmac()
    check("migrate_to_hmac() reports the old->new mapping", legacy_digest in migrated)
    new_digest = migrated.get(legacy_digest)
    check(
        "new digest is the real HMAC-SHA256 of the content",
        new_digest == compute_digest(legacy_data, store2._keys[store2._current_key_version]),
    )
    check("content is readable at the NEW digest after migration", new_digest is not None and store2.get(new_digest) == legacy_data)

    try:
        store2.get(legacy_digest)
        check("old digest raises EvidenceDeletedError after migration (not silently still 'live')", False)
    except Exception as e:
        from .store import EvidenceDeletedError

        check(
            "old digest raises EvidenceDeletedError after migration (not silently still 'live')",
            isinstance(e, EvidenceDeletedError),
        )

    old_history = store2.index_for(legacy_digest)
    check(
        "old digest's history still resolves to the new digest (not just vanished)",
        any("migrated_to_hmac" in (r.get("deletion_reason") or "") for r in old_history),
    )

    print("\n== migrate_to_hmac() is idempotent ==")
    migrated_again, problems_again = store2.migrate_to_hmac()
    check("second run migrates nothing new (already-migrated content is skipped)", migrated_again == {})
    check("second run reports no problems either", problems_again == [])

    print("\n== migrate_to_hmac() preserves content across key rotation ==")
    tmp3 = Path(tempfile.mkdtemp(prefix="hmac-migration-test3-"))
    store3 = _make_store(tmp3)
    data3 = b"blob encrypted under v1, migrated after rotation to v2"
    legacy3 = _write_legacy_blob(store3, data3, action_id="a3", session_id="s3", source="http_recon")
    from .store import rotate_key

    rotate_key(store3.key_path)
    store3 = _make_store(tmp3)  # reload to pick up the rotated key set
    migrated3, problems3 = store3.migrate_to_hmac()
    check("blob encrypted under the pre-rotation key still migrates correctly", legacy3 in migrated3)
    new3 = migrated3[legacy3]
    check("content correct after migrating a pre-rotation blob", store3.get(new3) == data3)

    print("\n== A corrupted blob is skipped and reported, not fatal to the whole batch ==")
    tmp4 = Path(tempfile.mkdtemp(prefix="hmac-migration-test4-"))
    store4 = _make_store(tmp4)
    good_data = b"this one is fine"
    good_digest = _write_legacy_blob(store4, good_data, action_id="a4", session_id="s4", source="http_recon")
    bad_data = b"this one will get corrupted on disk"
    bad_digest = _write_legacy_blob(store4, bad_data, action_id="a5", session_id="s5", source="http_recon")
    store4._blob_path(bad_digest).write_bytes(b"not a valid fernet token at all")  # simulate corruption

    migrated4, problems4 = store4.migrate_to_hmac()
    check("the corrupted blob is reported as a problem, not raised as an exception", any(p["digest"] == bad_digest for p in problems4))
    check("the good blob still migrates despite the other one being corrupted", good_digest in migrated4)
    check("content of the good blob is correct after a partial-failure migration run", store4.get(migrated4[good_digest]) == good_data)

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmp2, ignore_errors=True)
    shutil.rmtree(tmp3, ignore_errors=True)
    shutil.rmtree(tmp4, ignore_errors=True)

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
