"""Minimal CLI wiring contracts for the Phase2.2 release façade."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from kb2.cli import main
from kb2 import release
from kb2 import core


_EXAMPLE_AWS_SECRET = b"AKIA" + b"1234567890ABCDEF"


class CliReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="kb2-cli-root-", dir=r"D:\tmp")
        self.root = Path(self.temp.name)
        (self.root / "kb.yaml").write_text(
            "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            encoding="utf-8",
        )
        self.candidates = self.root / "candidates"
        self.candidates.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_candidate(
        self,
        content: bytes = b"# CLI release\n\npublic body\n",
        *,
        candidate_id: str = "CAND-CLI-01",
        idempotency_key: str = "idem-cli-01",
        title: str = "CLI release",
        media_type: str = "text/markdown",
    ) -> tuple[Path, Path, str]:
        try:
            content.decode("utf-8")
            unsafe_fixture = bool(core._secret_reasons(content))
        except UnicodeDecodeError:
            unsafe_fixture = True
        captured = core.ingest_bytes(self.root, b"safe fixture seed\n" if unsafe_fixture else content)
        fallback_capture = unsafe_fixture
        capture_ref = str(captured["capture_ref"])
        garden_ref = str(captured["garden_ref"])
        garden_id = capture_ref.removeprefix("capture://")
        garden = self.root / "garden" / "notes" / f"{garden_id}.md"
        candidate_dir = self.root / "governance" / "release-candidates" / candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=True)
        source = candidate_dir / "candidate.md"
        garden_content = garden.read_bytes()
        if fallback_capture:
            garden_content = content
            garden.write_bytes(garden_content)
        source.write_bytes(garden_content)
        digest = hashlib.sha256(garden_content).hexdigest()
        owner = candidate_dir / "owner.json"
        owner.write_text(
            json.dumps(
                {
                    "schema": "kb2-candidate-owner/v0.2",
                    "owner": "candidate-owner/v0.2",
                    "candidate_id": candidate_id,
                    "content_path": source.relative_to(self.root).as_posix(),
                    "content_sha256": digest,
                    "media_type": media_type,
                    "title": title,
                    "security": "public",
                    "source_capture_ref": capture_ref,
                    "source_garden_ref": garden_ref,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return source, owner, digest

    def run_cli(self, *args: str) -> tuple[int, dict[str, object], str]:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["--root", str(self.root), "--json", *args])
        text = output.getvalue()
        return exit_code, json.loads(text), text

    def publish_args(self, source: Path, owner: Path) -> tuple[str, ...]:
        return (
            "publish",
            "--candidate",
            str(source),
            "--owner",
            str(owner),
            "--candidate-id",
            "CAND-CLI-01",
            "--idempotency-key",
            "idem-cli-01",
            "--title",
            "CLI release",
            "--media-type",
            "text/markdown",
        )

    def test_publish_show_trace_happy_path(self) -> None:
        source, owner, _ = self.make_candidate()
        code, published, _ = self.run_cli(*self.publish_args(source, owner))
        self.assertEqual(code, 0)
        self.assertTrue(published["ok"])
        self.assertEqual(published["code"], "KB2_OK")
        data = published["data"]
        self.assertTrue(data["release"]["release_committed"])
        self.assertIn("bundle_path", data["release"])
        self.assertIn("projection", data)
        self.assertFalse(data["projection"]["attempted"])

        artifact_id = data["release"]["artifact_id"]
        code, shown, _ = self.run_cli("show", artifact_id)
        self.assertEqual(code, 0)
        self.assertEqual(shown["data"]["artifact"]["artifact_id"], artifact_id)
        self.assertIn("public body", shown["data"]["body"])

        code, traced, _ = self.run_cli("trace", data["release"]["receipt_id"])
        self.assertEqual(code, 0)
        chain = traced["data"]["trace"]
        self.assertEqual(set(chain), {"capture", "garden", "candidate", "artifact", "revision", "receipt"})
        for segment in chain.values():
            self.assertIn("trust", segment)
            self.assertFalse(segment["body_included"])
        self.assertEqual(chain["candidate"]["candidate_id"], "CAND-CLI-01")
        self.assertEqual(chain["revision"]["revision_id"], data["release"]["revision_id"])
        self.assertEqual(chain["receipt"]["receipt_id"], data["release"]["receipt_id"])
        self.assertEqual(chain["capture"]["capture_ref"].removeprefix("capture://"), chain["garden"]["garden_ref"].removeprefix("garden://notes/").removesuffix(".md"))
        self.assertNotIn("public body", json.dumps(chain, ensure_ascii=False))

    def test_show_relative_root_matches_absolute_root(self) -> None:
        source, owner, _ = self.make_candidate()
        _, published, _ = self.run_cli(*self.publish_args(source, owner))
        artifact_id = published["data"]["release"]["artifact_id"]
        absolute_code, absolute, _ = self.run_cli("show", artifact_id)

        previous = Path.cwd()
        try:
            os.chdir(self.root)
            output = StringIO()
            with redirect_stdout(output):
                relative_code = main(["--root", ".", "--json", "show", artifact_id])
            relative = json.loads(output.getvalue())
        finally:
            os.chdir(previous)

        self.assertEqual(relative_code, absolute_code)
        self.assertEqual(relative, absolute)

    def test_publish_exact_repeat_is_same_result(self) -> None:
        source, owner, _ = self.make_candidate()
        first_code, first, _ = self.run_cli(*self.publish_args(source, owner))
        second_code, second, _ = self.run_cli(*self.publish_args(source, owner))
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first["data"]["release"], second["data"]["release"])
        self.assertEqual(len(list((self.root / "released").rglob("receipt.json"))), 1)

    def test_show_and_trace_read_committed_bundle_before_idempotency_pointer_recovery(self) -> None:
        source, owner, digest = self.make_candidate()
        candidate = release.Candidate(
            path=source,
            owner_path=owner,
            candidate_id="CAND-CLI-01",
            media_type="text/markdown",
            title="CLI release",
            content_sha256=digest,
            idempotency_key="idem-cli-01",
        )
        with self.assertRaisesRegex(release.ReleaseError, "interrupted"):
            release.release_candidate(self.root, candidate, _fail_after_promotion=True)

        pointer = self.root / "released" / "idempotency" / "idem-cli-01.json"
        self.assertFalse(pointer.exists())

        def snapshot() -> dict[str, tuple[int, int, str]]:
            result: dict[str, tuple[int, int, str]] = {}
            for path in self.root.rglob("*"):
                if path.is_file():
                    stat = path.stat()
                    result[str(path.relative_to(self.root))] = (
                        stat.st_size,
                        stat.st_mtime_ns,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
            return result

        before = snapshot()
        artifact_id = "ART-CAND-CLI-01"
        bundle = self.root / "released" / "artifacts" / artifact_id / "revision-1"
        receipt_id = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))["receipt_id"]
        for command, ref in (("show", artifact_id), ("trace", receipt_id)):
            code, result, _ = self.run_cli(command, ref)
            self.assertEqual(code, 0)
            self.assertTrue(result["ok"])
        after = snapshot()
        self.assertEqual(after, before)
        self.assertFalse(pointer.exists())

        code, retried, _ = self.run_cli(*self.publish_args(source, owner))
        self.assertEqual(code, 0)
        self.assertTrue(retried["data"]["release"]["recovered"])
        self.assertTrue(pointer.exists())

    def test_show_and_trace_reject_foreign_or_staging_release_root_without_writes(self) -> None:
        source, owner, _ = self.make_candidate()
        _, published, _ = self.run_cli(*self.publish_args(source, owner))
        artifact_id = published["data"]["release"]["artifact_id"]

        def snapshot() -> dict[str, tuple[int, int, str]]:
            result: dict[str, tuple[int, int, str]] = {}
            for path in self.root.rglob("*"):
                if path.is_file():
                    stat = path.stat()
                    result[str(path.relative_to(self.root))] = (
                        stat.st_size,
                        stat.st_mtime_ns,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
            return result

        for name in ("foreign-entry", ".release-staging-test"):
            entry = self.root / "released" / name
            if name.startswith(".release-staging-"):
                entry.mkdir()
                (entry / "marker").write_text("partial\n", encoding="utf-8")
            else:
                entry.write_text("foreign\n", encoding="utf-8")
            before = snapshot()
            for command in ("show", "trace"):
                code, result, _ = self.run_cli(command, artifact_id)
                self.assertNotEqual(code, 0)
                self.assertFalse(result["ok"])
            self.assertEqual(snapshot(), before)
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    def test_show_and_trace_reject_duplicate_identity_without_writes(self) -> None:
        source, owner, _ = self.make_candidate()
        _, published, _ = self.run_cli(*self.publish_args(source, owner))
        release = published["data"]["release"]
        original_bundle = self.root / release["bundle_path"]
        duplicate_bundle = self.root / "released" / "artifacts" / "ART-DUPLICATE" / "revision-1"
        shutil.copytree(original_bundle, duplicate_bundle)
        for name in ("revision.json", "receipt.json"):
            metadata_path = duplicate_bundle / name
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["artifact_id"] = "ART-DUPLICATE"
            metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
        pointer = self.root / "released" / "idempotency" / "idem-cli-01.json"
        pointer.unlink()
        (self.root / "released" / "idempotency").rmdir()

        def snapshot() -> dict[str, tuple[int, int, str]]:
            result: dict[str, tuple[int, int, str]] = {}
            for path in self.root.rglob("*"):
                if path.is_file():
                    stat = path.stat()
                    result[str(path.relative_to(self.root))] = (
                        stat.st_size,
                        stat.st_mtime_ns,
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
            return result

        before = snapshot()
        for command, ref in (("show", release["artifact_id"]), ("trace", release["receipt_id"])):
            code, result, _ = self.run_cli(command, ref)
            self.assertNotEqual(code, 0)
            self.assertFalse(result["ok"])
        self.assertEqual(snapshot(), before)

    def test_publish_error_envelope_is_stable_and_secret_free(self) -> None:
        source, owner, _ = self.make_candidate(_EXAMPLE_AWS_SECRET + b"\n", candidate_id="CAND-SECRET")
        args = list(self.publish_args(source, owner))
        args[args.index("--candidate-id") + 1] = "CAND-SECRET"
        args[args.index("--idempotency-key") + 1] = "idem-secret"
        code, result, output = self.run_cli(*args)
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["schema"], "kb2-result/v0.1")
        self.assertEqual(result["code"], "KB2_POLICY_REJECTED")
        self.assertNotIn(_EXAMPLE_AWS_SECRET.decode("ascii"), output)
        self.assertFalse((self.root / "released").exists())

    def test_publish_candidate_owner_hash_mismatch_fails_closed(self) -> None:
        source, owner, _ = self.make_candidate()
        source.write_bytes(b"mutated after owner snapshot\n")
        code, result, _ = self.run_cli(*self.publish_args(source, owner))
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "KB2_EXPECTED_HASH_MISMATCH")
        self.assertFalse((self.root / "released").exists())

    def test_show_and_trace_missing_fail_closed(self) -> None:
        for command in ("show", "trace"):
            with self.subTest(command=command):
                code, result, _ = self.run_cli(command, "ART-MISSING")
                self.assertNotEqual(code, 0)
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "KB2_RELEASE_NOT_FOUND")

    def test_show_tampered_bundle_fails_closed(self) -> None:
        source, owner, _ = self.make_candidate()
        _, published, _ = self.run_cli(*self.publish_args(source, owner))
        bundle = self.root / published["data"]["release"]["bundle_path"]
        (bundle / "artifact.bin").write_bytes(b"tampered\n")
        code, result, _ = self.run_cli("show", published["data"]["release"]["artifact_id"])
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "KB2_RELEASE_INTEGRITY_FAILED")

    def test_show_rejects_outside_path_reference(self) -> None:
        source, owner, _ = self.make_candidate()
        self.run_cli(*self.publish_args(source, owner))
        outside = self.root.parent / "ART-CAND-CLI-01"
        code, result, _ = self.run_cli("show", str(outside))
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "KB2_RELEASE_NOT_FOUND")

    def test_trace_exposes_owner_digest_and_path_chain_without_body(self) -> None:
        source, owner, digest = self.make_candidate()
        _, published, _ = self.run_cli(*self.publish_args(source, owner))
        release = published["data"]["release"]
        for ref in (release["artifact_id"], release["revision_id"], release["receipt_id"], "idem-cli-01"):
            with self.subTest(ref=ref):
                code, result, output = self.run_cli("trace", ref)
                self.assertEqual(code, 0)
                trace = result["data"]["trace"]
                self.assertEqual(trace["candidate"]["content_sha256"], digest)
                self.assertEqual(trace["revision"]["owner"], "release-authority/v0.2")
                self.assertEqual(trace["receipt"]["owner"], "release-authority/v0.2")
                self.assertNotIn("public body", output)

    def test_post_publish_owner_drift_keeps_frozen_show_trace_but_republish_rejects(self) -> None:
        source, owner, _ = self.make_candidate()
        _, published, _ = self.run_cli(*self.publish_args(source, owner))
        release_data = published["data"]["release"]
        _, before_show, _ = self.run_cli("show", release_data["artifact_id"])
        _, before_trace, _ = self.run_cli("trace", release_data["receipt_id"])
        owner_data = json.loads(owner.read_text(encoding="utf-8"))
        owner_data["post_publish_drift"] = "only-owner-bytes"
        owner.write_text(json.dumps(owner_data, sort_keys=True), encoding="utf-8")
        _, after_show, _ = self.run_cli("show", release_data["artifact_id"])
        _, after_trace, _ = self.run_cli("trace", release_data["receipt_id"])
        self.assertEqual(after_show, before_show)
        self.assertEqual(after_trace, before_trace)
        before_bundle = {p.name: p.read_bytes() for p in (self.root / release_data["bundle_path"]).iterdir()}
        pointer = self.root / "released" / "idempotency" / "idem-cli-01.json"
        before_pointer = pointer.read_bytes()
        code, rejected, _ = self.run_cli(*self.publish_args(source, owner))
        self.assertNotEqual(code, 0)
        self.assertFalse(rejected["ok"])
        self.assertEqual(before_bundle, {p.name: p.read_bytes() for p in (self.root / release_data["bundle_path"]).iterdir()})
        self.assertEqual(before_pointer, pointer.read_bytes())

    def test_legacy_v01_bundle_is_readable_and_trace_is_explicitly_incomplete(self) -> None:
        artifact_id = "ART-LEGACY"
        revision_id = "ART-LEGACY-R1"
        receipt_id = "PUB-LEGACY"
        content = b"legacy body\n"
        digest = hashlib.sha256(content).hexdigest()
        bundle = self.root / "released" / "artifacts" / artifact_id / "revision-1"
        bundle.mkdir(parents=True)
        revision = {
            "schema": "kb2-artifact-revision/v0.1", "owner": "release-authority/v0.1",
            "artifact_id": artifact_id, "revision_id": revision_id, "revision": 1,
            "receipt_id": receipt_id, "candidate_id": "CAND-LEGACY", "candidate_path": "governance/release-candidates/CAND-LEGACY/candidate.md",
            "candidate_owner_path": "governance/release-candidates/CAND-LEGACY/owner.json", "idempotency_key": "idem-legacy",
            "content_path": "artifact.bin", "artifact_sha256": digest, "media_type": "text/plain", "title": "Legacy", "security": "public",
            "request_digest": "",
        }
        revision["request_digest"] = release._request_digest(revision, legacy=True)
        receipt_id = release._receipt_id_from_revision(revision)
        revision["receipt_id"] = receipt_id
        receipt = {"schema": "kb2-publication-receipt/v0.1", "owner": "release-authority/v0.1", "receipt_id": receipt_id,
                   "artifact_id": artifact_id, "revision_id": revision_id, "candidate_id": "CAND-LEGACY", "candidate_path": revision["candidate_path"],
                   "candidate_owner_path": revision["candidate_owner_path"], "idempotency_key": "idem-legacy", "content_sha256": digest,
                   "media_type": "text/plain", "title": "Legacy", "security": "public", "request_digest": revision["request_digest"]}
        (bundle / "artifact.bin").write_bytes(content)
        (bundle / "revision.json").write_text(json.dumps(revision), encoding="utf-8")
        (bundle / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        pointer = {"schema": "kb2-release-pointer/v0.1", "owner": "release-authority/v0.1", "idempotency_key": "idem-legacy",
                   "artifact_id": artifact_id, "revision_id": revision_id, "receipt_id": receipt_id, "candidate_id": "CAND-LEGACY",
                   "content_sha256": digest, "request_digest": revision["request_digest"], "candidate_path": revision["candidate_path"],
                   "candidate_owner_path": revision["candidate_owner_path"], "media_type": "text/plain", "title": "Legacy", "security": "public",
                   "bundle_path": "released/artifacts/ART-LEGACY/revision-1"}
        idem = self.root / "released" / "idempotency"
        idem.mkdir(parents=True)
        (idem / "idem-legacy.json").write_text(json.dumps(pointer), encoding="utf-8")
        code, shown, _ = self.run_cli("show", artifact_id)
        self.assertEqual(code, 0)
        self.assertEqual(shown["data"]["body"], content.decode())
        code, traced, _ = self.run_cli("trace", receipt_id)
        self.assertEqual(code, 0)
        legacy_trace = traced["data"]["trace"]
        self.assertEqual(traced["data"]["provenance"]["status"], "legacy-incomplete")
        self.assertEqual(set(legacy_trace), {"candidate", "artifact", "revision", "receipt"})
        self.assertEqual(set(traced["data"]["provenance"]["missing_segments"]), {"capture", "garden"})
        self.assertNotIn("owner_sha256", json.dumps(legacy_trace, ensure_ascii=False))
        self.assertNotIn("legacy body", json.dumps(traced, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
