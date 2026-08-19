"""Command-line façade used by the AI organizer and diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from . import __version__
from . import bootstrap
from . import context as context_core
from . import workflow
from .core import correct_bytes, explain, ingest_bytes, organize, recover_all, status
from . import release
from .result import KbError, envelope


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kb")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest")
    ingest.add_argument("--_fail-after-capture", action="store_true", help=argparse.SUPPRESS)
    ingest.add_argument("--context", help="machine Context reference for a natural-language stdin update")
    ingest.add_argument("--base-digest", help="machine CAS base digest for --context")

    commands.add_parser("status")
    commands.add_parser("build")
    commands.add_parser("publish-text")

    publish = commands.add_parser("publish")
    publish.add_argument("--candidate", "--output", dest="candidate", type=Path, required=True)
    publish.add_argument("--owner", "--candidate-owner", dest="owner", type=Path)
    publish.add_argument("--candidate-id")
    publish.add_argument("--idempotency-key")
    publish.add_argument("--media-type")
    publish.add_argument("--title")
    publish.add_argument("--security")

    show_parser = commands.add_parser("show")
    show_parser.add_argument("ref")

    trace_parser = commands.add_parser("trace")
    trace_parser.add_argument("ref")

    find_parser = commands.add_parser("find")
    find_parser.add_argument("query")

    explain_parser = commands.add_parser("explain")
    explain_parser.add_argument("ref")

    organize_parser = commands.add_parser("organize")
    organize_parser.add_argument("ref")

    correct_parser = commands.add_parser("correct")
    correct_parser.add_argument("ref")
    close_parser = commands.add_parser("close-context")
    close_parser.add_argument("ref")
    close_parser.add_argument("--status", choices=("completed", "blocked"), default="completed")
    commands.add_parser("recover")
    return parser


def _emit(value: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print(value["message"])
        if value.get("data"):
            print(json.dumps(value["data"], ensure_ascii=False, indent=2, sort_keys=True))


_RELEASE_ERROR_MAP = {
    "RELEASE_SECURITY_REFUSED": ("KB2_POLICY_REJECTED", 2),
    "RELEASE_TEXT_ONLY": ("KB2_VALIDATION_FAILED", 2),
    "RELEASE_CANDIDATE_HASH_MISMATCH": ("KB2_EXPECTED_HASH_MISMATCH", 3),
    "RELEASE_IDEMPOTENCY_CONFLICT": ("KB2_RELEASE_CONFLICT", 3),
    "RELEASE_IMMUTABLE_CONFLICT": ("KB2_RELEASE_CONFLICT", 3),
    "RELEASE_LOCK_HELD": ("KB2_LOCK_CONFLICT", 3),
    "RELEASE_LOCK_INVALID": ("KB2_LOCK_CONFLICT", 3),
    "RELEASE_TAMPERED": ("KB2_RELEASE_INTEGRITY_FAILED", 3),
    "RELEASE_PARTIAL_BUNDLE": ("KB2_RELEASE_INTEGRITY_FAILED", 3),
    "RELEASE_BUNDLE_INVALID": ("KB2_RELEASE_INTEGRITY_FAILED", 3),
    "RELEASE_FOREIGN_OWNER": ("KB2_RELEASE_INTEGRITY_FAILED", 3),
    "RELEASE_POINTER_INVALID": ("KB2_RELEASE_INTEGRITY_FAILED", 3),
    "RELEASE_NOT_FOUND": ("KB2_RELEASE_NOT_FOUND", 2),
}


def _release_error(exc: release.ReleaseError) -> tuple[str, int, str]:
    code, exit_code = _RELEASE_ERROR_MAP.get(exc.code, ("KB2_VALIDATION_FAILED", 2))
    messages = {
        "KB2_POLICY_REJECTED": "release candidate rejected by policy",
        "KB2_EXPECTED_HASH_MISMATCH": "release candidate hash does not match its owner",
        "KB2_RELEASE_CONFLICT": "release request conflicts with an existing publication",
        "KB2_LOCK_CONFLICT": "release writer lock is unavailable",
        "KB2_RELEASE_INTEGRITY_FAILED": "released record failed integrity validation",
        "KB2_RELEASE_NOT_FOUND": "released record was not found",
        "KB2_VALIDATION_FAILED": "release request failed validation",
    }
    return code, exit_code, messages.get(code, "release request failed")


def _read_candidate(root: Path, args: argparse.Namespace) -> release.Candidate:
    root = release._validate_root(root)
    candidate_input = args.candidate if args.candidate.is_absolute() else root / args.candidate
    candidate_path = release._confined_plain_path(root, candidate_input, "candidate", file=True)
    owner_arg = args.owner or candidate_path.parent / "owner.json"
    if not owner_arg.is_absolute():
        owner_arg = root / owner_arg
    owner_path = release._confined_plain_path(root, owner_arg, "candidate owner", file=True)
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        content = candidate_path.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise release.ReleaseError("candidate owner is invalid", "RELEASE_CANDIDATE_INVALID") from exc
    if not isinstance(owner, dict):
        raise release.ReleaseError("candidate owner is invalid", "RELEASE_CANDIDATE_INVALID")

    def field(name: str, supplied: str | None) -> str:
        value = supplied if supplied is not None else owner.get(name)
        if not isinstance(value, str):
            raise release.ReleaseError("candidate owner is invalid", "RELEASE_CANDIDATE_INVALID")
        return value

    candidate_id = field("candidate_id", args.candidate_id)
    media_type = field("media_type", args.media_type)
    title = field("title", args.title)
    security = field("security", args.security) if args.security is not None else owner.get("security", "public")
    if not isinstance(security, str):
        raise release.ReleaseError("candidate owner is invalid", "RELEASE_CANDIDATE_INVALID")
    digest = owner.get("content_sha256")
    if not isinstance(digest, str):
        raise release.ReleaseError("candidate owner is invalid", "RELEASE_CANDIDATE_INVALID")
    idempotency_key = args.idempotency_key
    if idempotency_key is None:
        idempotency_key = workflow.default_idempotency_key(
            candidate_id,
            candidate_path.relative_to(root).as_posix(),
            digest,
            media_type,
            title,
        )
    return release.Candidate(
        path=candidate_path,
        owner_path=owner_path,
        candidate_id=candidate_id,
        media_type=media_type,
        title=title,
        content_sha256=digest,
        idempotency_key=idempotency_key,
        security=security,
        source_capture_ref=owner.get("source_capture_ref"),
        source_garden_ref=owner.get("source_garden_ref"),
    )


def _released_records(root: Path) -> list[dict[str, Any]]:
    return release._read_committed_records(root)


def _ref_value(ref: str) -> str:
    for prefix in ("artifact://", "revision://", "publication://", "receipt://", "candidate://", "idempotency://"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _find_released(root: Path, ref: str) -> dict[str, Any]:
    value = _ref_value(ref)
    if not value:
        raise release.ReleaseError("released record was not found", "RELEASE_NOT_FOUND")
    fields = ("artifact_id", "revision_id", "receipt_id", "candidate_id", "idempotency_key")
    for record in _released_records(root):
        if any(record.get(field) == value for field in fields):
            return record
    raise release.ReleaseError("released record was not found", "RELEASE_NOT_FOUND")


def _bundle_path(root: Path, record: dict[str, Any], name: str) -> Path:
    bundle = release._confined_plain_path(root, root / record["bundle_path"], "released bundle", file=False)
    return release._confined_plain_path(root, bundle / name, "released bundle leaf", file=True)


def _show(root: Path, ref: str) -> dict[str, Any]:
    root = release._validate_root(root)
    record = _find_released(root, ref)
    try:
        body = _bundle_path(root, record, "artifact.bin").read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise release.ReleaseError("released artifact is not valid UTF-8", "RELEASE_TEXT_ONLY") from exc
    return {
        "schema": "kb2-release-show/v0.2",
        "provenance_status": record.get("provenance_status", "legacy-incomplete"),
        "missing_segments": record.get("missing_segments", ["capture", "garden"]),
        "immutable_bundle_verified": True,
        "artifact": record,
        "body": body,
    }


def _trace(root: Path, ref: str) -> dict[str, Any]:
    record = _find_released(root, ref)
    bundle = record["bundle_path"]
    legacy = record.get("release_schema") == "v0.1"
    candidate_node = {
        "ref": f"candidate://{record['candidate_id']}",
        "candidate_ref": f"candidate://{record['candidate_id']}",
        "candidate_id": record["candidate_id"],
            "candidate_path": record["candidate_path"],
            "candidate_owner_path": record["candidate_owner_path"],
            "content_sha256": record["content_sha256"],
            "idempotency_key": record["idempotency_key"],
            "media_type": record["media_type"],
            "owner": record.get("candidate_owner", "candidate-owner/v0.1" if legacy else "candidate-owner/v0.2"),
            "trust": "verified-at-release",
            "security": record["security"],
            "title": record["title"],
            "body_included": False,
    }
    nodes = {}
    if not legacy:
        nodes["capture"] = {"ref": record["source_capture_ref"], "capture_ref": record["source_capture_ref"], "path": record["source_capture_path"], "owner": record["source_capture_owner"], "owner_sha256": record["source_capture_owner_sha256"], "content_sha256": record["source_capture_content_sha256"], "trust": "verified-at-release", "body_included": False}
        nodes["garden"] = {"ref": record["source_garden_ref"], "garden_ref": record["source_garden_ref"], "path": record["source_garden_path"], "owner": record["source_garden_owner"], "owner_sha256": record["source_garden_owner_sha256"], "content_sha256": record["source_garden_content_sha256"], "trust": "verified-at-release", "body_included": False}
        candidate_node.update({"owner_sha256": record["candidate_owner_sha256"], "trust": "verified-at-release", "body_included": False})
    nodes["candidate"] = candidate_node
    nodes["artifact"] = {"ref": f"artifact://{record['artifact_id']}", "artifact_id": record["artifact_id"], "path": f"{bundle}/artifact.bin", "content_sha256": record["content_sha256"], "trust": "verified-at-release", "body_included": False}
    nodes["revision"] = {"ref": f"revision://{record['revision_id']}", "artifact_id": record["artifact_id"], "revision_id": record["revision_id"], "path": f"{bundle}/revision.json", "owner": "release-authority/v0.1" if legacy else "release-authority/v0.2", "content_sha256": record["content_sha256"], "request_digest": record["request_digest"], "trust": "verified-at-release", "body_included": False}
    nodes["receipt"] = {"ref": f"receipt://{record['receipt_id']}", "receipt_id": record["receipt_id"], "artifact_id": record["artifact_id"], "revision_id": record["revision_id"], "path": f"{bundle}/receipt.json", "owner": "release-authority/v0.1" if legacy else "release-authority/v0.2", "content_sha256": record["content_sha256"], "request_digest": record["request_digest"], "trust": "verified-at-release", "body_included": False}
    return {
        "schema": "kb2-release-trace/v0.2",
        "provenance_status": record.get("provenance_status", "legacy-incomplete"),
        "missing_segments": record.get("missing_segments", ["capture", "garden"]),
        "immutable_bundle_verified": True,
        "provenance": {"status": record.get("provenance_status", "legacy-incomplete"), "release_schema": record.get("release_schema"), "immutable_bundle_verified": True, "missing_segments": record.get("missing_segments", ["capture", "garden"])},
        "trace": {
            **nodes,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result_code = "KB2_OK"
    try:
        if args.command == "ingest":
            data = ingest_bytes(
                args.root,
                sys.stdin.buffer.read(),
                fail_after_capture=args._fail_after_capture,
                context_ref=args.context,
                base_digest=args.base_digest,
            )
            message = "input captured and routed"
        elif args.command == "status":
            data = status(args.root)
            message = "status inspected"
        elif args.command == "build":
            data = bootstrap.build(args.root)
            message = "bootstrap generation built"
        elif args.command == "publish-text":
            data = workflow.publish_text(args.root, sys.stdin.buffer.read())
            result_code = str(data.pop("_result_code"))
            message = "text published and projected" if result_code == "KB2_OK" else "text published; projection is stale"
        elif args.command == "publish":
            published = release.release_candidate(args.root, _read_candidate(args.root, args))
            data = {
                "release": published.to_dict(),
                "projection": {
                    "attempted": False,
                    "ok": published.projection_ok,
                    "fresh": None,
                    "error": published.projection_error,
                },
            }
            message = "release committed"
        elif args.command == "show":
            data = _show(args.root, args.ref)
            message = "released artifact shown"
        elif args.command == "trace":
            data = _trace(args.root, args.ref)
            message = "released artifact traced"
        elif args.command == "find":
            data = bootstrap.find(args.root, args.query)
            message = "bootstrap Registry searched"
        elif args.command == "explain":
            data = explain(args.root, args.ref)
            message = "decision explained"
        elif args.command == "organize":
            data = organize(args.root, args.ref)
            message = "Garden note organized without overriding human content"
        elif args.command == "correct":
            data = correct_bytes(args.root, args.ref, sys.stdin.buffer.read())
            message = "natural-language correction recorded as a human override"
        elif args.command == "close-context":
            data = context_core.close_context(args.root, args.ref, status=args.status)
            message = "Context lifecycle closed"
        elif args.command == "recover":
            data = recover_all(args.root)
            if data["unresolved"]:
                raise KbError(
                    "KB2_RECOVERY_UNRESOLVED",
                    "recovery completed with unresolved owner state",
                    3,
                    data,
                )
            message = "quarantine and correction recovery replay completed"
        else:  # pragma: no cover - argparse prevents this
            raise KbError("KB2_COMMAND_UNKNOWN", "unknown command", 2)
        changed = list(data.pop("changed", []))
        result = envelope(ok=True, code=result_code, message=message, data=data, changed=changed)
        _emit(result, args.json)
        return 0
    except release.ReleaseError as exc:
        code, exit_code, message = _release_error(exc)
        result = envelope(
            ok=False,
            code=code,
            message=message,
            data={"release_code": exc.code},
            diagnostics=[{"code": code, "message": message}],
        )
        _emit(result, args.json)
        return exit_code
    except KbError as exc:
        result = envelope(
            ok=False,
            code=exc.code,
            message=exc.message,
            data=exc.data,
            diagnostics=[{"code": exc.code, "message": exc.message}],
            changed=exc.changed,
        )
        _emit(result, args.json)
        return exc.exit_code
    except Exception:
        result = envelope(
            ok=False,
            code="KB2_INTERNAL_ERROR",
            message="unexpected internal failure; captured inputs, if any, were not deleted",
            diagnostics=[{"code": "KB2_INTERNAL_ERROR", "message": "unexpected internal failure"}],
        )
        _emit(result, args.json)
        return 4


if __name__ == "__main__":
    sys.exit(main())
