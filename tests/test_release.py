"""Phase2.1 Release Authority contract tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import kb2.release as release_module
import kb2.core as core
from kb2.release import (
    Candidate,
    ReleaseError,
    _acquire_lock,
    _ensure_plain_directory,
    _release_lock,
    release_candidate,
)


_EXAMPLE_AWS_SECRET = b"AKIA" + b"1234567890ABCDEF"
_EXAMPLE_PRIVATE_KEY_HEADER = b"-----BEGIN " + b"PRIVATE KEY-----"


class ReleaseSliceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="kb2-release-root-", dir=r"D:\tmp")
        self.root = Path(self.temp.name)
        (self.root / "kb.yaml").write_text(
            "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def candidate(
        self,
        name: str = "candidate.bin",
        content: bytes = b"safe artifact bytes\n",
        *,
        candidate_id: str = "CAND-01",
        idempotency_key: str = "idem-01",
        media_type: str = "text/plain",
        title: str = "Safe artifact",
        security: str = "public",
        directory: Path | None = None,
        owner_root: Path | None = None,
    ) -> Candidate:
        root = owner_root or self.root
        try:
            content.decode("utf-8")
            unsafe_fixture = bool(core._secret_reasons(content))
        except UnicodeDecodeError:
            unsafe_fixture = True
        capture = core.ingest_bytes(root, b"safe fixture seed\n" if unsafe_fixture else content)
        fallback_capture = unsafe_fixture
        capture_id = str(capture["capture_ref"]).removeprefix("capture://")
        parent = root / "governance" / "release-candidates" / candidate_id
        parent.mkdir(parents=True, exist_ok=True)
        garden = root / "garden" / "notes" / f"{capture_id}.md"
        garden_content = garden.read_bytes()
        if fallback_capture:
            garden_content = content
            garden.write_bytes(garden_content)
        source = parent / "candidate.md"
        source.write_bytes(garden_content)
        digest = hashlib.sha256(garden_content).hexdigest()
        owner = parent / "owner.json"
        owner.write_text(
            json.dumps(
                {
                    "schema": "kb2-candidate-owner/v0.2",
                    "owner": "candidate-owner/v0.2",
                    "candidate_id": candidate_id,
                    "content_path": source.relative_to(owner_root or self.root).as_posix(),
                    "content_sha256": digest,
                    "media_type": media_type,
                    "title": title,
                    "security": security,
                    "source_capture_ref": capture["capture_ref"],
                    "source_garden_ref": capture["garden_ref"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return Candidate(
            path=source,
            owner_path=owner,
            candidate_id=candidate_id,
            media_type=media_type,
            title=title,
            content_sha256=digest,
            idempotency_key=idempotency_key,
            security=security,
        )

    def test_publish_rejects_generated_candidate_without_verified_garden_provenance(self) -> None:
        candidate = self.candidate(candidate_id="CAND-GENERATED", idempotency_key="idem-generated")
        generated = self.root / "generated" / "candidate.md"
        generated.parent.mkdir()
        generated.write_bytes(candidate.path.read_bytes())
        generated_owner = generated.parent / "owner.json"
        generated_owner.write_text(
            json.dumps(
                {
                    "schema": "kb2-candidate-owner/v0.1",
                    "owner": "candidate-owner/v0.1",
                    "candidate_id": candidate.candidate_id,
                    "content_path": "generated/candidate.md",
                    "content_sha256": candidate.content_sha256,
                    "media_type": candidate.media_type,
                    "title": candidate.title,
                    "security": "public",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        generated_candidate = Candidate(
            path=generated,
            owner_path=generated_owner,
            candidate_id=candidate.candidate_id,
            media_type=candidate.media_type,
            title=candidate.title,
            content_sha256=candidate.content_sha256,
            idempotency_key=candidate.idempotency_key,
        )
        with self.assertRaisesRegex(ReleaseError, "Garden|candidate path|owner bundle"):
            release_candidate(self.root, generated_candidate)

    def test_publish_accepts_candidate_bound_to_garden_bytes(self) -> None:
        candidate = self.candidate()
        result = release_candidate(self.root, candidate)
        self.assertTrue(result.release_committed)
        garden_ref = json.loads(candidate.owner_path.read_text(encoding="utf-8"))["source_garden_ref"]
        garden_name = garden_ref.removeprefix("garden://notes/")
        self.assertEqual((self.root / "garden" / "notes" / garden_name).read_bytes(), (self.root / candidate.path).read_bytes())

    def test_publish_rejects_missing_or_mismatched_garden_source(self) -> None:
        missing = self.candidate(candidate_id="CAND-MISSING", idempotency_key="idem-missing")
        missing_garden = json.loads(missing.owner_path.read_text(encoding="utf-8"))["source_garden_ref"]
        (self.root / "garden" / "notes" / missing_garden.removeprefix("garden://notes/")).unlink()
        with self.assertRaisesRegex(ReleaseError, "Garden|source"):
            release_candidate(self.root, missing)

        mismatch = self.candidate(candidate_id="CAND-MISMATCH", idempotency_key="idem-mismatch")
        mismatch_garden = json.loads(mismatch.owner_path.read_text(encoding="utf-8"))["source_garden_ref"]
        (self.root / "garden" / "notes" / mismatch_garden.removeprefix("garden://notes/")).write_bytes(b"drifted garden\n")
        with self.assertRaisesRegex(ReleaseError, "Garden|source|bytes"):
            release_candidate(self.root, mismatch)

    def test_publish_rejects_capture_garden_ref_or_owner_inconsistency(self) -> None:
        candidate = self.candidate(candidate_id="CAND-CAPTURE-MISMATCH", idempotency_key="idem-capture-mismatch")
        owner = json.loads(candidate.owner_path.read_text(encoding="utf-8"))
        owner["source_capture_ref"] = "capture://CAP-01J00000000000000000000000"
        candidate.owner_path.write_text(json.dumps(owner, sort_keys=True), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "capture|provenance|owner"):
            release_candidate(self.root, candidate)

        candidate = self.candidate(candidate_id="CAND-CAPTURE-OWNER", idempotency_key="idem-capture-owner")
        capture_id = json.loads(candidate.owner_path.read_text(encoding="utf-8"))["source_capture_ref"].removeprefix("capture://")
        (self.root / "ingress" / "pending" / capture_id / "owner.json").unlink()
        with self.assertRaisesRegex(ReleaseError, "capture|owner|provenance"):
            release_candidate(self.root, candidate)

    def test_missing_artifacts_does_not_mask_invalid_idempotency_pointer(self) -> None:
        release_dir = self.root / "released"
        idempotency = release_dir / "idempotency"
        idempotency.mkdir(parents=True)
        (idempotency / "idem-malformed.json").write_text("invalid\n", encoding="utf-8")

        with self.assertRaises(ReleaseError) as raised:
            release_module._read_committed_records(self.root)
        self.assertEqual(raised.exception.code, "RELEASE_POINTER_INVALID")

    def test_missing_artifacts_rejects_valid_looking_unbound_pointer(self) -> None:
        release_dir = self.root / "released"
        idempotency = release_dir / "idempotency"
        idempotency.mkdir(parents=True)
        pointer = {
            "schema": "kb2-release-pointer/v0.1",
            "owner": "release-authority/v0.1",
            "idempotency_key": "idem-unbound",
            "candidate_id": "CAND-UNBOUND",
            "artifact_id": "ART-CAND-UNBOUND",
            "revision_id": "ART-CAND-UNBOUND-R1",
            "receipt_id": "PUB-UNBOUND",
            "content_sha256": "sha256:" + "0" * 64,
            "bundle_path": "released/artifacts/ART-CAND-UNBOUND/revision-1",
            "request_digest": "sha256:" + "1" * 64,
            "candidate_path": "governance/release-candidates/CAND-UNBOUND/candidate.md",
            "candidate_owner_path": "governance/release-candidates/CAND-UNBOUND/owner.json",
            "media_type": "text/plain",
            "title": "unbound",
            "security": "public",
        }
        (idempotency / "idem-unbound.json").write_text(json.dumps(pointer), encoding="utf-8")

        with self.assertRaises(ReleaseError) as raised:
            release_module._read_committed_records(self.root)
        self.assertEqual(raised.exception.code, "RELEASE_POINTER_INVALID")

    def test_publish_rejects_garden_ref_traversal(self) -> None:
        candidate = self.candidate(candidate_id="CAND-TRAVERSAL", idempotency_key="idem-traversal")
        owner = json.loads(candidate.owner_path.read_text(encoding="utf-8"))
        owner["source_garden_ref"] = "garden://notes/../generated/candidate.md"
        candidate.owner_path.write_text(json.dumps(owner, sort_keys=True), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "Garden|source|path"):
            release_candidate(self.root, candidate)

    @unittest.skipUnless(os.name == "nt", "Windows junction assertion")
    def test_publish_rejects_reparse_garden_source(self) -> None:
        candidate = self.candidate(candidate_id="CAND-GARDEN-REPARSE", idempotency_key="idem-garden-reparse")
        notes = self.root / "garden" / "notes"
        shutil.rmtree(notes)
        outside = Path(tempfile.mkdtemp(prefix="kb2-garden-reparse-target-", dir=r"D:\tmp"))
        try:
            garden_name = json.loads(candidate.owner_path.read_text(encoding="utf-8"))["source_garden_ref"].removeprefix("garden://notes/")
            (outside / garden_name).write_bytes(candidate.path.read_bytes())
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(notes), str(outside)], capture_output=True)
            if made.returncode != 0:
                self.skipTest("junction creation is unavailable")
            with self.assertRaisesRegex(ReleaseError, "Garden|reparse|path"):
                release_candidate(self.root, candidate)
        finally:
            if notes.exists():
                notes.rmdir()
            shutil.rmtree(outside)

    def test_safe_publish_is_exact_artifact_and_one_receipt(self) -> None:
        candidate = self.candidate()
        result = release_candidate(self.root, candidate)
        self.assertTrue(result.release_committed)
        self.assertTrue(result.projection_ok)
        bundle = self.root / result.bundle_path
        self.assertEqual(
            {path.name for path in bundle.iterdir()},
            {"artifact.bin", "revision.json", "receipt.json"},
        )
        self.assertEqual((bundle / "artifact.bin").read_bytes(), candidate.path.read_bytes())
        revision = json.loads((bundle / "revision.json").read_text(encoding="utf-8"))
        receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(revision["artifact_sha256"], candidate.content_sha256)
        self.assertEqual(revision["revision_id"], result.revision_id)
        self.assertEqual(receipt["receipt_id"], result.receipt_id)
        self.assertEqual(receipt["revision_id"], revision["revision_id"])
        self.assertEqual(receipt["idempotency_key"], candidate.idempotency_key)

    def test_v02_bundle_freezes_candidate_source_fields_across_revision_receipt_pointer(self) -> None:
        candidate = self.candidate()
        result = release_candidate(self.root, candidate)
        bundle = self.root / result.bundle_path
        revision = json.loads((bundle / "revision.json").read_text(encoding="utf-8"))
        receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
        pointer = json.loads((self.root / "released" / "idempotency" / f"{candidate.idempotency_key}.json").read_text(encoding="utf-8"))
        self.assertEqual(revision["schema"], "kb2-artifact-revision/v0.2")
        self.assertEqual(receipt["schema"], "kb2-publication-receipt/v0.2")
        self.assertEqual(pointer["schema"], "kb2-release-pointer/v0.2")
        for field in ("candidate_id", "candidate_path", "candidate_owner_path", "idempotency_key", "media_type", "title", "security", "source_capture_ref", "source_garden_ref", "candidate_owner_sha256"):
            self.assertEqual(revision[field], receipt[field], field)
            self.assertEqual(revision[field], pointer[field], field)
        self.assertEqual(revision["revision_id"], receipt["revision_id"])
        self.assertEqual(revision["revision_id"], pointer["revision_id"])
        self.assertEqual(revision["receipt_id"], receipt["receipt_id"])
        self.assertEqual(revision["receipt_id"], pointer["receipt_id"])

    def test_duplicate_is_same_identity_and_one_bundle(self) -> None:
        candidate = self.candidate()
        first = release_candidate(self.root, candidate)
        second = release_candidate(self.root, candidate)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(len(list((self.root / "released" / "artifacts").rglob("receipt.json"))), 1)

    def test_post_publish_candidate_owner_drift_is_rejected_without_bundle_mutation(self) -> None:
        candidate = self.candidate()
        result = release_candidate(self.root, candidate)
        bundle = self.root / result.bundle_path
        before = {path.name: path.read_bytes() for path in bundle.iterdir()}
        pointer = self.root / "released" / "idempotency" / f"{candidate.idempotency_key}.json"
        pointer_before = pointer.read_bytes()
        owner = json.loads(candidate.owner_path.read_text(encoding="utf-8"))
        owner["owner_drift_marker"] = "post-publish"
        candidate.owner_path.write_text(json.dumps(owner, sort_keys=True), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "owner|integrity|candidate|idempotency|request"):
            release_candidate(self.root, candidate)
        self.assertEqual(before, {path.name: path.read_bytes() for path in bundle.iterdir()})
        self.assertEqual(pointer_before, pointer.read_bytes())

    def test_same_key_concurrency_converges(self) -> None:
        candidate = self.candidate()
        results: list[object] = []
        errors: list[BaseException] = []

        def publish() -> None:
            try:
                results.append(release_candidate(self.root, candidate))
            except BaseException as exc:  # pragma: no cover - assertion reports it
                errors.append(exc)

        threads = [threading.Thread(target=publish) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({item.revision_id for item in results}, {results[0].revision_id})
        self.assertEqual(len(list((self.root / "released" / "artifacts").rglob("receipt.json"))), 1)

    def test_plain_directory_creation_race_revalidates_and_rejects_foreign_shapes(self) -> None:
        target = self.root / "released"
        original_exists = Path.exists
        original_mkdir = Path.mkdir
        first_probe = True

        def racing_exists(path: Path, *args: object, **kwargs: object) -> bool:
            nonlocal first_probe
            if path == target and first_probe:
                first_probe = False
                return False
            return original_exists(path)

        def racing_mkdir(path: Path, *args: object, **kwargs: object) -> None:
            if path == target:
                original_mkdir(path, *args, **kwargs)
                raise FileExistsError(17, "already exists", str(path))
            original_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "exists", autospec=True, side_effect=racing_exists):
            with mock.patch.object(Path, "mkdir", autospec=True, side_effect=racing_mkdir):
                _ensure_plain_directory(target, "release store")
        self.assertTrue(target.is_dir())

        foreign = self.root / "foreign"
        foreign.write_text("file", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "plain directory"):
            _ensure_plain_directory(foreign, "release store")

        reparse = self.root / "reparse"
        reparse.mkdir()
        with mock.patch.object(release_module, "_is_reparse", side_effect=lambda path: path == reparse):
            with self.assertRaisesRegex(ReleaseError, "plain directory"):
                _ensure_plain_directory(reparse, "release store")

    def test_lock_owner_disappearance_during_read_retries_and_converges(self) -> None:
        release_dir = self.root / "released-owner-window"
        release_dir.mkdir()
        lock_path = release_dir / ".release.lock"
        lock_path.mkdir()
        owner_path = lock_path / "owner.json"
        owner_path.write_text(
            json.dumps({"schema": "kb2-release-lock/v0.1", "owner": "release-authority/v0.1", "token": "old"}),
            encoding="utf-8",
        )
        original_read_text = Path.read_text
        disappeared = False

        def disappear_before_read(path: Path, *args: object, **kwargs: object) -> str:
            nonlocal disappeared
            if path == owner_path and not disappeared:
                disappeared = True
                owner_path.unlink()
                lock_path.rmdir()
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=disappear_before_read):
            acquired_path, token, _ = _acquire_lock(release_dir, timeout=0.5)
        try:
            self.assertTrue(disappeared)
            self.assertEqual(acquired_path, lock_path)
            self.assertTrue((lock_path / "owner.json").is_file())
            self.assertNotEqual(token, "old")
        finally:
            _release_lock(acquired_path, token)

    def test_lock_disappearance_after_exists_retries_and_converges(self) -> None:
        release_dir = self.root / "released-lock-window"
        release_dir.mkdir()
        lock_path = release_dir / ".release.lock"
        lock_path.mkdir()
        owner_path = lock_path / "owner.json"
        owner_path.write_text(
            json.dumps({"schema": "kb2-release-lock/v0.1", "owner": "release-authority/v0.1", "token": "old"}),
            encoding="utf-8",
        )
        original_exists = Path.exists
        disappeared = False

        def disappear_after_exists(path: Path, *args: object, **kwargs: object) -> bool:
            nonlocal disappeared
            if path == lock_path and not disappeared:
                disappeared = True
                owner_path.unlink()
                lock_path.rmdir()
                return True
            return original_exists(path)

        with mock.patch.object(Path, "exists", autospec=True, side_effect=disappear_after_exists):
            acquired_path, token, _ = _acquire_lock(release_dir, timeout=0.5)
        try:
            self.assertTrue(disappeared)
            self.assertEqual(acquired_path, lock_path)
            self.assertTrue((lock_path / "owner.json").is_file())
            self.assertNotEqual(token, "old")
        finally:
            _release_lock(acquired_path, token)

    def test_lock_malformed_and_foreign_owner_fail_closed(self) -> None:
        cases = (("malformed", b"{"), ("foreign", b'{"schema":"kb2-release-lock/v0.1","owner":"foreign"}'))
        for label, content in cases:
            with self.subTest(label=label):
                release_dir = self.root / f"released-{label}"
                release_dir.mkdir()
                lock_path = release_dir / ".release.lock"
                lock_path.mkdir()
                (lock_path / "owner.json").write_bytes(content)
                expected = "malformed" if label == "malformed" else "foreign owner"
                with self.assertRaisesRegex(ReleaseError, expected):
                    _acquire_lock(release_dir, timeout=0.05)

    def test_candidate_mutation_before_commit_fails_closed(self) -> None:
        candidate = self.candidate()

        def mutate() -> None:
            candidate.path.write_bytes(b"changed before commit")

        with self.assertRaisesRegex(ReleaseError, "candidate hash"):
            release_candidate(self.root, candidate, _before_commit=mutate)
        self.assertFalse((self.root / "released").exists())

    def test_same_key_different_content_conflicts(self) -> None:
        first = self.candidate()
        release_candidate(self.root, first)
        second = self.candidate(content=b"different", candidate_id="CAND-02")
        second = Candidate(
            path=second.path,
            owner_path=second.owner_path,
            candidate_id=second.candidate_id,
            media_type=second.media_type,
            title=second.title,
            content_sha256=second.content_sha256,
            idempotency_key=first.idempotency_key,
            security=second.security,
        )
        with self.assertRaisesRegex(ReleaseError, "idempotency"):
            release_candidate(self.root, second)

    def test_root_validation_rejects_weak_anchor_variants(self) -> None:
        cases = {
            "missing": None,
            "empty": "",
            "schema-contains": "schema: kb-root/v0.1-extra\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            "invalid-id": "schema: kb-root/v0.1\nid: not-a-kb-id\n",
            "duplicate": "schema: kb-root/v0.1\nschema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            "alias": "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\nvalue: &v unsafe\ncopy: *v\n",
        }
        for name, anchor in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory(prefix="kb2-release-root-contract-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    if anchor is not None:
                        (root / "kb.yaml").write_text(anchor, encoding="utf-8")
                    candidate = self.candidate(
                        candidate_id=f"CAND-ROOT-{name}",
                        idempotency_key=f"idem-root-{name}",
                    )
                    with self.assertRaises(ReleaseError):
                        release_candidate(root, candidate)

    def test_same_key_changed_immutable_request_fields_conflict(self) -> None:
        first = self.candidate()
        release_candidate(self.root, first)
        variants = (
            {"media_type": "text/markdown"},
            {"title": "Changed title"},
        )
        for index, changes in enumerate(variants):
            with self.subTest(changes=changes):
                changed = self.candidate(
                    media_type=changes.get("media_type", first.media_type),
                    title=changes.get("title", first.title),
                    candidate_id=first.candidate_id,
                    idempotency_key=first.idempotency_key,
                )
                with self.assertRaisesRegex(ReleaseError, "idempotency|request"):
                    release_candidate(self.root, changed)
        alternate = self.candidate(
            name="same-body-different-path.bin",
            directory=self.root / "alternate-candidate",
            owner_root=self.root,
            candidate_id=first.candidate_id,
            idempotency_key=first.idempotency_key,
        )
        alternate_owner = json.loads(alternate.owner_path.read_text(encoding="utf-8"))
        alternate = Candidate(
            path=self.root / "alternate-candidate" / "candidate.md",
            owner_path=self.root / "alternate-candidate" / "owner.json",
            candidate_id=alternate.candidate_id,
            media_type=alternate.media_type,
            title=alternate.title,
            content_sha256=alternate.content_sha256,
            idempotency_key=alternate.idempotency_key,
            security=alternate.security,
        )
        alternate.path.parent.mkdir(parents=True)
        alternate.path.write_bytes(first.path.read_bytes())
        alternate_owner.update({"schema": "kb2-candidate-owner/v0.2", "content_path": "alternate-candidate/candidate.md"})
        alternate.owner_path.write_text(
            json.dumps(alternate_owner, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ReleaseError, "candidate owner bundle|path"):
            release_candidate(self.root, alternate)

    def test_duplicate_bundle_and_directory_metadata_mismatch_fail_closed(self) -> None:
        first = self.candidate()
        result = release_candidate(self.root, first)
        bundle = self.root / result.bundle_path
        duplicate = self.root / "released" / "artifacts" / "ART-COPY" / "revision-1"
        duplicate.parent.mkdir(parents=True)
        shutil.copytree(bundle, duplicate)
        with self.assertRaisesRegex(ReleaseError, "duplicate|ambiguous|bundle|artifact"):
            release_candidate(self.root, first)

        with tempfile.TemporaryDirectory(prefix="kb2-release-bundle-name-", dir=r"D:\tmp") as root_name:
            root = Path(root_name)
            (root / "kb.yaml").write_text(
                "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                encoding="utf-8",
            )
            candidate = self.candidate(
                directory=root / "candidates", owner_root=root, candidate_id="CAND-BUNDLE", idempotency_key="idem-bundle"
            )
            fresh = release_candidate(root, candidate)
            source_bundle = root / fresh.bundle_path
            mismatch = root / "released" / "artifacts" / "ART-DIR-NAME" / "revision-1"
            mismatch.parent.mkdir(parents=True)
            shutil.copytree(source_bundle, mismatch)
            with self.assertRaisesRegex(ReleaseError, "bundle|artifact|duplicate"):
                release_candidate(root, candidate)

    @unittest.skipUnless(os.environ.get("KB2_RELEASE_STRESS") == "1", "opt-in stress")
    def test_lock_init_window_converges_24_callers(self) -> None:
        original_open = Path.open
        entered = threading.Event()
        proceed = threading.Event()
        candidate = self.candidate()

        def delayed_open(path: Path, *args: object, **kwargs: object):
            if path.name == ".release.lock" or (path.parent.name == ".release.lock" and (path.name == "owner.json" or path.name.startswith(".owner-"))):
                entered.set()
                proceed.wait(5)
            return original_open(path, *args, **kwargs)

        results: list[object] = []
        errors: list[BaseException] = []

        def publish() -> None:
            try:
                results.append(release_candidate(self.root, candidate, lock_timeout=10))
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(Path, "open", autospec=True, side_effect=delayed_open):
            threads = [threading.Thread(target=publish) for _ in range(24)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(2))
            proceed.set()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({item.revision_id for item in results}, {results[0].revision_id})
        self.assertEqual({item.receipt_id for item in results}, {results[0].receipt_id})

    def test_lock_create_failure_never_deletes_inserted_foreign_owner(self) -> None:
        original_open = Path.open
        candidate = self.candidate()

        def foreign_failure(path: Path, *args: object, **kwargs: object):
            if path.name == ".release.lock" or (path.parent.name == ".release.lock" and (path.name == "owner.json" or path.name.startswith(".owner-"))):
                foreign = {"schema": "kb2-release-lock/v0.1", "owner": "foreign-owner", "token": "foreign"}
                if path.name.startswith(".owner-"):
                    with original_open(path.parent / "owner.json", "w", encoding="utf-8") as handle:
                        handle.write(json.dumps(foreign))
                elif path.name == "owner.json":
                    with original_open(path, "w", encoding="utf-8") as handle:
                        handle.write(json.dumps(foreign))
                else:
                    with original_open(path, "w", encoding="utf-8") as handle:
                        handle.write(json.dumps(foreign))
                raise OSError("injected create failure")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=foreign_failure):
            with self.assertRaises(ReleaseError):
                release_candidate(self.root, candidate)
        lock_path = self.root / "released" / ".release.lock"
        self.assertTrue(lock_path.exists())
        if lock_path.is_dir():
            self.assertTrue((lock_path / "owner.json").exists())
        with self.assertRaisesRegex(ReleaseError, "foreign|lock"):
            release_candidate(self.root, candidate, lock_timeout=0)

    @unittest.skipUnless(os.environ.get("KB2_RELEASE_STRESS") == "1", "opt-in stress")
    def test_24_way_same_key_converges_for_three_rounds_and_different_keys_do_not_interfere(self) -> None:
        for round_number in range(3):
            with tempfile.TemporaryDirectory(prefix="kb2-release-24way-", dir=r"D:\tmp") as root_name:
                root = Path(root_name)
                (root / "kb.yaml").write_text(
                    "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                    encoding="utf-8",
                )
                candidate = self.candidate(
                    directory=root / "candidate-a", owner_root=root, candidate_id=f"CAND-24-{round_number}", idempotency_key=f"idem-24-{round_number}"
                )
                results: list[object] = []
                errors: list[BaseException] = []

                def publish() -> None:
                    try:
                        results.append(release_candidate(root, candidate, lock_timeout=10))
                    except BaseException as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=publish) for _ in range(24)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                with self.subTest(round=round_number):
                    self.assertEqual(errors, [])
                    self.assertEqual({item.revision_id for item in results}, {results[0].revision_id})
                    self.assertEqual({item.receipt_id for item in results}, {results[0].receipt_id})
                    self.assertEqual(len(list((root / "released" / "artifacts").rglob("receipt.json"))), 1)

    def test_different_requests_are_serialized_by_single_writer(self) -> None:
        first = self.candidate(idempotency_key="idem-a", candidate_id="CAND-A")
        second_dir = self.root / "second-candidate"
        second = self.candidate(
            name="candidate-b.bin",
            content=b"second",
            candidate_id="CAND-B",
            idempotency_key="idem-b",
            directory=second_dir,
        )
        entered = threading.Event()
        release = threading.Event()

        def hold() -> None:
            def before() -> None:
                entered.set()
                release.wait(2)

            release_candidate(self.root, first, _before_commit=before)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(entered.wait(2))
        with self.assertRaisesRegex(ReleaseError, "lock"):
            release_candidate(self.root, second, lock_timeout=0.05)
        release.set()
        thread.join()

    def test_precommit_disk_failure_leaves_no_visible_release(self) -> None:
        candidate = self.candidate()
        with self.assertRaisesRegex(ReleaseError, "pre-commit"):
            release_candidate(self.root, candidate, _fail_before_promotion=True)
        self.assertFalse((self.root / "released" / "artifacts").exists())
        self.assertEqual(list((self.root / "released").glob(".release-staging-*")), [])

    def test_postpromotion_interruption_recovers_same_revision(self) -> None:
        candidate = self.candidate()
        with self.assertRaisesRegex(ReleaseError, "interrupted"):
            release_candidate(self.root, candidate, _fail_after_promotion=True)
        bundle_count = len(list((self.root / "released" / "artifacts").rglob("receipt.json")))
        self.assertEqual(bundle_count, 1)
        recovered = release_candidate(self.root, candidate)
        self.assertTrue(recovered.recovered)
        self.assertEqual(len(list((self.root / "released" / "artifacts").rglob("receipt.json"))), 1)

    def test_projection_exception_is_distinct_and_retry_is_idempotent(self) -> None:
        candidate = self.candidate()

        def fail_projection(_: object) -> None:
            raise RuntimeError("projection failed")

        result = release_candidate(self.root, candidate, projection=fail_projection)
        self.assertTrue(result.release_committed)
        self.assertFalse(result.projection_ok)
        self.assertEqual(result.release_code, "RELEASE_COMMITTED")
        retry = release_candidate(self.root, candidate)
        self.assertEqual(retry.revision_id, result.revision_id)
        self.assertEqual(len(list((self.root / "released" / "artifacts").rglob("receipt.json"))), 1)

    def test_released_tamper_and_partial_bundle_fail_closed(self) -> None:
        candidate = self.candidate()
        result = release_candidate(self.root, candidate)
        bundle = self.root / result.bundle_path
        (bundle / "artifact.bin").write_bytes(b"tampered")
        with self.assertRaisesRegex(ReleaseError, "tamper"):
            release_candidate(self.root, candidate)

        with tempfile.TemporaryDirectory(prefix="kb2-release-metadata-tamper-", dir=r"D:\tmp") as metadata_name:
            metadata_root = Path(metadata_name)
            (metadata_root / "kb.yaml").write_text(
                "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                encoding="utf-8",
            )
            metadata_candidate = self.candidate(
                directory=metadata_root / "candidates", owner_root=metadata_root, candidate_id="CAND-META", idempotency_key="idem-meta"
            )
            metadata_result = release_candidate(metadata_root, metadata_candidate)
            metadata_bundle = metadata_root / metadata_result.bundle_path
            revision = json.loads((metadata_bundle / "revision.json").read_text(encoding="utf-8"))
            revision["title"] = "tampered metadata"
            (metadata_bundle / "revision.json").write_text(json.dumps(revision), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseError, "partial|bind"):
                release_candidate(metadata_root, metadata_candidate)

        with tempfile.TemporaryDirectory(prefix="kb2-release-receipt-tamper-", dir=r"D:\tmp") as receipt_name:
            receipt_root = Path(receipt_name)
            (receipt_root / "kb.yaml").write_text(
                "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                encoding="utf-8",
            )
            receipt_candidate = self.candidate(
                directory=receipt_root / "candidates", owner_root=receipt_root, candidate_id="CAND-RECEIPT", idempotency_key="idem-receipt"
            )
            receipt_result = release_candidate(receipt_root, receipt_candidate)
            receipt_bundle = receipt_root / receipt_result.bundle_path
            receipt = json.loads((receipt_bundle / "receipt.json").read_text(encoding="utf-8"))
            receipt["title"] = "tampered receipt"
            (receipt_bundle / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseError, "partial|bind"):
                release_candidate(receipt_root, receipt_candidate)

        with tempfile.TemporaryDirectory(prefix="kb2-release-partial-", dir=r"D:\tmp") as partial_name:
            partial_root = Path(partial_name)
            (partial_root / "kb.yaml").write_text(
                "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                encoding="utf-8",
            )
            partial_candidate = self.candidate(
                name="other.bin",
                content=b"other",
                candidate_id="CAND-OTHER",
                idempotency_key="idem-other",
                directory=partial_root / "candidates",
                owner_root=partial_root,
            )
            partial = partial_root / "released" / "artifacts" / "ART-CAND-PARTIAL" / "revision-1"
            partial.mkdir(parents=True)
            (partial / "revision.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseError, "partial"):
                release_candidate(partial_root, partial_candidate)

    def test_reparse_foreign_lock_and_pointer_are_rejected(self) -> None:
        candidate = self.candidate()
        outside = Path(tempfile.mkdtemp(prefix="kb2-release-outside-", dir=r"D:\tmp"))
        try:
            foreign_source = outside / "candidate.bin"
            foreign_source.write_bytes(b"foreign")
            foreign_owner = outside / "owner.json"
            foreign_owner.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseError, "root"):
                release_candidate(
                    self.root,
                    Candidate(
                        path=foreign_source,
                        owner_path=foreign_owner,
                        candidate_id=candidate.candidate_id,
                        media_type=candidate.media_type,
                        title=candidate.title,
                        content_sha256=hashlib.sha256(b"foreign").hexdigest(),
                        idempotency_key="foreign",
                    ),
                )
        finally:
            shutil.rmtree(outside)

        release_dir = self.root / "released"
        release_dir.mkdir()
        (release_dir / ".release.lock").write_text(
            json.dumps({"schema": "foreign-lock", "owner": "other"}), encoding="utf-8"
        )
        with self.assertRaisesRegex(ReleaseError, "lock"):
            release_candidate(self.root, candidate, lock_timeout=0)
        (release_dir / ".release.lock").unlink()
        pointer = release_dir / "idempotency" / "idem-01.json"
        pointer.parent.mkdir()
        pointer.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "pointer"):
            release_candidate(self.root, candidate)

    def test_pointer_binding_tamper_fails_closed(self) -> None:
        candidate = self.candidate()
        release_candidate(self.root, candidate)
        pointer = self.root / "released" / "idempotency" / f"{candidate.idempotency_key}.json"
        value = json.loads(pointer.read_text(encoding="utf-8"))
        value["bundle_path"] = "released/artifacts/ART-FOREIGN/revision-1"
        pointer.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ReleaseError, "pointer"):
            release_candidate(self.root, candidate)

    @unittest.skipUnless(os.name == "nt", "Windows junction assertion")
    def test_reparse_candidate_lock_and_pointer_are_rejected(self) -> None:
        candidate = self.candidate()
        outside = Path(tempfile.mkdtemp(prefix="kb2-release-reparse-outside-", dir=r"D:\tmp"))
        try:
            link = self.root / "reparse-candidates"
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if made.returncode != 0:
                self.skipTest("junction creation is unavailable")
            foreign_source = link / "candidate.bin"
            foreign_owner = link / "owner.json"
            with self.assertRaisesRegex(ReleaseError, "plain directory|reparse|path"):
                release_candidate(
                    self.root,
                    Candidate(
                        path=foreign_source,
                        owner_path=foreign_owner,
                        candidate_id=candidate.candidate_id,
                        media_type=candidate.media_type,
                        title=candidate.title,
                        content_sha256=candidate.content_sha256,
                        idempotency_key="reparse-candidate",
                    ),
                )
        finally:
            if (self.root / "reparse-candidates").exists():
                (self.root / "reparse-candidates").rmdir()
            shutil.rmtree(outside)

        release_dir = self.root / "released"
        release_dir.mkdir()
        lock_target = Path(tempfile.mkdtemp(prefix="kb2-release-lock-target-", dir=r"D:\tmp"))
        try:
            lock_link = release_dir / ".release.lock"
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(lock_link), str(lock_target)], capture_output=True)
            if made.returncode != 0:
                self.skipTest("junction creation is unavailable")
            with self.assertRaisesRegex(ReleaseError, "lock|plain"):
                release_candidate(self.root, candidate, lock_timeout=0)
        finally:
            if (release_dir / ".release.lock").exists():
                (release_dir / ".release.lock").rmdir()
            shutil.rmtree(lock_target)

        pointer_target = Path(tempfile.mkdtemp(prefix="kb2-release-pointer-target-", dir=r"D:\tmp"))
        try:
            pointer_dir = release_dir / "idempotency"
            pointer_dir.mkdir(exist_ok=True)
            pointer_link = pointer_dir / "reparse-pointer.json"
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(pointer_link), str(pointer_target)], capture_output=True)
            if made.returncode != 0:
                self.skipTest("junction creation is unavailable")
            with self.assertRaisesRegex(ReleaseError, "pointer|reparse"):
                release_candidate(self.root, candidate)
        finally:
            if (release_dir / "idempotency" / "reparse-pointer.json").exists():
                (release_dir / "idempotency" / "reparse-pointer.json").rmdir()
            shutil.rmtree(pointer_target)

    def test_secret_and_restricted_candidates_are_refused(self) -> None:
        restricted = self.candidate(security="restricted")
        with self.assertRaisesRegex(ReleaseError, "security"):
            release_candidate(self.root, restricted)
        secret = self.candidate(content=_EXAMPLE_AWS_SECRET + b"\n", candidate_id="CAND-SECRET")
        with self.assertRaisesRegex(ReleaseError, "security"):
            release_candidate(self.root, secret)
        self.assertFalse((self.root / "released").exists())

    def test_private_key_secret_like_is_refused_before_release_writes(self) -> None:
        candidate = self.candidate(
            content=_EXAMPLE_PRIVATE_KEY_HEADER + b"\nnot-released\n",
            candidate_id="CAND-PRIVATE-KEY",
            idempotency_key="idem-private-key",
        )
        with self.assertRaisesRegex(ReleaseError, "security"):
            release_candidate(self.root, candidate)
        self.assertFalse((self.root / "released").exists())

    def test_non_text_and_invalid_utf8_are_refused_before_release_writes(self) -> None:
        cases = (
            ("binary-mime", "application/octet-stream", b"\x89PNG\x00binary"),
            ("invalid-utf8", "text/plain", b"invalid-utf8-\xff"),
        )
        for name, media_type, content in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory(prefix="kb2-release-text-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    candidate = self.candidate(
                        name="payload.bin",
                        content=content,
                        media_type=media_type,
                        candidate_id=f"CAND-TEXT-{name}",
                        idempotency_key=f"idem-text-{name}",
                        directory=root / "candidates",
                        owner_root=root,
                    )
                    with self.assertRaisesRegex(ReleaseError, "text|UTF-8"):
                        release_candidate(root, candidate)
                    self.assertFalse((root / "released").exists())

    def test_new_candidate_does_not_overwrite_old_revision(self) -> None:
        first = self.candidate()
        first_result = release_candidate(self.root, first)
        second = self.candidate(
            name="new.bin", content=b"new revision candidate", candidate_id="CAND-NEW", idempotency_key="idem-new"
        )
        second_result = release_candidate(self.root, second)
        self.assertNotEqual(first_result.revision_id, second_result.revision_id)
        self.assertEqual((self.root / first_result.bundle_path / "artifact.bin").read_bytes(), first.path.read_bytes())
        self.assertEqual(len(list((self.root / "released" / "artifacts").rglob("receipt.json"))), 2)

    def test_staging_is_same_volume_and_no_half_visible_bundle(self) -> None:
        candidate = self.candidate()
        observed: dict[str, str] = {}

        def before() -> None:
            staging = list((self.root / "released").glob(".release-staging-*/"))
            self.assertEqual(len(staging), 1)
            observed["volume"] = staging[0].drive
            observed["root"] = str(staging[0])

        release_candidate(self.root, candidate, _before_commit=before)
        self.assertEqual(observed["volume"], self.root.drive)
        self.assertFalse(Path(observed["root"]).exists())
        self.assertEqual(list((self.root / "released").glob(".release-staging-*")), [])
        for bundle in (self.root / "released" / "artifacts").glob("*/revision-1"):
            self.assertEqual({path.name for path in bundle.iterdir()}, {"artifact.bin", "revision.json", "receipt.json"})


if __name__ == "__main__":
    unittest.main()
