"""Stage 1.1 capture, safety routing, Garden override, and explanation core."""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime
from typing import Any

from .result import KbError


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CAPTURE_RE = re.compile(r"^CAP-[0-9A-HJKMNP-TV-Z]{26}$")
_KB_ID_RE = re.compile(r"^KB-[0-9A-HJKMNP-TV-Z]{26}$")
_OVERRIDE_RE = re.compile(r"^OVR-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    ("private-key-header", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai-style-token", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("bearer-token", re.compile(rb"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{16,}")),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    value = (int(time.time() * 1000) << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    chars: list[str] = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return f"{prefix}-" + "".join(reversed(chars))


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink():
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _guard_root(root: Path) -> Path:
    absolute = root.absolute()
    if not absolute.exists() or not absolute.is_dir():
        raise KbError("KB2_ROOT_INVALID", "knowledge base root must be an existing directory", 2)
    if _is_reparse(absolute):
        raise KbError("KB2_REPARSE_REJECTED", "knowledge base root cannot be a reparse point", 2)
    resolved = absolute.resolve(strict=True)
    anchor = resolved / "kb.yaml"
    if not anchor.is_file() or _is_reparse(anchor):
        raise KbError("KB2_ROOT_UNANCHORED", "knowledge base root requires a plain kb.yaml anchor", 2)
    try:
        raw = anchor.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise KbError("KB2_ROOT_INVALID", "kb.yaml anchor is unreadable", 2) from exc
    if "<<:" in raw or re.search(r"(^|\s)[&*][A-Za-z0-9_-]+", raw):
        raise KbError("KB2_ROOT_INVALID", "kb.yaml anchor cannot use merge keys or aliases", 2)

    def scalar(name: str) -> str:
        matches = re.findall(rf"(?m)^{re.escape(name)}\s*:\s*([^#\r\n]+?)\s*$", raw)
        if len(matches) != 1:
            raise KbError("KB2_ROOT_INVALID", f"kb.yaml must contain exactly one {name}", 2)
        return matches[0].strip().strip("'\"")

    if scalar("schema") != "kb-root/v0.1":
        raise KbError("KB2_ROOT_INVALID", "kb.yaml has an unsupported root schema", 2)
    if not _KB_ID_RE.fullmatch(scalar("id")):
        raise KbError("KB2_ROOT_INVALID", "kb.yaml has an invalid or empty KB id", 2)
    return resolved


def _guard_path(root: Path, path: Path, *, allow_missing: bool = True) -> Path:
    root = _guard_root(root)
    absolute = path.absolute()
    try:
        absolute.relative_to(root)
    except ValueError as exc:
        raise KbError("KB2_PATH_ESCAPE", "path is outside the knowledge base root", 2) from exc

    current = root
    for part in absolute.relative_to(root).parts:
        current = current / part
        if not current.exists():
            if allow_missing:
                break
            raise KbError("KB2_PATH_MISSING", "required path does not exist", 2)
        if _is_reparse(current):
            raise KbError("KB2_REPARSE_REJECTED", "reparse points are not allowed on write paths", 2)

    resolved = absolute.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise KbError("KB2_PATH_ESCAPE", "resolved path escapes the knowledge base root", 2) from exc
    return absolute


def _ensure_plain_directory(root: Path, relative: str) -> Path:
    current = _guard_root(root)
    for part in Path(relative).parts:
        candidate = current / part
        _guard_path(root, candidate)
        if candidate.exists():
            if not candidate.is_dir() or _is_reparse(candidate):
                raise KbError("KB2_REPARSE_REJECTED", "required directory is not a plain directory", 2)
        else:
            candidate.mkdir()
        current = candidate
    return current


def _current_user_sid() -> str:
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=False,
        capture_output=True,
    )
    text = result.stdout.decode(errors="replace")
    match = re.search(r"S-\d(?:-\d+)+", text)
    if result.returncode != 0 or not match:
        raise KbError("KB2_SPOOL_PROTECTION_FAILED", "could not determine the current Windows SID")
    return match.group(0)


def _protect_directory(path: Path) -> None:
    if os.name == "nt":
        sid = _current_user_sid()
        script = r"""
$path = $env:KB2_ACL_PATH
$userSid = $env:KB2_ACL_USER_SID
$systemSid = 'S-1-5-18'
$ErrorActionPreference = 'Stop'
& icacls $path '/inheritance:r' | Out-Null
if ($LASTEXITCODE -ne 0) { exit 11 }
& icacls $path '/grant:r' "*$userSid`:(OI)(CI)F" "*$systemSid`:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { exit 12 }
$initial = Get-Acl -LiteralPath $path
$unknownSids = @($initial.Access | ForEach-Object {
    $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
} | Where-Object { $_ -notin @($userSid, $systemSid) } | Sort-Object -Unique)
foreach ($unknownSid in $unknownSids) {
    & icacls $path '/remove' "*$unknownSid" | Out-Null
    if ($LASTEXITCODE -ne 0) { exit 13 }
}
& icacls $path '/remove:d' "*$userSid" "*$systemSid" | Out-Null
if ($LASTEXITCODE -ne 0) { exit 14 }
& icacls $path '/grant:r' "*$userSid`:(OI)(CI)F" "*$systemSid`:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { exit 15 }
$verified = Get-Acl -LiteralPath $path
$rules = @($verified.Access)
$actualSids = @($rules | ForEach-Object {
    $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
})
$allowedSids = @($userSid, $systemSid)
$badRule = @($rules | Where-Object {
    $translated = $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    $translated -notin $allowedSids -or
    $_.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
    ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne [Security.AccessControl.FileSystemRights]::FullControl -or
    $_.IsInherited
})
if (-not $verified.AreAccessRulesProtected -or $rules.Count -ne 2 -or $badRule.Count -ne 0 -or $userSid -notin $actualSids -or $systemSid -notin $actualSids) {
    exit 19
}
"""
        environment = os.environ.copy()
        environment["KB2_ACL_PATH"] = str(path)
        environment["KB2_ACL_USER_SID"] = sid
        result = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            env=environment,
        )
        if result.returncode != 0:
            raise KbError(
                "KB2_SPOOL_PROTECTION_FAILED",
                "could not apply or verify the protected spool ACL",
                4,
                {"native_exit": result.returncode, "stderr": result.stderr.decode(errors="replace")[-1000:]},
            )
    else:
        path.chmod(0o700)


def _ensure_protected_directory(root: Path, relative: str) -> Path:
    path = _ensure_plain_directory(root, relative)
    _protect_directory(path)
    return path


