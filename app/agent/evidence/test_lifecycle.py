"""Evidence lifecycle regression suite — M4.2 acceptance list: tamper, wrong key, restore,
retention, deletion propagation. Run directly: `python3 -m agent.evidence.test_lifecycle`.

Uses a temp directory for every store instance — never touches the real
agent/state/evidence/ directory that holds actual engagement evidence.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from .store import (
    EvidenceDeletedError,
    EvidenceIntegrityError,
    EvidenceStore,
    rotate_key,
)

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def _new_store(tmp: Path) -> EvidenceStore:
    d = tmp / f"store-{time.time_ns()}"
    return EvidenceStore(
        evidence_dir=d,
        index_path=d / "index.jsonl",
        access_log_path=d / "access_log.jsonl",
        key_path=d / "evidence_key.bin",
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="evidence-lifecycle-test-"))

    print("== Basic put/get round trip + integrity ==")
    store = _new_store(tmp)
    digest = store.put(b"hello evidence", action_id="a1", session_id="s1", source="test")
    check("get() returns exactly what was put", store.get(digest) == b"hello evidence")

    print("\n== Tamper detection ==")
    blob_path = store._blob_path(digest)
    original = bytearray(blob_path.read_bytes())
    original[5] ^= 0xFF
    blob_path.write_bytes(bytes(original))
    try:
        store.get(digest)
        check("tampered blob raises EvidenceIntegrityError", False, "no exception raised")
    except EvidenceIntegrityError:
        check("tampered blob raises EvidenceIntegrityError", True)

    print("\n== Wrong key ==")
    store2 = _new_store(tmp)
    digest2 = store2.put(b"encrypted under store2's key", action_id="a1", session_id="s1", source="test")
    # Point a fresh store at store2's blobs/index but store1's (different) key.
    cross = EvidenceStore(
        evidence_dir=store2.evidence_dir,
        index_path=store2.index_path,
        access_log_path=store2.access_log_path,
        key_path=store.key_path,  # wrong key on purpose
    )
    try:
        cross.get(digest2)
        check("wrong key raises EvidenceIntegrityError", False, "no exception raised")
    except EvidenceIntegrityError:
        check("wrong key raises EvidenceIntegrityError", True)

    print("\n== Deletion tombstone ==")
    store3 = _new_store(tmp)
    digest3 = store3.put(b"will be deleted", action_id="a1", session_id="s1", source="test")
    assert store3.delete(digest3, reason="manual")
    try:
        store3.get(digest3)
        check("deleted evidence raises EvidenceDeletedError, not silently unavailable", False)
    except EvidenceDeletedError:
        check("deleted evidence raises EvidenceDeletedError, not silently unavailable", True)
    check(
        "tombstone stays queryable after deletion (history isn't erased)",
        len(store3.index_for(digest3)) == 2,  # original put + tombstone
    )
    check("double-delete is idempotent, not an error", store3.delete(digest3, reason="again") is False)

    print("\n== Retention job (backdated 'short' class expires, 'indefinite' never does) ==")
    store4 = _new_store(tmp)
    short_digest = store4.put(b"short-lived", action_id="a1", session_id="s1", source="test", retention_class="short")
    indefinite_digest = store4.put(b"keep forever", action_id="a1", session_id="s1", source="test", retention_class="indefinite")
    lines = store4.index_path.read_text().splitlines()
    backdated = []
    for line in lines:
        rec = json.loads(line)
        if rec["digest"] == short_digest:
            rec["stored_at"] = time.time() - 8 * 86400  # 8 days, past the 7-day "short" limit
        backdated.append(json.dumps(rec))
    store4.index_path.write_text("\n".join(backdated) + "\n")
    deleted = store4.run_retention_job()
    check("retention job deletes the expired 'short' blob", short_digest in deleted)
    check("retention job leaves 'indefinite' blob alone", indefinite_digest not in deleted)
    try:
        store4.get(indefinite_digest)
        check("'indefinite' blob still readable after retention job runs", True)
    except Exception:
        check("'indefinite' blob still readable after retention job runs", False)

    print("\n== Key rotation ==")
    store5 = _new_store(tmp)
    pre_rotation_digest = store5.put(b"pre-rotation content", action_id="a1", session_id="s1", source="test")
    new_version = rotate_key(store5.key_path)
    store5b = EvidenceStore(  # fresh instance, as a separate process/run would create
        evidence_dir=store5.evidence_dir,
        index_path=store5.index_path,
        access_log_path=store5.access_log_path,
        key_path=store5.key_path,
    )
    check("current key version advances after rotation", store5b._current_key_version == new_version)
    check(
        "content encrypted before rotation still decrypts after rotation",
        store5b.get(pre_rotation_digest) == b"pre-rotation content",
    )
    post_rotation_digest = store5b.put(b"post-rotation content", action_id="a1", session_id="s1", source="test")
    post_record = store5b._latest_record(post_rotation_digest)
    check("content written after rotation is tagged with the new key version", post_record["key_version"] == new_version)

    print("\n== Restore from a backup copy ==")
    store6 = _new_store(tmp)
    restore_digest = store6.put(b"restore me", action_id="a1", session_id="s1", source="test")
    backup_dir = tmp / "backup-copy"
    shutil.copytree(store6.evidence_dir, backup_dir)
    restored = EvidenceStore(
        evidence_dir=backup_dir,
        index_path=backup_dir / store6.index_path.name,
        access_log_path=backup_dir / store6.access_log_path.name,
        key_path=backup_dir / store6.key_path.name,
    )
    check("evidence readable from a restored (copied) directory", restored.get(restore_digest) == b"restore me")

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    shutil.rmtree(tmp, ignore_errors=True)
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
