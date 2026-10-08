"""Encrypted, content-addressed evidence store — §8a: "Raw outputs and secrets belong in a
separately encrypted evidence store with access control and retention limits — not duplicated
indiscriminately into JSONL." The audit log and findings keep a digest reference and a capped
excerpt; the full raw content lives here, encrypted at rest.

M4.2 additions (2026-08-31, per 01-security-agent-main-direction.md's acceptance list): key
versioning (rotation-capable — old blobs stay decryptable under the key version that encrypted
them), a deletion tombstone (distinct from "never existed"), and a retention job with a real
restore test. Still a Phase-4-appropriate baseline, not full KMS/HSM: keys live in one JSON file
on disk, not hardware-backed — documented as a known limitation, not hidden.

ADR-0005 (2026-08-31): content addressing switched from plaintext `SHA-256(data)` to
`HMAC-SHA256(key, data)`, closing the "equality leakage" gap a plaintext hash has (an attacker
who can see digests, e.g. via the audit log's non-secret `content_digest` field, could otherwise
confirm whether stored evidence matches a guessed/known plaintext by hashing it themselves — the
encryption itself was never broken by this, only the *fact of equality* was inferable). The HMAC
key is the same key already used for encryption (see `_load_or_create_keys` / key versioning
above) — not a new key-management burden, a second use of the existing one, per the ADR's own
discussion. `agent/audit_log.py`'s independently-computed `content_digest` moved to the same
scheme so `Broker._finalize()`'s cross-reference assertion
(`entry["content_digest"] == evidence_digest`) still holds for every dispatch after this pass.

Migration of evidence stored before this pass: `migrate_to_hmac()` below re-addresses every live
blob (decrypt under its recorded key version, recompute the HMAC digest under that same version,
rename the file, append new-format index records) and returns an old-digest→new-digest map — the
evidence index is a plain append-only JSONL log, not tamper-evident like the audit log, so
rewriting/appending to it doesn't undermine any chain-of-custody guarantee. The audit log's
*historical* entries are deliberately left untouched: their `entry_hash` chain is what actually
provides tamper-evidence, and retroactively editing old entries' `content_digest` to match a new
addressing scheme would be indistinguishable from the exact tampering that chain exists to
detect. A pre-migration audit entry's `content_digest`/`evidence_digest` refers to the legacy
plaintext-SHA-256 address; `migrate_to_hmac()`'s returned map is how a reader resolves one to the
blob's new address.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .. import config

DEFAULT_KEY_VERSION = "v1"
# retention_class -> days a blob may live before the retention job deletes it. "indefinite"
# means "never auto-delete" (explicit, not just a large number, so it's unambiguous in configs
# and logs — matching the doc's own preference for explicit states over magic values).
RETENTION_DAYS = {"short": 7, "standard": 90, "indefinite": None}


def compute_digest(data: bytes, key: bytes) -> str:
    """ADR-0005: HMAC-SHA256(key, data), not plaintext SHA-256 — the content address itself is
    unguessable without the key, closing the equality-leakage gap a plaintext hash has. Shared
    by EvidenceStore.put()/get() and agent/audit_log.py's AuditLog.record() so the two systems'
    independently-computed digests still agree, matching this project's existing
    "assert they match" cross-reference discipline."""
    return hmac.new(key, data, hashlib.sha256).hexdigest()


class EvidenceIntegrityError(RuntimeError):
    pass


class EvidenceNotFoundError(RuntimeError):
    pass


class EvidenceDeletedError(RuntimeError):
    """Distinct from EvidenceNotFoundError: this digest existed and was deliberately deleted
    (retention expiry or explicit request), not "never existed" — the tombstone in the index
    is still queryable to show *why* and *when*, even though the content itself is gone."""


class EvidenceKeyError(RuntimeError):
    """The evidence key file exists but cannot be trusted. Never recovered from by generating a
    new key: a new key does not unlock the blobs already on disk, it only makes the loss silent."""


def _looks_like_fernet_key(raw: bytes) -> bool:
    """Whether `raw` is plausibly the pre-M4.2 on-disk format: one urlsafe-base64 Fernet key.

    A Fernet key is exactly 32 random bytes, urlsafe-base64 encoded — 44 characters ending in
    `=`. Checking the shape matters because the legacy branch ADOPTS whatever it is given as the
    key material, so "anything that is not JSON" was far too wide a door (see
    `_write_key_doc_atomic` for what came through it).
    """
    candidate = raw.strip()
    if len(candidate) != 44:
        return False
    try:
        Fernet(candidate)
    except Exception:
        return False
    return True


def _write_key_doc_atomic(path: Path, doc: dict) -> None:
    """Write the key document so a concurrent reader sees the old file or the new one, never a
    partial one, and so the file is never briefly readable by other users.

    This is not a theoretical hardening. `path.write_text()` truncates before it writes, and a
    reader landing in that window got partial JSON — which `_load_or_create_keys` then treated
    as the legacy "the file IS the key" format, adopted the fragment as key material, and WROTE
    IT BACK OVER THE KEY FILE. The real key was then gone from disk: every evidence blob already
    stored became permanently undecryptable and every audit entry's `content_digest` stopped
    verifying. Reproduced before this fix by truncating the file between two loads.

    The mode is set on the temp file before the replace, so there is no window in which the key
    exists at the final path with default permissions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover — the replace normally consumed it
            pass


