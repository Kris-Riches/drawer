"""Minimal read-only Registry, HOME, and basic-find bootstrap."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from . import context as context_core
from . import core
from .result import KbError


_CONFIG_DIGEST = core._digest(
    b"kb2-bootstrap/v0.2-search\n"
    b"ranking=artifact-id:1000,uri:1000,title-exact:850,title:800,body-phrase:300,body-terms:250\n"
)
_BUILD_RE = re.compile(r"^BLD-[0-9A-HJKMNP-TV-Z]{26}$")
_CAPTURE_RE = re.compile(r"^CAP-[0-9A-HJKMNP-TV-Z]{26}$")
_CONTEXT_RE = re.compile(r"^CTX-[0-9A-HJKMNP-TV-Z]{26}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_SEARCH_SCHEMA = "kb2-bootstrap-search/v0.2"


def _relative(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace(os.sep, "/")


def _optional_directory(root: Path, relative: str) -> Path | None:
    path = root / relative
    core._guard_path(root, path)
    if not path.exists():
        return None
    return core._guard_plain_directory(root, path)


def _read_source(root: Path, path: Path, manifest: dict[str, str]) -> bytes:
    guarded = core._guard_plain_file(root, path)
    data = guarded.read_bytes()
    manifest[_relative(root, guarded)] = core._digest(data)
    return data


def _record_optional_source(root: Path, path: Path, manifest: dict[str, str]) -> None:
    core._guard_path(root, path)
    if path.exists():
        _read_source(root, path, manifest)
    else:
        manifest[f"@presence/{_relative(root, path)}"] = "missing"


def _record_capture_chain_sources(root: Path, capture_ref: str, manifest: dict[str, str]) -> None:
    capture_id = _capture_id(capture_ref)
    capture_dir = core._capture_directory(root, capture_id)
    for name in ("owner.json", "capture.json", "payload.bin"):
        _read_source(root, capture_dir / name, manifest)
    _record_optional_source(root, capture_dir / "payload-snapshot.bin", manifest)


def _record_context_chain_sources(root: Path, ref: str, manifest: dict[str, str]) -> None:
    intents = _optional_directory(root, "governance/context-intents")
    if intents is not None:
        for owner_dir in sorted(intents.iterdir()):
            if owner_dir.name.startswith(".staging-"):
                continue
            owner_dir = core._guard_plain_directory(root, owner_dir)
            owner_path = core._guard_plain_file(root, owner_dir / "intent.json")
            owner = context_core._load_owner_json(
                owner_path,
                code="KB2_CONTEXT_INTENT_INVALID",
                detail="Context intent owner document is invalid",
            )
            if owner.get("context_ref") != ref:
                continue
            _, owner, candidate = context_core._load_intent_owner(root, owner_dir)
            _read_source(root, owner_path, manifest)
            _read_source(root, candidate, manifest)
            _record_capture_chain_sources(root, str(owner["capture_ref"]), manifest)

    updates = _optional_directory(root, "governance/context-updates")
    if updates is not None:
        for update_dir in sorted(updates.iterdir()):
            if update_dir.name.startswith(".staging-"):
                continue
            update_dir = core._guard_plain_directory(root, update_dir)
            update_path = core._guard_plain_file(root, update_dir / "update.json")
            header = context_core._load_owner_json(
                update_path,
                code="KB2_CONTEXT_UPDATE_INVALID",
                detail="Context update owner document is invalid",
            )
            if header.get("target") != ref or header.get("stage") != "applied":
                continue
            update_path, update, expected, candidate, claim_path, claim, claimed = context_core._load_update_owner(
                root,
                update_dir,
            )
            _read_source(root, update_path, manifest)
            _read_source(root, expected, manifest)
            _read_source(root, candidate, manifest)
            _read_source(root, claim_path, manifest)
            _read_source(root, claimed, manifest)
            records = claim.get("reappeared_entries")
            if records is None and claim.get("reappeared_entry") is not None:
                records = [{"entry": claim.get("reappeared_entry")}]
            for record in records or []:
                if isinstance(record, dict) and isinstance(record.get("entry"), str):
                    _read_source(root, claim_path.parent / record["entry"], manifest)
            _record_capture_chain_sources(root, str(update["capture_ref"]), manifest)


def _safe_text(data: bytes) -> str:
    if core._secret_reasons(data):
        raise KbError(
            "KB2_BOOTSTRAP_SECURITY_REJECTED",
            "secret-like canonical content cannot enter bootstrap outputs",
            3,
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KbError("KB2_BOOTSTRAP_SOURCE_INVALID", "canonical text is not valid UTF-8", 3) from exc


def _title_summary(data: bytes, *, fallback: str) -> tuple[str, str]:
    text = _safe_text(data)
    lines = text.splitlines()
    title = ""
    for line in lines:
        if line.startswith("# ") and line[2:].strip():
            title = line[2:].strip()
            break
    if not title:
        for line in lines:
            match = re.match(r'^title:\s*["\']?(.*?)["\']?\s*$', line)
            if match and match.group(1).strip():
                title = match.group(1).strip().strip('"')
                break
    title = title[:120] or fallback

    summary_lines: list[str] = []
    in_now = False
    for line in lines:
        if re.match(r"^##\s+", line):
            in_now = line.strip() == "## 现在"
            continue
        if line.startswith("# "):
            continue
        if line.strip() and (in_now or not summary_lines):
            value = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line.strip())
            if value:
                summary_lines.append(value)
        if summary_lines and not in_now:
            break
    return title, " ".join(summary_lines)[:240]


def _security_summary(state: dict[str, Any]) -> dict[str, Any]:
    decision = state.get("decision")
    security = decision.get("security") if isinstance(decision, dict) else None
    if not isinstance(security, dict):
        raise KbError("KB2_BOOTSTRAP_SECURITY_REJECTED", "canonical security decision is missing", 3)
    if security.get("precheck") != "passed" or security.get("profile") != "personal-full/v1":
        raise KbError("KB2_BOOTSTRAP_SECURITY_REJECTED", "canonical security decision is not indexable", 3)
    return {
        "precheck": "passed",
        "profile": "personal-full/v1",
        "policy": security.get("policy"),
    }


def _capture_id(ref: Any) -> str:
    if not isinstance(ref, str) or not ref.startswith("capture://"):
        raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "canonical capture reference is invalid", 3)
    capture_id = ref.removeprefix("capture://")
    if not _CAPTURE_RE.fullmatch(capture_id):
        raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "canonical capture identity is invalid", 3)
    return capture_id


def _record_override(
    target: str,
    state: dict[str, Any],
    overrides: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    override_id = state.get("active_override")
    if override_id is None:
        return None
    if not isinstance(override_id, str) or not re.fullmatch(r"OVR-[0-9A-HJKMNP-TV-Z]{26}", override_id):
        raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "active override identity is invalid", 3)
    record = overrides.get(override_id)
    if record is None or record.get("target") != target:
        raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "active override owner is invalid", 3)
    return {
        "active": True,
        "ref": f"override://{override_id}",
        "actor": record.get("actor"),
        "observed_digest": record.get("observed_digest"),
    }


def _scan_overrides(root: Path, manifest: dict[str, str]) -> dict[str, dict[str, Any]]:
    directory = _optional_directory(root, "governance/overrides")
    if directory is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        guarded = core._guard_plain_file(root, path)
        if guarded.suffix != ".yaml":
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "override directory contains an invalid owner entry", 3)
        record = core._load_override_owner(root, guarded)
        _read_source(root, guarded, manifest)
        override_id = str(record["id"])
        if override_id in result:
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "duplicate override owner", 3)
        result[override_id] = record
    return result


def _scan_garden_inventory(root: Path, manifest: dict[str, str]) -> None:
    states = _optional_directory(root, "governance/organizer-state")
    if states is None:
        return
    for state_dir in sorted(states.iterdir()):
        state_dir = core._guard_plain_directory(root, state_dir)
        capture_id = state_dir.name
        if not _CAPTURE_RE.fullmatch(capture_id):
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "organizer state identity is invalid", 3)
        ref = f"garden://notes/{capture_id}.md"
        _, state = core._load_organizer_state(root, capture_id, ref)
        base = _read_source(root, state_dir / "base.md", manifest)
        _read_source(root, state_dir / "state.json", manifest)
        if core._digest(base) != state.get("base_digest"):
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Garden organizer base owner drifted", 3)
        _safe_text(base)
        _security_summary(state)
        capture_dir = core._capture_directory(root, capture_id)
        metadata, payload = core._load_capture_owner(root, capture_dir)
        for name in ("owner.json", "capture.json", "payload.bin"):
            _read_source(root, capture_dir / name, manifest)
        snapshot = capture_dir / "payload-snapshot.bin"
        if snapshot.exists():
            _read_source(root, snapshot, manifest)
        if metadata.get("id") != capture_id or metadata.get("payload_digest") != core._digest(payload):
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Garden capture owner is invalid", 3)
        note_path = root / "garden" / "notes" / f"{capture_id}.md"
        core._guard_path(root, note_path)
        manifest[f"@presence/{_relative(root, note_path)}"] = "present" if note_path.exists() else "missing"


def _garden_records(root: Path, manifest: dict[str, str], overrides: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    notes = _optional_directory(root, "garden/notes")
    if notes is None:
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(notes.iterdir()):
        path = core._guard_plain_file(root, path)
        if not re.fullmatch(r"CAP-[0-9A-HJKMNP-TV-Z]{26}\.md", path.name):
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Garden contains an invalid canonical leaf", 3)
        note = _read_source(root, path, manifest)
        capture_id = path.stem
        capture_dir = core._capture_directory(root, capture_id)
        metadata, payload = core._load_capture_owner(root, capture_dir)
        for name in ("owner.json", "capture.json", "payload.bin"):
            _read_source(root, capture_dir / name, manifest)
        snapshot = capture_dir / "payload-snapshot.bin"
        if snapshot.exists():
            _read_source(root, snapshot, manifest)
        if metadata.get("id") != capture_id:
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Garden capture owner identity is invalid", 3)
        ref = f"garden://notes/{path.name}"
        state_dir, state = core._load_organizer_state(root, capture_id, ref)
        base_path = state_dir / "base.md"
        base = _read_source(root, base_path, manifest)
        _read_source(root, state_dir / "state.json", manifest)
        if core._digest(base) != state.get("base_digest"):
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Garden organizer base owner drifted", 3)
        _safe_text(base)
        security = _security_summary(state)
        title, summary = _title_summary(note, fallback="未命名 Garden")
        note_digest = core._digest(note)
        current = "current" if note_digest == state["base_digest"] else "needs-attention"
        route = core._decision_route(state)
        records.append(
            {
                "uri": ref,
                "type": "garden-note",
                "title": title,
                "summary": summary,
                "canonical_path": _relative(root, path),
                "lifecycle": "active",
                "current": {"status": current, "route": route.get("result"), "digest": note_digest},
                "security": security,
                "freshness": {"source": note_digest, "captured_at": metadata.get("created_at")},
                "override": _record_override(ref, state, overrides),
                "source_hash": note_digest,
            }
        )
    return records


def _context_records(root: Path, manifest: dict[str, str], overrides: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    contexts = _optional_directory(root, "contexts")
    if contexts is None:
        return []
    records: list[dict[str, Any]] = []
    for directory in sorted(contexts.iterdir()):
        if directory.name.startswith("."):
            continue
        directory = core._guard_plain_directory(root, directory)
        if not directory.name.startswith("CTX-"):
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "contexts contains an invalid canonical directory", 3)
        context_id = directory.name.split("-", 1)[0] + "-" + directory.name.split("-", 1)[1].split("-", 1)[0]
        if not _CONTEXT_RE.fullmatch(context_id):
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Context identity is invalid", 3)
        ref = f"context://{context_id}"
        state_dir, state, context_path = context_core._load_state(root, ref)
        lifecycle = context_core._context_lifecycle(ref, state)
        context_core._validate_applied_update_chain(root, ref, state_dir, state, context_path)
        _record_context_chain_sources(root, ref, manifest)
        current = _read_source(root, context_path, manifest)
        _validate_context_frontmatter(current, context_id)
        base = _read_source(root, state_dir / "base.md", manifest)
        _read_source(root, state_dir / "state.json", manifest)
        _safe_text(base)
        security = _security_summary(state)
        origin_id = _capture_id(state.get("origin_capture_ref"))
        latest_id = _capture_id(state.get("latest_capture_ref"))
        for capture_id in {origin_id, latest_id}:
            capture_dir = core._capture_directory(root, capture_id)
            metadata, payload = core._load_capture_owner(root, capture_dir)
            for name in ("owner.json", "capture.json", "payload.bin"):
                _read_source(root, capture_dir / name, manifest)
            snapshot = capture_dir / "payload-snapshot.bin"
            if snapshot.exists():
                _read_source(root, snapshot, manifest)
            if metadata.get("id") != capture_id or metadata.get("payload_digest") != core._digest(payload):
                raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Context capture owner is invalid", 3)
        title, summary = _title_summary(current, fallback="未命名 Context")
        current_digest = core._digest(current)
        lifecycle_status = lifecycle["status"]
        current_status = (
            lifecycle_status
            if lifecycle_status != "active"
            else ("current" if current_digest == state["base_digest"] else "needs-attention")
        )
        route = state["decision"].get("route")
        if not isinstance(route, dict):
            raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Context route decision is missing", 3)
        records.append(
            {
                "uri": ref,
                "type": "context",
                "title": title,
                "summary": summary,
                "canonical_path": _relative(root, context_path),
                "lifecycle": lifecycle_status,
                "current": {"status": current_status, "route": route.get("result"), "digest": current_digest},
                "security": security,
                "freshness": {"source": current_digest, "captured_at": state.get("created_at")},
                "override": _record_override(ref, state, overrides),
                "source_hash": current_digest,
            }
        )
    return records


def _released_records(root: Path, manifest: dict[str, str]) -> list[dict[str, Any]]:
    from . import release as release_core

    try:
        verified_records = release_core._read_committed_records(root)
    except release_core.ReleaseError as exc:
        if exc.code == "RELEASE_NOT_FOUND":
            return []
        raise KbError(
            "KB2_BOOTSTRAP_RELEASE_INVALID",
            "committed release failed strict validation",
            3,
        ) from exc

    records: list[dict[str, Any]] = []
    for verified in verified_records:
        bundle = root / str(verified["bundle_path"])
        for name in ("artifact.bin", "revision.json", "receipt.json"):
            _read_source(root, bundle / name, manifest)
        content_hash = f"sha256:{verified['content_sha256']}"
        canonical_path = _relative(root, bundle / "artifact.bin")
        records.append(
            {
                "uri": f"artifact://{verified['artifact_id']}",
                "type": "artifact",
                "title": verified["title"],
                "summary": f"revision={verified['revision_id']}; receipt={verified['receipt_id']}",
                "canonical_path": canonical_path,
                "lifecycle": "released",
                "current": {"status": "current", "revision": verified["revision_id"]},
                "security": "public",
                "freshness": {"status": "current", "source": content_hash},
                "source_hash": content_hash,
                "artifact_id": verified["artifact_id"],
                "revision_id": verified["revision_id"],
                "receipt_id": verified["receipt_id"],
                "content_sha256": content_hash,
            }
        )
    return records


def _collect(root: Path) -> tuple[list[dict[str, Any]], dict[str, str], str]:
    manifest: dict[str, str] = {}
    anchor = root / "kb.yaml"
    anchor_bytes = _read_source(root, anchor, manifest)
    protocol_value = "PROTOCOL.md"
    match = re.search(r"(?m)^protocol\s*:\s*([^#\r\n]+?)\s*$", anchor_bytes.decode("utf-8"))
    if match:
        protocol_value = match.group(1).strip().strip("'\"")
    protocol_path = Path(protocol_value)
    if protocol_path.is_absolute() or ".." in protocol_path.parts or len(protocol_path.parts) != 1:
        raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "protocol path is outside the root contract", 3)
    _read_source(root, root / protocol_path, manifest)
    overrides = _scan_overrides(root, manifest)
    _scan_garden_inventory(root, manifest)
    records = (
        _garden_records(root, manifest, overrides)
        + _context_records(root, manifest, overrides)
        + _released_records(root, manifest)
    )
    records.sort(key=lambda item: (item["type"], item["uri"]))
    return records, manifest, _relative(root, root / protocol_path)


def _source_digest(manifest: dict[str, str]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return core._digest(payload)


def _identity(build_id: str, source_digest: str, generated_at: str) -> dict[str, Any]:
    return {
        "build_id": build_id,
        "source_digest": source_digest,
        "config_digest": _CONFIG_DIGEST,
        "generated": True,
        "do-not-edit": True,
        "generated_at": generated_at,
    }


def _handoff_binding(
    records: list[dict[str, Any]],
    manifest: dict[str, str],
    protocol_path: str,
    generated_at: str,
) -> dict[str, Any]:
    contexts = [
        item
        for item in records
        if item["type"] == "context"
        and item["lifecycle"] == "active"
        and item["current"]["status"] == "current"
    ]
    binding: dict[str, Any] = {
        "handoff_schema": "kb2-handoff/v0.1-stage1",
        "handoff_protocol_path": protocol_path,
        "handoff_protocol_sha256": manifest[protocol_path],
        "handoff_context_count_at_build": len(contexts),
        "handoff_selection": "explicit-single-active-context" if len(contexts) == 1 else "unavailable-no-binding",
        "handoff_inputs_verified": len(contexts) == 1,
        "handoff_verified_scope": "protocol+selected-context+owner-chain+source+config",
        "handoff_verified_at": generated_at,
        "handoff_binding_freshness": "valid-if-bound-files-match" if len(contexts) == 1 else "unavailable-no-binding",
    }
    if len(contexts) == 1:
        selected = contexts[0]
        binding.update(
            {
                "handoff_context_uri": selected["uri"],
                "handoff_context_path": selected["canonical_path"],
                "handoff_context_sha256": selected["current"]["digest"],
            }
        )
    return binding


def _render_home(
    records: list[dict[str, Any]],
    identity: dict[str, Any],
    *,
    protocol_path: str,
    handoff: dict[str, Any],
) -> str:
    def frontmatter_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    lines = [
        "---",
        "schema: kb2-home/v0.1-pilot",
        f"build_id: {identity['build_id']}",
        f"source_digest: {identity['source_digest']}",
        f"config_digest: {identity['config_digest']}",
        f"generated_at: {identity['generated_at']}",
        "generated: true",
        "do-not-edit: true",
    ]
    lines.extend(f"{key}: {frontmatter_value(handoff[key])}" for key in (
        "handoff_schema",
        "handoff_protocol_path",
        "handoff_protocol_sha256",
        "handoff_context_uri",
        "handoff_context_path",
        "handoff_context_sha256",
        "handoff_context_count_at_build",
        "handoff_selection",
        "handoff_inputs_verified",
        "handoff_verified_scope",
        "handoff_verified_at",
        "handoff_binding_freshness",
    ) if key in handoff)
    lines.extend([
        "---",
        "",
        "# Knowledge Base Home",
        "",
        f"- Protocol: [{protocol_path}](../../../../{protocol_path})",
        "",
        "## Active Context",
        "",
    ])
    contexts = [
        item
        for item in records
        if item["type"] == "context"
        and item["lifecycle"] == "active"
        and item["current"]["status"] == "current"
    ]
    if contexts:
        lines.extend(f"- {item['uri']} — {item['title']} ({item['canonical_path']})" for item in contexts)
    else:
        lines.append("- 无 active Context。")
    lines.extend(["", "## Recently Completed", ""])
    completed = [item for item in records if item["type"] == "context" and item["lifecycle"] == "completed"]
    if completed:
        lines.extend(f"- {item['uri']} — {item['title']} ({item['canonical_path']})" for item in completed)
    else:
        lines.append("- 无最近完成的 Context。")
    lines.extend(["", "## Blocked / Needs Attention", ""])
    blocked = [
        item
        for item in records
        if item["type"] == "context"
        and (item["lifecycle"] == "blocked" or item["current"]["status"] == "needs-attention")
    ]
    if blocked:
        lines.extend(f"- {item['uri']} — {item['title']} ({item['canonical_path']})" for item in blocked)
    else:
        lines.append("- 无 blocked Context 或需关注的 Context。")
    lines.extend(["", "## Garden / Needs Attention", ""])
    attention = [item for item in records if item["current"]["status"] == "needs-attention"]
    if attention:
        lines.extend(f"- {item['uri']} — {item['title']} ({item['canonical_path']})" for item in attention)
    else:
        lines.append("- 无需关注项。")
    lines.extend(["", "## Released Artifacts", ""])
    released = [item for item in records if item["type"] == "artifact"]
    if released:
        lines.extend(f"- {item['uri']} — {item['title']} ({item['canonical_path']})" for item in released)
    else:
        lines.append("- 无已发布 Artifact。")
    return "\n".join(lines) + "\n"


def _frontmatter(data: bytes) -> dict[str, str]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "HOME frontmatter is not valid UTF-8", 3) from exc
    if not lines or lines[0] != "---":
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "HOME frontmatter is missing", 3)
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "HOME frontmatter is unterminated", 3) from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if ": " not in line:
            raise KbError("KB2_HANDOFF_BINDING_INVALID", "HOME frontmatter contains an invalid field", 3)
        key, value = line.split(": ", 1)
        if not key or key in values:
            raise KbError("KB2_HANDOFF_BINDING_INVALID", "HOME frontmatter contains duplicate fields", 3)
        values[key] = value
    return values


def _validate_context_frontmatter(data: bytes, context_id: str) -> None:
    try:
        fields = _frontmatter(data)
    except KbError as exc:
        raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Context frontmatter is invalid", 3) from exc
    if fields.get("schema") != "context-current/v0.1-pilot" or fields.get("id") != context_id:
        raise KbError("KB2_BOOTSTRAP_OWNER_INVALID", "Context frontmatter identity is invalid", 3)


def _binding_path(root: Path, value: str, *, context: bool = False) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "handoff binding path is not normalized and relative", 3)
    parts = value.split("/")
    if context and (len(parts) != 3 or parts[0] != "contexts" or parts[2] != "CONTEXT.md"):
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "handoff Context path is invalid", 3)
    return core._guard_plain_file(root, root.joinpath(*parts))


def _verify_handoff_binding(root: Path, home_path: Path) -> dict[str, bool]:
    root = core._guard_root(root)
    home = core._guard_plain_file(root, home_path)
    home_parts = home.relative_to(root).as_posix().split("/")
    if (
        len(home_parts) != 5
        or home_parts[:3] != ["generated", "bootstrap", "generations"]
        or not _BUILD_RE.fullmatch(home_parts[3])
        or home_parts[4] != "HOME.md"
    ):
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "HOME path is not a canonical generated path", 3)
    fields = _frontmatter(home.read_bytes())
    if (
        fields.get("schema") != "kb2-home/v0.1-pilot"
        or fields.get("generated") != "true"
        or fields.get("do-not-edit") != "true"
        or fields.get("build_id") != home_parts[3]
        or not core._DIGEST_RE.fullmatch(fields.get("source_digest", ""))
        or not core._DIGEST_RE.fullmatch(fields.get("config_digest", ""))
        or not _RFC3339_RE.fullmatch(fields.get("generated_at", ""))
    ):
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "HOME generated identity is invalid", 3)
    required = {
        "handoff_schema": "kb2-handoff/v0.1-stage1",
        "handoff_selection": "explicit-single-active-context",
        "handoff_inputs_verified": "true",
        "handoff_verified_scope": "protocol+selected-context+owner-chain+source+config",
        "handoff_binding_freshness": "valid-if-bound-files-match",
        "handoff_context_count_at_build": "1",
    }
    if any(fields.get(key) != value for key, value in required.items()):
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "HOME handoff selection is not a verified single Context", 3)
    if (
        not _RFC3339_RE.fullmatch(fields.get("handoff_verified_at", ""))
        or fields.get("handoff_verified_at") != fields.get("generated_at")
    ):
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "handoff verification time is not the build time", 3)
    protocol_path = _binding_path(root, fields.get("handoff_protocol_path", ""))
    context_path = _binding_path(root, fields.get("handoff_context_path", ""), context=True)
    protocol_id = fields.get("handoff_protocol_sha256", "")
    context_digest = fields.get("handoff_context_sha256", "")
    if not core._DIGEST_RE.fullmatch(protocol_id) or not core._DIGEST_RE.fullmatch(context_digest):
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "handoff binding digest is invalid", 3)
    context_parts = context_path.relative_to(root).as_posix().split("/")
    context_match = re.match(r"^(CTX-[0-9A-HJKMNP-TV-Z]{26})-", context_parts[1])
    context_id = context_match.group(1) if context_match else ""
    context_uri = fields.get("handoff_context_uri", "")
    if not _CONTEXT_RE.fullmatch(context_id) or context_uri != f"context://{context_id}":
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "handoff Context URI does not match its path", 3)
    if protocol_id != core._digest(protocol_path.read_bytes()) or context_digest != core._digest(context_path.read_bytes()):
        raise KbError("KB2_HANDOFF_BINDING_INVALID", "handoff binding bytes do not match", 3)
    return {"valid": True}


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _search_rows(root: Path, records: list[dict[str, Any]], identity: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the disposable full-text projection from verified public releases only."""
    rows: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") != "artifact" or record.get("security") != "public":
            continue
        path = core._guard_plain_file(root, root / str(record["canonical_path"]))
        content_bytes = path.read_bytes()
        if record.get("content_sha256") != core._digest(content_bytes):
            raise KbError("KB2_BOOTSTRAP_SOURCE_DRIFT", "released Artifact changed during search build", 3)
        content = _safe_text(content_bytes)
        rows.append(
            {
                "schema": _SEARCH_SCHEMA,
                **identity,
                "uri": record["uri"],
                "artifact_id": record["artifact_id"],
                "title": record["title"],
                "canonical_path": record["canonical_path"],
                "content_sha256": record["content_sha256"],
                "content": content,
            }
        )
    return rows


