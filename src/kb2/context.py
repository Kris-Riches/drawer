"""Phase 1.2 minimal Context current-state owner and CAS paths."""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from . import core
from .result import KbError


_CONTEXT_RE = re.compile(r"^CTX-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_CONTEXT_REAPPEARANCES = 8
_LIFECYCLE_SCHEMA = "context-lifecycle/v0.2"
_LIFECYCLE_OWNER = "context-organizer/v0.2"
_LIFECYCLE_STATUSES = {"active", "completed", "blocked"}
_LIFECYCLE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_INTENT_TERMS = (
    re.compile(r"跨会话"),
    re.compile(r"(?:持续|继续|后续)推进"),
    re.compile(r"恢复.{0,8}(?:工作|任务|项目|现场)"),
    re.compile(r"(?:交接|接手)"),
    re.compile(r"正式(?:产出|交付|成果)"),
    re.compile(r"(?:next|future)\s+session", re.I),
    re.compile(r"(?:continue|resume|handoff).{0,24}(?:work|task|project)", re.I),
)


def context_intent(text: str) -> bool:
    """Conservative pilot route: one explicit durable-work signal is required."""
    return any(pattern.search(text) for pattern in _INTENT_TERMS)


def _context_ref(context_id: str) -> str:
    return f"context://{context_id}"


def _parse_context_ref(ref: str) -> str:
    prefix = "context://"
    context_id = ref[len(prefix) :] if ref.startswith(prefix) else ""
    if not _CONTEXT_RE.fullmatch(context_id):
        raise KbError("KB2_REF_INVALID", "Context reference has an invalid identity", 2)
    return context_id


def _friendly_name(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "context")
    first = re.sub(r"^[#>*\-\s]+", "", first)
    first = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", first)
    first = re.sub(r"\s+", "-", first).strip("-. ")
    return (first[:36].rstrip("-. ") or "context")


def _title(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "未命名 Context")
    return (re.sub(r"^[#>*\-\s]+", "", first)[:80] or "未命名 Context")


def _render_context(context_id: str, capture_id: str, text: str, created_at: str) -> bytes:
    rendered = (
        "---\n"
        "schema: context-current/v0.1-pilot\n"
        f"id: {context_id}\n"
        f"title: {json.dumps(_title(text), ensure_ascii=False)}\n"
        f"created_at: {created_at}\n"
        f"capture: capture://{capture_id}\n"
        "generated_by: ai-organizer-pilot\n"
        "---\n\n"
        f"# {_title(text)}\n\n"
        "## 为什么\n\n"
        f"{text.strip()}\n\n"
        "## 现在\n\n"
        "已建立可跨会话恢复的 current state，等待按目标继续推进。\n\n"
        "## 下一步\n\n"
        "1. 根据目标继续推进。\n"
        "2. 在关键节点记录真实验证。\n"
        "3. 交接前更新当前状态。\n\n"
        "## 阻塞与等待\n\n"
        "- 当前输入未表达明确阻塞或等待。\n\n"
        "## 最近验证\n\n"
        "- 尚未提供可核验结果。\n\n"
        "## 接手注意\n\n"
        f"- 从本文件恢复；原始意图见 `capture://{capture_id}`。\n"
    )
    return rendered.encode("utf-8")


def _render_update(current: bytes, text: str, *, correction: bool) -> bytes:
    heading = "## 用户纠正" if correction else "## 当前推进"
    return current.rstrip() + f"\n\n{heading}\n\n{text.strip()}\n".encode("utf-8")


def _validate_context_entry(context_id: str, entry: object) -> Path:
    if not isinstance(entry, str):
        raise KbError("KB2_CONTEXT_OWNER_INVALID", "Context owner path is invalid", 3)
    path = Path(entry)
    if (
        len(path.parts) != 3
        or path.parts[0] != "contexts"
        or not path.parts[1].startswith(context_id + "-")
        or path.parts[1] == context_id + "-"
        or path.parts[2] != "CONTEXT.md"
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise KbError("KB2_CONTEXT_OWNER_INVALID", "Context owner path is invalid", 3)
    return path


def _state_directory(root: Path, context_id: str, *, required: bool = True) -> Path:
    path = root / "governance" / "context-state" / context_id
    if required:
        return core._guard_plain_directory(root, path)
    return core._ensure_plain_directory(root, f"governance/context-state/{context_id}")


def _context_lifecycle(ref: str, state: dict[str, Any]) -> dict[str, Any]:
    lifecycle = state.get("lifecycle")
    if lifecycle is None:
        return {"status": "active", "legacy": True}
    context_id = _parse_context_ref(ref)
    entry = _validate_context_entry(context_id, state.get("context_entry"))
    if (
        not isinstance(lifecycle, dict)
        or lifecycle.get("schema") != _LIFECYCLE_SCHEMA
        or lifecycle.get("owner") != _LIFECYCLE_OWNER
        or lifecycle.get("context_ref") != ref
        or lifecycle.get("context_entry") != str(entry).replace(os.sep, "/")
        or lifecycle.get("status") not in _LIFECYCLE_STATUSES
        or not isinstance(lifecycle.get("created_at"), str)
        or not _LIFECYCLE_TIME_RE.fullmatch(lifecycle["created_at"])
        or not isinstance(lifecycle.get("updated_at"), str)
        or not _LIFECYCLE_TIME_RE.fullmatch(lifecycle["updated_at"])
    ):
        raise KbError("KB2_CONTEXT_LIFECYCLE_INVALID", "Context lifecycle owner is invalid", 3)
    return lifecycle


def _new_context_lifecycle(
    ref: str,
    entry: str,
    *,
    status: str = "active",
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    now = updated_at or core._now()
    return {
        "schema": _LIFECYCLE_SCHEMA,
        "owner": _LIFECYCLE_OWNER,
        "context_ref": ref,
        "context_entry": entry,
        "status": status,
        "created_at": created_at or now,
        "updated_at": now,
    }


def _load_state(root: Path, ref: str) -> tuple[Path, dict[str, Any], Path]:
    context_id = _parse_context_ref(ref)
    state_dir = _state_directory(root, context_id)
    state = core._load_json(core._guard_plain_file(root, state_dir / "state.json"))
    entry = _validate_context_entry(context_id, state.get("context_entry"))
    if (
        state.get("schema") != "context-organizer-state/v0.1-pilot"
        or state.get("context_ref") != ref
        or not isinstance(state.get("origin_capture_ref"), str)
        or not isinstance(state.get("latest_capture_ref"), str)
        or not isinstance(state.get("base_digest"), str)
        or not _DIGEST_RE.fullmatch(state["base_digest"])
        or state.get("active_override") is not None
        and not core._OVERRIDE_RE.fullmatch(str(state.get("active_override")))
        or not isinstance(state.get("decision"), dict)
    ):
        raise KbError("KB2_CONTEXT_OWNER_INVALID", "Context organizer state is invalid", 3)
    _context_lifecycle(ref, state)
    base_path = core._guard_plain_file(root, state_dir / "base.md")
    if core._digest(base_path.read_bytes()) != state["base_digest"]:
        raise KbError("KB2_CONTEXT_OWNER_INVALID", "Context organizer base owner drifted", 3)
    path = core._guard_plain_file(root, root / entry)
    return state_dir, state, path


def _save_state(state_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = core._now()
    if isinstance(state.get("lifecycle"), dict):
        state["lifecycle"]["updated_at"] = state["updated_at"]
    core._replace_file_after_sync(state_dir / "state.json", core._json_bytes(state))


def _set_capture_route(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    *,
    state: str,
    route: dict[str, Any],
) -> None:
    current, payload = core._load_capture_owner(root, capture_dir)
    if current.get("state") == state and current.get("route") == route:
        metadata.clear()
        metadata.update(current)
        return
    updated = json.loads(json.dumps(current))
    updated["state"] = state
    updated["route"] = route
    core._update_capture(
        root,
        capture_dir,
        updated,
        expected_metadata=current,
        expected_payload=payload,
    )
    metadata.clear()
    metadata.update(updated)


def _write_directory_bundle(staging: Path, final: Path) -> bool:
    try:
        os.rename(staging, final)
        return True
    except FileExistsError:
        return False
    except OSError:
        if final.exists():
            return False
        raise


def _load_owner_json(path: Path, *, code: str, detail: str) -> dict[str, Any]:
    """Normalize only owner-document decoding/shape/read failures."""
    try:
        return core._load_json(path)
    except KbError as exc:
        if exc.code != "KB2_STATE_INVALID":
            raise
        raise KbError(code, detail, 3) from exc
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, TypeError) as exc:
        raise KbError(code, detail, 3) from exc


def _load_intent_owner(root: Path, owner_dir: Path) -> tuple[Path, dict[str, Any], Path]:
    owner_dir = core._guard_plain_directory(root, owner_dir)
    if not re.fullmatch(r"[0-9a-f]{64}", owner_dir.name):
        raise KbError("KB2_CONTEXT_INTENT_INVALID", "Context intent owner identity is invalid", 3)
    owner_path = core._guard_plain_file(root, owner_dir / "intent.json")
    owner = _load_owner_json(
        owner_path,
        code="KB2_CONTEXT_INTENT_INVALID",
        detail="Context intent owner document is invalid",
    )
    context_id = owner.get("context_id")
    capture_ref = owner.get("capture_ref")
    candidate = core._guard_plain_file(root, owner_dir / "candidate.md")
    if (
        owner.get("schema") != "context-intent/v0.1-pilot"
        or owner.get("owner") != "context-organizer/v0.1-pilot"
        or owner.get("intent_key") != f"sha256:{owner_dir.name}"
        or not isinstance(context_id, str)
        or not _CONTEXT_RE.fullmatch(context_id)
        or owner.get("context_ref") != _context_ref(context_id)
        or not isinstance(capture_ref, str)
        or not core._CAPTURE_RE.fullmatch(capture_ref.removeprefix("capture://"))
        or owner.get("candidate_entry") != "candidate.md"
        or owner.get("candidate_digest") != core._digest(candidate.read_bytes())
    ):
        raise KbError("KB2_CONTEXT_INTENT_INVALID", "Context intent owner contract is invalid", 3)
    _validate_context_entry(context_id, owner.get("context_entry"))
    return owner_path, owner, candidate


def _install_context_state(
    root: Path,
    owner: dict[str, Any],
    candidate: bytes,
) -> None:
    context_id = str(owner["context_id"])
    ref = str(owner["context_ref"])
    state_root = core._ensure_plain_directory(root, "governance/context-state")
    final = state_root / context_id
    if final.exists():
        state_dir, state, _ = _load_state(root, ref)
        base = core._guard_plain_file(root, state_dir / "base.md").read_bytes()
        if state.get("base_digest") != core._digest(base) or base != candidate:
            raise KbError("KB2_CONTEXT_CONFLICT", "existing Context state conflicts with its create intent", 3)
        return
    staging = state_root / (".staging-" + context_id + "-" + core._new_id("TMP"))
    core._guard_path(root, staging)
    staging.mkdir()
    now = core._now()
    state = {
        "schema": "context-organizer-state/v0.1-pilot",
        "context_ref": ref,
        "context_entry": owner["context_entry"],
        "origin_capture_ref": owner["capture_ref"],
        "latest_capture_ref": owner["capture_ref"],
        "base_digest": core._digest(candidate),
        "active_override": None,
        "lifecycle": _new_context_lifecycle(ref, owner["context_entry"], created_at=now, updated_at=now),
        "decision": {
            "route": {"result": "context-created", "context_ref": ref},
            "security": {
                "precheck": "passed",
                "profile": "personal-full/v1",
                "policy": "deterministic-secret-precheck/v0.1-pilot",
                "latest_hold": None,
            },
        },
        "updated_at": core._now(),
    }
    try:
        core._write_file_synced(staging / "base.md", candidate)
        core._write_file_synced(staging / "state.json", core._json_bytes(state))
        if not _write_directory_bundle(staging, final):
            _install_context_state(root, owner, candidate)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _advance_intent(root: Path, owner_dir: Path) -> dict[str, Any]:
    owner_path, owner, candidate_path = _load_intent_owner(root, owner_dir)
    candidate = candidate_path.read_bytes()
    stage = owner.get("stage")
    if stage not in {"prepared", "applied"}:
        raise KbError("KB2_CONTEXT_INTENT_INVALID", "Context intent stage is invalid", 3)
    entry = _validate_context_entry(str(owner["context_id"]), owner["context_entry"])
    context_dir = root / entry.parent
    context_path = root / entry
    if stage == "prepared":
        contexts = core._ensure_plain_directory(root, "contexts")
        if not context_dir.exists():
            staging = contexts / (".staging-" + str(owner["context_id"]) + "-" + core._new_id("TMP"))
            core._guard_path(root, staging)
            staging.mkdir()
            try:
                core._write_file_synced(staging / "CONTEXT.md", candidate)
                _write_directory_bundle(staging, context_dir)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        current = core._guard_plain_file(root, context_path).read_bytes()
        if current != candidate:
            raise KbError("KB2_CONTEXT_CONFLICT", "Context changed before create intent completed", 3)
        _install_context_state(root, owner, candidate)
        capture_id = str(owner["capture_ref"]).removeprefix("capture://")
        capture_dir = core._capture_directory(root, capture_id)
        metadata, payload = core._load_capture_owner(root, capture_dir)
        if core._digest(payload) != owner.get("payload_digest"):
            raise KbError("KB2_CONTEXT_INTENT_INVALID", "Context intent capture payload changed", 3)
        _set_capture_route(
            root,
            capture_dir,
            metadata,
            state="context-created",
            route={"result": "context-created", "context_ref": owner["context_ref"]},
        )
        owner["stage"] = "applied"
        owner["applied_at"] = core._now()
        core._replace_file_after_sync(owner_path, core._json_bytes(owner))
    return owner


def _prepare_intent(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    payload: bytes,
    text: str,
) -> tuple[Path, bool]:
    normalized = " ".join(text.split()).casefold().encode("utf-8")
    key = core._digest(normalized).removeprefix("sha256:")
    owners = core._ensure_plain_directory(root, "governance/context-intents")
    final = owners / key
    if final.exists():
        _load_intent_owner(root, final)
        return final, False
    context_id = core._new_id("CTX")
    context_name = f"{context_id}-{_friendly_name(text)}"
    candidate = _render_context(context_id, metadata["id"], text, metadata["created_at"])
    staging = owners / (".staging-" + core._new_id("INT"))
    core._guard_path(root, staging)
    staging.mkdir()
    owner = {
        "schema": "context-intent/v0.1-pilot",
        "owner": "context-organizer/v0.1-pilot",
        "intent_key": f"sha256:{key}",
        "context_id": context_id,
        "context_ref": _context_ref(context_id),
        "context_entry": f"contexts/{context_name}/CONTEXT.md",
        "capture_ref": f"capture://{metadata['id']}",
        "payload_digest": core._digest(payload),
        "candidate_entry": "candidate.md",
        "candidate_digest": core._digest(candidate),
        "stage": "prepared",
        "created_at": core._now(),
    }
    try:
        core._write_file_synced(staging / "candidate.md", candidate)
        core._write_file_synced(staging / "intent.json", core._json_bytes(owner))
        created = _write_directory_bundle(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    _load_intent_owner(root, final)
    return final, created


def _new_context_conflict(
    root: Path,
    ref: str,
    expected: bytes,
    candidate: bytes,
    observed: bytes,
    *,
    capture_ref: str,
    base_digest: str,
) -> Path:
    conflicts = core._ensure_plain_directory(root, "governance/context-conflicts")
    conflict_id = core._new_id("CCF")
    final = conflicts / conflict_id
    final.mkdir()
    record = {
        "schema": "context-conflict/v0.1-pilot",
        "id": conflict_id,
        "target": ref,
        "capture_ref": capture_ref,
        "base_digest": base_digest,
        "expected_entry": "expected.md",
        "expected_digest": core._digest(expected),
        "candidate_entry": "candidate.md",
        "candidate_digest": core._digest(candidate),
        "observed_entry": "observed.md",
        "observed_digest": core._digest(observed),
        "stage": "needs-review",
        "created_at": core._now(),
    }
    core._write_file_synced(final / "expected.md", expected)
    core._write_file_synced(final / "candidate.md", candidate)
    core._write_file_synced(final / "observed.md", observed)
    core._write_file_synced(final / "conflict.json", core._json_bytes(record))
    return final


def _load_context_conflict(root: Path, conflict_dir: Path) -> dict[str, Any]:
    conflict_dir = core._guard_plain_directory(root, conflict_dir)
    if not re.fullmatch(r"CCF-[0-9A-HJKMNP-TV-Z]{26}", conflict_dir.name):
        raise KbError("KB2_CONTEXT_CONFLICT_INVALID", "Context conflict identity is invalid", 3)
    record = core._load_json(core._guard_plain_file(root, conflict_dir / "conflict.json"))
    expected = core._guard_plain_file(root, conflict_dir / "expected.md").read_bytes()
    candidate = core._guard_plain_file(root, conflict_dir / "candidate.md").read_bytes()
    observed = core._guard_plain_file(root, conflict_dir / "observed.md").read_bytes()
    if (
        record.get("schema") != "context-conflict/v0.1-pilot"
        or record.get("id") != conflict_dir.name
        or record.get("stage") != "needs-review"
        or record.get("expected_entry") != "expected.md"
        or record.get("candidate_entry") != "candidate.md"
        or record.get("observed_entry") != "observed.md"
        or record.get("expected_digest") != core._digest(expected)
        or record.get("candidate_digest") != core._digest(candidate)
        or record.get("observed_digest") != core._digest(observed)
        or not _DIGEST_RE.fullmatch(str(record.get("base_digest")))
    ):
        raise KbError("KB2_CONTEXT_CONFLICT_INVALID", "Context conflict owner is invalid", 3)
    _parse_context_ref(str(record.get("target")))
    return record


def _create_claim_owner(
    root: Path,
    update_id: str,
    ref: str,
    expected_digest: str,
) -> tuple[Path, Path]:
    claims = core._ensure_protected_directory(root, "ingress/context-quarantine")
    claim_id = core._new_id("CCL")
    claim_dir = claims / claim_id
    claim_dir.mkdir()
    claim_path = claim_dir / "claim.json"
    claim = {
        "schema": "context-claim/v0.1-pilot",
        "id": claim_id,
        "owner": "context-update/v0.1-pilot",
        "update_id": update_id,
        "target": ref,
        "expected_digest": expected_digest,
        "claimed_entry": "claimed.bin",
        "claimed_digest": None,
        "stage": "prepared",
        "created_at": core._now(),
    }
    core._write_file_synced(claim_path, core._json_bytes(claim))
    return claim_dir, claim_path


def _load_claim_owner(
    root: Path,
    claim_dir: Path,
    *,
    update_id: str,
    ref: str,
) -> tuple[Path, dict[str, Any], Path]:
    claim_dir = core._guard_plain_directory(root, claim_dir)
    if not re.fullmatch(r"CCL-[0-9A-HJKMNP-TV-Z]{26}", claim_dir.name):
        raise KbError("KB2_CONTEXT_CLAIM_INVALID", "Context claim identity is invalid", 3)
    claim_path = core._guard_plain_file(root, claim_dir / "claim.json")
    claim = _load_owner_json(
        claim_path,
        code="KB2_CONTEXT_CLAIM_INVALID",
        detail="Context claim owner document is invalid",
    )
    claimed = core._guard_plain_file(root, claim_dir / "claimed.bin", required=False)
    if (
        claim.get("schema") != "context-claim/v0.1-pilot"
        or claim.get("id") != claim_dir.name
        or claim.get("owner") != "context-update/v0.1-pilot"
        or claim.get("update_id") != update_id
        or claim.get("target") != ref
        or claim.get("claimed_entry") != "claimed.bin"
        or not _DIGEST_RE.fullmatch(str(claim.get("expected_digest")))
        or claim.get("stage") not in {"prepared", "claimed", "applied", "conflict", "restricted"}
    ):
        raise KbError("KB2_CONTEXT_CLAIM_INVALID", "Context claim owner is invalid", 3)
    if claim["stage"] != "prepared":
        if not claimed.is_file() or claim.get("claimed_digest") != core._digest(claimed.read_bytes()):
            raise KbError("KB2_CONTEXT_CLAIM_INVALID", "Context claimed bytes drifted", 3)
    records = claim.get("reappeared_entries")
    if records is None:
        records = []
        if claim.get("reappeared_entry") is not None:
            records.append(
                {
                    "entry": claim.get("reappeared_entry"),
                    "digest": claim.get("reappeared_digest"),
                    "classification": claim.get("reappearance_classification"),
                    "reason_codes": claim.get("reason_codes", []),
                }
            )
    if not isinstance(records, list) or len(records) > _MAX_CONTEXT_REAPPEARANCES + 1:
        raise KbError("KB2_CONTEXT_CLAIM_INVALID", "Context reappearance retained owner is invalid", 3)
    for record in records:
        if not isinstance(record, dict):
            raise KbError("KB2_CONTEXT_CLAIM_INVALID", "Context reappearance retained owner is invalid", 3)
        entry = record.get("entry")
        reappeared = core._guard_plain_file(root, claim_dir / str(entry), required=False)
        if (
            not isinstance(entry, str)
            or not re.fullmatch(r"reappeared(?:-[0-9A-HJKMNP-TV-Z]{26})?\.bin", entry)
            or not _DIGEST_RE.fullmatch(str(record.get("digest")))
            or record.get("classification") not in {"safe", "secret"}
            or not isinstance(record.get("reason_codes"), list)
            or not reappeared.is_file()
            or record.get("digest") != core._digest(reappeared.read_bytes())
        ):
            raise KbError("KB2_CONTEXT_CLAIM_INVALID", "Context reappearance retained owner is invalid", 3)
    return claim_path, claim, claimed


def _context_security_decision(
    root: Path,
    ref: str,
    context_path: Path,
    state_dir: Path,
    state: dict[str, Any],
    claim_dir: Path,
    claim_path: Path,
    claim: dict[str, Any],
    claimed: Path,
    replacement: bytes,
    reasons: list[str],
) -> Path:
    if context_path.exists():
        if context_path.read_bytes() != replacement or core._secret_reasons(context_path.read_bytes()):
            raise KbError("KB2_CONTEXT_CONFLICT", "Context reappeared during restricted recovery", 3)
    else:
        core._install_bytes_to_absent(context_path, replacement)
    if context_path.read_bytes() != replacement or core._secret_reasons(context_path.read_bytes()):
        raise KbError("KB2_SECURITY_RESTORE_FAILED", "Context safe current state could not be restored", 4)
    claim["stage"] = "restricted"
    claim["reason_codes"] = reasons
    claim["replacement_digest"] = core._digest(replacement)
    claim["restricted_at"] = core._now()
    core._replace_file_after_sync(claim_path, core._json_bytes(claim))
    restricted = core._ensure_protected_directory(root, "ingress/restricted-hold")
    summary_path = restricted / f"{claim_dir.name}.json"
    summary = {
        "schema": "restricted-hold/v0.1-pilot",
        "id": claim_dir.name,
        "target": ref,
        "claim_ref": f"context-claim://{claim_dir.name}",
        "payload_digest": core._digest(claimed.read_bytes()),
        "reason_codes": reasons,
        "contains_payload": False,
        "externalization_pending": True,
        "created_at": core._now(),
    }
    core._replace_file_after_sync(summary_path, core._json_bytes(summary))
    state["decision"] = {
        "route": {"result": "restricted-hold", "context_ref": ref, "hold_ref": f"context-claim://{claim_dir.name}"},
        "security": {
            "precheck": "rejected",
            "profile": "restricted-summary/v1",
            "policy": "deterministic-secret-precheck/v0.1-pilot",
            "latest_hold": {
                "hold_ref": f"context-claim://{claim_dir.name}",
                "observed_digest": core._digest(claimed.read_bytes()),
                "reason_codes": reasons,
                "externalization_pending": True,
                "occurred_at": core._now(),
            },
        },
    }
    _save_state(state_dir, state)
    return summary_path


def _load_update_owner(root: Path, update_dir: Path) -> tuple[Path, dict[str, Any], Path, Path, Path, dict[str, Any], Path]:
    update_dir = core._guard_plain_directory(root, update_dir)
    if not re.fullmatch(r"[0-9a-f]{64}", update_dir.name):
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context update owner identity is invalid", 3)
    update_path = core._guard_plain_file(root, update_dir / "update.json")
    update = _load_owner_json(
        update_path,
        code="KB2_CONTEXT_UPDATE_INVALID",
        detail="Context update owner document is invalid",
    )
    expected = core._guard_plain_file(root, update_dir / "expected.md")
    candidate = core._guard_plain_file(root, update_dir / "candidate.md")
    update_id = update.get("id")
    ref = update.get("target")
    capture_ref = update.get("capture_ref")
    claim_id = update.get("claim_id")
    if (
        update.get("schema") != "context-update/v0.1-pilot"
        or update.get("owner") != "context-organizer/v0.1-pilot"
        or update.get("operation_key") != f"sha256:{update_dir.name}"
        or not isinstance(update_id, str)
        or not re.fullmatch(r"CUP-[0-9A-HJKMNP-TV-Z]{26}", update_id)
        or not isinstance(ref, str)
        or update.get("expected_entry") != "expected.md"
        or update.get("candidate_entry") != "candidate.md"
        or update.get("expected_digest") != core._digest(expected.read_bytes())
        or update.get("candidate_digest") != core._digest(candidate.read_bytes())
        or not _DIGEST_RE.fullmatch(str(update.get("base_digest")))
        or update.get("expected_digest") != update.get("base_digest")
        or update.get("operation") not in {"update", "correction"}
        or update.get("stage") not in {"prepared", "claimed", "installed", "applied", "conflict", "restricted"}
        or not isinstance(capture_ref, str)
        or not core._CAPTURE_RE.fullmatch(capture_ref.removeprefix("capture://"))
        or not isinstance(claim_id, str)
        or not re.fullmatch(r"CCL-[0-9A-HJKMNP-TV-Z]{26}", claim_id)
    ):
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context update owner contract is invalid", 3)
    _parse_context_ref(ref)
    capture_dir = core._capture_directory(root, capture_ref.removeprefix("capture://"))
    metadata, payload = core._load_capture_owner(root, capture_dir)
    source = metadata.get("source")
    expected_kind = "human-correction" if update["operation"] == "correction" else "direct-stdin"
    if (
        core._digest(payload) != update.get("payload_digest")
        or not isinstance(source, dict)
        or source.get("kind") != expected_kind
        or source.get("target") != ref
    ):
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context update capture provenance is invalid", 3)
    claim_dir = root / "ingress" / "context-quarantine" / claim_id
    claim_path, claim, claimed = _load_claim_owner(root, claim_dir, update_id=update_id, ref=ref)
    if claim.get("expected_digest") != update.get("base_digest"):
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context claim expected digest does not match update", 3)
    return update_path, update, expected, candidate, claim_path, claim, claimed


def _validate_applied_update_chain(
    root: Path,
    ref: str,
    state_dir: Path,
    state: dict[str, Any],
    context_path: Path,
) -> None:
    """Validate applied updates as one digest-linked chain without replaying them."""
    base_path = core._guard_plain_file(root, state_dir / "base.md")
    current = context_path.read_bytes()
    current_digest = core._digest(current)
    state_base = state.get("base_digest")
    if (
        state_base != current_digest
        or core._digest(base_path.read_bytes()) != state_base
    ):
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context current state does not match its owner", 3)
    active_override = state.get("active_override")
    if active_override is not None:
        if not isinstance(active_override, str) or not core._OVERRIDE_RE.fullmatch(active_override):
            raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context active override linkage is invalid", 3)
        core._load_override_owner(
            root,
            root / "governance" / "overrides" / f"{active_override}.yaml",
            expected_id=active_override,
            expected_target=ref,
        )

    intents = root / "governance" / "context-intents"
    roots: list[str] = []
    if intents.exists():
        intents = core._guard_plain_directory(root, intents)
        for owner_dir in intents.iterdir():
            if owner_dir.name.startswith(".staging-"):
                continue
            try:
                owner_path = core._guard_plain_file(root, owner_dir / "intent.json")
                header = _load_owner_json(
                    owner_path,
                    code="KB2_CONTEXT_INTENT_INVALID",
                    detail="Context intent owner document is invalid",
                )
            except KbError:
                continue
            if header.get("context_ref") != ref:
                continue
            _, owner, candidate_path = _load_intent_owner(root, owner_dir)
            if owner.get("stage") != "applied":
                raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context update chain root is not applied", 3)
            roots.append(str(owner.get("candidate_digest")))
            if owner.get("candidate_digest") != core._digest(candidate_path.read_bytes()):
                raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context update chain root digest is invalid", 3)
    if len(roots) != 1:
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context update chain has no unique applied root", 3)

    updates = root / "governance" / "context-updates"
    applied: list[dict[str, Any]] = []
    if updates.exists():
        updates = core._guard_plain_directory(root, updates)
        for update_dir in updates.iterdir():
            if update_dir.name.startswith(".staging-"):
                continue
            try:
                update_path = core._guard_plain_file(root, update_dir / "update.json")
                header = _load_owner_json(
                    update_path,
                    code="KB2_CONTEXT_UPDATE_INVALID",
                    detail="Context update owner document is invalid",
                )
            except KbError:
                continue
            if header.get("target") != ref or header.get("stage") != "applied":
                continue
            _, update, expected_path, candidate_path, claim_path, claim, claimed = _load_update_owner(
                root,
                update_dir,
            )
            if (
                update.get("claimed_digest") != update.get("base_digest")
                or claim.get("stage") != "applied"
                or claim.get("expected_digest") != update.get("base_digest")
                or claim.get("claimed_digest") != update.get("base_digest")
                or claim.get("applied_digest") != update.get("candidate_digest")
                or not claimed.is_file()
                or core._digest(claimed.read_bytes()) != update.get("base_digest")
                or core._digest(expected_path.read_bytes()) != update.get("base_digest")
                or core._digest(candidate_path.read_bytes()) != update.get("candidate_digest")
            ):
                raise KbError("KB2_CONTEXT_UPDATE_INVALID", "applied Context update evidence is inconsistent", 3)

            supersedes = update.get("supersedes")
            if supersedes is not None:
                if not isinstance(supersedes, str) or not core._OVERRIDE_RE.fullmatch(supersedes):
                    raise KbError("KB2_CONTEXT_UPDATE_INVALID", "applied Context override linkage is invalid", 3)
                core._load_override_owner(
                    root,
                    root / "governance" / "overrides" / f"{supersedes}.yaml",
                    expected_id=supersedes,
                    expected_target=ref,
                )

            if update.get("operation") == "correction":
                override_id = update.get("override_id")
                if not isinstance(override_id, str) or not core._OVERRIDE_RE.fullmatch(override_id):
                    raise KbError("KB2_CONTEXT_UPDATE_INVALID", "applied Context correction override is invalid", 3)
                override = core._load_override_owner(
                    root,
                    root / "governance" / "overrides" / f"{override_id}.yaml",
                    expected_id=override_id,
                    expected_target=ref,
                )
                if (
                    override.get("base_digest") != update.get("base_digest")
                    or override.get("observed_digest") != update.get("candidate_digest")
                    or override.get("correction_capture_ref") != update.get("capture_ref")
                    or override.get("supersedes") != supersedes
                ):
                    raise KbError("KB2_CONTEXT_UPDATE_INVALID", "applied Context correction linkage is invalid", 3)
            elif update.get("override_id") is not None:
                raise KbError("KB2_CONTEXT_UPDATE_INVALID", "ordinary Context update has an override owner", 3)

            applied.append(update)

    by_base: dict[str, list[dict[str, Any]]] = {}
    by_candidate: dict[str, list[dict[str, Any]]] = {}
    for update in applied:
        by_base.setdefault(str(update["base_digest"]), []).append(update)
        by_candidate.setdefault(str(update["candidate_digest"]), []).append(update)
    if any(len(items) != 1 for items in by_base.values()) or any(
        len(items) != 1 for items in by_candidate.values()
    ):
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context applied update chain forks or duplicates a successor", 3)

    chain: list[dict[str, Any]] = []
    digest = roots[0]
    visited: set[str] = set()
    while digest in by_base:
        update = by_base[digest][0]
        update_id = str(update["id"])
        if update_id in visited:
            raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context applied update chain contains a cycle", 3)
        visited.add(update_id)
        chain.append(update)
        digest = str(update["candidate_digest"])

    if digest != current_digest or len(chain) != len(applied):
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context applied update chain has a gap or orphan", 3)


def _prepare_update(
    root: Path,
    ref: str,
    capture_dir: Path,
    metadata: dict[str, Any],
    payload: bytes,
    text: str,
    base_digest: str,
    *,
    correction: bool,
) -> tuple[Path, bool]:
    operation = "correction" if correction else "update"
    operation_bytes = f"{ref}\n{base_digest}\n{core._digest(payload)}\n{operation}".encode("utf-8")
    key = core._digest(operation_bytes).removeprefix("sha256:")
    updates = core._ensure_plain_directory(root, "governance/context-updates")
    final = updates / key
    if final.exists():
        _load_update_owner(root, final)
        return final, False
    state_dir, state, context_path = _load_state(root, ref)
    base_path = core._guard_plain_file(root, state_dir / "base.md")
    expected = base_path.read_bytes()
    current = context_path.read_bytes()
    candidate = _render_update(current, text, correction=correction)
    update_id = core._new_id("CUP")
    claim_dir, _ = _create_claim_owner(root, update_id, ref, base_digest)
    staging = updates / (".staging-" + core._new_id("UPI"))
    staging.mkdir()
    update: dict[str, Any] = {
        "schema": "context-update/v0.1-pilot",
        "owner": "context-organizer/v0.1-pilot",
        "id": update_id,
        "operation_key": f"sha256:{key}",
        "operation": operation,
        "target": ref,
        "capture_ref": f"capture://{metadata['id']}",
        "payload_digest": core._digest(payload),
        "base_digest": base_digest,
        "expected_entry": "expected.md",
        "expected_digest": core._digest(expected),
        "candidate_entry": "candidate.md",
        "candidate_digest": core._digest(candidate),
        "claim_id": claim_dir.name,
        "supersedes": state.get("active_override"),
        "override_id": core._new_id("OVR") if correction else None,
        "stage": "prepared",
        "created_at": core._now(),
    }
    try:
        core._write_file_synced(staging / "expected.md", expected)
        core._write_file_synced(staging / "candidate.md", candidate)
        core._write_file_synced(staging / "update.json", core._json_bytes(update))
        created = _write_directory_bundle(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if not created:
        # The losing claim has no canonical update owner and contains no unique user fact.
        # Remove only this prepared, byte-free claim; the winner owns the durable update.
        _, losing_claim, losing_claimed = _load_claim_owner(
            root,
            claim_dir,
            update_id=update_id,
            ref=ref,
        )
        if losing_claim["stage"] != "prepared" or losing_claimed.exists():
            raise KbError("KB2_CONTEXT_CLAIM_INVALID", "losing Context claim is not safely disposable", 3)
        shutil.rmtree(claim_dir)
    _load_update_owner(root, final)
    return final, created


def _write_context_override(
    root: Path,
    update: dict[str, Any],
    expected: bytes,
    candidate: bytes,
) -> tuple[str, Path, bool]:
    override_id = str(update["override_id"])
    if not core._OVERRIDE_RE.fullmatch(override_id):
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "Context correction override identity is invalid", 3)
    overrides = core._ensure_plain_directory(root, "governance/overrides")
    path = core._guard_plain_file(root, overrides / f"{override_id}.yaml", required=False)
    capture_ref = str(update["capture_ref"])
    if path.exists():
        core._load_override_owner(root, path, expected_id=override_id, expected_target=str(update["target"]))
        return override_id, path, False
    diff = "".join(
        difflib.unified_diff(
            expected.decode("utf-8").splitlines(keepends=True),
            candidate.decode("utf-8").splitlines(keepends=True),
            fromfile="context-base",
            tofile="human-correction",
        )
    )
    record = {
        "schema": "human-override/v0.1-pilot",
        "id": override_id,
        "target": update["target"],
        "scope": {"kind": "object", "ref": update["target"]},
        "actor": "human-natural-language-correction",
        "reason": f"natural-language correction from {capture_ref}",
        "created_at": core._now(),
        "base_digest": update["expected_digest"],
        "observed_digest": update["candidate_digest"],
        "diff_format": "unified",
        "diff": diff,
        "supersedes": update.get("supersedes"),
        "correction_capture_ref": capture_ref,
    }
    core._write_file_synced(path, core._json_bytes(record))
    core._load_override_owner(root, path, expected_id=override_id, expected_target=str(update["target"]))
    return override_id, path, True


def _mark_update_capture(
    root: Path,
    update: dict[str, Any],
    *,
    result: str,
    reason: str | None = None,
    override_ref: str | None = None,
) -> None:
    capture_dir = core._capture_directory(root, str(update["capture_ref"]).removeprefix("capture://"))
    metadata, _ = core._load_capture_owner(root, capture_dir)
    route: dict[str, Any] = {"result": result, "context_ref": update["target"]}
    if reason is not None:
        route["reason"] = reason
    if override_ref is not None:
        route["override_ref"] = override_ref
    _set_capture_route(root, capture_dir, metadata, state=result, route=route)


def _claim_context_reappearance(
    root: Path,
    context_path: Path,
    claim_path: Path,
    claim: dict[str, Any],
) -> tuple[Path, bytes, list[str]]:
    records = claim.get("reappeared_entries")
    if records is None:
        records = []
        if claim.get("reappeared_entry") is not None:
            records.append(
                {
                    "entry": claim["reappeared_entry"],
                    "digest": claim["reappeared_digest"],
                    "classification": claim.get("reappearance_classification"),
                    "reason_codes": claim.get("reason_codes", []),
                }
            )
    if len(records) >= _MAX_CONTEXT_REAPPEARANCES + 1:
        raise KbError(
            "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED",
            "Context reappearance contention exceeded the bounded retry limit",
            3,
        )
    entry = "reappeared.bin" if not records else f"reappeared-{core._new_id('OBS').removeprefix('OBS-')}.bin"
    retained = core._guard_plain_file(root, claim_path.parent / entry, required=False)
    try:
        core._move_file_to_absent(context_path, retained)
    except (FileNotFoundError, FileExistsError) as exc:
        raise KbError("KB2_CONTEXT_CONFLICT", "Context reappearance could not be claimed", 3) from exc
    observed = retained.read_bytes()
    reasons = core._secret_reasons(observed)
    records.append(
        {
            "entry": entry,
            "digest": core._digest(observed),
            "classification": "secret" if reasons else "safe",
            "reason_codes": reasons,
            "observed_at": core._now(),
        }
    )
    claim["reappeared_entries"] = records
    claim["reappeared_entry"] = records[-1]["entry"]
    claim["reappeared_digest"] = records[-1]["digest"]
    claim["reappearance_classification"] = records[-1]["classification"]
    core._replace_file_after_sync(claim_path, core._json_bytes(claim))
    return retained, observed, reasons


def _install_bounded_context_safe_side(context_path: Path, content: bytes) -> None:
    """Restore the verified safe side and let the caller inspect one final race."""
    try:
        core._install_bytes_to_absent(context_path, content)
    except FileExistsError:
        return


def _install_context_safe_side_without_retry(context_path: Path, content: bytes) -> None:
    """Close the bounded result boundary without opening another install hook."""
    fd, temporary_name = tempfile.mkstemp(prefix=".kb2-bounded-restore-", dir=context_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        core._move_file_to_absent(temporary, context_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _context_reappearance_count(claim: dict[str, Any]) -> int:
    records = claim.get("reappeared_entries")
    if isinstance(records, list):
        return len(records)
    return 1 if claim.get("reappeared_entry") is not None else 0


def _handle_context_reappearance(
    root: Path,
    update_path: Path,
    update: dict[str, Any],
    expected: bytes,
    candidate: bytes,
    context_path: Path,
    state_dir: Path,
    state: dict[str, Any],
    claim_path: Path,
    claim: dict[str, Any],
) -> None:
    claim_dir = claim_path.parent
    attempts = 0
    bounded_exit = False
    latest_secret: tuple[Path, list[str]] | None = None
    while True:
        while context_path.exists():
            retained, observed, reasons = _claim_context_reappearance(
                root,
                context_path,
                claim_path,
                claim,
            )
            attempts += 1
            if reasons:
                latest_secret = (retained, reasons)
                if context_path.exists() and context_path.read_bytes() != expected:
                    if _context_reappearance_count(claim) >= _MAX_CONTEXT_REAPPEARANCES:
                        retained, _, reasons = _claim_context_reappearance(
                            root,
                            context_path,
                            claim_path,
                            claim,
                        )
                        latest_secret = (retained, reasons)
                        bounded_exit = True
                    else:
                        continue
                if _context_reappearance_count(claim) >= _MAX_CONTEXT_REAPPEARANCES:
                    _install_bounded_context_safe_side(context_path, expected)
                    bounded_exit = True
                else:
                    core._install_bytes_to_absent(context_path, expected)
                if context_path.exists() and context_path.read_bytes() != expected:
                    if _context_reappearance_count(claim) >= _MAX_CONTEXT_REAPPEARANCES:
                        retained, _, reasons = _claim_context_reappearance(
                            root,
                            context_path,
                            claim_path,
                            claim,
                        )
                        latest_secret = (retained, reasons)
                        _install_context_safe_side_without_retry(context_path, expected)
                        bounded_exit = True
                        break
                    continue
                break
            core._install_bytes_to_absent(context_path, observed)
            if context_path.read_bytes() != observed:
                if attempts >= _MAX_CONTEXT_REAPPEARANCES:
                    raise KbError(
                        "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED",
                        "Context reappearance contention exceeded the bounded retry limit",
                        3,
                    )
                continue
            conflict = _new_context_conflict(
                root,
                str(update["target"]),
                expected,
                candidate,
                observed,
                capture_ref=str(update["capture_ref"]),
                base_digest=str(update["base_digest"]),
            )
            claim["stage"] = "conflict"
            claim["conflict_ref"] = f"context-conflict://{conflict.name}"
            core._replace_file_after_sync(claim_path, core._json_bytes(claim))
            update["stage"] = "conflict"
            update["conflict_ref"] = f"context-conflict://{conflict.name}"
            update["conflict_at"] = core._now()
            core._replace_file_after_sync(update_path, core._json_bytes(update))
            state["decision"]["route"] = {
                "result": "needs-review",
                "context_ref": update["target"],
                "conflict_ref": update["conflict_ref"],
            }
            _save_state(state_dir, state)
            _mark_update_capture(root, update, result="needs-review", reason="context-reappearance-safe")
            raise KbError(
                "KB2_CONTEXT_CONFLICT",
                "safe Context reappearance retained both sides for review",
                3,
                {"context_ref": update["target"], "conflict_ref": update["conflict_ref"]},
                [str(conflict.relative_to(root))],
            )

        if latest_secret is None:
            raise KbError(
                "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED",
                "Context reappearance owner has no classified retained bytes",
                3,
            )
        reappeared_path, reasons = latest_secret
        summary = _context_security_decision(
            root,
            str(update["target"]),
            context_path,
            state_dir,
            state,
            claim_dir,
            claim_path,
            claim,
            reappeared_path,
            expected,
            reasons,
        )
        if context_path.exists() and context_path.read_bytes() != expected:
            if _context_reappearance_count(claim) >= _MAX_CONTEXT_REAPPEARANCES:
                retained, _, reasons = _claim_context_reappearance(
                    root,
                    context_path,
                    claim_path,
                    claim,
                )
                latest_secret = (retained, reasons)
                _install_context_safe_side_without_retry(context_path, expected)
                bounded_exit = True
                if context_path.exists() and core._secret_reasons(context_path.read_bytes()):
                    raise KbError(
                        "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED",
                        "Context reappearance remained secret-like at the bounded result boundary",
                        3,
                    )
            else:
                continue
        update["stage"] = "restricted"
        update["restricted_ref"] = f"context-claim://{claim_dir.name}"
        update["restricted_at"] = core._now()
        core._replace_file_after_sync(update_path, core._json_bytes(update))
        _mark_update_capture(root, update, result="restricted-hold", reason="context-reappearance-secret")
        if bounded_exit:
            raise KbError(
                "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED",
                "Context reappearance contention reached the bounded result boundary",
                3,
                {"context_ref": update["target"], "hold_ref": f"context-claim://{claim_dir.name}"},
                [str(claim_dir.relative_to(root)), str(summary.relative_to(root))],
            )
        raise KbError(
            "KB2_RESTRICTED_EDIT",
            "secret-like Context reappearance was retained in a protected owner",
            2,
            {"context_ref": update["target"], "hold_ref": f"context-claim://{claim_dir.name}"},
            [str(claim_dir.relative_to(root)), str(summary.relative_to(root))],
        )


def _advance_update(
    root: Path,
    update_dir: Path,
    *,
    before_claim: Any | None = None,
) -> dict[str, Any]:
    update_path, update, expected_path, candidate_path, claim_path, claim, claimed = _load_update_owner(
        root,
        update_dir,
    )
    ref = str(update["target"])
    state_dir, state, context_path = _load_state(root, ref)
    expected = expected_path.read_bytes()
    candidate = candidate_path.read_bytes()
    stage = update["stage"]
    if stage == "applied":
        _validate_applied_update_chain(root, ref, state_dir, state, context_path)
        override_ref = None
        if update["operation"] == "correction":
            override_ref = f"override://{update['override_id']}"
            core._load_override_owner(
                root,
                root / "governance" / "overrides" / f"{update['override_id']}.yaml",
                expected_id=str(update["override_id"]),
                expected_target=ref,
            )
        return {"override_ref": override_ref, "created_override": False}
    if stage == "conflict":
        raise KbError("KB2_CONTEXT_CONFLICT", "Context update remains in needs-review", 3)
    if stage == "restricted":
        raise KbError("KB2_RESTRICTED_EDIT", "Context update remains in restricted hold", 2)

    if stage == "prepared":
        if before_claim is not None:
            before_claim()
        if claimed.exists():
            raise KbError("KB2_CONTEXT_CLAIM_INVALID", "prepared Context claim already has bytes", 3)
        try:
            core._move_file_to_absent(context_path, claimed)
        except (FileNotFoundError, FileExistsError) as exc:
            raise KbError("KB2_CONTEXT_CONFLICT", "Context could not be claimed for CAS update", 3) from exc
        claimed_bytes = claimed.read_bytes()
        claim["claimed_digest"] = core._digest(claimed_bytes)
        claim["stage"] = "claimed"
        claim["claimed_at"] = core._now()
        core._replace_file_after_sync(claim_path, core._json_bytes(claim))
        update["stage"] = "claimed"
        update["claimed_digest"] = claim["claimed_digest"]
        core._replace_file_after_sync(update_path, core._json_bytes(update))
        stage = "claimed"

    if stage == "claimed":
        claimed_bytes = claimed.read_bytes()
        claimed_digest = core._digest(claimed_bytes)
        reasons = core._secret_reasons(claimed_bytes)
        state_base = state.get("base_digest")
        if claimed_digest != update["base_digest"] or state_base != update["base_digest"]:
            if reasons:
                summary = _context_security_decision(
                    root,
                    ref,
                    context_path,
                    state_dir,
                    state,
                    claimed.parent,
                    claim_path,
                    claim,
                    claimed,
                    expected,
                    reasons,
                )
                update["stage"] = "restricted"
                update["restricted_ref"] = f"context-claim://{claimed.parent.name}"
                update["restricted_at"] = core._now()
                core._replace_file_after_sync(update_path, core._json_bytes(update))
                _mark_update_capture(root, update, result="restricted-hold", reason="context-cas-secret-race")
                raise KbError(
                    "KB2_RESTRICTED_EDIT",
                    "secret-like concurrent Context bytes were retained in a protected owner",
                    2,
                    {"context_ref": ref, "hold_ref": update["restricted_ref"]},
                    [str(claimed.parent.relative_to(root)), str(summary.relative_to(root))],
                )
            core._install_bytes_to_absent(context_path, claimed_bytes)
            conflict = _new_context_conflict(
                root,
                ref,
                expected,
                candidate,
                claimed_bytes,
                capture_ref=str(update["capture_ref"]),
                base_digest=str(update["base_digest"]),
            )
            update["stage"] = "conflict"
            update["conflict_ref"] = f"context-conflict://{conflict.name}"
            update["conflict_at"] = core._now()
            core._replace_file_after_sync(update_path, core._json_bytes(update))
            claim["stage"] = "conflict"
            core._replace_file_after_sync(claim_path, core._json_bytes(claim))
            state["decision"]["route"] = {
                "result": "needs-review",
                "context_ref": ref,
                "conflict_ref": update["conflict_ref"],
            }
            _save_state(state_dir, state)
            _mark_update_capture(root, update, result="needs-review", reason="context-base-conflict")
            raise KbError(
                "KB2_CONTEXT_CONFLICT",
                "Context changed before CAS update; current and candidate were retained",
                3,
                {"context_ref": ref, "conflict_ref": update["conflict_ref"]},
                [str(conflict.relative_to(root))],
            )

        if reasons:
            raise KbError("KB2_CONTEXT_UPDATE_INVALID", "verified Context base unexpectedly contains secret-like bytes", 3)
        if context_path.exists():
            _handle_context_reappearance(
                root,
                update_path,
                update,
                expected,
                candidate,
                context_path,
                state_dir,
                state,
                claim_path,
                claim,
            )
        core._install_bytes_to_absent(context_path, candidate)
        if context_path.read_bytes() != candidate:
            raise KbError("KB2_CONTEXT_CONFLICT", "Context candidate install postcondition failed", 3)
        update["stage"] = "installed"
        update["installed_at"] = core._now()
        core._replace_file_after_sync(update_path, core._json_bytes(update))
        stage = "installed"

    if stage != "installed" or context_path.read_bytes() != candidate:
        raise KbError("KB2_CONTEXT_UPDATE_INVALID", "installed Context update owner is inconsistent", 3)

    override_ref = None
    created_override = False
    if update["operation"] == "correction":
        override_id, _, created_override = _write_context_override(root, update, expected, candidate)
        state["active_override"] = override_id
        override_ref = f"override://{override_id}"
    base_path = core._guard_plain_file(root, state_dir / "base.md")
    core._replace_file_after_sync(base_path, candidate)
    state["base_digest"] = update["candidate_digest"]
    state["latest_capture_ref"] = update["capture_ref"]
    route_result = "correction-applied" if update["operation"] == "correction" else "context-updated"
    state["decision"]["route"] = {"result": route_result, "context_ref": ref}
    state["decision"]["security"] = {
        "precheck": "passed",
        "profile": "personal-full/v1",
        "policy": "deterministic-secret-precheck/v0.1-pilot",
        "latest_hold": state["decision"].get("security", {}).get("latest_hold"),
    }
    _save_state(state_dir, state)
    _mark_update_capture(root, update, result=route_result, override_ref=override_ref)
    claim["stage"] = "applied"
    claim["applied_digest"] = update["candidate_digest"]
    core._replace_file_after_sync(claim_path, core._json_bytes(claim))
    update["stage"] = "applied"
    update["applied_at"] = core._now()
    core._replace_file_after_sync(update_path, core._json_bytes(update))
    if context_path.read_bytes() != candidate:
        observed = context_path.read_bytes()
        if core._secret_reasons(observed):
            # A post-commit secret reappearance is isolated by the normal organize path.
            organize_context(root, ref)
        conflict = _new_context_conflict(
            root,
            ref,
            candidate,
            candidate,
            observed,
            capture_ref=str(update["capture_ref"]),
            base_digest=str(update["candidate_digest"]),
        )
        raise KbError(
            "KB2_CONTEXT_CONFLICT",
            "Context changed at update result boundary",
            3,
            {"context_ref": ref, "conflict_ref": f"context-conflict://{conflict.name}"},
        )
    return {"override_ref": override_ref, "created_override": created_override}


def _missing_base(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    ref: str,
) -> None:
    _set_capture_route(
        root,
        capture_dir,
        metadata,
        state="needs-review",
        route={"result": "needs-review", "reason": "context-base-digest-required", "context_ref": ref},
    )
    raise KbError(
        "KB2_BASE_DIGEST_REQUIRED",
        "Context update requires the machine caller's base digest",
        2,
        {"capture_ref": f"capture://{metadata['id']}", "context_ref": ref, "committed": True},
        [str(capture_dir.relative_to(root))],
    )


def create_or_update_context(
    root: Path,
    capture_dir: Path,
    metadata: dict[str, Any],
    payload: bytes,
    text: str,
    *,
    context_ref: str | None,
    base_digest: str | None,
    fail_after_context_intent: bool,
    before_context_claim: Any | None,
) -> dict[str, Any]:
    root = core._guard_root(root)
    core._verify_capture_snapshot(root, capture_dir, metadata, payload)
    if context_ref is None:
        owner_dir, created = _prepare_intent(root, capture_dir, metadata, payload, text)
        if fail_after_context_intent and created:
            raise KbError(
                "KB2_INJECTED_CONTEXT_INTENT",
                "injected failure after stable Context intent owner commit",
                4,
                {"capture_ref": f"capture://{metadata['id']}", "committed": True},
                [str(owner_dir.relative_to(root))],
            )
        owner = _advance_intent(root, owner_dir)
        ref = str(owner["context_ref"])
        # Exact replay has its own durable capture, but never creates another Context.
        _set_capture_route(
            root,
            capture_dir,
            metadata,
            state="context-created",
            route={"result": "context-created", "context_ref": ref, "replayed": not created},
        )
        organize_context(root, ref, require_recovery=False)
        return {
            "route": "context-created",
            "capture_ref": f"capture://{metadata['id']}",
            "context_ref": ref,
            "payload_digest": metadata["payload_digest"],
            "user_structured_fields": 0,
            "replayed": not created,
            "changed": [str(capture_dir.relative_to(root)), str(_load_state(root, ref)[2].relative_to(root))],
        }

    _parse_context_ref(context_ref)
    if base_digest is None:
        _missing_base(root, capture_dir, metadata, context_ref)
    if not _DIGEST_RE.fullmatch(base_digest):
        _set_capture_route(
            root,
            capture_dir,
            metadata,
            state="needs-review",
            route={"result": "needs-review", "reason": "context-base-digest-invalid", "context_ref": context_ref},
        )
        raise KbError("KB2_BASE_DIGEST_MISMATCH", "Context base digest is invalid", 2)
    update_dir, created = _prepare_update(
        root,
        context_ref,
        capture_dir,
        metadata,
        payload,
        text,
        base_digest,
        correction=False,
    )
    result = _advance_update(root, update_dir, before_claim=before_context_claim if created else None)
    if not created:
        _set_capture_route(
            root,
            capture_dir,
            metadata,
            state="context-updated",
            route={"result": "context-updated", "context_ref": context_ref, "replayed": True},
        )
    return {
        "route": "context-updated",
        "capture_ref": f"capture://{metadata['id']}",
        "context_ref": context_ref,
        "payload_digest": metadata["payload_digest"],
        "user_structured_fields": 0,
        "replayed": not created,
        "override": {"active": bool(_load_state(root, context_ref)[1].get("active_override")), "created": False},
        "changed": [str(capture_dir.relative_to(root)), str(_load_state(root, context_ref)[2].relative_to(root))],
        **result,
    }


def _direct_context_secret(
    root: Path,
    ref: str,
    state_dir: Path,
    state: dict[str, Any],
    context_path: Path,
    replacement: bytes,
    reasons: list[str],
) -> None:
    claim_dir, claim_path = _create_claim_owner(root, core._new_id("CUP"), ref, str(state["base_digest"]))
    claim = core._load_json(claim_path)
    claimed = claim_dir / "claimed.bin"
    core._move_file_to_absent(context_path, claimed)
    claim["claimed_digest"] = core._digest(claimed.read_bytes())
    claim["stage"] = "claimed"
    core._replace_file_after_sync(claim_path, core._json_bytes(claim))
    summary = _context_security_decision(
        root,
        ref,
        context_path,
        state_dir,
        state,
        claim_dir,
        claim_path,
        claim,
        claimed,
        replacement,
        reasons,
    )
    raise KbError(
        "KB2_RESTRICTED_EDIT",
        "secret-like Context edit was retained in a protected owner",
        2,
        {"context_ref": ref, "hold_ref": f"context-claim://{claim_dir.name}"},
        [str(claim_dir.relative_to(root)), str(summary.relative_to(root))],
    )


def organize_context(
    root: Path,
    ref: str,
    *,
    actor: str = "human-direct-edit",
    reason: str | None = None,
    correction_capture_ref: str | None = None,
    require_recovery: bool = True,
) -> dict[str, Any]:
    root = core._guard_root(root)
    if require_recovery:
        core._require_recovery_clear(root)
    state_dir, state, context_path = _load_state(root, ref)
    base_path = core._guard_plain_file(root, state_dir / "base.md")
    base = base_path.read_bytes()
    if core._digest(base) != state.get("base_digest"):
        raise KbError("KB2_BASE_DIGEST_MISMATCH", "Context base owner does not match state", 3)
    current = context_path.read_bytes()
    current_digest = core._digest(current)
    if current_digest == state["base_digest"]:
        active = state.get("active_override")
        if active:
            core._load_override_owner(
                root,
                root / "governance" / "overrides" / f"{active}.yaml",
                expected_id=str(active),
                expected_target=ref,
            )
        return {
            "route": state["decision"]["route"]["result"],
            "context_ref": ref,
            "changed": [],
            "override": {"active": bool(active), "created": False},
        }
    reasons = core._secret_reasons(current)
    if reasons:
        _direct_context_secret(root, ref, state_dir, state, context_path, base, reasons)
    if context_path.read_bytes() != current:
        observed = context_path.read_bytes()
        if core._secret_reasons(observed):
            _direct_context_secret(root, ref, state_dir, state, context_path, base, core._secret_reasons(observed))
        conflict = _new_context_conflict(
            root,
            ref,
            current,
            current,
            observed,
            capture_ref=str(state["latest_capture_ref"]),
            base_digest=str(state["base_digest"]),
        )
        raise KbError(
            "KB2_CONTEXT_CONFLICT",
            "Context changed during organize; both safe sides were retained",
            3,
            {"context_ref": ref, "conflict_ref": f"context-conflict://{conflict.name}"},
        )
    overrides = core._ensure_plain_directory(root, "governance/overrides")
    existing = core._find_existing_override(
        root,
        overrides,
        ref,
        str(state["base_digest"]),
        current_digest,
        state.get("active_override"),
        actor,
        correction_capture_ref,
    )
    if existing is None:
        override_id = core._new_id("OVR")
        diff = "".join(
            difflib.unified_diff(
                base.decode("utf-8").splitlines(keepends=True),
                current.decode("utf-8").splitlines(keepends=True),
                fromfile="context-base",
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
            "created_at": core._now(),
            "base_digest": state["base_digest"],
            "observed_digest": current_digest,
            "diff_format": "unified",
            "diff": diff,
            "supersedes": state.get("active_override"),
        }
        if correction_capture_ref is not None:
            record["correction_capture_ref"] = correction_capture_ref
        override_path = overrides / f"{override_id}.yaml"
        core._write_file_synced(override_path, core._json_bytes(record))
        core._load_override_owner(root, override_path, expected_id=override_id, expected_target=ref)
    else:
        override_id = str(existing["id"])
        override_path = overrides / f"{override_id}.yaml"
    if context_path.read_bytes() != current:
        raise KbError("KB2_CONTEXT_CONFLICT", "Context changed before override commit", 3)
    core._replace_file_after_sync(base_path, current)
    state["base_digest"] = current_digest
    state["active_override"] = override_id
    state["last_organized_at"] = core._now()
    _save_state(state_dir, state)
    if context_path.read_bytes() != current:
        raise KbError("KB2_CONTEXT_CONFLICT", "Context changed at organize result boundary", 3)
    return {
        "route": state["decision"]["route"]["result"],
        "context_ref": ref,
        "changed": [str(override_path.relative_to(root))],
        "override": {"active": True, "created": existing is None, "ref": f"override://{override_id}"},
    }


def correct_context(
    root: Path,
    ref: str,
    capture_dir: Path,
    metadata: dict[str, Any],
    payload: bytes,
    correction: str,
) -> dict[str, Any]:
    root = core._guard_root(root)
    organize_context(root, ref)
    state_dir, state, context_path = _load_state(root, ref)
    base_digest = str(state["base_digest"])
    if core._digest(context_path.read_bytes()) != base_digest:
        raise KbError("KB2_BASE_DIGEST_MISMATCH", "Context changed before correction prepare", 3)
    update_dir, created = _prepare_update(
        root,
        ref,
        capture_dir,
        metadata,
        payload,
        correction,
        base_digest,
        correction=True,
    )
    applied = _advance_update(root, update_dir)
    _, applied_state, _ = _load_state(root, ref)
    result = {
        "route": applied_state["decision"]["route"]["result"],
        "context_ref": ref,
        "correction_recorded": True,
        "correction_capture_ref": f"capture://{metadata['id']}",
        "replayed": not created,
        "override": {
            "active": True,
            "created": applied["created_override"],
            "ref": applied["override_ref"],
        },
        "changed": [
            str(capture_dir.relative_to(root)),
            str(context_path.relative_to(root)),
            str(update_dir.relative_to(root)),
        ],
    }
    return result


def explain_context(root: Path, ref: str) -> dict[str, Any]:
    root = core._guard_root(root)
    _parse_context_ref(ref)
    # Guard the requested object before global replay, matching the Phase 1.1 leaf rule.
    state_dir, state, context_path = _load_state(root, ref)
    core._guard_plain_file(root, context_path)
    core._require_recovery_clear(root)
    state_dir, state, context_path = _load_state(root, ref)
    origin_id = str(state["origin_capture_ref"]).removeprefix("capture://")
    latest_id = str(state["latest_capture_ref"]).removeprefix("capture://")
    origin_metadata, _ = core._load_capture_owner(root, core._capture_directory(root, origin_id))
    latest_metadata, _ = core._load_capture_owner(root, core._capture_directory(root, latest_id))
    override = None
    override_id = state.get("active_override")
    if override_id:
        record = core._load_override_owner(
            root,
            root / "governance" / "overrides" / f"{override_id}.yaml",
            expected_id=str(override_id),
            expected_target=ref,
        )
        override = {
            "ref": f"override://{override_id}",
            "scope": record["scope"],
            "actor": record["actor"],
            "reason": record["reason"],
            "base_digest": record["base_digest"],
            "observed_digest": record["observed_digest"],
            "diff": record["diff"],
            "correction_capture_ref": record.get("correction_capture_ref"),
        }
    decision = state["decision"]
    return {
        "ref": ref,
        "context_entry": str(context_path.relative_to(root)).replace(os.sep, "/"),
        "capture_ref": state["origin_capture_ref"],
        "latest_capture_ref": state["latest_capture_ref"],
        "capture_digest": origin_metadata.get("payload_digest"),
        "latest_capture_digest": latest_metadata.get("payload_digest"),
        "route": decision["route"],
        "security": decision["security"],
        "base_digest": state["base_digest"],
        "current_digest": core._digest(context_path.read_bytes()),
        "human_override": override,
        "decision_order": [
            "hard-security-invariants",
            "human-correction",
            "object-override",
            "organizer-proposal",
            "safe-reversible-default",
        ],
    }


def close_context(root: Path, ref: str, *, status: str = "completed") -> dict[str, Any]:
    if status not in {"completed", "blocked"}:
        raise KbError("KB2_CONTEXT_LIFECYCLE_INVALID", "Context close status is invalid", 2)
    state_dir, state, context_path = _load_state(root, ref)
    _validate_applied_update_chain(root, ref, state_dir, state, context_path)
    lifecycle = _context_lifecycle(ref, state)
    current = lifecycle["status"]
    if current in {"completed", "blocked"}:
        if current != status:
            raise KbError("KB2_CONTEXT_LIFECYCLE_CONFLICT", "Context is already closed with another status", 3)
        return {"context_ref": ref, "status": current, "changed": []}
    if lifecycle.get("legacy"):
        created_at = state.get("updated_at")
        if not isinstance(created_at, str) or not _LIFECYCLE_TIME_RE.fullmatch(created_at):
            created_at = core._now()
        lifecycle = _new_context_lifecycle(
            ref,
            state["context_entry"],
            status=status,
            created_at=created_at,
        )
    else:
        lifecycle = dict(lifecycle)
        lifecycle["status"] = status
        lifecycle["updated_at"] = core._now()
    state["lifecycle"] = lifecycle
    _save_state(state_dir, state)
    return {"context_ref": ref, "status": status, "changed": [str((state_dir / "state.json").relative_to(root)).replace(os.sep, "/")]}


def _recover_intents(root: Path) -> dict[str, Any]:
    owners = root / "governance" / "context-intents"
    if not owners.exists():
        return {"recovered": 0, "unresolved": []}
    owners = core._guard_plain_directory(root, owners)
    recovered = 0
    unresolved: list[dict[str, str]] = []
    for owner_dir in sorted(path for path in owners.iterdir() if path.name and not path.name.startswith(".staging-")):
        try:
            _, before, _ = _load_intent_owner(root, owner_dir)
            was_applied = before.get("stage") == "applied"
            after = _advance_intent(root, owner_dir)
            if not was_applied and after.get("stage") == "applied":
                recovered += 1
        except KbError as exc:
            unresolved.append({"context_intent": owner_dir.name, "code": exc.code})
    return {"recovered": recovered, "unresolved": unresolved}


def _recover_updates(root: Path) -> dict[str, Any]:
    updates = root / "governance" / "context-updates"
    if not updates.exists():
        return {"recovered": 0, "unresolved": []}
    updates = core._guard_plain_directory(root, updates)
    recovered = 0
    unresolved: list[dict[str, str]] = []
    for update_dir in sorted(path for path in updates.iterdir() if path.name and not path.name.startswith(".staging-")):
        try:
            _, update, expected, candidate, claim_path, claim, claimed = _load_update_owner(root, update_dir)
            stage = update["stage"]
            if stage == "conflict":
                unresolved.append({"context_update": str(update["id"]), "code": "KB2_CONTEXT_CONFLICT"})
                continue
            if stage == "restricted":
                _, state, context_path = _load_state(root, str(update["target"]))
                if context_path.exists() and context_path.read_bytes() != expected.read_bytes():
                    try:
                        _handle_context_reappearance(
                            root,
                            root / "governance" / "context-updates" / update_dir.name / "update.json",
                            update,
                            expected.read_bytes(),
                            candidate.read_bytes(),
                            context_path,
                            _state_directory(root, _parse_context_ref(str(update["target"]))),
                            state,
                            claim_path,
                            claim,
                        )
                    except KbError as exc:
                        code = (
                            "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED"
                            if exc.code in {"KB2_RESTRICTED_EDIT", "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED"}
                            else exc.code
                        )
                        unresolved.append({"context_update": update_dir.name, "code": code})
                        continue
                records = claim.get("reappeared_entries")
                if records is None:
                    records = []
                    if claim.get("reappeared_entry") is not None:
                        records.append(
                            {
                                "entry": claim.get("reappeared_entry"),
                                "digest": claim.get("reappeared_digest"),
                                "classification": claim.get("reappearance_classification"),
                            }
                        )
                latest = records[-1] if records else {}
                if latest.get("classification") == "secret":
                    reappeared = core._guard_plain_file(root, claim_path.parent / str(latest["entry"]))
                    summary_path = core._guard_plain_file(
                        root,
                        root / "ingress" / "restricted-hold" / f"{claim_path.parent.name}.json",
                    )
                    summary = core._load_json(summary_path)
                    if (
                        claim.get("stage") != "restricted"
                        or not core._secret_reasons(reappeared.read_bytes())
                        or latest.get("digest") != core._digest(reappeared.read_bytes())
                        or context_path.read_bytes() != expected.read_bytes()
                        or core._secret_reasons(context_path.read_bytes())
                        or state["decision"]["security"].get("precheck") != "rejected"
                        or summary.get("payload_digest") != latest.get("digest")
                        or summary.get("contains_payload") is not False
                        or summary.get("externalization_pending") is not True
                    ):
                        raise KbError("KB2_CONTEXT_RESTRICTED_INVALID", "restricted Context reappearance owner is invalid", 3)
                    unresolved.append(
                        {"context_update": update_dir.name, "code": "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED"}
                    )
                    continue
                if (
                    claim.get("stage") != "restricted"
                    or not claimed.is_file()
                    or not core._secret_reasons(claimed.read_bytes())
                    or context_path.read_bytes() != expected.read_bytes()
                    or core._secret_reasons(context_path.read_bytes())
                    or state["decision"]["security"].get("precheck") != "rejected"
                ):
                    raise KbError("KB2_CONTEXT_RESTRICTED_INVALID", "restricted Context update owner is invalid", 3)
                continue
            before = stage
            _advance_update(root, update_dir)
            if before != "applied":
                recovered += 1
        except KbError as exc:
            unresolved.append({"context_update": update_dir.name, "code": exc.code})
    return {"recovered": recovered, "unresolved": unresolved}


def _recover_conflicts(root: Path) -> dict[str, Any]:
    conflicts = root / "governance" / "context-conflicts"
    if not conflicts.exists():
        return {"recovered": 0, "unresolved": []}
    conflicts = core._guard_plain_directory(root, conflicts)
    unresolved: list[dict[str, str]] = []
    for conflict_dir in sorted(conflicts.glob("CCF-*")):
        try:
            record = _load_context_conflict(root, conflict_dir)
            _, state, context_path = _load_state(root, str(record["target"]))
            current = context_path.read_bytes()
            reasons = core._secret_reasons(current)
            if reasons:
                base = core._guard_plain_file(root, _state_directory(root, _parse_context_ref(str(record["target"]))) / "base.md").read_bytes()
                _direct_context_secret(
                    root,
                    str(record["target"]),
                    _state_directory(root, _parse_context_ref(str(record["target"]))),
                    state,
                    context_path,
                    base,
                    reasons,
                )
            unresolved.append({"context_conflict": conflict_dir.name, "code": "KB2_CONTEXT_CONFLICT"})
        except KbError as exc:
            unresolved.append({"context_conflict": conflict_dir.name, "code": exc.code})
    return {"recovered": 0, "unresolved": unresolved}


def recover_contexts(root: Path) -> dict[str, Any]:
    root = core._guard_root(root)
    intents = _recover_intents(root)
    updates = _recover_updates(root)
    conflicts = _recover_conflicts(root)
    return {
        "recovered": intents["recovered"] + updates["recovered"] + conflicts["recovered"],
        "unresolved": [*intents["unresolved"], *updates["unresolved"], *conflicts["unresolved"]],
        "intents": intents,
        "updates": updates,
        "conflicts": conflicts,
    }
