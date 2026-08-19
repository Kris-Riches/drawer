"""Minimal Phase2.1 Release Authority.

This module deliberately owns only the release transaction.  It does not
read derived projections or any other non-canonical index.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Any, Callable
import uuid

from .core import _guard_root, _secret_reasons
from .result import KbError


_OWNER = "release-authority/v0.2"
_LEGACY_OWNER = "release-authority/v0.1"
_LOCK_OWNER = _LEGACY_OWNER
_CANDIDATE_OWNER = "candidate-owner/v0.2"
_LEGACY_CANDIDATE_OWNER = "candidate-owner/v0.1"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")
_GARDEN_REF = re.compile(r"^garden://notes/(CAP-[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.md$")
_CAPTURE_REF = re.compile(r"^capture://(CAP-[A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
class ReleaseError(Exception):
    """A deterministic, non-secret release failure."""

    def __init__(self, message: str, code: str = "RELEASE_FAILED") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Candidate:
    """A verified candidate file and its adjacent immutable owner record."""

    path: Path
    owner_path: Path
    candidate_id: str
    media_type: str
    title: str
    content_sha256: str
    idempotency_key: str
    security: str = "public"
    source_capture_ref: str | None = None
    source_garden_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    release_committed: bool
    projection_ok: bool
    release_code: str
    artifact_id: str
    revision_id: str
    receipt_id: str
    bundle_path: str
    recovered: bool = False
    projection_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_committed": self.release_committed,
            "projection_ok": self.projection_ok,
            "release_code": self.release_code,
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "receipt_id": self.receipt_id,
            "bundle_path": self.bundle_path,
            "recovered": self.recovered,
            "projection_error": self.projection_error,
        }


def release_candidate(
    root: Path,
    candidate: Candidate,
    *,
    projection: Callable[[ReleaseResult], Any] | None = None,
    lock_timeout: float = 5.0,
    _before_commit: Callable[[], Any] | None = None,
    _fail_before_promotion: bool = False,
    _fail_after_promotion: bool = False,
) -> ReleaseResult:
    """Publish one candidate or return its existing idempotent publication.

    The underscored arguments are narrow deterministic test seams.  They are
    intentionally not part of a CLI or a persisted contract.
    """

    root_path = _validate_root(root)
    verified = _verify_candidate(root_path, candidate)
    release_dir = root_path / "released"
    lock_path, lock_token, created_release_tree = _acquire_lock(
        release_dir, lock_timeout
    )
    stage: Path | None = None
    promoted = False
    try:
        records, recovered_keys = _recover_release_state(root_path, release_dir)
        existing = _resolve_existing(records, verified)
        if existing is not None:
            result = _result_from_record(existing, recovered=verified["idempotency_key"] in recovered_keys)
            return _finish_projection(result, projection)

        stage = release_dir / f".release-staging-{uuid.uuid4().hex}"
        _create_staging(stage)
        artifact_id = f"ART-{verified['candidate_id']}"
        revision_id = f"{artifact_id}-R1"
        receipt_id = _receipt_id(revision_id, verified)
        revision = _revision_record(artifact_id, revision_id, receipt_id, verified)
        receipt = _receipt_record(receipt_id, revision, verified)
        _write_staged_bundle(stage, verified["path"], revision, receipt, verified["content_sha256"])

        if _before_commit is not None:
            _before_commit()
        _verify_candidate(root_path, candidate)
        _verify_staged_bundle(stage, revision, receipt, verified["content_sha256"])
        if _fail_before_promotion:
            raise ReleaseError("pre-commit disk failure", "RELEASE_PRECOMMIT_FAILED")

        final_bundle = release_dir / "artifacts" / artifact_id / "revision-1"
        _ensure_plain_directory(release_dir / "artifacts", "artifact store")
        _ensure_plain_directory(final_bundle.parent, "artifact owner")
        if final_bundle.exists():
            raise ReleaseError("existing revision is immutable", "RELEASE_IMMUTABLE_CONFLICT")
        os.replace(stage, final_bundle)
        stage = None
        promoted = True
        _validate_bundle(root_path, final_bundle)
        if _fail_after_promotion:
            raise ReleaseError("interrupted after bundle promotion", "RELEASE_INTERRUPTED")
        pointer = _pointer_record(revision, receipt, verified)
        _write_pointer_create(release_dir, verified["idempotency_key"], pointer)
        result = ReleaseResult(
            release_committed=True,
            projection_ok=True,
            release_code="RELEASE_COMMITTED",
            artifact_id=artifact_id,
            revision_id=revision_id,
            receipt_id=receipt_id,
            bundle_path=_relative(root_path, final_bundle),
        )
        return _finish_projection(result, projection)
    except ReleaseError:
        if not promoted and stage is not None:
            _remove_staging(stage)
        raise
    except Exception as exc:
        if not promoted and stage is not None:
            _remove_staging(stage)
        raise ReleaseError("release failed before commit", "RELEASE_PRECOMMIT_FAILED") from exc
    finally:
        _release_lock(lock_path, lock_token)
        if created_release_tree:
            _remove_empty_release_tree(release_dir)


def _finish_projection(
    result: ReleaseResult, projection: Callable[[ReleaseResult], Any] | None
) -> ReleaseResult:
    if projection is None:
        return result
    try:
        projection(result)
    except Exception:
        return ReleaseResult(
            release_committed=True,
            projection_ok=False,
            release_code="RELEASE_COMMITTED",
            artifact_id=result.artifact_id,
            revision_id=result.revision_id,
            receipt_id=result.receipt_id,
            bundle_path=result.bundle_path,
            recovered=result.recovered,
            projection_error="projection callback failed",
        )
    return result


def _validate_root(root: Path) -> Path:
    try:
        return _guard_root(Path(root))
    except KbError as exc:
        raise ReleaseError("root anchor is invalid", "RELEASE_ROOT_INVALID") from exc


def _verify_candidate(root: Path, candidate: Candidate) -> dict[str, Any]:
    if not _ID.fullmatch(candidate.candidate_id) or not _ID.fullmatch(candidate.idempotency_key):
        raise ReleaseError("candidate identity is invalid", "RELEASE_CANDIDATE_INVALID")
    if not _MEDIA_TYPE.fullmatch(candidate.media_type):
        raise ReleaseError("candidate media type is invalid", "RELEASE_CANDIDATE_INVALID")
    if not candidate.media_type.startswith("text/"):
        raise ReleaseError("candidate media type is not public text", "RELEASE_TEXT_ONLY")
    if not candidate.title or "\n" in candidate.title or "\r" in candidate.title:
        raise ReleaseError("candidate title is invalid", "RELEASE_CANDIDATE_INVALID")
    if candidate.security != "public":
        raise ReleaseError("candidate security is not releasable", "RELEASE_SECURITY_REFUSED")
    if not _SHA256.fullmatch(candidate.content_sha256):
        raise ReleaseError("candidate hash is invalid", "RELEASE_CANDIDATE_INVALID")
    source = _confined_plain_path(root, candidate.path, "candidate", file=True)
    owner_path = _confined_plain_path(root, candidate.owner_path, "candidate owner", file=True)
    expected_dir = root / "governance" / "release-candidates" / candidate.candidate_id
    expected_source = expected_dir / "candidate.md"
    expected_owner = expected_dir / "owner.json"
    if source != expected_source or owner_path != expected_owner:
        raise ReleaseError("candidate owner bundle is invalid", "RELEASE_CANDIDATE_INVALID")
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("candidate owner is invalid", "RELEASE_CANDIDATE_INVALID") from exc
    if not isinstance(owner, dict) or (owner.get("schema"), owner.get("owner")) not in {("kb2-candidate-owner/v0.1", _LEGACY_CANDIDATE_OWNER), ("kb2-candidate-owner/v0.2", _CANDIDATE_OWNER)}:
        raise ReleaseError("candidate owner is invalid", "RELEASE_CANDIDATE_INVALID")
    expected_path = _relative(root, source)
    expected = {
        "candidate_id": candidate.candidate_id,
        "content_path": expected_path,
        "content_sha256": candidate.content_sha256.lower(),
        "media_type": candidate.media_type,
        "title": candidate.title,
        "security": "public",
    }
    if any(owner.get(key) != value for key, value in expected.items()):
        raise ReleaseError("candidate owner does not match", "RELEASE_CANDIDATE_INVALID")
    garden_ref = owner.get("source_garden_ref")
    garden_match = _GARDEN_REF.fullmatch(garden_ref) if isinstance(garden_ref, str) else None
    if garden_match is None:
        raise ReleaseError("candidate Garden provenance is invalid", "RELEASE_CANDIDATE_INVALID")
    capture_ref = owner.get("source_capture_ref")
    capture_match = _CAPTURE_REF.fullmatch(capture_ref) if isinstance(capture_ref, str) else None
    if capture_match is None or capture_match.group(1) != garden_match.group(1):
        raise ReleaseError("candidate capture provenance is invalid", "RELEASE_CANDIDATE_INVALID")
    if candidate.source_capture_ref is not None and candidate.source_capture_ref != capture_ref:
        raise ReleaseError("candidate capture provenance is invalid", "RELEASE_CANDIDATE_INVALID")
    if candidate.source_garden_ref is not None and candidate.source_garden_ref != garden_ref:
        raise ReleaseError("candidate Garden provenance is invalid", "RELEASE_CANDIDATE_INVALID")
    garden_path = _confined_plain_path(
        root,
        root / "garden" / "notes" / f"{garden_match.group(1)}.md",
        "Garden source",
        file=True,
    )
    try:
        content = source.read_bytes()
        garden_content = garden_path.read_bytes()
    except OSError as exc:
        raise ReleaseError("candidate cannot be read", "RELEASE_CANDIDATE_INVALID") from exc
    if content != garden_content:
        raise ReleaseError("candidate hash does not match Garden source", "RELEASE_CANDIDATE_HASH_MISMATCH")
    digest = hashlib.sha256(content).hexdigest()
    if digest != candidate.content_sha256.lower():
        raise ReleaseError("candidate hash does not match", "RELEASE_CANDIDATE_HASH_MISMATCH")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError("candidate is not valid UTF-8 text", "RELEASE_TEXT_ONLY") from exc
    if _secret_reasons(content) or _secret_reasons(candidate.title.encode("utf-8")):
        raise ReleaseError("candidate security is not releasable", "RELEASE_SECURITY_REFUSED")
    try:
        from . import core
        capture_dir = core._capture_directory(root, capture_match.group(1))
        capture_metadata, capture_payload = core._load_capture_owner(root, capture_dir)
        if capture_metadata.get("id") != capture_match.group(1):
            raise ReleaseError("candidate capture provenance is invalid", "RELEASE_CANDIDATE_INVALID")
        capture_route = capture_metadata.get("route", {})
        if capture_route.get("result") != "garden-organized" or capture_route.get("garden_ref") != garden_ref:
            raise ReleaseError("candidate capture route is invalid", "RELEASE_CANDIDATE_INVALID")
        explanation = core.explain(root, garden_ref)
        if explanation.get("capture_ref") != capture_ref or explanation.get("ref") != garden_ref:
            raise ReleaseError("candidate Garden explanation is invalid", "RELEASE_CANDIDATE_INVALID")
        if explanation.get("base_digest") != f"sha256:{digest}" or explanation.get("capture_digest") != f"sha256:{hashlib.sha256(capture_payload).hexdigest()}":
            raise ReleaseError("candidate Garden organizer state is stale", "RELEASE_CANDIDATE_HASH_MISMATCH")
        security = explanation.get("security", {})
        if explanation.get("route", {}).get("result") != "garden-organized" or security.get("precheck") != "passed" or security.get("latest_hold") is not None:
            raise ReleaseError("candidate Garden is not releasable", "RELEASE_SECURITY_REFUSED")
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError("candidate provenance is invalid", "RELEASE_CANDIDATE_INVALID") from exc
    owner_digest = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    capture_id = capture_match.group(1)
    capture_owner_path = capture_dir / "owner.json"
    garden_owner_path = root / "governance" / "organizer-state" / capture_id / "state.json"
    capture_owner_digest = hashlib.sha256(capture_owner_path.read_bytes()).hexdigest()
    garden_owner_digest = hashlib.sha256(garden_owner_path.read_bytes()).hexdigest()
    verified = {
        "path": source,
        "owner_path": owner_path,
        "candidate_owner_path": _relative(root, owner_path),
        "candidate_path": _relative(root, source),
        "candidate_id": candidate.candidate_id,
        "media_type": candidate.media_type,
        "title": candidate.title,
        "content_sha256": digest,
        "idempotency_key": candidate.idempotency_key,
        "security": "public",
        "source_capture_ref": capture_ref,
        "source_garden_ref": garden_ref,
        "candidate_owner": owner["owner"],
        "candidate_owner_sha256": owner_digest,
        "source_capture_path": _relative(root, capture_dir / "payload.bin"),
        "source_capture_owner_path": _relative(root, capture_owner_path),
        "source_capture_owner": "durable-capture-writer/v0.1-pilot",
        "source_capture_owner_sha256": capture_owner_digest,
        "source_capture_content_sha256": hashlib.sha256(capture_payload).hexdigest(),
        "source_garden_path": _relative(root, garden_path),
        "source_garden_owner_path": _relative(root, garden_owner_path),
        "source_garden_owner": "organizer-state/v0.1-pilot",
        "source_garden_owner_sha256": garden_owner_digest,
        "source_garden_content_sha256": digest,
        "release_schema": "v0.2",
    }
    verified["request_digest"] = _request_digest({**verified, "_v02": True})
    verified["legacy_request_digest"] = _request_digest(verified, legacy=True)
    return verified


def _acquire_lock(release_dir: Path, timeout: float) -> tuple[Path, str, bool]:
    created_release_tree = not release_dir.exists()
    _ensure_plain_directory(release_dir, "release store")
    lock_path = release_dir / ".release.lock"
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if lock_path.exists():
            if _is_reparse(lock_path):
                raise ReleaseError("release lock is a reparse point", "RELEASE_LOCK_INVALID")
            if not lock_path.is_dir():
                if not lock_path.exists():
                    continue
                raise ReleaseError("release lock has a foreign shape", "RELEASE_LOCK_INVALID")
            owner_path = lock_path / "owner.json"
            if not owner_path.exists():
                if time.monotonic() >= deadline:
                    raise ReleaseError("release writer lock is held", "RELEASE_LOCK_HELD")
                time.sleep(0.01)
                continue
            try:
                _ensure_plain_file(owner_path, "release lock owner")
            except ReleaseError:
                if not owner_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                    continue
                raise
            if _lock_is_initializing(lock_path):
                if time.monotonic() >= deadline:
                    raise ReleaseError("release writer lock is held", "RELEASE_LOCK_HELD")
                time.sleep(0.01)
                continue
            try:
                value = json.loads(owner_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                if (not lock_path.exists() or not owner_path.exists()) and time.monotonic() < deadline:
                    time.sleep(0.01)
                    continue
                if not lock_path.exists() or not owner_path.exists():
                    raise ReleaseError("release writer lock is held", "RELEASE_LOCK_HELD")
                raise ReleaseError("release lock is malformed", "RELEASE_LOCK_INVALID")
            if not isinstance(value, dict) or value.get("schema") != "kb2-release-lock/v0.1" or value.get("owner") != _LOCK_OWNER:
                raise ReleaseError("release lock has a foreign owner", "RELEASE_LOCK_INVALID")
            if time.monotonic() >= deadline:
                raise ReleaseError("release writer lock is held", "RELEASE_LOCK_HELD")
            time.sleep(0.01)
            continue
        try:
            lock_path.mkdir()
            creator_path = lock_path / f".creator-{token}"
            with creator_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            owner_path = lock_path / "owner.json"
            owner_tmp = lock_path / f".owner-{token}.tmp"
            owner_value = {"schema": "kb2-release-lock/v0.1", "owner": _LOCK_OWNER, "token": token}
            with owner_tmp.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(owner_value, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            with owner_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(owner_value, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            owner_tmp.unlink()
            creator_path.unlink()
            return lock_path, token, created_release_tree
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ReleaseError("release writer lock is held", "RELEASE_LOCK_HELD")
        except OSError as exc:
            _safe_cleanup_created_lock(lock_path, token)
            raise ReleaseError("release lock creation failed", "RELEASE_LOCK_INVALID") from exc


def _lock_is_initializing(lock_path: Path) -> bool:
    return any(lock_path.glob(".creator-*")) or any(lock_path.glob(".owner-*.tmp"))


def _release_lock(lock_path: Path, token: str) -> None:
    if not lock_path.exists() or _is_reparse(lock_path):
        return
    try:
        if not lock_path.is_dir():
            return
        owner_path = lock_path / "owner.json"
        value = json.loads(owner_path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("schema") == "kb2-release-lock/v0.1" and value.get("owner") == _LOCK_OWNER and value.get("token") == token:
            owner_path.unlink()
            for marker in lock_path.glob(f".creator-{token}"):
                if not _is_reparse(marker):
                    marker.unlink()
            for temp in lock_path.glob(f".owner-{token}.tmp"):
                if not _is_reparse(temp):
                    temp.unlink()
            lock_path.rmdir()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return


def _safe_cleanup_created_lock(lock_path: Path, token: str) -> None:
    """Clean only artifacts whose owner token is still provably ours."""
    if not lock_path.exists() or not lock_path.is_dir() or _is_reparse(lock_path):
        return
    creator = lock_path / f".creator-{token}"
    if not creator.exists() or _is_reparse(creator):
        return
    owner = lock_path / "owner.json"
    if owner.exists():
        try:
            value = json.loads(owner.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or value.get("token") != token or value.get("owner") != _LOCK_OWNER:
            return
        try:
            owner.unlink()
        except OSError:
            return
    for temp in lock_path.glob(f".owner-{token}.tmp"):
        try:
            value = json.loads(temp.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("token") == token and value.get("owner") == _LOCK_OWNER:
            try:
                temp.unlink()
            except OSError:
                return
    try:
        creator.unlink()
        lock_path.rmdir()
    except OSError:
        return


def _recover_release_state(root: Path, release_dir: Path) -> tuple[list[dict[str, Any]], set[str]]:
    _validate_release_root_entries(release_dir)
    _ensure_plain_directory(release_dir / "artifacts", "artifact store")
    _ensure_plain_directory(release_dir / "idempotency", "idempotency store")
    records, pointers = _scan_release_state(root, release_dir)
    recovered: set[str] = set()
    for record in records:
        if record["idempotency_key"] not in pointers:
            _write_pointer_create(release_dir, record["idempotency_key"], _pointer_record_from_record(record))
            recovered.add(record["idempotency_key"])
    return records, recovered


def _validate_release_root_entries(release_dir: Path) -> None:
    try:
        release_entries = list(release_dir.iterdir())
    except OSError as exc:
        raise ReleaseError("release store cannot be listed", "RELEASE_PATH_INVALID") from exc
    allowed = {".release.lock", "artifacts", "idempotency"}
    for entry in release_entries:
        if _is_reparse(entry):
            raise ReleaseError("release store contains a reparse point", "RELEASE_PATH_INVALID")
        if entry.name.startswith(".release-staging-"):
            raise ReleaseError("staging bundle is partial", "RELEASE_STAGING_INVALID")
        if entry.name not in allowed:
            raise ReleaseError("release store has a foreign entry", "RELEASE_FOREIGN_OWNER")


def _scan_release_state(
    root: Path, release_dir: Path
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    _validate_release_root_entries(release_dir)
    artifacts = release_dir / "artifacts"
    idempotency = release_dir / "idempotency"
    # The immutable Artifact bundle is the commit point.  The idempotency
    # directory/pointer is a recoverable secondary index and may be absent
    # after a post-promotion interruption.  Any pointer that is present is
    # still validated strictly before an Artifact is returned.
    if not idempotency.exists():
        pointers: dict[str, dict[str, Any]] = {}
    else:
        _ensure_plain_directory(idempotency, "idempotency store")
        pointers = {}
        for pointer_path in _plain_children(idempotency, directories=False, label="idempotency pointer"):
            if pointer_path.suffix != ".json" or not _ID.fullmatch(pointer_path.stem):
                raise ReleaseError("foreign idempotency pointer", "RELEASE_POINTER_INVALID")
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ReleaseError("idempotency pointer is malformed", "RELEASE_POINTER_INVALID") from exc
            if not isinstance(pointer, dict) or pointer.get("schema") not in {"kb2-release-pointer/v0.1", "kb2-release-pointer/v0.2"} or pointer.get("owner") not in {_LEGACY_OWNER, _OWNER}:
                raise ReleaseError("idempotency pointer has a foreign owner", "RELEASE_POINTER_INVALID")
            if (pointer.get("schema") == "kb2-release-pointer/v0.1") != (pointer.get("owner") == _LEGACY_OWNER):
                raise ReleaseError("idempotency pointer version owner mismatch", "RELEASE_POINTER_INVALID")
            if pointer.get("idempotency_key") != pointer_path.stem:
                raise ReleaseError("idempotency pointer identity is invalid", "RELEASE_POINTER_INVALID")
            pointers[pointer_path.stem] = pointer

    if not artifacts.exists():
        if pointers:
            raise ReleaseError("idempotency pointer does not bind release", "RELEASE_POINTER_INVALID")
        raise ReleaseError("released record was not found", "RELEASE_NOT_FOUND")
    _ensure_plain_directory(artifacts, "artifact store")
    records: list[dict[str, Any]] = []
    for artifact_dir in _plain_children(artifacts, directories=True, label="artifact owner"):
        if not artifact_dir.name.startswith("ART-"):
            raise ReleaseError("foreign artifact owner", "RELEASE_FOREIGN_OWNER")
        for revision_dir in _plain_children(artifact_dir, directories=True, label="revision"):
            if revision_dir.name != "revision-1":
                raise ReleaseError("foreign revision path", "RELEASE_PARTIAL_BUNDLE")
            records.append(_validate_bundle(root, revision_dir))
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        for field in ("idempotency_key", "candidate_id", "artifact_id", "revision_id", "receipt_id"):
            value = record[field]
            prior = seen.get(f"{field}:{value}")
            if prior is not None:
                raise ReleaseError("release identity is duplicate or ambiguous", "RELEASE_DUPLICATE_IDENTITY")
            seen[f"{field}:{value}"] = record
        if Path(record["bundle_path"]).parent.name != record["artifact_id"]:
            raise ReleaseError("bundle directory does not match artifact identity", "RELEASE_BUNDLE_INVALID")
    by_key = {record["idempotency_key"]: record for record in records}
    for key, pointer in pointers.items():
        record = by_key.get(key)
        pointer_fields = ("artifact_id", "revision_id", "receipt_id", "content_sha256", "candidate_id", "bundle_path", "request_digest", "candidate_path", "candidate_owner_path", "media_type", "title", "security")
        if pointer.get("schema") == "kb2-release-pointer/v0.2":
            pointer_fields += ("source_capture_ref", "source_capture_path", "source_capture_owner_path", "source_capture_owner", "source_capture_owner_sha256", "source_capture_content_sha256", "source_garden_ref", "source_garden_path", "source_garden_owner_path", "source_garden_owner", "source_garden_owner_sha256", "source_garden_content_sha256", "candidate_owner", "candidate_owner_sha256", "release_schema")
        expected_pointer_schema = "kb2-release-pointer/v0.1" if record and record.get("release_schema") == "v0.1" else "kb2-release-pointer/v0.2"
        if pointer.get("schema") != expected_pointer_schema:
            raise ReleaseError("idempotency pointer version does not bind release", "RELEASE_POINTER_INVALID")
        if record is None or any(pointer.get(field) != record.get(field) for field in pointer_fields):
            raise ReleaseError("idempotency pointer does not bind release", "RELEASE_POINTER_INVALID")
    return records, pointers


def _read_committed_records(root: Path) -> list[dict[str, Any]]:
    """Read committed bundles without repairing their recovery index."""

    root = _validate_root(root)
    release_dir = root / "released"
    if not release_dir.exists():
        raise ReleaseError("released record was not found", "RELEASE_NOT_FOUND")
    _ensure_plain_directory(release_dir, "release store")
    records, _ = _scan_release_state(root, release_dir)
    if not records:
        raise ReleaseError("released record was not found", "RELEASE_NOT_FOUND")
    return records


def _resolve_existing(records: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any] | None:
    for record in records:
        if record["idempotency_key"] == candidate["idempotency_key"]:
            expected_digest = candidate.get("legacy_request_digest") if record.get("release_schema") == "v0.1" else candidate["request_digest"]
            if record["request_digest"] != expected_digest:
                raise ReleaseError("idempotency key conflicts with existing request", "RELEASE_IDEMPOTENCY_CONFLICT")
            return record
        if record["candidate_id"] == candidate["candidate_id"]:
            if record["content_sha256"] != candidate["content_sha256"]:
                raise ReleaseError("released candidate is immutable", "RELEASE_IMMUTABLE_CONFLICT")
            raise ReleaseError("candidate already has a different publication", "RELEASE_IMMUTABLE_CONFLICT")
    return None


def _create_staging(stage: Path) -> None:
    if stage.exists():
        raise ReleaseError("staging path already exists", "RELEASE_STAGING_INVALID")
    stage.mkdir()
    _ensure_plain_directory(stage, "staging")


def _write_staged_bundle(stage: Path, source: Path, revision: dict[str, Any], receipt: dict[str, Any], digest: str) -> None:
    artifact_path = stage / "artifact.bin"
    with source.open("rb") as source_handle, artifact_path.open("xb") as artifact_handle:
        copied = hashlib.sha256()
        while True:
            chunk = source_handle.read(1024 * 1024)
            if not chunk:
                break
            copied.update(chunk)
            artifact_handle.write(chunk)
        artifact_handle.flush()
        os.fsync(artifact_handle.fileno())
    if copied.hexdigest() != digest:
        raise ReleaseError("candidate hash changed during staging", "RELEASE_CANDIDATE_HASH_MISMATCH")
    _write_json_create(stage / "revision.json", revision)
    _write_json_create(stage / "receipt.json", receipt)


def _verify_staged_bundle(stage: Path, revision: dict[str, Any], receipt: dict[str, Any], digest: str) -> None:
    if {item.name for item in stage.iterdir()} != {"artifact.bin", "revision.json", "receipt.json"}:
        raise ReleaseError("staging bundle is partial", "RELEASE_PARTIAL_BUNDLE")
    if hashlib.sha256((stage / "artifact.bin").read_bytes()).hexdigest() != digest:
        raise ReleaseError("staging artifact hash changed", "RELEASE_CANDIDATE_HASH_MISMATCH")
    if json.loads((stage / "revision.json").read_text(encoding="utf-8")) != revision or json.loads((stage / "receipt.json").read_text(encoding="utf-8")) != receipt:
        raise ReleaseError("staging metadata changed", "RELEASE_PARTIAL_BUNDLE")


def _validate_bundle(root: Path, bundle: Path) -> dict[str, Any]:
    _ensure_plain_directory(bundle, "released bundle")
    if {item.name for item in bundle.iterdir()} != {"artifact.bin", "revision.json", "receipt.json"}:
        raise ReleaseError("released bundle is partial", "RELEASE_PARTIAL_BUNDLE")
    for name in ("artifact.bin", "revision.json", "receipt.json"):
        _ensure_plain_file(bundle / name, "released bundle leaf")
    try:
        revision = json.loads((bundle / "revision.json").read_text(encoding="utf-8"))
        receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("released metadata is malformed", "RELEASE_PARTIAL_BUNDLE") from exc
    if not isinstance(revision, dict) or not isinstance(receipt, dict) or revision.get("schema") not in {"kb2-artifact-revision/v0.1", "kb2-artifact-revision/v0.2"} or receipt.get("schema") not in {"kb2-publication-receipt/v0.1", "kb2-publication-receipt/v0.2"}:
        raise ReleaseError("released metadata is invalid", "RELEASE_PARTIAL_BUNDLE")
    legacy = revision.get("schema") == "kb2-artifact-revision/v0.1" and receipt.get("schema") == "kb2-publication-receipt/v0.1"
    if (revision.get("schema").endswith("/v0.1")) != (receipt.get("schema").endswith("/v0.1")):
        raise ReleaseError("released metadata versions do not match", "RELEASE_PARTIAL_BUNDLE")
    expected_owner = _LEGACY_OWNER if legacy else _OWNER
    if revision.get("owner") != expected_owner or receipt.get("owner") != expected_owner or revision.get("security") != "public" or receipt.get("security") != "public":
        raise ReleaseError("released metadata has a foreign owner", "RELEASE_FOREIGN_OWNER")
    digest = hashlib.sha256((bundle / "artifact.bin").read_bytes()).hexdigest()
    if digest != revision.get("artifact_sha256") or digest != receipt.get("content_sha256"):
        raise ReleaseError("released artifact was tampered", "RELEASE_TAMPERED")
    if bundle.name != "revision-1" or revision.get("revision") != 1 or revision.get("content_path") != "artifact.bin":
        raise ReleaseError("released revision is invalid", "RELEASE_PARTIAL_BUNDLE")
    if bundle.parent.name != revision.get("artifact_id"):
        raise ReleaseError("bundle directory does not match artifact identity", "RELEASE_BUNDLE_INVALID")
    fields = ("artifact_id", "revision_id", "receipt_id", "candidate_id", "idempotency_key", "media_type", "title", "candidate_path", "candidate_owner_path", "request_digest", "security")
    if not legacy:
        fields += ("source_capture_ref", "source_garden_ref", "candidate_owner", "candidate_owner_sha256", "content_sha256")
        fields += ("source_capture_path", "source_capture_owner_path", "source_capture_owner", "source_capture_owner_sha256", "source_capture_content_sha256", "source_garden_path", "source_garden_owner_path", "source_garden_owner", "source_garden_owner_sha256", "source_garden_content_sha256")
        for digest_field in ("artifact_sha256", "content_sha256", "candidate_owner_sha256", "source_capture_owner_sha256", "source_capture_content_sha256", "source_garden_owner_sha256", "source_garden_content_sha256"):
            if not isinstance(revision.get(digest_field), str) or not _SHA256.fullmatch(revision[digest_field]):
                raise ReleaseError("released digest is invalid", "RELEASE_PARTIAL_BUNDLE")
        capture_match = _CAPTURE_REF.fullmatch(str(revision.get("source_capture_ref", "")))
        garden_match = _GARDEN_REF.fullmatch(str(revision.get("source_garden_ref", "")))
        if capture_match is None or garden_match is None or capture_match.group(1) != garden_match.group(1):
            raise ReleaseError("source provenance identities do not match", "RELEASE_PARTIAL_BUNDLE")
        capture_id = capture_match.group(1)
        expected_paths = {
            "candidate_path": f"governance/release-candidates/{revision['candidate_id']}/candidate.md",
            "candidate_owner_path": f"governance/release-candidates/{revision['candidate_id']}/owner.json",
            "source_capture_path": f"ingress/pending/{capture_id}/payload.bin",
            "source_capture_owner_path": f"ingress/pending/{capture_id}/owner.json",
            "source_garden_path": f"garden/notes/{capture_id}.md",
            "source_garden_owner_path": f"governance/organizer-state/{capture_id}/state.json",
        }
        if any(revision.get(key) != value for key, value in expected_paths.items()):
            raise ReleaseError("released provenance path is invalid", "RELEASE_BUNDLE_INVALID")
        if revision.get("source_garden_content_sha256") != revision.get("content_sha256") or revision.get("artifact_sha256") != revision.get("content_sha256"):
            raise ReleaseError("released artifact/content digest is invalid", "RELEASE_PARTIAL_BUNDLE")
    if any(revision.get(field) != receipt.get(field) for field in fields if field != "receipt_id"):
        raise ReleaseError("receipt does not bind revision", "RELEASE_PARTIAL_BUNDLE")
    if receipt.get("revision_id") != revision.get("revision_id") or receipt.get("artifact_id") != revision.get("artifact_id"):
        raise ReleaseError("receipt does not bind revision", "RELEASE_PARTIAL_BUNDLE")
    if not legacy and revision.get("request_digest") != _request_digest(revision):
        raise ReleaseError("revision request binding is invalid", "RELEASE_PARTIAL_BUNDLE")
    expected_receipt = _receipt_id_from_revision(revision)
    if receipt.get("receipt_id") != expected_receipt or revision.get("receipt_id") != expected_receipt:
        raise ReleaseError("receipt identity is invalid", "RELEASE_PARTIAL_BUNDLE")
    return {
        "bundle_path": _relative(root, bundle),
        "artifact_id": revision["artifact_id"],
        "revision_id": revision["revision_id"],
        "receipt_id": revision["receipt_id"],
        "candidate_id": revision["candidate_id"],
        "idempotency_key": revision["idempotency_key"],
        "candidate_path": revision["candidate_path"],
        "request_digest": revision["request_digest"],
        "content_sha256": revision["artifact_sha256"],
        "media_type": revision["media_type"],
        "title": revision["title"],
        "candidate_owner_path": revision["candidate_owner_path"],
        "security": revision["security"],
        "release_schema": "v0.1" if legacy else "v0.2",
        "provenance_status": "legacy-incomplete" if legacy else "complete",
        "missing_segments": ["capture", "garden"] if legacy else [],
        "candidate_owner_sha256": revision.get("candidate_owner_sha256"),
        "candidate_owner": revision.get("candidate_owner", "candidate-owner/v0.1" if legacy else None),
        "source_capture_ref": revision.get("source_capture_ref"),
        "source_capture_path": revision.get("source_capture_path"),
        "source_capture_owner_path": revision.get("source_capture_owner_path"),
        "source_capture_owner": revision.get("source_capture_owner"),
        "source_capture_owner_sha256": revision.get("source_capture_owner_sha256"),
        "source_capture_content_sha256": revision.get("source_capture_content_sha256"),
        "source_garden_ref": revision.get("source_garden_ref"),
        "source_garden_path": revision.get("source_garden_path"),
        "source_garden_owner_path": revision.get("source_garden_owner_path"),
        "source_garden_owner": revision.get("source_garden_owner"),
        "source_garden_owner_sha256": revision.get("source_garden_owner_sha256"),
        "source_garden_content_sha256": revision.get("source_garden_content_sha256"),
    }


def _write_pointer_create(release_dir: Path, key: str, pointer: dict[str, Any]) -> None:
    idempotency = release_dir / "idempotency"
    _ensure_plain_directory(idempotency, "idempotency store")
    path = idempotency / f"{key}.json"
    if path.exists():
        _ensure_plain_file(path, "idempotency pointer")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("idempotency pointer is malformed", "RELEASE_POINTER_INVALID") from exc
        if existing != pointer:
            raise ReleaseError("idempotency pointer conflicts", "RELEASE_POINTER_INVALID")
        return
    _write_json_create(path, pointer)


def _pointer_record(revision: dict[str, Any], receipt: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "kb2-release-pointer/v0.2",
        "owner": _OWNER,
        "idempotency_key": candidate["idempotency_key"],
        "artifact_id": revision["artifact_id"],
        "revision_id": revision["revision_id"],
        "receipt_id": receipt["receipt_id"],
        "candidate_id": candidate["candidate_id"],
        "content_sha256": candidate["content_sha256"],
        "request_digest": candidate["request_digest"],
        "candidate_path": candidate["candidate_path"],
        "candidate_owner_path": candidate["candidate_owner_path"],
        "media_type": candidate["media_type"],
        "title": candidate["title"],
        "security": candidate["security"],
        "bundle_path": f"released/artifacts/{revision['artifact_id']}/revision-1",
        "release_schema": "v0.2",
        "source_capture_ref": candidate["source_capture_ref"],
        "source_garden_ref": candidate["source_garden_ref"],
        "candidate_owner": candidate["candidate_owner"],
        "candidate_owner_sha256": candidate["candidate_owner_sha256"],
        "source_capture_path": candidate["source_capture_path"],
        "source_capture_owner_path": candidate["source_capture_owner_path"],
        "source_capture_owner": candidate["source_capture_owner"],
        "source_capture_owner_sha256": candidate["source_capture_owner_sha256"],
        "source_capture_content_sha256": candidate["source_capture_content_sha256"],
        "source_garden_path": candidate["source_garden_path"],
        "source_garden_owner_path": candidate["source_garden_owner_path"],
        "source_garden_owner": candidate["source_garden_owner"],
        "source_garden_owner_sha256": candidate["source_garden_owner_sha256"],
        "source_garden_content_sha256": candidate["source_garden_content_sha256"],
    }


def _pointer_record_from_record(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("release_schema") == "v0.1":
        return {"schema": "kb2-release-pointer/v0.1", "owner": _LEGACY_OWNER, "idempotency_key": record["idempotency_key"], "artifact_id": record["artifact_id"], "revision_id": record["revision_id"], "receipt_id": record["receipt_id"], "candidate_id": record["candidate_id"], "content_sha256": record["content_sha256"], "request_digest": record["request_digest"], "candidate_path": record["candidate_path"], "candidate_owner_path": record["candidate_owner_path"], "media_type": record["media_type"], "title": record["title"], "security": record["security"], "bundle_path": record["bundle_path"]}
    return {
        "schema": "kb2-release-pointer/v0.2",
        "owner": _OWNER,
        "idempotency_key": record["idempotency_key"],
        "artifact_id": record["artifact_id"],
        "revision_id": record["revision_id"],
        "receipt_id": record["receipt_id"],
        "candidate_id": record["candidate_id"],
        "content_sha256": record["content_sha256"],
        "request_digest": record["request_digest"],
        "candidate_path": record["candidate_path"],
        "candidate_owner_path": record["candidate_owner_path"],
        "media_type": record["media_type"],
        "title": record["title"],
        "security": record["security"],
        "bundle_path": record["bundle_path"],
        "release_schema": record["release_schema"],
        "source_capture_ref": record["source_capture_ref"],
        "source_garden_ref": record["source_garden_ref"],
        "candidate_owner": record["candidate_owner"],
        "candidate_owner_sha256": record["candidate_owner_sha256"],
        "source_capture_path": record["source_capture_path"],
        "source_capture_owner_path": record["source_capture_owner_path"],
        "source_capture_owner": record["source_capture_owner"],
        "source_capture_owner_sha256": record["source_capture_owner_sha256"],
        "source_capture_content_sha256": record["source_capture_content_sha256"],
        "source_garden_path": record["source_garden_path"],
        "source_garden_owner_path": record["source_garden_owner_path"],
        "source_garden_owner": record["source_garden_owner"],
        "source_garden_owner_sha256": record["source_garden_owner_sha256"],
        "source_garden_content_sha256": record["source_garden_content_sha256"],
    }


def _revision_record(artifact_id: str, revision_id: str, receipt_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "kb2-artifact-revision/v0.2",
        "owner": _OWNER,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
        "revision": 1,
        "receipt_id": receipt_id,
        "candidate_id": candidate["candidate_id"],
        "candidate_path": candidate["candidate_path"],
        "candidate_owner_path": candidate["candidate_owner_path"],
        "idempotency_key": candidate["idempotency_key"],
        "content_path": "artifact.bin",
        "artifact_sha256": candidate["content_sha256"],
        "media_type": candidate["media_type"],
        "title": candidate["title"],
        "security": "public",
        "request_digest": candidate["request_digest"],
        "source_capture_ref": candidate["source_capture_ref"],
        "source_garden_ref": candidate["source_garden_ref"],
        "candidate_owner": candidate["candidate_owner"],
        "candidate_owner_sha256": candidate["candidate_owner_sha256"],
        "content_sha256": candidate["content_sha256"],
        "source_capture_ref": candidate["source_capture_ref"],
        "source_capture_path": candidate["source_capture_path"],
        "source_capture_owner_path": candidate["source_capture_owner_path"],
        "source_capture_owner": candidate["source_capture_owner"],
        "source_capture_owner_sha256": candidate["source_capture_owner_sha256"],
        "source_capture_content_sha256": candidate["source_capture_content_sha256"],
        "source_garden_ref": candidate["source_garden_ref"],
        "source_garden_path": candidate["source_garden_path"],
        "source_garden_owner_path": candidate["source_garden_owner_path"],
        "source_garden_owner": candidate["source_garden_owner"],
        "source_garden_owner_sha256": candidate["source_garden_owner_sha256"],
        "source_garden_content_sha256": candidate["source_garden_content_sha256"],
    }


def _receipt_record(receipt_id: str, revision: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "kb2-publication-receipt/v0.2",
        "owner": _OWNER,
        "receipt_id": receipt_id,
        "artifact_id": revision["artifact_id"],
        "revision_id": revision["revision_id"],
        "candidate_id": candidate["candidate_id"],
        "candidate_path": candidate["candidate_path"],
        "candidate_owner_path": candidate["candidate_owner_path"],
        "idempotency_key": candidate["idempotency_key"],
        "content_sha256": candidate["content_sha256"],
        "media_type": candidate["media_type"],
        "title": candidate["title"],
        "security": "public",
        "request_digest": candidate["request_digest"],
        "source_capture_ref": candidate["source_capture_ref"],
        "source_garden_ref": candidate["source_garden_ref"],
        "candidate_owner": candidate["candidate_owner"],
        "candidate_owner_sha256": candidate["candidate_owner_sha256"],
        "content_sha256": candidate["content_sha256"],
        "source_capture_ref": candidate["source_capture_ref"],
        "source_capture_path": candidate["source_capture_path"],
        "source_capture_owner_path": candidate["source_capture_owner_path"],
        "source_capture_owner": candidate["source_capture_owner"],
        "source_capture_owner_sha256": candidate["source_capture_owner_sha256"],
        "source_capture_content_sha256": candidate["source_capture_content_sha256"],
        "source_garden_ref": candidate["source_garden_ref"],
        "source_garden_path": candidate["source_garden_path"],
        "source_garden_owner_path": candidate["source_garden_owner_path"],
        "source_garden_owner": candidate["source_garden_owner"],
        "source_garden_owner_sha256": candidate["source_garden_owner_sha256"],
        "source_garden_content_sha256": candidate["source_garden_content_sha256"],
    }


def _receipt_id(revision_id: str, candidate: dict[str, Any]) -> str:
    material = "|".join((revision_id, candidate["candidate_id"], candidate["content_sha256"], candidate["idempotency_key"]))
    return "PUB-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24].upper()


def _request_digest(request: dict[str, Any], *, legacy: bool = False) -> str:
    material = {
        "candidate_id": request["candidate_id"],
        "candidate_path": request["candidate_path"],
        "candidate_owner_path": request["candidate_owner_path"],
        "content_sha256": request.get("content_sha256", request.get("artifact_sha256")),
        "idempotency_key": request["idempotency_key"],
        "media_type": request["media_type"],
        "security": request["security"],
        "title": request["title"],
    }
    if not legacy and (request.get("_v02") or request.get("schema") == "kb2-artifact-revision/v0.2"):
        material.update(
            {
                "source_capture_ref": request["source_capture_ref"],
                "source_capture_path": request["source_capture_path"],
                "source_capture_owner_path": request["source_capture_owner_path"],
                "source_capture_owner": request["source_capture_owner"],
                "source_capture_owner_sha256": request["source_capture_owner_sha256"],
                "source_capture_content_sha256": request["source_capture_content_sha256"],
                "source_garden_ref": request["source_garden_ref"],
                "source_garden_path": request["source_garden_path"],
                "source_garden_owner_path": request["source_garden_owner_path"],
                "source_garden_owner": request["source_garden_owner"],
                "source_garden_owner_sha256": request["source_garden_owner_sha256"],
                "source_garden_content_sha256": request["source_garden_content_sha256"],
                "candidate_owner": request["candidate_owner"],
                "candidate_owner_sha256": request["candidate_owner_sha256"],
            }
        )
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _receipt_id_from_revision(revision: dict[str, Any]) -> str:
    material = "|".join((revision["revision_id"], revision["candidate_id"], revision["artifact_sha256"], revision["idempotency_key"]))
    return "PUB-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24].upper()


def _result_from_record(record: dict[str, Any], *, recovered: bool) -> ReleaseResult:
    return ReleaseResult(
        release_committed=True,
        projection_ok=True,
        release_code="RELEASE_COMMITTED",
        artifact_id=record["artifact_id"],
        revision_id=record["revision_id"],
        receipt_id=record["receipt_id"],
        bundle_path=record["bundle_path"],
        recovered=recovered,
    )


def _confined_plain_path(root: Path, path: Path, label: str, *, file: bool) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ReleaseError(f"{label} is outside root", "RELEASE_PATH_INVALID") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current == candidate and file:
            _ensure_plain_file(current, label)
        elif not current.exists() or not current.is_dir() or _is_reparse(current):
            raise ReleaseError(f"{label} path is not a plain directory", "RELEASE_PATH_INVALID")
    if file:
        _ensure_plain_file(candidate, label)
    return candidate


def _ensure_plain_file(path: Path, label: str) -> None:
    if not path.exists() or not path.is_file() or _is_reparse(path):
        raise ReleaseError(f"{label} is not a plain file", "RELEASE_PATH_INVALID")


def _ensure_plain_directory(path: Path, label: str) -> None:
    if path.exists():
        if not path.is_dir() or _is_reparse(path):
            raise ReleaseError(f"{label} is not a plain directory", "RELEASE_PATH_INVALID")
        return
    try:
        path.mkdir()
    except FileExistsError:
        pass
    if not path.is_dir() or _is_reparse(path):
        raise ReleaseError(f"{label} is not a plain directory", "RELEASE_PATH_INVALID")


def _plain_children(path: Path, *, directories: bool, label: str) -> list[Path]:
    try:
        children = list(path.iterdir())
    except OSError as exc:
        raise ReleaseError(f"{label} cannot be listed", "RELEASE_PATH_INVALID") from exc
    result: list[Path] = []
    for child in children:
        if _is_reparse(child):
            raise ReleaseError(f"{label} contains a reparse point", "RELEASE_PATH_INVALID")
        if child.is_dir() == directories:
            result.append(child)
        else:
            raise ReleaseError(f"{label} contains an unexpected entry", "RELEASE_PARTIAL_BUNDLE")
    return sorted(result, key=lambda item: item.name)


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        return bool(path.lstat().st_file_attributes & _REPARSE)
    except FileNotFoundError:
        return False
    except (OSError, AttributeError):
        return True


def _write_json_create(path: Path, value: dict[str, Any]) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ReleaseError("immutable metadata already exists", "RELEASE_IMMUTABLE_CONFLICT") from exc
    except OSError as exc:
        raise ReleaseError("metadata write failed", "RELEASE_PRECOMMIT_FAILED") from exc


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _remove_staging(stage: Path) -> None:
    if not stage.exists() or _is_reparse(stage):
        return
    try:
        shutil.rmtree(stage)
    except OSError:
        return


def _remove_empty_release_tree(release_dir: Path) -> None:
    try:
        for child in (release_dir / "idempotency", release_dir / "artifacts"):
            if child.exists() and child.is_dir() and not any(child.iterdir()):
                child.rmdir()
        if release_dir.exists() and release_dir.is_dir() and not any(release_dir.iterdir()):
            release_dir.rmdir()
    except OSError:
        return