def _validate_search_identity(row: dict[str, Any], build: dict[str, Any]) -> None:
    if (
        row.get("schema") != _SEARCH_SCHEMA
        or row.get("build_id") != build.get("build_id")
        or row.get("source_digest") != build.get("source_digest")
        or row.get("config_digest") != build.get("config_digest")
        or row.get("generated") is not True
        or row.get("do-not-edit") is not True
        or row.get("generated_at") != build.get("generated_at")
        or not isinstance(row.get("uri"), str)
        or not isinstance(row.get("artifact_id"), str)
        or not isinstance(row.get("title"), str)
        or not isinstance(row.get("canonical_path"), str)
        or not isinstance(row.get("content_sha256"), str)
        or not isinstance(row.get("content"), str)
        or not core._DIGEST_RE.fullmatch(str(row.get("content_sha256")))
        or core._digest(str(row.get("content")).encode("utf-8")) != row.get("content_sha256")
    ):
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Search identity is invalid", 3)


def _search_match(query: str, row: dict[str, Any]) -> tuple[str, int] | None:
    folded = query.casefold()
    artifact_id = str(row["artifact_id"])
    uri = str(row["uri"])
    title = str(row["title"])
    content = str(row["content"])
    if folded == artifact_id.casefold():
        return "artifact_id", 1000
    if folded == uri.casefold():
        return "uri", 1000
    if folded in artifact_id.casefold():
        return "artifact_id", 950
    if folded in uri.casefold():
        return "uri", 900
    if folded == title.casefold():
        return "title", 850
    if folded in title.casefold():
        return "title", 800
    folded_content = content.casefold()
    if folded in folded_content:
        return "body", 300
    words = re.findall(r"[0-9A-Za-z_]+", folded)
    if words and all(word in folded_content for word in words):
        return "body", 250
    return None


