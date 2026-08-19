"""Zero-form orchestration for one public UTF-8 text publication."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Any

from . import bootstrap, core, release
from .result import KbError


def default_idempotency_key(
    candidate_id: str,
    candidate_path: str,
    content_sha256: str,
    media_type: str,
    title: str,
) -> str:
    material = "|".join((candidate_id, candidate_path, content_sha256, media_type, title))
    return "idem-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _title(payload: bytes) -> str:
    text = payload.decode("utf-8")
    first = next((line.strip().lstrip("#").strip() for line in text.splitlines() if line.strip()), "公开文本")
    return first[:160]


def _candidate(root: Path, ingested: dict[str, Any], payload: bytes) -> release.Candidate:
    if ingested.get("route") != "garden-organized":
        code = "KB2_POLICY_REJECTED" if ingested.get("route") == "restricted-hold" else "KB2_WORKFLOW_NOT_PUBLISHABLE"
        raise KbError(
            code,
            "captured input is not a publishable public Garden item",
            2,
            {
                "capture_ref": ingested.get("capture_ref"),
                "route": ingested.get("route"),
                "release_committed": False,
            },
            list(ingested.get("changed", [])),
        )

    root = core._guard_root(root)
    capture_ref = str(ingested["capture_ref"])
    garden_ref = str(ingested["garden_ref"])
    capture_id = capture_ref.removeprefix("capture://")
    expected_garden_ref = f"garden://notes/{capture_id}.md"
    if garden_ref != expected_garden_ref:
        raise KbError("KB2_WORKFLOW_STATE_INVALID", "ingest result has invalid Garden provenance", 3)

    garden_path = core._guard_plain_file(root, root / "garden" / "notes" / f"{capture_id}.md")
    content = garden_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    candidate_id = f"CAND-{capture_id}"
    candidate_parent = core._ensure_plain_directory(root, "governance/release-candidates")
    candidate_dir = candidate_parent / candidate_id
    core._guard_path(root, candidate_dir)
    if candidate_dir.exists():
        raise KbError("KB2_CANDIDATE_EXISTS", "automatic candidate already exists", 3)

    staging = candidate_parent / f".staging-{candidate_id}-{uuid.uuid4().hex}"
    core._guard_path(root, staging)
    staging.mkdir()
    candidate_path = candidate_dir / "candidate.md"
    owner_path = candidate_dir / "owner.json"
    title = _title(payload)
    relative_candidate = candidate_path.relative_to(root).as_posix()
    owner = {
        "schema": "kb2-candidate-owner/v0.2",
        "owner": "candidate-owner/v0.2",
        "candidate_id": candidate_id,
        "content_path": relative_candidate,
        "content_sha256": digest,
        "media_type": "text/markdown",
        "title": title,
        "security": "public",
        "source_capture_ref": capture_ref,
        "source_garden_ref": garden_ref,
    }
    try:
        core._write_file_synced(staging / "candidate.md", content)
        core._write_file_synced(
            staging / "owner.json",
            (json.dumps(owner, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        os.replace(staging, candidate_dir)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return release.Candidate(
        path=candidate_path,
        owner_path=owner_path,
        candidate_id=candidate_id,
        media_type="text/markdown",
        title=title,
        content_sha256=digest,
        idempotency_key=default_idempotency_key(
            candidate_id,
            relative_candidate,
            digest,
            "text/markdown",
            title,
        ),
        security="public",
        source_capture_ref=capture_ref,
        source_garden_ref=garden_ref,
    )


def publish_text(root: Path, payload: bytes) -> dict[str, Any]:
    """Capture, organize, publish, build, and locate one public text input."""

    root = core._guard_root(root)
    ingested = core.ingest_bytes(root, payload)
    candidate = _candidate(root, ingested, payload)
    published = release.release_candidate(root, candidate)
    changed = [
        *ingested.get("changed", []),
        candidate.path.relative_to(root).as_posix(),
        candidate.owner_path.relative_to(root).as_posix(),
        published.bundle_path,
    ]
    candidate_data = {
        "ref": f"candidate://{candidate.candidate_id}",
        "candidate_id": candidate.candidate_id,
        "path": candidate.path.relative_to(root).as_posix(),
        "owner_path": candidate.owner_path.relative_to(root).as_posix(),
        "content_sha256": candidate.content_sha256,
        "source_capture_ref": ingested["capture_ref"],
        "source_garden_ref": ingested["garden_ref"],
        "user_structured_fields": 0,
    }

    try:
        built = bootstrap.build(root)
        projection_status = bootstrap.status(root)
        found = bootstrap.find(root, published.artifact_id)
        artifact_uri = f"artifact://{published.artifact_id}"
        if not projection_status["fresh"] or not any(
            item.get("uri") == artifact_uri for item in found.get("matches", [])
        ):
            raise KbError(
                "KB2_PROJECTION_INCOMPLETE",
                "new projection is not fresh or cannot locate the release",
                4,
            )
        projection = {
            "attempted": True,
            "ok": True,
            "fresh": True,
            "build_id": built["build_id"],
            "error": None,
        }
        changed.extend(built.get("changed", []))
        result_code = "KB2_OK"
    except Exception as exc:
        projection = {
            "attempted": True,
            "ok": False,
            "fresh": False,
            "build_id": None,
            "error": exc.code if isinstance(exc, KbError) else "KB2_PROJECTION_FAILED",
        }
        found = {"matches": []}
        result_code = "KB2_PUBLISHED_INDEX_STALE"

    return {
        "_result_code": result_code,
        "route": ingested["route"],
        "capture_ref": ingested["capture_ref"],
        "garden_ref": ingested["garden_ref"],
        "user_structured_fields": 0,
        "candidate": candidate_data,
        "release": published.to_dict(),
        "projection": projection,
        "find": found,
        "changed": changed,
    }