def _create_key_doc_exclusive(path: Path, doc: dict) -> bool:
    """Create the key file only if it does not exist. True if we created it, False if we lost.

    `os.replace` is the wrong primitive for CREATION: it overwrites. Two processes (or threads)
    starting against a missing key file each generated a key and each replaced the file, so the
    last writer won and every other caller held a key that was no longer on disk — evidence
    encrypted under it became undecryptable the moment it was written. Reproduced before this
    fix: 24 concurrent first-loads produced 14 distinct keys.

    `O_CREAT | O_EXCL` makes the create-or-lose decision atomically in the kernel, and the loser
    re-reads the winner's file instead of imposing its own.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.new{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(json.dumps(doc, indent=2))
        tmp.chmod(0o600)
        try:
            # `os.link` is both atomic and exclusive: it fails if the target exists, and the
            # file at `path` is fully-formed the instant it appears. `O_CREAT|O_EXCL` would also
            # be exclusive, but it creates a ZERO-LENGTH file and then writes into it, leaving a
            # window in which another loader reads 0 bytes and — correctly, by the rule above —
            # refuses to touch a key file it cannot parse. Seen in the race test.
            os.link(tmp, path)
        except FileExistsError:
            return False
        except (OSError, NotImplementedError):
            # A filesystem without hardlinks. Fall back to exclusive create plus write, which
            # reopens the zero-length window but is still exclusive; better than overwriting.
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return False
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f, indent=2)
        return True
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass


def _load_or_create_keys(path: Path) -> tuple[dict[str, bytes], str]:
    # Two passes: read an existing file, or create one; if creation lost a race, come back round
    # and read what the winner wrote. More than two passes cannot help — by then the file
    # exists, so the second pass reads or refuses.
    for _ in range(2):
        result = _load_or_create_keys_once(path)
        if result is not None:
            return result
    raise EvidenceKeyError(  # pragma: no cover — requires losing the race and then losing the file
        f"{path} could neither be read nor created; it appeared and disappeared between attempts"
    )


def _load_or_create_keys_once(path: Path) -> tuple[dict[str, bytes], str] | None:
    """One attempt. None means "lost the creation race, try reading again"."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raw = path.read_bytes()
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # UnicodeDecodeError as well as JSONDecodeError: `json.loads` on BYTES sniffs the
            # encoding first, so bytes that happen to look like a UTF-16 prefix raise a decode
            # error instead — which escaped a `except json.JSONDecodeError` and propagated out
            # of the key loader as an unhandled exception rather than the refusal below.
            # Pre-M4.2 format: the file *was* a single raw Fernet key (already base64), written
            # via path.write_bytes(key). Migrate in place rather than discard it — real evidence
            # blobs already on disk were encrypted under this exact key and must stay
            # decryptable, not orphaned by a format change. But migrate ONLY something that is
            # actually a Fernet key; anything else is a damaged or half-written file, and
            # adopting it would overwrite the real key with garbage.
            if not _looks_like_fernet_key(raw):
                raise EvidenceKeyError(
                    f"{path} is neither a key document nor a legacy Fernet key "
                    f"({len(raw)} bytes). Refusing to replace it: generating a new key would "
                    f"not decrypt the evidence already stored under the old one, it would only "
                    f"hide the loss. Restore the file from backup, or move it aside explicitly "
                    f"if the evidence store is known to be empty."
                )
            doc = {"current": DEFAULT_KEY_VERSION,
                   "keys": {DEFAULT_KEY_VERSION: raw.strip().decode()}}
            _write_key_doc_atomic(path, doc)
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), dict) \
                or doc.get("current") not in (doc.get("keys") or {}):
            raise EvidenceKeyError(
                f"{path} parses but is not a usable key document (no 'keys' mapping, or "
                f"'current' names a version it does not contain). Refusing to replace it — see "
                f"the message above for why a fresh key is not a recovery."
            )
        keys = {v: k.encode() for v, k in doc["keys"].items()}
        return keys, doc["current"]
    raw_key = Fernet.generate_key()
    doc = {"current": DEFAULT_KEY_VERSION, "keys": {DEFAULT_KEY_VERSION: raw_key.decode()}}
    if not _create_key_doc_exclusive(path, doc):
        return None  # another caller created it first; read theirs rather than impose ours
    return {DEFAULT_KEY_VERSION: raw_key}, DEFAULT_KEY_VERSION