def _write_file_synced(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_file_after_sync(path: Path, data: bytes) -> None:
    parent = path.parent
    fd, temporary_name = tempfile.mkstemp(prefix=".kb2-write-", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _move_file_to_absent(source: Path, destination: Path) -> None:
    """Move a file without ever replacing an existing destination."""
    if destination.exists():
        raise FileExistsError(str(destination))
    if os.name == "nt":
        os.rename(source, destination)
        return
    os.link(source, destination)
    source.unlink()


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise KbError("KB2_STATE_INVALID", "machine state must be a JSON object")
    return value


def _guard_plain_file(root: Path, path: Path, *, required: bool = True) -> Path:
    guarded = _guard_path(root, path, allow_missing=not required)
    if guarded.exists():
        if _is_reparse(guarded) or not guarded.is_file():
            raise KbError("KB2_REPARSE_REJECTED", "required state entry is not a plain file", 3)
    elif required:
        raise KbError("KB2_PATH_MISSING", "required state entry does not exist", 3)
    return guarded


def _guard_plain_directory(root: Path, path: Path) -> Path:
    guarded = _guard_path(root, path, allow_missing=False)
    if _is_reparse(guarded) or not guarded.is_dir():
        raise KbError("KB2_REPARSE_REJECTED", "recovery entry is not a plain directory", 3)
    return guarded


def _secret_reasons(data: bytes) -> list[str]:
    return [name for name, pattern in _SECRET_PATTERNS if pattern.search(data)]


def _capture_directory(root: Path, capture_id: str) -> Path:
    if not _CAPTURE_RE.fullmatch(capture_id):
        raise KbError("KB2_REF_INVALID", "invalid capture identity", 2)
    path = root / "ingress" / "pending" / capture_id
    return _guard_plain_directory(root, path)


def _capture_owner_record(metadata: dict[str, Any], payload_snapshot_entry: str | None) -> dict[str, Any]:
    metadata_bytes = _json_bytes(metadata)
    return {
        "schema": "capture-owner/v0.1-pilot",
        "owner": "durable-capture-writer/v0.1-pilot",
        "capture_id": metadata["id"],
        "payload_entry": "payload.bin",
        "payload_digest": metadata["payload_digest"],
        "payload_snapshot_entry": payload_snapshot_entry,
        "metadata_entry": "capture.json",
        "metadata_digest": _digest(metadata_bytes),
        "metadata_snapshot": metadata,
    }


def _write_capture_owner(
    capture_dir: Path,
    metadata: dict[str, Any],
    *,
    payload_snapshot_entry: str | None = None,
    create: bool = False,
) -> None:
    owner_path = capture_dir / "owner.json"
    if owner_path.exists() and payload_snapshot_entry is None:
        existing = _load_json(owner_path)
        existing_entry = existing.get("payload_snapshot_entry")
        payload_snapshot_entry = existing_entry if isinstance(existing_entry, str) else None
    owner_bytes = _json_bytes(_capture_owner_record(metadata, payload_snapshot_entry))
    if create:
        _write_file_synced(owner_path, owner_bytes)
    else:
        _replace_file_after_sync(owner_path, owner_bytes)


def _capture_update_snapshot(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    owned_metadata, payload = _load_capture_owner(root, capture_dir)
    if owned_metadata != metadata:
        raise KbError("KB2_CAPTURE_OWNER_INVALID", "capture update metadata does not match its owner", 3)
    return json.loads(json.dumps(owned_metadata)), payload


def _update_capture(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    *,
    expected_metadata: dict[str, Any],
    expected_payload: bytes,
) -> None:
    _load_capture_owner(
        root,
        capture_dir,
        expected_metadata=expected_metadata,
        expected_payload=expected_payload,
    )
    update_id = _new_id("UPD")
    transaction_path = capture_dir / f"capture-update-{update_id}.json"
    candidate_path = capture_dir / f"metadata-candidate-{update_id}.json"
    claimed_path = capture_dir / f"metadata-claimed-{update_id}.json"
    expected_path = capture_dir / f"metadata-expected-{update_id}.json"
    transaction = {
        "schema": "capture-metadata-update/v0.1-pilot",
        "id": update_id,
        "capture_ref": f"capture://{capture_dir.name}",
        "candidate_entry": candidate_path.name,
        "claimed_entry": claimed_path.name,
        "expected_entry": expected_path.name,
        "expected_metadata_digest": _digest(_json_bytes(expected_metadata)),
        "new_metadata_digest": _digest(_json_bytes(metadata)),
        "payload_digest": _digest(expected_payload),
        "new_metadata_snapshot": metadata,
        "stage": "prepared",
        "created_at": _now(),
    }
    _write_file_synced(expected_path, _json_bytes(expected_metadata))
    _write_file_synced(candidate_path, _json_bytes(metadata))
    _write_file_synced(transaction_path, _json_bytes(transaction))
    _advance_capture_metadata_update(
        root,
        capture_dir,
        transaction_path,
        transaction,
        expected_metadata,
        expected_payload,
    )


def _load_capture_update_expected(
    root: Path,
    capture_dir: Path,
    transaction: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    update_id = transaction.get("id")
    expected_entry = transaction.get("expected_entry")
    if not isinstance(update_id, str) or expected_entry != f"metadata-expected-{update_id}.json":
        raise KbError("KB2_CAPTURE_UPDATE_INVALID", "capture update expected owner identity is invalid", 3)
    try:
        expected_path = _guard_plain_file(root, capture_dir / expected_entry)
        expected_bytes = expected_path.read_bytes()
        expected_metadata = json.loads(expected_bytes.decode("utf-8"))
    except (KbError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KbError("KB2_CAPTURE_UPDATE_INVALID", "capture update expected owner is unreadable", 3) from exc
    if (
        not isinstance(expected_metadata, dict)
        or _json_bytes(expected_metadata) != expected_bytes
        or _digest(expected_bytes) != transaction.get("expected_metadata_digest")
    ):
        raise KbError("KB2_CAPTURE_UPDATE_INVALID", "capture update expected owner is invalid", 3)
    return expected_metadata, expected_bytes


def _mark_capture_update_drift(
    root: Path,
    capture_dir: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    expected_metadata: dict[str, Any],
    expected_payload: bytes,
    claimed_path: Path,
) -> None:
    owner_id = _new_id("OBS")
    observed_metadata = capture_dir / f"metadata-observed-{owner_id}.json"
    _move_file_to_absent(claimed_path, observed_metadata)
    retained = [
        {
            "kind": "metadata-observed",
            "entry": observed_metadata.name,
            "digest": _digest(observed_metadata.read_bytes()),
        }
    ]
    payload_path = _guard_plain_file(root, capture_dir / "payload.bin")
    if payload_path.read_bytes() != expected_payload:
        observed_payload = capture_dir / f"payload-observed-{owner_id}.bin"
        _move_file_to_absent(payload_path, observed_payload)
        retained.append(
            {
                "kind": "payload-observed",
                "entry": observed_payload.name,
                "digest": _digest(observed_payload.read_bytes()),
            }
        )
        _write_file_synced(payload_path, expected_payload)
    canonical = _guard_plain_file(root, capture_dir / "capture.json", required=False)
    if canonical.exists():
        raise KbError("KB2_CAPTURE_OWNER_DRIFT", "capture metadata reappeared during drift retention", 3)
    _write_file_synced(canonical, _json_bytes(expected_metadata))
    drift_path = capture_dir / f"snapshot-drift-{owner_id}.json"
    drift = {
        "schema": "capture-snapshot-drift/v0.1-pilot",
        "owner": "durable-capture-writer/v0.1-pilot",
        "capture_ref": f"capture://{capture_dir.name}",
        "expected_payload_digest": _digest(expected_payload),
        "expected_metadata_digest": _digest(_json_bytes(expected_metadata)),
        "retained_entries": retained,
        "state": "needs-review",
        "detected_at": _now(),
    }
    _write_file_synced(drift_path, _json_bytes(drift))
    transaction["stage"] = "drift"
    transaction["drift_ref"] = drift_path.name
    transaction["drift_at"] = _now()
    _replace_file_after_sync(transaction_path, _json_bytes(transaction))
    raise KbError("KB2_CAPTURE_OWNER_DRIFT", "capture metadata changed while its update was claimed", 3)


def _advance_capture_metadata_update(
    root: Path,
    capture_dir: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    expected_metadata: dict[str, Any],
    expected_payload: bytes,
) -> None:
    candidate_path = _guard_plain_file(root, capture_dir / str(transaction["candidate_entry"]), required=False)
    claimed_path = _guard_plain_file(root, capture_dir / str(transaction["claimed_entry"]), required=False)
    canonical = _guard_plain_file(root, capture_dir / "capture.json", required=False)
    stage = transaction.get("stage")
    expected_bytes = _json_bytes(expected_metadata)
    new_bytes = _json_bytes(transaction["new_metadata_snapshot"])
    owned_expected, owned_expected_bytes = _load_capture_update_expected(root, capture_dir, transaction)
    if owned_expected_bytes != expected_bytes or owned_expected != expected_metadata:
        raise KbError("KB2_CAPTURE_UPDATE_INVALID", "capture update expected owner changed", 3)

    if stage == "prepared":
        if not candidate_path.is_file() or _digest(candidate_path.read_bytes()) != transaction.get("new_metadata_digest"):
            raise KbError("KB2_CAPTURE_UPDATE_INVALID", "capture metadata candidate is invalid", 3)
        if claimed_path.is_file() and not canonical.exists():
            pass
        else:
            try:
                _move_file_to_absent(canonical, claimed_path)
            except (FileNotFoundError, FileExistsError) as exc:
                raise KbError("KB2_CAPTURE_UPDATE_CONFLICT", "capture metadata could not be claimed", 3) from exc
        transaction["stage"] = "claimed"
        transaction["claimed_digest"] = _digest(claimed_path.read_bytes())
        transaction["claimed_at"] = _now()
        _replace_file_after_sync(transaction_path, _json_bytes(transaction))
        stage = "claimed"

    if stage == "claimed":
        claimed_bytes = _guard_plain_file(root, claimed_path).read_bytes()
        payload = _guard_plain_file(root, capture_dir / "payload.bin").read_bytes()
        if claimed_bytes != expected_bytes or payload != expected_payload:
            _mark_capture_update_drift(
                root,
                capture_dir,
                transaction_path,
                transaction,
                expected_metadata,
                expected_payload,
                claimed_path,
            )
        if canonical.exists():
            if canonical.read_bytes() != new_bytes or candidate_path.exists():
                raise KbError("KB2_CAPTURE_UPDATE_CONFLICT", "capture metadata reappeared before candidate install", 3)
        else:
            _move_file_to_absent(candidate_path, canonical)
            if canonical.read_bytes() != new_bytes:
                raise KbError("KB2_CAPTURE_UPDATE_CONFLICT", "installed capture metadata is invalid", 3)
        transaction["stage"] = "installed"
        transaction["installed_at"] = _now()
        _replace_file_after_sync(transaction_path, _json_bytes(transaction))
        stage = "installed"

    if stage == "installed":
        if canonical.read_bytes() != new_bytes or _guard_plain_file(root, claimed_path).read_bytes() != expected_bytes:
            raise KbError("KB2_CAPTURE_UPDATE_CONFLICT", "capture metadata update owner is inconsistent", 3)
        _write_capture_owner(capture_dir, transaction["new_metadata_snapshot"])
        transaction["stage"] = "applied"
        transaction["applied_at"] = _now()
        _replace_file_after_sync(transaction_path, _json_bytes(transaction))
        return
    if stage != "applied":
        raise KbError("KB2_CAPTURE_UPDATE_STAGE_INVALID", "capture metadata update has an invalid stage", 3)


def _capture_bytes(
    root: Path,
    payload: bytes,
    *,
    source: dict[str, Any],
    media_type: str = "text/plain; charset=utf-8",
) -> tuple[str, Path, dict[str, Any]]:
    root = _guard_root(root)
    pending = _ensure_protected_directory(root, "ingress/pending")
    capture_id = _new_id("CAP")
    staging = pending / (".staging-" + capture_id)
    final = pending / capture_id
    _guard_path(root, staging)
    _guard_path(root, final)
    metadata: dict[str, Any] = {
        "schema": "capture/v0.1-pilot",
        "id": capture_id,
        "state": "captured",
        "created_at": _now(),
        "media_type": media_type,
        "payload_digest": _digest(payload),
        "payload_entry": "payload.bin",
        "source": source,
        "user_structured_fields": 0,
    }
    staging.mkdir()
    try:
        _write_file_synced(staging / "payload.bin", payload)
        _write_file_synced(staging / "capture.json", _json_bytes(metadata))
        _write_capture_owner(staging, metadata, create=True)
        os.replace(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return capture_id, final, metadata


def _retain_capture_snapshot_drift(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    expected_payload: bytes,
) -> list[str]:
    expected_metadata = _json_bytes(metadata)
    leaves = (
        ("payload-observed", capture_dir / "payload.bin", expected_payload, ".bin"),
        ("metadata-observed", capture_dir / "capture.json", expected_metadata, ".json"),
    )
    owner_id = _new_id("OBS")
    retained: list[dict[str, str]] = []
    changed: list[str] = []
    for kind, source, expected, suffix in leaves:
        guarded = _guard_plain_file(root, source)
        observed = guarded.read_bytes()
        if observed == expected:
            continue
        destination = _guard_plain_file(
            root,
            capture_dir / f"{kind}-{owner_id}{suffix}",
            required=False,
        )
        _move_file_to_absent(guarded, destination)
        claimed = destination.read_bytes()
        retained.append({"kind": kind, "entry": destination.name, "digest": _digest(claimed)})
        changed.append(str(destination.relative_to(root)))
        _write_file_synced(guarded, expected)

    owner_path = capture_dir / f"snapshot-drift-{owner_id}.json"
    owner = {
        "schema": "capture-snapshot-drift/v0.1-pilot",
        "owner": "durable-capture-writer/v0.1-pilot",
        "capture_ref": f"capture://{metadata['id']}",
        "expected_payload_digest": _digest(expected_payload),
        "expected_metadata_digest": _digest(expected_metadata),
        "retained_entries": retained,
        "state": "needs-review",
        "detected_at": _now(),
    }
    _write_file_synced(owner_path, _json_bytes(owner))
    changed.append(str(owner_path.relative_to(root)))
    if (
        _guard_plain_file(root, capture_dir / "payload.bin").read_bytes() != expected_payload
        or _guard_plain_file(root, capture_dir / "capture.json").read_bytes() != expected_metadata
    ):
        raise KbError(
            "KB2_CAPTURE_OWNER_DRIFT",
            "capture owner changed while its snapshot was being retained",
            3,
            {"capture_ref": owner["capture_ref"], "committed": True},
            changed,
        )
    return changed


def _seal_capture_payload_snapshot(root: Path, capture_dir: Path, metadata: dict[str, Any], payload: bytes) -> None:
    snapshot_path = _guard_plain_file(root, capture_dir / "payload-snapshot.bin", required=False)
    if snapshot_path.exists():
        if snapshot_path.read_bytes() != payload:
            raise KbError("KB2_CAPTURE_OWNER_DRIFT", "capture payload snapshot does not match verified bytes", 3)
    else:
        _write_file_synced(snapshot_path, payload)
    _write_capture_owner(capture_dir, metadata, payload_snapshot_entry="payload-snapshot.bin")


def _load_capture_owner(
    root: Path,
    capture_dir: Path,
    *,
    expected_metadata: dict[str, Any] | None = None,
    expected_payload: bytes | None = None,
) -> tuple[dict[str, Any], bytes]:
    capture_dir = _guard_plain_directory(root, capture_dir)
    if not _CAPTURE_RE.fullmatch(capture_dir.name):
        raise KbError("KB2_CAPTURE_ENTRY_INVALID", "capture owner directory has an invalid identity", 3)
    owner_path = _guard_plain_file(root, capture_dir / "owner.json")
    owner = _load_json(owner_path)
    metadata_snapshot = owner.get("metadata_snapshot")
    snapshot_entry = owner.get("payload_snapshot_entry")
    if (
        owner.get("schema") != "capture-owner/v0.1-pilot"
        or owner.get("owner") != "durable-capture-writer/v0.1-pilot"
        or owner.get("capture_id") != capture_dir.name
        or owner.get("payload_entry") != "payload.bin"
        or owner.get("metadata_entry") != "capture.json"
        or not isinstance(metadata_snapshot, dict)
        or metadata_snapshot.get("id") != capture_dir.name
        or metadata_snapshot.get("payload_entry") != "payload.bin"
        or owner.get("payload_digest") != metadata_snapshot.get("payload_digest")
        or owner.get("metadata_digest") != _digest(_json_bytes(metadata_snapshot))
        or snapshot_entry not in {None, "payload-snapshot.bin"}
    ):
        raise KbError("KB2_CAPTURE_OWNER_INVALID", "capture owner contract is invalid", 3)
    if expected_metadata is not None and metadata_snapshot != expected_metadata:
        raise KbError("KB2_CAPTURE_OWNER_INVALID", "capture caller metadata does not match its strict owner", 3)
    if expected_payload is None:
        expected_payload_path = _guard_plain_file(
            root,
            capture_dir / (snapshot_entry or "payload.bin"),
        )
        verified_payload = expected_payload_path.read_bytes()
    else:
        verified_payload = expected_payload
    if _digest(verified_payload) != owner.get("payload_digest"):
        raise KbError("KB2_CAPTURE_OWNER_INVALID", "capture owner payload snapshot is invalid", 3)

    for drift_path in sorted(capture_dir.glob("snapshot-drift-OBS-*.json")):
        drift_path = _guard_plain_file(root, drift_path)
        drift = _load_json(drift_path)
        entries = drift.get("retained_entries")
        if (
            drift.get("schema") != "capture-snapshot-drift/v0.1-pilot"
            or drift.get("owner") != "durable-capture-writer/v0.1-pilot"
            or drift.get("capture_ref") != f"capture://{capture_dir.name}"
            or drift.get("state") != "needs-review"
            or not isinstance(entries, list)
        ):
            raise KbError("KB2_CAPTURE_OWNER_INVALID", "capture drift owner contract is invalid", 3)
        for retained in entries:
            if not isinstance(retained, dict):
                raise KbError("KB2_CAPTURE_OWNER_INVALID", "capture retained entry is invalid", 3)
            entry = retained.get("entry")
            digest = retained.get("digest")
            if (
                not isinstance(entry, str)
                or not re.fullmatch(r"(?:payload|metadata)-observed-OBS-[0-9A-HJKMNP-TV-Z]{26}\.(?:bin|json)", entry)
                or not isinstance(digest, str)
                or not _DIGEST_RE.fullmatch(digest)
            ):
                raise KbError("KB2_CAPTURE_OWNER_INVALID", "capture retained entry is invalid", 3)
            retained_path = _guard_plain_file(root, capture_dir / entry)
            if _digest(retained_path.read_bytes()) != digest:
                raise KbError("KB2_CAPTURE_RETAINED_DRIFT", "capture retained owner changed", 3)
        raise KbError("KB2_CAPTURE_OWNER_DRIFT", "capture owner has a sticky retained drift", 3)

    payload_path = _guard_plain_file(root, capture_dir / "payload.bin")
    metadata_path = _guard_plain_file(root, capture_dir / "capture.json")
    if payload_path.read_bytes() != verified_payload or metadata_path.read_bytes() != _json_bytes(metadata_snapshot):
        _retain_capture_snapshot_drift(root, capture_dir, metadata_snapshot, verified_payload)
        raise KbError("KB2_CAPTURE_OWNER_DRIFT", "capture payload or metadata drifted from its owner", 3)
    return metadata_snapshot, verified_payload


def _verify_capture_snapshot(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    expected_payload: bytes,
) -> None:
    _load_capture_owner(
        root,
        capture_dir,
        expected_metadata=metadata,
        expected_payload=expected_payload,
    )


def _decode_captured_utf8(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    *,
    snapshot: bytes | None = None,
) -> str:
    if snapshot is None:
        payload_path = _guard_plain_file(root, capture_dir / "payload.bin")
        payload = payload_path.read_bytes()
        if _digest(payload) != metadata.get("payload_digest"):
            raise KbError("KB2_CAPTURE_OWNER_DRIFT", "capture payload does not match its metadata owner", 3)
    else:
        payload = snapshot
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        expected_metadata, expected_payload = _capture_update_snapshot(root, capture_dir, metadata)
        metadata["state"] = "needs-review"
        metadata["route"] = {"result": "needs-review", "reason": "invalid-utf8"}
        _update_capture(
            root,
            capture_dir,
            metadata,
            expected_metadata=expected_metadata,
            expected_payload=expected_payload,
        )
        raise KbError(
            "KB2_INPUT_ENCODING_INVALID",
            "input was captured but is not valid UTF-8",
            2,
            {"capture_ref": f"capture://{metadata['id']}", "committed": True},
            [str(capture_dir)],
        ) from exc


def _render_note(capture_id: str, text: str, created_at: str) -> bytes:
    nonempty = next((line.strip() for line in text.splitlines() if line.strip()), "未命名捕获")
    title = re.sub(r"^[#>\-*\s]+", "", nonempty)[:80] or "未命名捕获"
    body = text.rstrip() + "\n"
    rendered = (
        "---\n"
        "schema: garden-note/v0.1-pilot\n"
        f"capture: capture://{capture_id}\n"
        f"created_at: {created_at}\n"
        "route: garden\n"
        "security_profile: personal-full/v1\n"
        "generated_by: ai-organizer-pilot\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}"
    )
    return rendered.encode("utf-8")


def _create_organizer_state(
    root: Path,
    capture_id: str,
    capture_ref: str,
    garden_ref: str,
    note_bytes: bytes,
) -> tuple[Path, dict[str, Any]]:
    state_dir = _ensure_plain_directory(root, f"governance/organizer-state/{capture_id}")
    base_path = state_dir / "base.md"
    _write_file_synced(base_path, note_bytes)
    state = {
        "schema": "organizer-state/v0.1-pilot",
        "capture_ref": capture_ref,
        "garden_ref": garden_ref,
        "base_digest": _digest(note_bytes),
        "active_override": None,
        "decision": {
            "route": {"result": "garden-organized", "garden_ref": garden_ref},
            "security": {
                "precheck": "passed",
                "profile": "personal-full/v1",
                "policy": "deterministic-secret-precheck/v0.1-pilot",
                "latest_hold": None,
            },
        },
        "updated_at": _now(),
    }
    _write_file_synced(state_dir / "state.json", _json_bytes(state))
    return state_dir, state


def ingest_bytes(
    root: Path,
    payload: bytes,
    *,
    fail_after_capture: bool = False,
    context_ref: str | None = None,
    base_digest: str | None = None,
    fail_after_context_intent: bool = False,
    before_context_claim: Any | None = None,
) -> dict[str, Any]:
    root = _guard_root(root)
    capture_id, capture_dir, metadata = _capture_bytes(
        root,
        payload,
        source={"kind": "direct-stdin", **({"target": context_ref} if context_ref else {})},
    )
    capture_ref = f"capture://{capture_id}"
    if fail_after_capture:
        raise KbError(
            "KB2_INJECTED_AFTER_CAPTURE",
            "injected failure after durable capture",
            4,
            {"capture_ref": capture_ref, "committed": True},
            [str(capture_dir.relative_to(root))],
        )

    _verify_capture_snapshot(root, capture_dir, metadata, payload)
    reasons = _secret_reasons(payload)
    _verify_capture_snapshot(root, capture_dir, metadata, payload)
    if reasons:
        hold = _ensure_protected_directory(root, "ingress/restricted-hold")
        hold_record = {
            "schema": "restricted-hold/v0.1-pilot",
            "capture_ref": capture_ref,
            "created_at": _now(),
            "payload_digest": metadata["payload_digest"],
            "reason_codes": reasons,
            "contains_payload": False,
            "externalization_pending": True,
        }
        hold_path = hold / f"{capture_id}.json"
        _replace_file_after_sync(hold_path, _json_bytes(hold_record))
        expected_metadata, expected_payload = _capture_update_snapshot(root, capture_dir, metadata)
        metadata["state"] = "restricted-hold"
        metadata["route"] = {
            "result": "restricted-hold",
            "reason_codes": reasons,
            "externalization_pending": True,
        }
        _update_capture(
            root,
            capture_dir,
            metadata,
            expected_metadata=expected_metadata,
            expected_payload=expected_payload,
        )
        return {
            "route": "restricted-hold",
            "capture_ref": capture_ref,
            "payload_digest": metadata["payload_digest"],
            "user_structured_fields": 0,
            "changed": [str(capture_dir.relative_to(root)), str(hold_path.relative_to(root))],
        }

    _seal_capture_payload_snapshot(root, capture_dir, metadata, payload)
    text = _decode_captured_utf8(root, capture_dir, metadata, snapshot=payload)

    from .context import context_intent, create_or_update_context

    if context_ref is not None or context_intent(text):
        return create_or_update_context(
            root,
            capture_dir,
            metadata,
            payload,
            text,
            context_ref=context_ref,
            base_digest=base_digest,
            fail_after_context_intent=fail_after_context_intent,
            before_context_claim=before_context_claim,
        )

    notes = _ensure_plain_directory(root, "garden/notes")
    _ensure_plain_directory(root, "governance/overrides")
    note_path = notes / f"{capture_id}.md"
    note_bytes = _render_note(capture_id, text, metadata["created_at"])
    _verify_capture_snapshot(root, capture_dir, metadata, payload)
    _replace_file_after_sync(note_path, note_bytes)
    garden_ref = f"garden://notes/{capture_id}.md"
    _create_organizer_state(root, capture_id, capture_ref, garden_ref, note_bytes)
    expected_metadata, expected_payload = _capture_update_snapshot(root, capture_dir, metadata)
    metadata["state"] = "garden-organized"
    metadata["route"] = {"result": "garden-organized", "garden_ref": garden_ref}
    _update_capture(
        root,
        capture_dir,
        metadata,
        expected_metadata=expected_metadata,
        expected_payload=expected_payload,
    )
    return {
        "route": "garden-organized",
        "capture_ref": capture_ref,
        "garden_ref": garden_ref,
        "payload_digest": metadata["payload_digest"],
        "user_structured_fields": 0,
        "changed": [str(capture_dir.relative_to(root)), str(note_path.relative_to(root))],
    }


def ingest_text(
    root: Path,
    text: str,
    *,
    fail_after_capture: bool = False,
    context_ref: str | None = None,
    base_digest: str | None = None,
    fail_after_context_intent: bool = False,
    before_context_claim: Any | None = None,
) -> dict[str, Any]:
    return ingest_bytes(
        root,
        text.encode("utf-8"),
        fail_after_capture=fail_after_capture,
        context_ref=context_ref,
        base_digest=base_digest,
        fail_after_context_intent=fail_after_context_intent,
        before_context_claim=before_context_claim,
    )


def _garden_path(root: Path, ref: str) -> tuple[Path, str]:
    prefix = "garden://notes/"
    if not ref.startswith(prefix):
        raise KbError("KB2_REF_INVALID", "only garden note references are supported in this slice", 2)
    name = ref[len(prefix) :]
    if "/" in name or "\\" in name or not name.endswith(".md"):
        raise KbError("KB2_REF_INVALID", "garden note reference has an invalid path", 2)
    capture_id = name[:-3]
    if not _CAPTURE_RE.fullmatch(capture_id):
        raise KbError("KB2_REF_INVALID", "garden note reference has an invalid capture identity", 2)
    path = _guard_plain_file(root, root / "garden" / "notes" / name)
    return path, capture_id


def _garden_location(root: Path, ref: str, *, require_file: bool) -> tuple[Path, str]:
    prefix = "garden://notes/"
    if not ref.startswith(prefix):
        raise KbError("KB2_REF_INVALID", "only garden note references are supported in this slice", 2)
    name = ref[len(prefix) :]
    if "/" in name or "\\" in name or not name.endswith(".md"):
        raise KbError("KB2_REF_INVALID", "garden note reference has an invalid path", 2)
    capture_id = name[:-3]
    if not _CAPTURE_RE.fullmatch(capture_id):
        raise KbError("KB2_REF_INVALID", "garden note reference has an invalid capture identity", 2)
    path = _guard_plain_file(root, root / "garden" / "notes" / name, required=require_file)
    return path, capture_id


def _organizer_state_directory(root: Path, capture_id: str, *, require: bool = True) -> Path:
    path = root / "governance" / "organizer-state" / capture_id
    if require:
        return _guard_plain_directory(root, path)
    return _ensure_plain_directory(root, f"governance/organizer-state/{capture_id}")


def _load_organizer_state(root: Path, capture_id: str, ref: str) -> tuple[Path, dict[str, Any]]:
    state_dir = _organizer_state_directory(root, capture_id)
    state = _load_json(_guard_plain_file(root, state_dir / "state.json"))
    if state.get("garden_ref") != ref or state.get("capture_ref") != f"capture://{capture_id}":
        raise KbError("KB2_STATE_INVALID", "organizer state does not own the requested Garden note")
    return state_dir, state


def _save_organizer_state(state_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _replace_file_after_sync(state_dir / "state.json", _json_bytes(state))


def _decision_route(state: dict[str, Any]) -> dict[str, Any]:
    decision = state.get("decision")
    if not isinstance(decision, dict) or not isinstance(decision.get("route"), dict):
        raise KbError("KB2_STATE_INVALID", "persisted organizer decision is missing")
    return decision["route"]


def _load_override_owner(
    root: Path,
    path: Path,
    *,
    expected_id: str | None = None,
    expected_target: str | None = None,
    expected_observed_digest: str | None = None,
    correction_transaction: dict[str, Any] | None = None,
    displaced: Path | None = None,
    candidate: Path | None = None,
) -> dict[str, Any]:
    path = _guard_plain_file(root, path)
    if not re.fullmatch(r"OVR-[0-9A-HJKMNP-TV-Z]{26}\.yaml", path.name):
        raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "override entry has an invalid filename", 3)
    record = _load_json(path)
    override_id = record.get("id")
    target = record.get("target")
    supersedes = record.get("supersedes")
    correction_ref = record.get("correction_capture_ref")
    try:
        created_at = datetime.fromisoformat(str(record.get("created_at")))
    except ValueError as exc:
        raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "override created_at is invalid", 3) from exc
    if (
        record.get("schema") != "human-override/v0.1-pilot"
        or not isinstance(override_id, str)
        or not _OVERRIDE_RE.fullmatch(override_id)
        or override_id != path.stem
        or not isinstance(target, str)
        or not re.fullmatch(
            r"(?:garden://notes/CAP-[0-9A-HJKMNP-TV-Z]{26}\.md|context://CTX-[0-9A-HJKMNP-TV-Z]{26})",
            target,
        )
        or record.get("scope") != {"kind": "object", "ref": target}
        or record.get("actor") not in {"human-direct-edit", "human-natural-language-correction"}
        or not isinstance(record.get("reason"), str)
        or not isinstance(record.get("base_digest"), str)
        or not _DIGEST_RE.fullmatch(record["base_digest"])
        or not isinstance(record.get("observed_digest"), str)
        or not _DIGEST_RE.fullmatch(record["observed_digest"])
        or (supersedes is not None and (not isinstance(supersedes, str) or not _OVERRIDE_RE.fullmatch(supersedes)))
        or record.get("diff_format") != "unified"
        or not isinstance(record.get("diff"), str)
        or created_at.tzinfo is None
    ):
        raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "override owner contract is invalid", 3)
    if expected_id is not None and override_id != expected_id:
        raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "override identity does not match its owner", 3)
    if expected_target is not None and target != expected_target:
        raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "override target does not match its owner", 3)
    if expected_observed_digest is not None and record.get("observed_digest") != expected_observed_digest:
        raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "active override digest does not match organizer state", 3)

    if correction_ref is None:
        if record.get("actor") != "human-direct-edit":
            raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "direct override provenance is invalid", 3)
    else:
        if (
            not isinstance(correction_ref, str)
            or not correction_ref.startswith("capture://")
            or not _CAPTURE_RE.fullmatch(correction_ref[len("capture://") :])
            or record.get("actor") != "human-natural-language-correction"
            or record.get("reason") != f"natural-language correction from {correction_ref}"
        ):
            raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "correction override provenance is invalid", 3)

    if correction_transaction is not None:
        if displaced is None or candidate is None:
            raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "correction override evidence is missing", 3)
        expected_diff = "".join(
            difflib.unified_diff(
                displaced.read_text(encoding="utf-8").splitlines(keepends=True),
                candidate.read_text(encoding="utf-8").splitlines(keepends=True),
                fromfile="organizer-base",
                tofile="human-correction",
            )
        )
        if (
            override_id != correction_transaction.get("override_id")
            or target != correction_transaction.get("target")
            or record.get("base_digest") != correction_transaction.get("target_base_digest")
            or record.get("observed_digest") != correction_transaction.get("candidate_digest")
            or supersedes != correction_transaction.get("supersedes")
            or correction_ref != correction_transaction.get("correction_capture_ref")
            or record.get("diff") != expected_diff
        ):
            raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "correction override evidence does not match its transaction", 3)
    return record