def _registry_match(query: str, row: dict[str, Any]) -> tuple[str, int] | None:
    folded = query.casefold()
    for field, exact_score, contains_score in (
        ("uri", 1000, 900),
        ("title", 800, 700),
        ("canonical_path", 500, 400),
        ("type", 350, 300),
        ("summary", 250, 200),
    ):
        value = str(row.get(field, "")).casefold()
        if folded == value:
            return field, exact_score
        if folded in value:
            return field, contains_score
    return None


def _pointer(root: Path, name: str) -> dict[str, Any] | None:
    path = root / "generated" / "bootstrap" / name
    if core._is_reparse(path):
        raise KbError("KB2_REPARSE_REJECTED", "bootstrap pointer cannot be a reparse point", 3)
    core._guard_path(root, path)
    if not os.path.lexists(path):
        return None
    try:
        value = core._load_json(core._guard_plain_file(root, path))
    except KbError as exc:
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "bootstrap pointer is invalid", 3) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "bootstrap pointer is unreadable", 3) from exc
    if not isinstance(value, dict):
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "bootstrap pointer must be a JSON object", 3)
    return value


def _generation_from_pointer(root: Path, pointer: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    generation = pointer.get("generation")
    if (
        not isinstance(generation, str)
        or len(Path(generation).parts) != 2
        or Path(generation).parts[0] != "generations"
        or not _BUILD_RE.fullmatch(Path(generation).parts[1])
    ):
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "generation pointer is invalid", 3)
    generation_path = core._guard_plain_directory(root, root / "generated" / "bootstrap" / generation)
    build = core._load_json(core._guard_plain_file(root, generation_path / "build.json"))
    if (
        build.get("schema") != "kb2-bootstrap-build/v0.1-pilot"
        or build.get("generation") != generation
        or not _BUILD_RE.fullmatch(str(build.get("build_id")))
        or not core._DIGEST_RE.fullmatch(str(build.get("source_digest")))
        or not core._DIGEST_RE.fullmatch(str(build.get("config_digest")))
        or not isinstance(build.get("generated_at"), str)
        or build.get("build_id") != Path(generation).name
        or build.get("generated") is not True
        or build.get("do-not-edit") is not True
        or pointer.get("schema") != "kb2-bootstrap-pointer/v0.1-pilot"
        or any(pointer.get(field) != build.get(field) for field in (
            "build_id",
            "source_digest",
            "config_digest",
            "generated",
            "do-not-edit",
            "generated_at",
            "generation",
        ))
    ):
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "generation identity is invalid", 3)
    return generation_path, build