def rotate_key(path: Path = config.EVIDENCE_KEY_PATH) -> str:
    """Adds a new key version and makes it current; old versions are kept (not deleted) so
    blobs encrypted under them stay decryptable — rotation, not re-encryption-in-place."""
    keys, _ = _load_or_create_keys(path)
    existing_versions = [int(v[1:]) for v in keys if v.startswith("v") and v[1:].isdigit()]
    new_version = f"v{max(existing_versions, default=0) + 1}"
    keys[new_version] = Fernet.generate_key()
    doc = {"current": new_version, "keys": {v: k.decode() if isinstance(k, bytes) else k for v, k in keys.items()}}
    # Normalize any already-decoded bytes back to str for JSON serialization.
    doc["keys"] = {v: (k.decode() if isinstance(k, (bytes, bytearray)) else k) for v, k in keys.items()}
    # Atomic, and mode-set before the replace: rotation rewrites the file that holds EVERY key
    # version, so a torn write here loses access to every blob in the store at once.
    _write_key_doc_atomic(path, doc)
    return new_version


class EvidenceStore:
    def __init__(
        self,
        evidence_dir: Path = config.EVIDENCE_DIR,
        index_path: Path = config.EVIDENCE_INDEX_PATH,
        access_log_path: Path = config.EVIDENCE_ACCESS_LOG_PATH,
        key_path: Path = config.EVIDENCE_KEY_PATH,
    ):
        self.evidence_dir = evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = index_path
        self.access_log_path = access_log_path
        self.key_path = key_path
        self._keys, self._current_key_version = _load_or_create_keys(key_path)

    def _fernet_for(self, version: str) -> Fernet:
        if version not in self._keys:
            # Keys may have been rotated by another process/instance since this one started —
            # reload rather than assume the in-memory key set is still complete.
            self._keys, self._current_key_version = _load_or_create_keys(self.key_path)
        if version not in self._keys:
            raise EvidenceIntegrityError(f"unknown encryption key version: {version!r}")
        return Fernet(self._keys[version])

    def _blob_path(self, digest: str) -> Path:
        return self.evidence_dir / f"{digest}.enc"

    def put(
        self,
        data: bytes,
        *,
        action_id: str,
        session_id: str,
        source: str,
        retention_class: str = "standard",
        sensitivity: str = "normal",
        content_type: str = "application/octet-stream",
    ) -> str:
        if retention_class not in RETENTION_DAYS:
            raise ValueError(f"unknown retention_class: {retention_class!r}")
        digest = compute_digest(data, self._keys[self._current_key_version])
        blob_path = self._blob_path(digest)
        if not blob_path.exists():  # content-addressed: identical content, one copy
            fernet = self._fernet_for(self._current_key_version)
            blob_path.write_bytes(fernet.encrypt(data))
        record = {
            "digest": digest,
            "action_id": action_id,
            "session_id": session_id,
            "source": source,
            "retention_class": retention_class,
            "sensitivity": sensitivity,
            "content_type": content_type,
            "size_bytes": len(data),
            "key_version": self._current_key_version,
            "stored_at": time.time(),
            "deleted_at": None,
            "deletion_reason": None,
        }
        with self.index_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return digest

    def get(self, digest: str, *, accessed_by: str = "unknown") -> bytes:
        record = self._latest_record(digest)
        if record is None:
            raise EvidenceNotFoundError(digest)
        if record.get("deleted_at") is not None:
            raise EvidenceDeletedError(
                f"{digest} was deleted at {record['deleted_at']} "
                f"(reason: {record['deletion_reason']})"
            )

        blob_path = self._blob_path(digest)
        if not blob_path.exists():
            raise EvidenceIntegrityError(f"index has a live record for {digest} but the blob is missing")
        encrypted = blob_path.read_bytes()
        key_version = record.get("key_version", DEFAULT_KEY_VERSION)
        fernet = self._fernet_for(key_version)
        try:
            data = fernet.decrypt(encrypted)
        except InvalidToken as e:
            raise EvidenceIntegrityError(f"decryption failed for {digest}: {e}") from None

        # Verified under the *recorded* key version, not necessarily the current one — a blob's
        # address was computed under whichever key was current when it was put(), and stays
        # verifiable under that same key across later rotations (same reasoning as decryption
        # above, applied to the digest instead of the ciphertext).
        #
        # ADR-0005: falls back to the legacy plaintext-SHA-256 scheme if the HMAC check doesn't
        # match — an un-migrated blob (migrate_to_hmac() hasn't run yet, or hasn't reached this
        # one) must still be readable, not become inaccessible the moment this code shipped.
        # New digests (put()) are always HMAC; this fallback only helps *read* old ones.
        recomputed = compute_digest(data, self._keys[key_version])
        if recomputed != digest and hashlib.sha256(data).hexdigest() != digest:
            raise EvidenceIntegrityError(
                f"content hash mismatch for {digest}: decrypted content hashes to {recomputed} "
                f"(also checked against the legacy plaintext-SHA-256 scheme, no match either)"
            )

        with self.access_log_path.open("a") as f:
            f.write(json.dumps({"digest": digest, "accessed_by": accessed_by, "at": time.time()}) + "\n")
        return data

    def migrate_to_hmac(self) -> tuple[dict[str, str], list[dict]]:
        """ADR-0005: re-addresses every live blob still under the legacy plaintext-SHA-256
        scheme to HMAC-SHA256. Idempotent (a blob already HMAC-addressed — its stored digest
        already equals `compute_digest(data, key)` — is left alone) and safe to re-run. Returns
        ({old_digest: new_digest} for everything actually migrated, [{"digest":...,
        "reason":...}] for anything that couldn't be — a blob that fails to decrypt or verify
        under either scheme is skipped and reported, never silently dropped, and never allowed
        to abort migrating every *other*, healthy blob (the same "one bad record shouldn't sink
        the whole batch" principle `agent/skills/library.py`'s `load_all()` already uses).

        The blob's *ciphertext* is unchanged (copied byte-for-byte to the new filename, not
        re-encrypted) — only its address changes. The old digest is tombstoned with
        `deletion_reason="migrated_to_hmac:<new_digest>"` (distinct from a real deletion) rather
        than silently vanishing, so a caller holding an old digest (e.g. from a pre-migration
        audit log entry) can still resolve it via `index_for(old_digest)`'s last record.
        """
        migrated: dict[str, str] = {}
        problems: list[dict] = []
        for digest in sorted(self._all_digests()):
            record = self._latest_record(digest)
            if record is None or record.get("deleted_at") is not None:
                continue
            blob_path = self._blob_path(digest)
            if not blob_path.exists():
                continue

            key_version = record.get("key_version", DEFAULT_KEY_VERSION)
            try:
                fernet = self._fernet_for(key_version)
                data = fernet.decrypt(blob_path.read_bytes())
            except InvalidToken as e:
                problems.append({"digest": digest, "reason": f"decryption failed: {e}"})
                continue
            except EvidenceIntegrityError as e:
                problems.append({"digest": digest, "reason": f"unknown key version: {e}"})
                continue

            new_digest = compute_digest(data, self._keys[key_version])
            if new_digest == digest:
                continue  # already HMAC-addressed — nothing to do

            legacy_digest = hashlib.sha256(data).hexdigest()
            if legacy_digest != digest:
                problems.append({
                    "digest": digest,
                    "reason": "matches neither the HMAC nor the legacy plaintext-SHA-256 scheme",
                })
                continue

            new_blob_path = self._blob_path(new_digest)
            if not new_blob_path.exists():
                new_blob_path.write_bytes(blob_path.read_bytes())  # ciphertext copied as-is

            new_record = dict(record)
            new_record["digest"] = new_digest
            new_record["migrated_from"] = digest
            # stored_at is CARRIED OVER, not reset. Setting it to now restarted the retention
            # clock: evidence already past its window survived run_retention_job() and got
            # another full one, and the live index record claimed it had been collected today.
            # Re-addressing a blob changes its address, not when it was collected — and
            # "when was this collected" is the question the retention policy and any chain of
            # custody both ask. `migrated_at` records the re-addressing separately.
            new_record["stored_at"] = record.get("stored_at", time.time())
            new_record["migrated_at"] = time.time()
            with self.index_path.open("a") as f:
                f.write(json.dumps(new_record) + "\n")

            blob_path.unlink(missing_ok=True)
            tombstone = dict(record)
            tombstone["deleted_at"] = time.time()
            tombstone["deletion_reason"] = f"migrated_to_hmac:{new_digest}"
            with self.index_path.open("a") as f:
                f.write(json.dumps(tombstone) + "\n")

            migrated[digest] = new_digest
        return migrated, problems

    def delete(self, digest: str, *, reason: str) -> bool:
        """Deletes the encrypted blob and appends a tombstone record — the digest's history
        (that it existed, when, why it was removed) stays queryable via index_for() even though
        the content itself is gone, per the doc's explicit "deletion tombstone" requirement."""
        record = self._latest_record(digest)
        if record is None or record.get("deleted_at") is not None:
            return False
        blob_path = self._blob_path(digest)
        blob_path.unlink(missing_ok=True)
        tombstone = dict(record)
        tombstone["deleted_at"] = time.time()
        tombstone["deletion_reason"] = reason
        with self.index_path.open("a") as f:
            f.write(json.dumps(tombstone) + "\n")
        return True

    def run_retention_job(self, *, now: float | None = None) -> list[str]:
        """Deletes every live blob whose retention_class has expired. Returns the list of
        deleted digests. Safe to run repeatedly (idempotent — already-deleted digests are
        skipped via the same check `delete()` uses)."""
        now = now if now is not None else time.time()
        deleted = []
        for digest in self._all_digests():
            record = self._latest_record(digest)
            if record is None or record.get("deleted_at") is not None:
                continue
            days = RETENTION_DAYS.get(record.get("retention_class", "standard"))
            if days is None:  # "indefinite" — never auto-delete
                continue
            age_days = (now - record["stored_at"]) / 86400  # stored_at existed pre-M4.2 too
            if age_days >= days:
                if self.delete(digest, reason=f"retention_expired ({record['retention_class']}, {age_days:.1f}d old)"):
                    deleted.append(digest)
        return deleted

    def _all_digests(self) -> set[str]:
        if not self.index_path.exists():
            return set()
        digests = set()
        for line in self.index_path.read_text().splitlines():
            if line.strip():
                digests.add(json.loads(line)["digest"])
        return digests

    def _latest_record(self, digest: str) -> dict | None:
        """Index is append-only (put() appends, delete() appends a tombstone) — the *last*
        matching record for a digest is its current state."""
        records = self.index_for(digest)
        return records[-1] if records else None

    def index_for(self, digest: str) -> list[dict]:
        if not self.index_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.index_path.read_text().splitlines()
            if line.strip() and json.loads(line)["digest"] == digest
        ]