def _find_existing_override(
    root: Path,
    overrides: Path,
    target: str,
    base_digest: str,
    observed_digest: str,
    supersedes: str | None,
    actor: str,
    correction_capture_ref: str | None,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for path in sorted(overrides.glob("OVR-*.yaml")):
        record = _load_override_owner(root, path)
        correction_identity_matches = (
            record.get("correction_capture_ref") == correction_capture_ref
            if correction_capture_ref is not None
            else "correction_capture_ref" not in record
        )
        if (
            record.get("target") == target
            and record.get("base_digest") == base_digest
            and record.get("observed_digest") == observed_digest
            and record.get("supersedes") == supersedes
            and record.get("actor") == actor
            and record.get("scope") == {"kind": "object", "ref": target}
            and correction_identity_matches
        ):
            matches.append(record)
    if len(matches) > 1:
        raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "multiple overrides claim the same observed target state", 3)
    return matches[0] if matches else None


def _restricted_stub(capture_id: str, hold_ref: str) -> bytes:
    return (
        "---\n"
        "schema: garden-note/v0.1-pilot\n"
        f"capture: capture://{capture_id}\n"
        "route: restricted-hold\n"
        "security_profile: restricted-summary/v1\n"
        "generated_by: deterministic-safety-precheck\n"
        "---\n\n"
        "# 内容已隔离\n\n"
        "外部编辑触发了安全下限；原始编辑已保存在受保护边界，普通 Garden 不保留其正文。\n\n"
        f"隔离引用：{hold_ref}\n"
    ).encode("utf-8")