def _load_current_generation(root: Path) -> tuple[Path, dict[str, Any]]:
    current = _pointer(root, "CURRENT.json")
    if current is not None:
        pointer = current
    else:
        pointer = _pointer(root, "last-good.json")
    if pointer is None:
        raise KbError("KB2_BOOTSTRAP_NOT_BUILT", "no bootstrap generation is available", 2)
    return _generation_from_pointer(root, pointer)


def _pointer_bytes(root: Path, path: Path) -> bytes | None:
    core._guard_plain_file(root, path, required=False)
    return path.read_bytes() if path.exists() else None


def _restore_pointer(path: Path, data: bytes | None) -> None:
    if data is None:
        if path.exists():
            path.unlink()
        return
    fd, temporary_name = tempfile.mkstemp(prefix=".kb2-pointer-restore-", dir=path.parent)
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


def build(root: Path, *, before_commit: Callable[[Path], None] | None = None) -> dict[str, Any]:
    root = core._guard_root(root)
    records, manifest, protocol_path = _collect(root)
    source_digest = _source_digest(manifest)
    if before_commit is not None:
        before_commit(root)
    _, checked_manifest, _ = _collect(root)
    if _source_digest(checked_manifest) != source_digest:
        raise KbError("KB2_BOOTSTRAP_SOURCE_DRIFT", "canonical source changed during bootstrap build", 3)
    build_id = core._new_id("BLD")
    generated_at = core._now()
    identity = _identity(build_id, source_digest, generated_at)
    handoff = _handoff_binding(records, manifest, protocol_path, generated_at)
    public_rows = [{**identity, **record} for record in records]
    build_record = {
        "schema": "kb2-bootstrap-build/v0.1-pilot",
        **identity,
        "generation": f"generations/{build_id}",
        "entry_count": len(public_rows),
    }
    bootstrap_root = core._ensure_plain_directory(root, "generated/bootstrap")
    generations = core._ensure_plain_directory(root, "generated/bootstrap/generations")
    staging = bootstrap_root / f".staging-{build_id}"
    final = generations / build_id
    pointer_paths = {
        name: bootstrap_root / name
        for name in ("CURRENT.json", "last-good.json")
    }
    pointer_before = {
        name: _pointer_bytes(root, path)
        for name, path in pointer_paths.items()
    }
    core._guard_path(root, staging)
    core._guard_path(root, final)
    staging.mkdir()
    try:
        registry = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in public_rows)
        search_rows = _search_rows(root, records, identity)
        search = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in search_rows)
        core._write_file_synced(staging / "registry.jsonl", registry.encode("utf-8"))
        core._write_file_synced(staging / "search.jsonl", search.encode("utf-8"))
        core._write_file_synced(
            staging / "HOME.md",
            _render_home(records, identity, protocol_path=protocol_path, handoff=handoff).encode("utf-8"),
        )
        core._write_file_synced(staging / "build.json", _json_bytes(build_record))
        os.rename(staging, final)
        pointer = {
            "schema": "kb2-bootstrap-pointer/v0.1-pilot",
            **identity,
            "generation": f"generations/{build_id}",
        }
        for name in ("CURRENT.json", "last-good.json"):
            pointer_path = pointer_paths[name]
            core._guard_plain_file(root, pointer_path, required=False)
            core._replace_file_after_sync(pointer_path, _json_bytes(pointer))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if final.exists():
            core._guard_plain_directory(root, final)
            shutil.rmtree(final)
        for name, path in pointer_paths.items():
            _restore_pointer(path, pointer_before[name])
        raise
    return {
        "schema": "kb2-bootstrap-result/v0.1-pilot",
        **identity,
        "generation": f"generations/{build_id}",
        "entry_count": len(public_rows),
        "entries": [{"uri": item["uri"], "canonical_path": item["canonical_path"]} for item in public_rows],
        "changed": [
            _relative(root, final / "registry.jsonl"),
            _relative(root, final / "search.jsonl"),
            _relative(root, final / "HOME.md"),
            _relative(root, final / "build.json"),
            _relative(root, bootstrap_root / "CURRENT.json"),
            _relative(root, bootstrap_root / "last-good.json"),
        ],
    }