def _quarantine_garden_edit(
    root: Path,
    note_path: Path,
    capture_id: str,
    ref: str,
    state_dir: Path,
    state: dict[str, Any],
    current: bytes,
    current_digest: str,
    reasons: list[str],
    *,
    fail_after_quarantine: bool = False,
    fail_after_restore: bool = False,
) -> tuple[str, str, list[str]]:
    quarantine_root = _ensure_protected_directory(root, "ingress/quarantine")
    hold_id = _new_id("HLD")
    hold_ref = f"hold://{hold_id}"
    quarantine_ref = f"quarantine://{hold_id}"
    staging = quarantine_root / (".staging-" + hold_id)
    final = quarantine_root / hold_id
    _guard_path(root, staging)
    _guard_path(root, final)
    transaction: dict[str, Any] = {
        "schema": "security-quarantine/v0.1-pilot",
        "id": hold_id,
        "owner": "durable-capture-security-quarantine/v0.1-pilot",
        "state": "externalization_pending",
        "stage": "quarantine-committed",
        "capture_ref": f"capture://{capture_id}",
        "garden_ref": ref,
        "created_at": _now(),
        "payload_digest": current_digest,
        "payload_entry": "payload.bin",
        "reason_codes": reasons,
        "quarantine_ref": quarantine_ref,
        "hold_ref": hold_ref,
    }
    staging.mkdir()
    try:
        _write_file_synced(staging / "payload.bin", current)
        _write_file_synced(staging / "quarantine.json", _json_bytes(transaction))
        os.replace(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if fail_after_quarantine:
        raise KbError(
            "KB2_INJECTED_AFTER_QUARANTINE",
            "injected failure after quarantine commit",
            4,
            {"garden_ref": ref, "hold_ref": hold_ref, "quarantine_ref": quarantine_ref},
            [str(final.relative_to(root))],
        )

    action = _restore_quarantined_garden(root, final)
    if fail_after_restore:
        raise KbError(
            "KB2_INJECTED_AFTER_GARDEN_RESTORE",
            "injected failure after Garden restore and before decision update",
            4,
            {"garden_ref": ref, "hold_ref": hold_ref, "quarantine_ref": quarantine_ref},
            [str(final.relative_to(root)), str(note_path.relative_to(root))],
        )
    summary_path = _finalize_quarantine_decision(root, final)
    changed = [
        str(final.relative_to(root)),
        str(summary_path.relative_to(root)),
        str(note_path.relative_to(root)),
        str((state_dir / "state.json").relative_to(root)),
    ]
    return hold_ref, action, changed


def _load_quarantine_transaction(root: Path, quarantine_dir: Path) -> tuple[Path, dict[str, Any]]:
    quarantine_dir = _guard_plain_directory(root, quarantine_dir)
    if not re.fullmatch(r"HLD-[0-9A-HJKMNP-TV-Z]{26}", quarantine_dir.name):
        raise KbError("KB2_QUARANTINE_ENTRY_INVALID", "quarantine entry has an invalid identity", 3)
    transaction_path = _guard_plain_file(root, quarantine_dir / "quarantine.json")
    transaction = _load_json(transaction_path)
    hold_id = quarantine_dir.name
    garden_ref = transaction.get("garden_ref")
    capture_ref = transaction.get("capture_ref")
    garden_prefix = "garden://notes/"
    garden_name = garden_ref[len(garden_prefix) :] if isinstance(garden_ref, str) and garden_ref.startswith(garden_prefix) else ""
    garden_capture_id = garden_name[:-3] if garden_name.endswith(".md") and "/" not in garden_name and "\\" not in garden_name else ""
    if (
        transaction.get("schema") != "security-quarantine/v0.1-pilot"
        or transaction.get("owner") != "durable-capture-security-quarantine/v0.1-pilot"
        or transaction.get("state") != "externalization_pending"
        or transaction.get("id") != hold_id
        or transaction.get("payload_entry") != "payload.bin"
        or transaction.get("hold_ref") != f"hold://{hold_id}"
        or transaction.get("quarantine_ref") != f"quarantine://{hold_id}"
        or not _CAPTURE_RE.fullmatch(garden_capture_id)
        or capture_ref != f"capture://{garden_capture_id}"
    ):
        raise KbError("KB2_QUARANTINE_ENTRY_INVALID", "quarantine transaction owner contract is invalid", 3)
    payload_path = _guard_plain_file(root, quarantine_dir / "payload.bin")
    if _digest(payload_path.read_bytes()) != transaction.get("payload_digest"):
        raise KbError("KB2_QUARANTINE_PAYLOAD_INVALID", "quarantine payload does not match its recorded digest", 3)
    retained_entries = transaction.get("retained_observed_entries", [])
    if not isinstance(retained_entries, list):
        raise KbError("KB2_QUARANTINE_RETAINED_INVALID", "retained observed ownership is invalid", 3)
    for retained in retained_entries:
        if not isinstance(retained, dict):
            raise KbError("KB2_QUARANTINE_RETAINED_INVALID", "retained observed ownership is invalid", 3)
        entry = retained.get("entry")
        expected_digest = retained.get("digest")
        if (
            not isinstance(entry, str)
            or not re.fullmatch(r"garden-observed(?:-OBS-[0-9A-HJKMNP-TV-Z]{26})?\.bin", entry)
            or not isinstance(expected_digest, str)
        ):
            raise KbError("KB2_QUARANTINE_RETAINED_INVALID", "retained observed entry is invalid", 3)
        retained_path = _guard_plain_file(root, quarantine_dir / entry, required=False)
        actual_digest = _digest(retained_path.read_bytes()) if retained_path.is_file() else "missing"
        if actual_digest != expected_digest:
            transaction["stage"] = "recovery-conflict"
            transaction["recovery_conflict_entry"] = entry
            transaction["recovery_conflict_digest"] = actual_digest
            transaction["recovery_conflict_at"] = _now()
            _replace_file_after_sync(transaction_path, _json_bytes(transaction))
            raise KbError(
                "KB2_QUARANTINE_RETAINED_DRIFT",
                "retained observed bytes changed or disappeared; recovery remains unresolved",
                3,
            )
    retained_bases = transaction.get("retained_organizer_base_entries", [])
    if not isinstance(retained_bases, list):
        raise KbError("KB2_QUARANTINE_RETAINED_INVALID", "retained organizer base ownership is invalid", 3)
    for retained in retained_bases:
        if not isinstance(retained, dict):
            raise KbError("KB2_QUARANTINE_RETAINED_INVALID", "retained organizer base ownership is invalid", 3)
        entry = retained.get("entry")
        expected_digest = retained.get("digest")
        if (
            not isinstance(entry, str)
            or not re.fullmatch(r"organizer-base-observed(?:-OBS-[0-9A-HJKMNP-TV-Z]{26})?\.bin", entry)
            or not isinstance(expected_digest, str)
            or not _DIGEST_RE.fullmatch(expected_digest)
        ):
            raise KbError("KB2_QUARANTINE_RETAINED_INVALID", "retained organizer base entry is invalid", 3)
        retained_path = _guard_plain_file(root, quarantine_dir / entry, required=False)
        actual_digest = _digest(retained_path.read_bytes()) if retained_path.is_file() else "missing"
        if actual_digest != expected_digest:
            transaction["stage"] = "recovery-conflict"
            transaction["recovery_conflict_code"] = "KB2_ORGANIZER_BASE_RETAINED_DRIFT"
            transaction["recovery_conflict_entry"] = entry
            transaction["recovery_conflict_digest"] = actual_digest
            transaction["recovery_conflict_at"] = _now()
            _replace_file_after_sync(transaction_path, _json_bytes(transaction))
            raise KbError(
                "KB2_ORGANIZER_BASE_RETAINED_DRIFT",
                "retained organizer base changed or disappeared; recovery remains unresolved",
                3,
            )
    return transaction_path, transaction


def _record_retained_observed(transaction: dict[str, Any], observed_path: Path, observed_digest: str) -> None:
    retained_entries = transaction.setdefault("retained_observed_entries", [])
    if not isinstance(retained_entries, list):
        raise KbError("KB2_QUARANTINE_RETAINED_INVALID", "retained observed ownership is invalid", 3)
    if not any(isinstance(item, dict) and item.get("entry") == observed_path.name for item in retained_entries):
        retained_entries.append(
            {
                "entry": observed_path.name,
                "digest": observed_digest,
                "retained_at": _now(),
            }
        )
    transaction["retained_observed_entry"] = observed_path.name
    transaction["retained_observed_digest"] = observed_digest


def _record_retained_organizer_base(
    transaction: dict[str, Any],
    observed_path: Path,
    observed_digest: str,
) -> None:
    retained_entries = transaction.setdefault("retained_organizer_base_entries", [])
    if not isinstance(retained_entries, list):
        raise KbError("KB2_QUARANTINE_RETAINED_INVALID", "retained organizer base ownership is invalid", 3)
    retained_entries.append(
        {
            "entry": observed_path.name,
            "digest": observed_digest,
            "retained_at": _now(),
        }
    )


def _persist_quarantine_conflict(
    transaction_path: Path,
    transaction: dict[str, Any],
    *,
    observed_digest: str,
    observed_entry: str | None = None,
    code: str | None = None,
) -> None:
    transaction["recovery_conflict_digest"] = observed_digest
    if observed_entry is not None:
        transaction["recovery_conflict_entry"] = observed_entry
    if code is not None:
        transaction["recovery_conflict_code"] = code
    transaction["stage"] = "recovery-conflict"
    transaction["recovery_conflict_at"] = _now()
    _replace_file_after_sync(transaction_path, _json_bytes(transaction))


def _install_bytes_to_absent(note_path: Path, content: bytes) -> None:
    fd, candidate_name = tempfile.mkstemp(prefix=".kb2-restore-", dir=note_path.parent)
    candidate = Path(candidate_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _move_file_to_absent(candidate, note_path)
    finally:
        if candidate.exists():
            candidate.unlink()


def _claim_and_classify_third_garden_version(
    root: Path,
    quarantine_dir: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    note_path: Path,
    replacement: bytes,
) -> None:
    while True:
        if not note_path.exists():
            try:
                _install_bytes_to_absent(note_path, replacement)
            except FileExistsError:
                continue
            _persist_quarantine_conflict(
                transaction_path,
                transaction,
                observed_digest="missing-before-claim",
            )
            raise KbError(
                "KB2_QUARANTINE_RECOVERY_CONFLICT",
                "Garden disappeared before third-version claim; safe side was restored for review",
                3,
            )

        destination = _guard_plain_file(root, quarantine_dir / "garden-observed.bin", required=False)
        if destination.exists():
            destination = _guard_plain_file(
                root,
                quarantine_dir / f"garden-observed-{_new_id('OBS')}.bin",
                required=False,
            )
        try:
            _move_file_to_absent(note_path, destination)
        except FileNotFoundError:
            continue
        except FileExistsError:
            continue

        claimed = destination.read_bytes()
        claimed_digest = _digest(claimed)
        _record_retained_observed(transaction, destination, claimed_digest)
        _persist_quarantine_conflict(
            transaction_path,
            transaction,
            observed_digest=claimed_digest,
            observed_entry=destination.name,
        )
        sensitive = bool(_secret_reasons(claimed))
        desired = replacement if sensitive else claimed
        desired_digest = _digest(desired)

        if note_path.exists():
            continue
        try:
            _install_bytes_to_absent(note_path, desired)
        except FileExistsError:
            continue
        if not note_path.is_file():
            continue
        postcondition = note_path.read_bytes()
        if _digest(postcondition) != desired_digest:
            continue
        if sensitive and _secret_reasons(postcondition):
            continue
        raise KbError(
            "KB2_QUARANTINE_RECOVERY_CONFLICT",
            "third Garden version was claimed, classified, and restored under sticky review",
            3,
            {
                "garden_ref": transaction.get("garden_ref"),
                "quarantine_ref": transaction.get("quarantine_ref"),
                "claimed_digest": claimed_digest,
                "claimed_sensitive": sensitive,
            },
        )


def _reconcile_quarantine_organizer_base(
    root: Path,
    quarantine_dir: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    state_dir: Path,
    state: dict[str, Any],
) -> bytes:
    capture_id = str(transaction["capture_ref"]).removeprefix("capture://")
    replacement = _restricted_stub(capture_id, str(transaction["hold_ref"]))
    replacement_digest = _digest(replacement)
    transaction["organizer_base_replacement_digest"] = replacement_digest
    _replace_file_after_sync(transaction_path, _json_bytes(transaction))
    base_path = _guard_plain_file(root, state_dir / "base.md", required=False)
    while True:
        if base_path.exists():
            current_base = _guard_plain_file(root, base_path).read_bytes()
            if current_base == replacement:
                break
            destination = _guard_plain_file(
                root,
                quarantine_dir / "organizer-base-observed.bin",
                required=False,
            )
            if destination.exists():
                destination = _guard_plain_file(
                    root,
                    quarantine_dir / f"organizer-base-observed-{_new_id('OBS')}.bin",
                    required=False,
                )
            try:
                _move_file_to_absent(base_path, destination)
            except (FileNotFoundError, FileExistsError):
                continue
            claimed_base = destination.read_bytes()
            _record_retained_organizer_base(transaction, destination, _digest(claimed_base))
            _replace_file_after_sync(transaction_path, _json_bytes(transaction))
            continue
        try:
            _install_bytes_to_absent(base_path, replacement)
        except FileExistsError:
            continue
        break
    if state.get("base_digest") != replacement_digest:
        state["base_digest"] = replacement_digest
        _save_organizer_state(state_dir, state)
    if _guard_plain_file(root, base_path).read_bytes() != replacement:
        return _reconcile_quarantine_organizer_base(
            root,
            quarantine_dir,
            transaction_path,
            transaction,
            state_dir,
            state,
        )
    return replacement


def _restore_quarantined_garden(
    root: Path,
    quarantine_dir: Path,
    *,
    before_quarantine_claim: Any | None = None,
) -> str:
    transaction_path, transaction = _load_quarantine_transaction(root, quarantine_dir)
    ref = str(transaction["garden_ref"])
    note_path, capture_id = _garden_location(root, ref, require_file=False)
    state_dir, state = _load_organizer_state(root, capture_id, ref)
    base_path = _guard_plain_file(root, state_dir / "base.md", required=False)
    last_safe = base_path.read_bytes() if base_path.is_file() else b""
    if last_safe and _digest(last_safe) == state.get("base_digest") and not _secret_reasons(last_safe):
        replacement = last_safe
        action = "restored-last-safe-base"
        base_issue_code = None
    else:
        action = "replaced-with-quarantine-stub"
        base_issue_code = "KB2_ORGANIZER_BASE_MISSING" if not base_path.exists() else "KB2_ORGANIZER_BASE_DRIFT"
        transaction["organizer_base_expected_digest"] = state.get("base_digest")
        transaction["organizer_base_issue"] = "missing" if not base_path.exists() else "drift"
        replacement = _reconcile_quarantine_organizer_base(
            root,
            quarantine_dir,
            transaction_path,
            transaction,
            state_dir,
            state,
        )

    observed_digest = str(transaction["payload_digest"])
    replacement_digest = _digest(replacement)
    observed_copy = _guard_plain_file(root, quarantine_dir / "garden-observed.bin", required=False)
    if note_path.exists():
        actual_digest = _digest(note_path.read_bytes())
        if actual_digest == replacement_digest:
            pass
        elif actual_digest == observed_digest:
            if before_quarantine_claim is not None:
                before_quarantine_claim()
            destination = observed_copy
            if destination.exists():
                destination = _guard_plain_file(
                    root,
                    quarantine_dir / f"garden-observed-{_new_id('OBS')}.bin",
                    required=False,
                )
            _move_file_to_absent(note_path, destination)
            observed_copy = destination
            moved_digest = _digest(observed_copy.read_bytes())
            _record_retained_observed(transaction, observed_copy, moved_digest)
            if moved_digest != observed_digest:
                _persist_quarantine_conflict(
                    transaction_path,
                    transaction,
                    observed_digest=moved_digest,
                    observed_entry=observed_copy.name,
                )
                if not note_path.exists():
                    try:
                        _install_bytes_to_absent(note_path, replacement)
                    except FileExistsError:
                        pass
                raise KbError(
                    "KB2_QUARANTINE_RECOVERY_CONFLICT",
                    "Garden changed while quarantine recovery claimed it; both observed bytes and the safe side were retained",
                    3,
                    {"garden_ref": ref, "quarantine_ref": transaction["quarantine_ref"]},
                )
            _replace_file_after_sync(transaction_path, _json_bytes(transaction))
        else:
            _claim_and_classify_third_garden_version(
                root,
                quarantine_dir,
                transaction_path,
                transaction,
                note_path,
                replacement,
            )

    if not note_path.exists():
        try:
            _install_bytes_to_absent(note_path, replacement)
        except FileExistsError as exc:
            reappeared_digest = _digest(note_path.read_bytes()) if note_path.is_file() else "unavailable"
            _persist_quarantine_conflict(
                transaction_path,
                transaction,
                observed_digest=reappeared_digest,
                observed_entry=observed_copy.name if observed_copy.exists() else None,
            )
            raise KbError(
                "KB2_QUARANTINE_RECOVERY_CONFLICT",
                "Garden reappeared during recovery; recovery did not overwrite it",
                3,
                {"garden_ref": ref, "quarantine_ref": transaction["quarantine_ref"]},
            ) from exc

    restored_bytes = note_path.read_bytes()
    if _digest(restored_bytes) != replacement_digest or _secret_reasons(restored_bytes):
        _persist_quarantine_conflict(
            transaction_path,
            transaction,
            observed_digest=_digest(restored_bytes),
            observed_entry=observed_copy.name if observed_copy.exists() else None,
        )
        raise KbError(
            "KB2_SECURITY_RESTORE_FAILED",
            "quarantine succeeded but the ordinary Garden surface is not verified safe",
            4,
            {"garden_ref": ref, "quarantine_ref": transaction["quarantine_ref"]},
        )
    if observed_copy.exists():
        _, verified_transaction = _load_quarantine_transaction(root, quarantine_dir)
        retained_digest = next(
            (
                item.get("digest")
                for item in verified_transaction.get("retained_observed_entries", [])
                if isinstance(item, dict) and item.get("entry") == observed_copy.name
            ),
            None,
        )
        actual_retained_digest = _digest(observed_copy.read_bytes())
        if retained_digest is None or actual_retained_digest != retained_digest:
            _persist_quarantine_conflict(
                transaction_path,
                transaction,
                observed_digest=actual_retained_digest,
                observed_entry=observed_copy.name,
            )
            raise KbError(
                "KB2_QUARANTINE_RETAINED_DRIFT",
                "retained observed bytes changed after claim; recovery did not delete them",
                3,
            )
    transaction["stage"] = "garden-restored"
    transaction["action"] = action
    transaction["replacement_digest"] = replacement_digest
    transaction["garden_restored_at"] = _now()
    if base_issue_code is not None:
        _persist_quarantine_conflict(
            transaction_path,
            transaction,
            observed_digest=transaction.get("payload_digest", "unknown"),
            code=base_issue_code,
        )
        raise KbError(
            base_issue_code,
            "organizer base was missing or drifted; protected recovery remains unresolved",
            3,
            {"garden_ref": ref, "quarantine_ref": transaction["quarantine_ref"]},
        )
    _replace_file_after_sync(transaction_path, _json_bytes(transaction))
    return action


def _finalize_quarantine_decision(root: Path, quarantine_dir: Path) -> Path:
    transaction_path, transaction = _load_quarantine_transaction(root, quarantine_dir)
    ref = str(transaction["garden_ref"])
    _, capture_id = _garden_location(root, ref, require_file=True)
    state_dir, state = _load_organizer_state(root, capture_id, ref)
    restricted = _ensure_protected_directory(root, "ingress/restricted-hold")
    hold_id = str(transaction["id"])
    summary_path = restricted / f"{hold_id}.json"
    latest_hold = {
        "hold_ref": transaction["hold_ref"],
        "quarantine_ref": transaction["quarantine_ref"],
        "observed_digest": transaction["payload_digest"],
        "reason_codes": transaction["reason_codes"],
        "action": transaction["action"],
        "replacement_digest": transaction["replacement_digest"],
        "externalization_pending": True,
        "occurred_at": transaction.get("garden_restored_at", _now()),
    }
    summary = {
        "schema": "restricted-hold/v0.1-pilot",
        "id": hold_id,
        "hold_ref": transaction["hold_ref"],
        "capture_ref": transaction["capture_ref"],
        "garden_ref": ref,
        "quarantine_ref": transaction["quarantine_ref"],
        "payload_digest": transaction["payload_digest"],
        "reason_codes": transaction["reason_codes"],
        "contains_payload": False,
        "externalization_pending": True,
        "action": transaction["action"],
        "created_at": transaction["created_at"],
    }
    _replace_file_after_sync(summary_path, _json_bytes(summary))
    state["decision"] = {
        "route": {
            "result": "restricted-hold",
            "garden_ref": ref,
            "hold_ref": transaction["hold_ref"],
        },
        "security": {
            "precheck": "rejected",
            "profile": "restricted-summary/v1",
            "policy": "deterministic-secret-precheck/v0.1-pilot",
            "latest_hold": latest_hold,
        },
    }
    _save_organizer_state(state_dir, state)
    transaction["stage"] = "decision-recorded"
    transaction["decision_recorded_at"] = _now()
    _replace_file_after_sync(transaction_path, _json_bytes(transaction))
    return summary_path


def recover_security_holds(
    root: Path,
    *,
    before_quarantine_claim: Any | None = None,
) -> dict[str, Any]:
    root = _guard_root(root)
    quarantine_root = root / "ingress" / "quarantine"
    if not quarantine_root.exists():
        return {"recovered": 0, "unresolved": []}
    quarantine_root = _ensure_protected_directory(root, "ingress/quarantine")
    recovered = 0
    unresolved: list[dict[str, str]] = []
    for quarantine_dir in sorted(quarantine_root.glob("HLD-*")):
        try:
            _, transaction = _load_quarantine_transaction(root, quarantine_dir)
            if transaction.get("organizer_base_issue") in {"missing", "drift"}:
                ref = str(transaction["garden_ref"])
                _, capture_id = _garden_location(root, ref, require_file=False)
                state_dir, state = _load_organizer_state(root, capture_id, ref)
                transaction_path = _guard_plain_file(root, quarantine_dir / "quarantine.json")
                _reconcile_quarantine_organizer_base(
                    root,
                    quarantine_dir,
                    transaction_path,
                    transaction,
                    state_dir,
                    state,
                )
                _, transaction = _load_quarantine_transaction(root, quarantine_dir)
            stage = transaction.get("stage")
            if stage == "decision-recorded":
                continue
            if stage == "recovery-conflict":
                unresolved.append(
                    {
                        "quarantine": quarantine_dir.name,
                        "code": str(transaction.get("recovery_conflict_code", "KB2_QUARANTINE_RECOVERY_CONFLICT")),
                    }
                )
                continue
            if stage not in {"quarantine-committed", "garden-restored"}:
                unresolved.append({"quarantine": quarantine_dir.name, "code": "KB2_QUARANTINE_STAGE_INVALID"})
                continue
            if stage == "quarantine-committed":
                _restore_quarantined_garden(
                    root,
                    quarantine_dir,
                    before_quarantine_claim=before_quarantine_claim,
                )
            _, transaction = _load_quarantine_transaction(root, quarantine_dir)
            if transaction.get("stage") == "garden-restored":
                _finalize_quarantine_decision(root, quarantine_dir)
            _, transaction = _load_quarantine_transaction(root, quarantine_dir)
            if transaction.get("stage") == "decision-recorded":
                recovered += 1
            else:
                unresolved.append({"quarantine": quarantine_dir.name, "code": "KB2_QUARANTINE_STAGE_INVALID"})
        except KbError as exc:
            unresolved.append({"quarantine": quarantine_dir.name, "code": exc.code})
    return {"recovered": recovered, "unresolved": unresolved}


def _retain_organizer_conflict(
    root: Path,
    ref: str,
    expected: bytes,
    observed: bytes,
    base_digest: str,
) -> Path:
    conflicts = _ensure_plain_directory(root, "governance/organizer-conflicts")
    conflict_id = _new_id("CNF")
    conflict_dir = conflicts / conflict_id
    conflict_dir.mkdir()
    expected_path = conflict_dir / "expected.md"
    record_path = conflict_dir / "conflict.json"
    record = {
        "schema": "organizer-conflict/v0.1-pilot",
        "id": conflict_id,
        "target": ref,
        "expected_entry": "expected.md",
        "expected_digest": _digest(expected),
        "observed_owner": ref,
        "observed_digest": _digest(observed),
        "base_digest": base_digest,
        "stage": "needs-review",
        "created_at": _now(),
    }
    _write_file_synced(expected_path, expected)
    _write_file_synced(record_path, _json_bytes(record))
    return conflict_dir


def _verify_garden_snapshot(
    root: Path,
    note_path: Path,
    capture_id: str,
    ref: str,
    state_dir: Path,
    state: dict[str, Any],
    expected: bytes,
) -> None:
    observed = _guard_plain_file(root, note_path).read_bytes()
    if observed == expected:
        return
    observed_digest = _digest(observed)
    reasons = _secret_reasons(observed)
    if reasons:
        hold_ref, action, changed = _quarantine_garden_edit(
            root,
            note_path,
            capture_id,
            ref,
            state_dir,
            state,
            observed,
            observed_digest,
            reasons,
        )
        raise KbError(
            "KB2_RESTRICTED_EDIT",
            "Garden changed to secret-like bytes during organize and was moved to a protected owner",
            2,
            {"garden_ref": ref, "route": "restricted-hold", "hold_ref": hold_ref, "action": action},
            changed,
        )
    conflict_dir = _retain_organizer_conflict(
        root,
        ref,
        expected,
        observed,
        str(state.get("base_digest")),
    )
    raise KbError(
        "KB2_GARDEN_CONFLICT",
        "Garden changed during organize; both safe versions were retained without overwrite",
        3,
        {
            "garden_ref": ref,
            "expected_digest": _digest(expected),
            "observed_digest": observed_digest,
            "conflict": conflict_dir.name,
        },
        [str(conflict_dir.relative_to(root))],
    )


def organize(
    root: Path,
    ref: str,
    *,
    actor: str = "human-direct-edit",
    reason: str | None = None,
    correction_capture_ref: str | None = None,
    fail_after_quarantine: bool = False,
    fail_after_restore: bool = False,
    fail_after_override_record: bool = False,
) -> dict[str, Any]:
    if ref.startswith("context://"):
        from .context import organize_context

        return organize_context(
            root,
            ref,
            actor=actor,
            reason=reason,
            correction_capture_ref=correction_capture_ref,
        )
    root = _guard_root(root)
    _require_recovery_clear(root)
    note_path, capture_id = _garden_path(root, ref)
    state_dir, state = _load_organizer_state(root, capture_id, ref)

    current = note_path.read_bytes()
    current_digest = _digest(current)
    base_digest = state.get("base_digest")
    if current_digest == base_digest:
        active_override = state.get("active_override")
        if active_override:
            if not isinstance(active_override, str) or not _OVERRIDE_RE.fullmatch(active_override):
                raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "active override identity is invalid", 3)
            _load_override_owner(
                root,
                root / "governance" / "overrides" / f"{active_override}.yaml",
                expected_id=active_override,
                expected_target=ref,
                expected_observed_digest=current_digest,
            )
        route = _decision_route(state)
        _verify_garden_snapshot(root, note_path, capture_id, ref, state_dir, state, current)
        return {
            "route": route["result"],
            "garden_ref": ref,
            "changed": [],
            "override": {"active": bool(state.get("active_override")), "created": False},
        }

    reasons = _secret_reasons(current)
    _verify_garden_snapshot(root, note_path, capture_id, ref, state_dir, state, current)
    if reasons:
        hold_ref, action, changed = _quarantine_garden_edit(
            root,
            note_path,
            capture_id,
            ref,
            state_dir,
            state,
            current,
            current_digest,
            reasons,
            fail_after_quarantine=fail_after_quarantine,
            fail_after_restore=fail_after_restore,
        )
        raise KbError(
            "KB2_RESTRICTED_EDIT",
            "Garden edit was preserved in a protected hold and removed from the ordinary Garden surface",
            2,
            {"garden_ref": ref, "route": "restricted-hold", "hold_ref": hold_ref, "action": action},
            changed,
        )

    overrides = _ensure_plain_directory(root, "governance/overrides")
    existing = _find_existing_override(
        root,
        overrides,
        ref,
        str(base_digest),
        current_digest,
        state.get("active_override"),
        actor,
        correction_capture_ref,
    )
    base_snapshot = _guard_plain_file(root, state_dir / "base.md")
    previous = base_snapshot.read_bytes()
    if _digest(previous) != base_digest:
        if existing is None:
            raise KbError("KB2_BASE_DIGEST_MISMATCH", "organizer base snapshot does not match its recorded digest", 3)
    _verify_garden_snapshot(root, note_path, capture_id, ref, state_dir, state, current)

    if existing is None:
        override_id = _new_id("OVR")
        diff = "".join(
            difflib.unified_diff(
                previous.decode("utf-8").splitlines(keepends=True),
                current.decode("utf-8").splitlines(keepends=True),
                fromfile="organizer-base",
                tofile="human-edit",
            )
        )
        record: dict[str, Any] = {
            "schema": "human-override/v0.1-pilot",
            "id": override_id,
            "target": ref,
            "scope": {"kind": "object", "ref": ref},
            "actor": actor,
            "reason": reason or "direct modification detected by base digest",
            "created_at": _now(),
            "base_digest": base_digest,
            "observed_digest": current_digest,
            "diff_format": "unified",
            "diff": diff,
            "supersedes": state.get("active_override"),
        }
        if correction_capture_ref is not None:
            record["correction_capture_ref"] = correction_capture_ref
        override_path = overrides / f"{override_id}.yaml"
        _replace_file_after_sync(override_path, _json_bytes(record))
        _verify_garden_snapshot(root, note_path, capture_id, ref, state_dir, state, current)
        if fail_after_override_record:
            raise KbError(
                "KB2_INJECTED_AFTER_OVERRIDE_RECORD",
                "injected failure after override record and before organizer state update",
                4,
                {"garden_ref": ref, "override_ref": f"override://{override_id}"},
                [str(override_path.relative_to(root))],
            )
    else:
        record = existing
        override_id = str(record["id"])
        override_path = overrides / f"{override_id}.yaml"

    _replace_file_after_sync(base_snapshot, current)
    _verify_garden_snapshot(root, note_path, capture_id, ref, state_dir, state, current)
    state["base_digest"] = current_digest
    state["active_override"] = override_id
    state["last_organized_at"] = _now()
    _save_organizer_state(state_dir, state)
    route = _decision_route(state)
    _verify_garden_snapshot(root, note_path, capture_id, ref, state_dir, state, current)
    return {
        "route": route["result"],
        "garden_ref": ref,
        "changed": [str(override_path.relative_to(root))],
        "override": {"active": True, "created": existing is None, "ref": f"override://{override_id}"},
    }


def _mark_correction_conflict(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    transaction_path: Path,
    transaction: dict[str, Any],
    *,
    observed_digest: str | None,
    reason: str = "garden-conflict",
) -> None:
    expected_metadata, expected_payload = _capture_update_snapshot(root, capture_dir, metadata)
    transaction["stage"] = "conflict"
    transaction["observed_digest"] = observed_digest
    transaction["conflict_reason"] = reason
    transaction["conflict_at"] = _now()
    _replace_file_after_sync(transaction_path, _json_bytes(transaction))
    metadata["state"] = "needs-review"
    metadata["route"] = {
        "result": "needs-review",
        "reason": "garden-conflict",
        "conflict_detail": reason,
        "target": transaction["target"],
    }
    _update_capture(
        root,
        capture_dir,
        metadata,
        expected_metadata=expected_metadata,
        expected_payload=expected_payload,
    )


def _copy_file_to_absent(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(str(destination))
    fd, temporary_name = tempfile.mkstemp(prefix=".kb2-correction-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_handle, os.fdopen(fd, "wb") as output_handle:
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        _move_file_to_absent(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_correction_owner(
    root: Path,
    transaction_dir: Path,
) -> tuple[Path, dict[str, Any], Path, dict[str, Any], Path, Path]:
    transaction_dir = _guard_plain_directory(root, transaction_dir)
    if not re.fullmatch(r"COR-[0-9A-HJKMNP-TV-Z]{26}", transaction_dir.name):
        raise KbError("KB2_CORRECTION_ENTRY_INVALID", "correction entry has an invalid identity", 3)
    transaction_path = _guard_plain_file(root, transaction_dir / "correction.json")
    transaction = _load_json(transaction_path)
    capture_id = transaction.get("correction_capture_id")
    capture_ref = transaction.get("correction_capture_ref")
    override_id = transaction.get("override_id")
    if (
        transaction.get("schema") != "correction-transaction/v0.1-pilot"
        or transaction.get("id") != transaction_dir.name
        or transaction.get("candidate_entry") != "candidate.md"
        or transaction.get("displaced_entry") != "displaced.md"
        or not isinstance(capture_id, str)
        or not _CAPTURE_RE.fullmatch(capture_id)
        or capture_ref != f"capture://{capture_id}"
        or not isinstance(transaction.get("correction_capture_digest"), str)
        or not isinstance(override_id, str)
        or not _OVERRIDE_RE.fullmatch(override_id)
    ):
        raise KbError("KB2_CORRECTION_ENTRY_INVALID", "correction transaction owner contract is invalid", 3)
    capture_dir = _capture_directory(root, capture_id)
    try:
        metadata, payload = _load_capture_owner(root, capture_dir)
    except KbError as exc:
        raise KbError("KB2_CORRECTION_CAPTURE_INVALID", "correction capture owner is not intact", 3) from exc
    source = metadata.get("source")
    if (
        metadata.get("schema") != "capture/v0.1-pilot"
        or metadata.get("id") != capture_id
        or metadata.get("payload_entry") != "payload.bin"
        or metadata.get("payload_digest") != transaction.get("correction_capture_digest")
        or not isinstance(source, dict)
        or source.get("kind") != "human-correction"
        or source.get("target") != transaction.get("target")
        or _digest(payload) != metadata.get("payload_digest")
    ):
        raise KbError("KB2_CORRECTION_CAPTURE_INVALID", "correction capture is not intact", 3)
    candidate = _guard_plain_file(root, transaction_dir / "candidate.md", required=False)
    displaced = _guard_plain_file(root, transaction_dir / "displaced.md", required=False)
    return transaction_path, transaction, capture_dir, metadata, candidate, displaced


def _raise_correction_conflict(
    root: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    capture_dir: Path,
    metadata: dict[str, Any],
    *,
    observed_digest: str | None,
    reason: str,
) -> None:
    _mark_correction_conflict(
        root,
        capture_dir,
        metadata,
        transaction_path,
        transaction,
        observed_digest=observed_digest,
        reason=reason,
    )
    raise KbError(
        "KB2_CORRECTION_CONFLICT",
        "correction recovery found conflicting state and did not overwrite it",
        3,
        {"garden_ref": transaction.get("target"), "correction": transaction.get("id")},
    )


def _finish_correction_transaction(
    root: Path,
    transaction_dir: Path,
    transaction_path: Path,
    transaction: dict[str, Any],
    capture_dir: Path,
    metadata: dict[str, Any],
    candidate: Path,
    displaced: Path,
) -> dict[str, Any]:
    ref = str(transaction["target"])
    note_path, target_capture_id = _garden_location(root, ref, require_file=False)
    candidate_digest = transaction.get("candidate_digest")
    target_digest = transaction.get("target_base_digest")
    if (
        not candidate.is_file()
        or _digest(candidate.read_bytes()) != candidate_digest
        or not displaced.is_file()
        or _digest(displaced.read_bytes()) != target_digest
        or not note_path.is_file()
        or _digest(note_path.read_bytes()) != candidate_digest
    ):
        observed = _digest(note_path.read_bytes()) if note_path.is_file() else None
        _raise_correction_conflict(
            root,
            transaction_path,
            transaction,
            capture_dir,
            metadata,
            observed_digest=observed,
            reason="installed-candidate-conflict",
        )

    state_dir, state = _load_organizer_state(root, target_capture_id, ref)
    base_snapshot = _guard_plain_file(root, state_dir / "base.md")
    base_file_digest = _digest(base_snapshot.read_bytes())
    override_id = str(transaction["override_id"])
    previous_override = transaction.get("supersedes")
    if state.get("base_digest") not in {target_digest, candidate_digest}:
        _raise_correction_conflict(
            root,
            transaction_path,
            transaction,
            capture_dir,
            metadata,
            observed_digest=state.get("base_digest"),
            reason="organizer-state-conflict",
        )
    if base_file_digest not in {target_digest, candidate_digest} or state.get("active_override") not in {
        previous_override,
        override_id,
    }:
        _raise_correction_conflict(
            root,
            transaction_path,
            transaction,
            capture_dir,
            metadata,
            observed_digest=base_file_digest,
            reason="organizer-base-conflict",
        )

    overrides = _ensure_plain_directory(root, "governance/overrides")
    override_path = _guard_plain_file(root, overrides / f"{override_id}.yaml", required=False)
    correction_capture_ref = str(transaction["correction_capture_ref"])
    if override_path.exists():
        try:
            record = _load_override_owner(
                root,
                override_path,
                expected_id=override_id,
                expected_target=ref,
                correction_transaction=transaction,
                displaced=displaced,
                candidate=candidate,
            )
        except KbError:
            _raise_correction_conflict(
                root,
                transaction_path,
                transaction,
                capture_dir,
                metadata,
                observed_digest=candidate_digest,
                reason="override-identity-conflict",
            )
        created = False
    else:
        diff = "".join(
            difflib.unified_diff(
                displaced.read_text(encoding="utf-8").splitlines(keepends=True),
                candidate.read_text(encoding="utf-8").splitlines(keepends=True),
                fromfile="organizer-base",
                tofile="human-correction",
            )
        )
        record = {
            "schema": "human-override/v0.1-pilot",
            "id": override_id,
            "target": ref,
            "scope": {"kind": "object", "ref": ref},
            "actor": "human-natural-language-correction",
            "reason": f"natural-language correction from {correction_capture_ref}",
            "created_at": _now(),
            "base_digest": target_digest,
            "observed_digest": candidate_digest,
            "diff_format": "unified",
            "diff": diff,
            "supersedes": previous_override,
            "correction_capture_ref": correction_capture_ref,
        }
        _write_file_synced(override_path, _json_bytes(record))
        _load_override_owner(
            root,
            override_path,
            expected_id=override_id,
            expected_target=ref,
            correction_transaction=transaction,
            displaced=displaced,
            candidate=candidate,
        )
        created = True

    candidate_bytes = candidate.read_bytes()
    if base_file_digest != candidate_digest:
        _replace_file_after_sync(base_snapshot, candidate_bytes)
    state["base_digest"] = candidate_digest
    state["active_override"] = override_id
    state["last_organized_at"] = _now()
    _save_organizer_state(state_dir, state)
    expected_metadata, expected_payload = _capture_update_snapshot(root, capture_dir, metadata)
    metadata["state"] = "correction-applied"
    metadata["route"] = {
        "result": "correction-applied",
        "target": ref,
        "override_ref": f"override://{override_id}",
    }
    _update_capture(
        root,
        capture_dir,
        metadata,
        expected_metadata=expected_metadata,
        expected_payload=expected_payload,
    )
    transaction["stage"] = "applied"
    transaction["override_ref"] = f"override://{override_id}"
    transaction["applied_at"] = _now()
    _replace_file_after_sync(transaction_path, _json_bytes(transaction))
    return {
        "route": _decision_route(state)["result"],
        "garden_ref": ref,
        "changed": [str(override_path.relative_to(root))],
        "override": {"active": True, "created": created, "ref": f"override://{override_id}"},
    }


def _advance_correction_transaction(
    root: Path,
    transaction_dir: Path,
    *,
    before_claim: Any | None = None,
    fail_after_claim: bool = False,
    fail_after_install: bool = False,
) -> dict[str, Any]:
    transaction_path, transaction, capture_dir, metadata, candidate, displaced = _load_correction_owner(
        root,
        transaction_dir,
    )
    stage = transaction.get("stage")
    if stage == "conflict":
        raise KbError("KB2_CORRECTION_CONFLICT", "correction transaction remains in needs-review", 3)
    if stage not in {"prepared", "claimed", "installed"}:
        raise KbError("KB2_CORRECTION_STAGE_INVALID", "correction transaction has an unknown stage", 3)
    candidate_digest = transaction.get("candidate_digest")
    target_digest = transaction.get("target_base_digest")
    if not isinstance(candidate_digest, str) or not candidate.is_file() or _digest(candidate.read_bytes()) != candidate_digest:
        _raise_correction_conflict(
            root,
            transaction_path,
            transaction,
            capture_dir,
            metadata,
            observed_digest=_digest(candidate.read_bytes()) if candidate.is_file() else None,
            reason="candidate-payload-conflict",
        )
    note_path, _ = _garden_location(root, str(transaction["target"]), require_file=False)

    if stage == "prepared":
        if displaced.exists():
            if _digest(displaced.read_bytes()) != target_digest or note_path.exists():
                _raise_correction_conflict(
                    root,
                    transaction_path,
                    transaction,
                    capture_dir,
                    metadata,
                    observed_digest=_digest(note_path.read_bytes()) if note_path.is_file() else None,
                    reason="prepared-owner-conflict",
                )
        else:
            if not note_path.is_file() or _digest(note_path.read_bytes()) != target_digest:
                _raise_correction_conflict(
                    root,
                    transaction_path,
                    transaction,
                    capture_dir,
                    metadata,
                    observed_digest=_digest(note_path.read_bytes()) if note_path.is_file() else None,
                    reason="garden-claim-conflict",
                )
            if before_claim is not None:
                before_claim()
            try:
                _move_file_to_absent(note_path, displaced)
            except (FileNotFoundError, FileExistsError):
                _raise_correction_conflict(
                    root,
                    transaction_path,
                    transaction,
                    capture_dir,
                    metadata,
                    observed_digest=_digest(note_path.read_bytes()) if note_path.is_file() else None,
                    reason="garden-claim-conflict",
                )
            claimed_digest = _digest(displaced.read_bytes())
            if claimed_digest != target_digest:
                if not note_path.exists():
                    _move_file_to_absent(displaced, note_path)
                _raise_correction_conflict(
                    root,
                    transaction_path,
                    transaction,
                    capture_dir,
                    metadata,
                    observed_digest=claimed_digest,
                    reason="garden-claim-race",
                )
        transaction["stage"] = "claimed"
        transaction["claimed_at"] = _now()
        _replace_file_after_sync(transaction_path, _json_bytes(transaction))
        stage = "claimed"
        if fail_after_claim:
            raise KbError("KB2_INJECTED_CORRECTION_CLAIMED", "injected failure after correction claim", 4)

    if stage == "claimed":
        if not displaced.is_file() or _digest(displaced.read_bytes()) != target_digest:
            _raise_correction_conflict(
                root,
                transaction_path,
                transaction,
                capture_dir,
                metadata,
                observed_digest=_digest(displaced.read_bytes()) if displaced.is_file() else None,
                reason="displaced-base-conflict",
            )
        if note_path.exists():
            if not note_path.is_file() or _digest(note_path.read_bytes()) != candidate_digest:
                _raise_correction_conflict(
                    root,
                    transaction_path,
                    transaction,
                    capture_dir,
                    metadata,
                    observed_digest=_digest(note_path.read_bytes()) if note_path.is_file() else None,
                    reason="candidate-install-conflict",
                )
        else:
            try:
                _copy_file_to_absent(candidate, note_path)
            except FileExistsError:
                _raise_correction_conflict(
                    root,
                    transaction_path,
                    transaction,
                    capture_dir,
                    metadata,
                    observed_digest=_digest(note_path.read_bytes()) if note_path.is_file() else None,
                    reason="candidate-install-conflict",
                )
        if _digest(note_path.read_bytes()) != candidate_digest:
            _raise_correction_conflict(
                root,
                transaction_path,
                transaction,
                capture_dir,
                metadata,
                observed_digest=_digest(note_path.read_bytes()),
                reason="candidate-install-race",
            )
        transaction["stage"] = "installed"
        transaction["installed_at"] = _now()
        _replace_file_after_sync(transaction_path, _json_bytes(transaction))
        stage = "installed"
        if fail_after_install:
            raise KbError("KB2_INJECTED_CORRECTION_INSTALLED", "injected failure after correction install", 4)

    return _finish_correction_transaction(
        root,
        transaction_dir,
        transaction_path,
        transaction,
        capture_dir,
        metadata,
        candidate,
        displaced,
    )


def _validate_applied_correction(root: Path, transaction_dir: Path) -> None:
    _, transaction, _, metadata, candidate, displaced = _load_correction_owner(root, transaction_dir)
    candidate_digest = transaction.get("candidate_digest")
    target_digest = transaction.get("target_base_digest")
    override_id = transaction.get("override_id")
    correction_capture_ref = transaction.get("correction_capture_ref")
    if (
        not isinstance(candidate_digest, str)
        or not isinstance(override_id, str)
        or not re.fullmatch(r"OVR-[0-9A-HJKMNP-TV-Z]{26}", override_id)
        or not candidate.is_file()
        or _digest(candidate.read_bytes()) != candidate_digest
        or not displaced.is_file()
        or _digest(displaced.read_bytes()) != target_digest
    ):
        raise KbError("KB2_CORRECTION_APPLIED_INVALID", "applied correction owner is incomplete", 3)
    override_path = root / "governance" / "overrides" / f"{override_id}.yaml"
    try:
        _load_override_owner(
            root,
            override_path,
            expected_id=override_id,
            expected_target=str(transaction.get("target")),
            correction_transaction=transaction,
            displaced=displaced,
            candidate=candidate,
        )
    except KbError as exc:
        raise KbError("KB2_CORRECTION_APPLIED_INVALID", "applied correction override linkage is invalid", 3) from exc
    route = metadata.get("route")
    if (
        metadata.get("state") != "correction-applied"
        or not isinstance(route, dict)
        or route.get("override_ref") != f"override://{override_id}"
    ):
        raise KbError("KB2_CORRECTION_APPLIED_INVALID", "applied correction capture linkage is invalid", 3)


def recover_corrections(root: Path) -> dict[str, Any]:
    root = _guard_root(root)
    corrections_root = root / "governance" / "corrections"
    if not corrections_root.exists():
        return {"recovered": 0, "unresolved": []}
    corrections_root = _guard_plain_directory(root, corrections_root)
    recovered = 0
    unresolved: list[dict[str, str]] = []
    for transaction_dir in sorted(corrections_root.glob("COR-*")):
        try:
            guarded_dir = _guard_plain_directory(root, transaction_dir)
            transaction_path = _guard_plain_file(root, guarded_dir / "correction.json")
            transaction = _load_json(transaction_path)
            stage = transaction.get("stage")
            if stage == "applied":
                _validate_applied_correction(root, guarded_dir)
                continue
            if stage == "conflict":
                unresolved.append({"correction": transaction_dir.name, "code": "KB2_CORRECTION_CONFLICT"})
                continue
            if stage not in {"prepared", "claimed", "installed"}:
                unresolved.append({"correction": transaction_dir.name, "code": "KB2_CORRECTION_STAGE_INVALID"})
                continue
            _advance_correction_transaction(root, guarded_dir)
            recovered += 1
        except KbError as exc:
            unresolved.append({"correction": transaction_dir.name, "code": exc.code})
    return {"recovered": recovered, "unresolved": unresolved}


def recover_capture_owners(root: Path) -> dict[str, Any]:
    root = _guard_root(root)
    pending = root / "ingress" / "pending"
    if not pending.exists():
        return {"recovered": 0, "unresolved": []}
    pending = _guard_plain_directory(root, pending)
    recovered = 0
    unresolved: list[dict[str, str]] = []
    for capture_dir in sorted(pending.glob("CAP-*")):
        try:
            capture_dir = _guard_plain_directory(root, capture_dir)
            for transaction_path in sorted(capture_dir.glob("capture-update-UPD-*.json")):
                transaction_path = _guard_plain_file(root, transaction_path)
                transaction = _load_json(transaction_path)
                update_id = transaction.get("id")
                if (
                    not isinstance(update_id, str)
                    or not re.fullmatch(r"UPD-[0-9A-HJKMNP-TV-Z]{26}", update_id)
                    or transaction_path.name != f"capture-update-{update_id}.json"
                    or transaction.get("schema") != "capture-metadata-update/v0.1-pilot"
                    or transaction.get("capture_ref") != f"capture://{capture_dir.name}"
                    or transaction.get("candidate_entry") != f"metadata-candidate-{update_id}.json"
                    or transaction.get("claimed_entry") != f"metadata-claimed-{update_id}.json"
                    or transaction.get("expected_entry") != f"metadata-expected-{update_id}.json"
                    or not isinstance(transaction.get("new_metadata_snapshot"), dict)
                    or transaction.get("new_metadata_digest")
                    != _digest(_json_bytes(transaction["new_metadata_snapshot"]))
                    or not isinstance(transaction.get("expected_metadata_digest"), str)
                    or not _DIGEST_RE.fullmatch(transaction["expected_metadata_digest"])
                    or not isinstance(transaction.get("payload_digest"), str)
                    or not _DIGEST_RE.fullmatch(transaction["payload_digest"])
                ):
                    raise KbError("KB2_CAPTURE_UPDATE_INVALID", "capture metadata update contract is invalid", 3)
                expected_metadata, expected_metadata_bytes = _load_capture_update_expected(
                    root,
                    capture_dir,
                    transaction,
                )
                stage = transaction.get("stage")
                if stage == "drift":
                    continue
                claimed_path = _guard_plain_file(
                    root,
                    capture_dir / str(transaction["claimed_entry"]),
                    required=False,
                )
                if stage == "applied":
                    if (
                        not claimed_path.is_file()
                        or _digest(claimed_path.read_bytes()) != transaction.get("expected_metadata_digest")
                    ):
                        raise KbError("KB2_CAPTURE_UPDATE_INVALID", "applied capture update lost its claimed owner", 3)
                    continue
                if stage not in {"prepared", "claimed", "installed"}:
                    raise KbError("KB2_CAPTURE_UPDATE_STAGE_INVALID", "capture metadata update stage is invalid", 3)
                owner = _load_json(_guard_plain_file(root, capture_dir / "owner.json"))
                snapshot_entry = owner.get("payload_snapshot_entry")
                payload_path = _guard_plain_file(root, capture_dir / (snapshot_entry or "payload.bin"))
                expected_payload = payload_path.read_bytes()
                if (
                    owner.get("metadata_snapshot") != expected_metadata
                    or owner.get("metadata_digest") != _digest(expected_metadata_bytes)
                    or _digest(expected_payload) != transaction.get("payload_digest")
                ):
                    raise KbError("KB2_CAPTURE_UPDATE_INVALID", "capture update recovery evidence is invalid", 3)
                _advance_capture_metadata_update(
                    root,
                    capture_dir,
                    transaction_path,
                    transaction,
                    expected_metadata,
                    expected_payload,
                )
                recovered += 1
            _load_capture_owner(root, capture_dir)
        except KbError as exc:
            unresolved.append({"capture": capture_dir.name, "code": exc.code})
    return {"recovered": recovered, "unresolved": unresolved}


def recover_organizer_conflicts(root: Path) -> dict[str, Any]:
    root = _guard_root(root)
    conflicts_root = root / "governance" / "organizer-conflicts"
    if not conflicts_root.exists():
        return {"recovered": 0, "unresolved": []}
    conflicts_root = _guard_plain_directory(root, conflicts_root)
    unresolved: list[dict[str, str]] = []
    for conflict_dir in sorted(conflicts_root.glob("CNF-*")):
        try:
            conflict_dir = _guard_plain_directory(root, conflict_dir)
            if not re.fullmatch(r"CNF-[0-9A-HJKMNP-TV-Z]{26}", conflict_dir.name):
                raise KbError("KB2_ORGANIZER_CONFLICT_INVALID", "organizer conflict identity is invalid", 3)
            record = _load_json(_guard_plain_file(root, conflict_dir / "conflict.json"))
            expected = _guard_plain_file(root, conflict_dir / "expected.md").read_bytes()
            if (
                record.get("schema") != "organizer-conflict/v0.1-pilot"
                or record.get("id") != conflict_dir.name
                or record.get("expected_entry") != "expected.md"
                or record.get("expected_digest") != _digest(expected)
                or record.get("observed_owner") != record.get("target")
                or record.get("stage") != "needs-review"
                or not isinstance(record.get("base_digest"), str)
                or not _DIGEST_RE.fullmatch(record["base_digest"])
            ):
                raise KbError("KB2_ORGANIZER_CONFLICT_INVALID", "organizer conflict owner is invalid", 3)
            ref = str(record.get("target"))
            note_path, capture_id = _garden_location(root, ref, require_file=True)
            observed = note_path.read_bytes()
            observed_digest = _digest(observed)
            reasons = _secret_reasons(observed)
            if reasons:
                state_dir, state = _load_organizer_state(root, capture_id, ref)
                hold_ref, action, _ = _quarantine_garden_edit(
                    root,
                    note_path,
                    capture_id,
                    ref,
                    state_dir,
                    state,
                    observed,
                    observed_digest,
                    reasons,
                )
                reclassifications = record.setdefault("reclassifications", [])
                if not isinstance(reclassifications, list):
                    raise KbError("KB2_ORGANIZER_CONFLICT_INVALID", "organizer conflict history is invalid", 3)
                reclassifications.append(
                    {
                        "observed_digest": observed_digest,
                        "result": "restricted-hold",
                        "hold_ref": hold_ref,
                        "action": action,
                        "classified_at": _now(),
                    }
                )
                record["observed_digest"] = _digest(note_path.read_bytes())
                _replace_file_after_sync(conflict_dir / "conflict.json", _json_bytes(record))
            elif record.get("observed_digest") != observed_digest:
                history = record.setdefault("safe_observed_history", [])
                if not isinstance(history, list):
                    raise KbError("KB2_ORGANIZER_CONFLICT_INVALID", "organizer conflict history is invalid", 3)
                previous_digest = record.get("observed_digest")
                if isinstance(previous_digest, str) and previous_digest not in history:
                    history.append(previous_digest)
                record["observed_digest"] = observed_digest
                record["observed_owner"] = ref
                record["last_observed_at"] = _now()
                _replace_file_after_sync(conflict_dir / "conflict.json", _json_bytes(record))
            unresolved.append({"organizer_conflict": conflict_dir.name, "code": "KB2_GARDEN_CONFLICT"})
        except KbError as exc:
            unresolved.append({"organizer_conflict": conflict_dir.name, "code": exc.code})
    return {"recovered": 0, "unresolved": unresolved}


def recover_all(root: Path) -> dict[str, Any]:
    captures = recover_capture_owners(root)
    security = recover_security_holds(root)
    corrections = recover_corrections(root)
    organizer = recover_organizer_conflicts(root)
    from .context import recover_contexts

    contexts = recover_contexts(root)
    return {
        "recovered": (
            captures["recovered"]
            + security["recovered"]
            + corrections["recovered"]
            + organizer["recovered"]
            + contexts["recovered"]
        ),
        "unresolved": [
            *captures["unresolved"],
            *security["unresolved"],
            *corrections["unresolved"],
            *organizer["unresolved"],
            *contexts["unresolved"],
        ],
        "captures": captures,
        "security": security,
        "corrections": corrections,
        "organizer": organizer,
        "contexts": contexts,
    }


def _require_recovery_clear(root: Path) -> None:
    recovery = recover_all(root)
    if recovery["unresolved"]:
        raise KbError(
            "KB2_RECOVERY_UNRESOLVED",
            "recovery has unresolved quarantine, correction, or organizer state",
            3,
            {"unresolved": recovery["unresolved"]},
        )


def correct_bytes(
    root: Path,
    ref: str,
    correction_bytes: bytes,
    *,
    before_claim: Any | None = None,
    fail_after_prepare: bool = False,
    fail_after_claim: bool = False,
    fail_after_install: bool = False,
) -> dict[str, Any]:
    root = _guard_root(root)
    correction_id, capture_dir, metadata = _capture_bytes(
        root,
        correction_bytes,
        source={"kind": "human-correction", "target": ref},
    )
    correction_capture_ref = f"capture://{correction_id}"
    reasons = _secret_reasons(correction_bytes)
    _verify_capture_snapshot(root, capture_dir, metadata, correction_bytes)
    if reasons:
        restricted = _ensure_protected_directory(root, "ingress/restricted-hold")
        summary_path = restricted / f"{correction_id}.json"
        summary = {
            "schema": "restricted-hold/v0.1-pilot",
            "capture_ref": correction_capture_ref,
            "target": ref,
            "payload_digest": metadata["payload_digest"],
            "reason_codes": reasons,
            "contains_payload": False,
            "externalization_pending": True,
            "created_at": _now(),
        }
        _replace_file_after_sync(summary_path, _json_bytes(summary))
        expected_metadata, expected_payload = _capture_update_snapshot(root, capture_dir, metadata)
        metadata["state"] = "restricted-hold"
        metadata["route"] = {
            "result": "restricted-hold",
            "reason_codes": reasons,
            "externalization_pending": True,
        }
        _update_capture(
            root,
            capture_dir,
            metadata,
            expected_metadata=expected_metadata,
            expected_payload=expected_payload,
        )
        raise KbError(
            "KB2_POLICY_REJECTED",
            "correction was captured but contains secret-like material",
            2,
            {"capture_ref": correction_capture_ref, "route": "restricted-hold", "committed": True},
            [str(capture_dir.relative_to(root)), str(summary_path.relative_to(root))],
        )
    _seal_capture_payload_snapshot(root, capture_dir, metadata, correction_bytes)
    correction = _decode_captured_utf8(root, capture_dir, metadata, snapshot=correction_bytes)

    if ref.startswith("context://"):
        from .context import correct_context

        return correct_context(root, ref, capture_dir, metadata, correction_bytes, correction)

    organize(root, ref)
    note_path, target_capture_id = _garden_path(root, ref)
    state_dir, state = _load_organizer_state(root, target_capture_id, ref)
    before = note_path.read_bytes()
    expected_digest = _digest(before)
    if expected_digest != state.get("base_digest"):
        expected_metadata, expected_payload = _capture_update_snapshot(root, capture_dir, metadata)
        metadata["state"] = "needs-review"
        metadata["route"] = {"result": "needs-review", "reason": "garden-conflict", "target": ref}
        _update_capture(
            root,
            capture_dir,
            metadata,
            expected_metadata=expected_metadata,
            expected_payload=expected_payload,
        )
        raise KbError(
            "KB2_BASE_DIGEST_MISMATCH",
            "Garden does not match its organizer base; correction was not applied",
            3,
            {"capture_ref": correction_capture_ref, "garden_ref": ref},
            [str(capture_dir.relative_to(root))],
        )

    corrected = before + ("\n## 用户纠正\n\n" + correction.strip() + "\n").encode("utf-8")
    corrections = _ensure_plain_directory(root, "governance/corrections")
    correction_tx_id = _new_id("COR")
    transaction_dir = corrections / correction_tx_id
    transaction_dir.mkdir()
    candidate = transaction_dir / "candidate.md"
    displaced = transaction_dir / "displaced.md"
    transaction_path = transaction_dir / "correction.json"
    transaction: dict[str, Any] = {
        "schema": "correction-transaction/v0.1-pilot",
        "id": correction_tx_id,
        "target": ref,
        "candidate_entry": "candidate.md",
        "displaced_entry": "displaced.md",
        "correction_capture_id": correction_id,
        "correction_capture_ref": correction_capture_ref,
        "correction_capture_digest": metadata["payload_digest"],
        "target_base_digest": expected_digest,
        "candidate_digest": _digest(corrected),
        "override_id": _new_id("OVR"),
        "supersedes": state.get("active_override"),
        "stage": "prepared",
        "created_at": _now(),
    }
    _write_file_synced(candidate, corrected)
    _write_file_synced(transaction_path, _json_bytes(transaction))
    if fail_after_prepare:
        raise KbError("KB2_INJECTED_CORRECTION_PREPARED", "injected failure after correction prepare", 4)
    try:
        result = _advance_correction_transaction(
            root,
            transaction_dir,
            before_claim=before_claim,
            fail_after_claim=fail_after_claim,
            fail_after_install=fail_after_install,
        )
    except KbError as exc:
        if exc.code == "KB2_CORRECTION_CONFLICT":
            raise KbError(
                "KB2_BASE_DIGEST_MISMATCH",
                "Garden changed during correction; recovery retained both sides for review",
                3,
                {"capture_ref": correction_capture_ref, "garden_ref": ref},
                [str(capture_dir.relative_to(root)), str(transaction_dir.relative_to(root))],
            ) from exc
        raise
    result["changed"] = [
        str(capture_dir.relative_to(root)),
        str(note_path.relative_to(root)),
        str(transaction_dir.relative_to(root)),
        *[item for item in result.get("changed", []) if item != str(note_path.relative_to(root))],
    ]
    result["correction_recorded"] = True
    result["correction_capture_ref"] = correction_capture_ref
    return result


def correct(
    root: Path,
    ref: str,
    correction: str,
    *,
    before_claim: Any | None = None,
    fail_after_prepare: bool = False,
    fail_after_claim: bool = False,
    fail_after_install: bool = False,
) -> dict[str, Any]:
    return correct_bytes(
        root,
        ref,
        correction.encode("utf-8"),
        before_claim=before_claim,
        fail_after_prepare=fail_after_prepare,
        fail_after_claim=fail_after_claim,
        fail_after_install=fail_after_install,
    )


def explain(root: Path, ref: str) -> dict[str, Any]:
    if ref.startswith("context://"):
        from .context import explain_context

        return explain_context(root, ref)
    root = _guard_root(root)
    _, capture_id = _garden_path(root, ref)
    capture_dir = _capture_directory(root, capture_id)
    _guard_plain_file(root, capture_dir / "capture.json")
    _require_recovery_clear(root)
    metadata, _ = _load_capture_owner(root, capture_dir)
    if metadata.get("id") != capture_id:
        raise KbError("KB2_CAPTURE_ENTRY_INVALID", "capture filename and record identity do not match", 3)
    _, state = _load_organizer_state(root, capture_id, ref)
    override_id = state.get("active_override")
    override: dict[str, Any] | None = None
    if override_id:
        if not isinstance(override_id, str) or not _OVERRIDE_RE.fullmatch(override_id):
            raise KbError("KB2_OVERRIDE_ENTRY_INVALID", "active override identity is invalid", 3)
        override_record = _load_override_owner(
            root,
            root / "governance" / "overrides" / f"{override_id}.yaml",
            expected_id=override_id,
            expected_target=ref,
            expected_observed_digest=str(state.get("base_digest")),
        )
        override = {
            "ref": f"override://{override_id}",
            "scope": override_record.get("scope"),
            "actor": override_record.get("actor"),
            "reason": override_record.get("reason"),
            "observed_digest": override_record.get("observed_digest"),
            "correction_capture_ref": override_record.get("correction_capture_ref"),
        }
    decision = state.get("decision")
    if not isinstance(decision, dict) or not isinstance(decision.get("security"), dict):
        raise KbError("KB2_STATE_INVALID", "persisted route/security decision is missing")
    return {
        "ref": ref,
        "capture_ref": f"capture://{capture_id}",
        "capture_digest": metadata.get("payload_digest"),
        "route": decision["route"],
        "security": decision["security"],
        "base_digest": state.get("base_digest"),
        "human_override": override,
        "decision_order": [
            "hard-security-invariants",
            "human-correction",
            "object-override",
            "organizer-proposal",
            "safe-reversible-default",
        ],
    }


def status(root: Path) -> dict[str, Any]:
    root = _guard_root(root)

    from . import bootstrap, release

    release_records: list[dict[str, Any]] = []
    release_dir = root / "released"
    if release_dir.exists():
        _guard_plain_directory(root, release_dir)
        try:
            release_records = release._read_committed_records(root)
        except release.ReleaseError as exc:
            if exc.code != "RELEASE_NOT_FOUND":
                raise KbError("KB2_RELEASE_INVALID", "released state failed strict validation", 3) from exc

    candidates = root / "governance" / "release-candidates"
    candidate_count = 0
    if candidates.exists():
        _guard_plain_directory(root, candidates)
        candidate_count = sum(
            1 for item in candidates.iterdir()
            if item.is_dir() and item.name.startswith("CAND-") and not item.is_symlink()
        )
    projection = bootstrap.status(root)
    capabilities = {
        "text_ingest": callable(ingest_bytes),
        "garden_organize": callable(organize),
        "context_current_state": callable(explain),
        "human_override": callable(correct_bytes),
        "release": callable(getattr(release, "release_candidate", None)),
        "projection": callable(getattr(bootstrap, "build", None)),
    }
    phase = 2 if capabilities["release"] else 1
    slice_parts = [f"phase-{phase}", "release" if capabilities["release"] else "capture-context"]
    if capabilities["projection"]:
        slice_parts.append("projection")
    slice_name = "-".join(slice_parts)

    def count(relative: str, pattern: str) -> int:
        path = root / relative
        if not path.exists():
            return 0
        _guard_path(root, path, allow_missing=False)
        return sum(1 for item in path.glob(pattern) if item.is_dir() or item.is_file())

    return {
        "phase": phase,
        "slice": slice_name,
        "capabilities": capabilities,
        "counts": {
            "captures": count("ingress/pending", "CAP-*"),
            "restricted_holds": count("ingress/restricted-hold", "*.json"),
            "quarantines": count("ingress/quarantine", "HLD-*"),
            "garden_notes": count("garden/notes", "CAP-*.md"),
            "contexts": count("contexts", "CTX-*"),
            "overrides": count("governance/overrides", "OVR-*.yaml"),
            "candidates": candidate_count,
            "artifacts": len(release_records),
            "revisions": len(release_records),
            "receipts": len(release_records),
        },
        "projection": projection,
    }