def find(root: Path, query: str) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise KbError("KB2_FIND_QUERY_INVALID", "find query must be non-empty text", 2)
    query = query.strip()
    root = core._guard_root(root)
    generation_path, build = _load_current_generation(root)
    live_records, live_manifest, _ = _collect(root)
    if _source_digest(live_manifest) != build.get("source_digest") or _CONFIG_DIGEST != build.get("config_digest"):
        raise KbError("KB2_BOOTSTRAP_PROJECTION_STALE", "bootstrap projection is stale; rebuild is required", 2)
    identity = {
        field: build[field]
        for field in ("build_id", "source_digest", "config_digest", "generated", "do-not-edit", "generated_at")
    }
    expected_search = {
        str(row["uri"]): row
        for row in _search_rows(root, live_records, identity)
    }
    expected_registry = {
        str(record["uri"]): {**identity, **record}
        for record in live_records
    }
    registry_path = core._guard_plain_file(root, generation_path / "registry.jsonl")
    search_path = core._guard_plain_file(root, generation_path / "search.jsonl")
    if core._is_reparse(search_path):
        raise KbError("KB2_REPARSE_REJECTED", "search projection cannot be a reparse point", 3)
    try:
        search_lines = search_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Search projection is unreadable", 3) from exc
    search_rows: list[dict[str, Any]] = []
    seen_search: set[str] = set()
    for line in search_lines:
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Search contains invalid JSON", 3) from exc
        if not isinstance(row, dict):
            raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Search row must be an object", 3)
        _validate_search_identity(row, build)
        uri = str(row["uri"])
        if uri in seen_search or expected_search.get(uri) != row:
            raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Search row is not bound to a public release", 3)
        seen_search.add(uri)
        search_rows.append(row)
    if seen_search != set(expected_search):
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Search projection is incomplete", 3)

    matches_by_uri: dict[str, dict[str, Any]] = {}
    for row in search_rows:
        hit = _search_match(query, row)
        if hit is not None:
            field, score = hit
            matches_by_uri[row["uri"]] = {
                "uri": row["uri"],
                "title": row["title"],
                "matched_field": field,
                "score": score,
                "build_id": build["build_id"],
                "canonical_path": row["canonical_path"],
            }

    # Preserve the original Registry metadata search contract for non-searchable rows.
    try:
        registry_lines = registry_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Registry is unreadable", 3) from exc
    seen_registry: set[str] = set()
    registry_rows: list[dict[str, Any]] = []
    for line in registry_lines:
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Registry contains invalid JSON", 3) from exc
        if not isinstance(row, dict) or not isinstance(row.get("uri"), str):
            raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Registry identity is invalid", 3)
        uri = str(row["uri"])
        if uri in seen_registry or expected_registry.get(uri) != row:
            raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Registry row is not bound to canonical source", 3)
        seen_registry.add(uri)
        registry_rows.append(row)
    if seen_registry != set(expected_registry):
        raise KbError("KB2_BOOTSTRAP_GENERATION_INVALID", "Registry projection is incomplete", 3)
    for row in registry_rows:
        metadata_hit = _registry_match(query, row)
        if metadata_hit is not None:
            uri = str(row["uri"])
            if uri not in matches_by_uri:
                matched_field, score = metadata_hit
                matches_by_uri[uri] = {
                    "uri": uri,
                    "title": str(row.get("title", "")),
                    "matched_field": matched_field,
                    "score": score,
                    "build_id": build["build_id"],
                    "canonical_path": str(row["canonical_path"]),
                }
    matches = sorted(matches_by_uri.values(), key=lambda item: (-int(item["score"]), str(item["uri"])))
    return {
        "schema": "kb2-bootstrap-find/v0.1-pilot",
        "build_id": build["build_id"],
        "source_digest": build["source_digest"],
        "config_digest": build["config_digest"],
        "matches": matches,
    }


def status(root: Path) -> dict[str, Any]:
    root = core._guard_root(root)
    generated = root / "generated"
    core._guard_path(root, generated)
    if not generated.exists() or _optional_directory(root, "generated/bootstrap") is None:
        return {"implemented": False, "fresh": False, "entry_count": 0}
    current = _pointer(root, "CURRENT.json")
    if current is not None:
        pointer = current
    else:
        pointer = _pointer(root, "last-good.json")
    if pointer is None:
        return {"implemented": False, "fresh": False, "entry_count": 0}
    _, build = _generation_from_pointer(root, pointer)
    _, live_manifest, _ = _collect(root)
    live_source_digest = _source_digest(live_manifest)
    return {
        "implemented": True,
        "fresh": live_source_digest == build["source_digest"] and _CONFIG_DIGEST == build["config_digest"],
        "build_id": build["build_id"],
        "source_digest": build["source_digest"],
        "config_digest": build["config_digest"],
        "entry_count": build.get("entry_count", 0),
    }
