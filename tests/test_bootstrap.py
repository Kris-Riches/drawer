import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import kb2.bootstrap as bootstrap
import kb2.core as core
import kb2.context as context_core
from kb2.release import Candidate, release_candidate
from kb2.result import KbError


_EXAMPLE_OPENAI_SECRET = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
_EXAMPLE_MALFORMED_SECRET = "sk-" + "raw-malformed-secret-1234567890"


class BootstrapSliceTests(unittest.TestCase):
    def setUp(self) -> None:
        Path(r"D:\tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kb2-bootstrap-root-", dir=r"D:\tmp")
        self.root = Path(self.temp.name)
        (self.root / "kb.yaml").write_text(
            "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            encoding="utf-8",
        )
        (self.root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_anchor(self, root: Path) -> None:
        (root / "kb.yaml").write_text(
            "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            encoding="utf-8",
        )
        (root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")

    def _home_frontmatter(self, path: Path) -> dict[str, str]:
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "---")
        end = lines.index("---", 1)
        values: dict[str, str] = {}
        for line in lines[1:end]:
            key, value = line.split(": ", 1)
            values[key] = value
        return values

    def _publish_artifact(
        self,
        *,
        title: str = "Released Artifact Title",
        body: str = "ARTIFACT_BODY_SENTINEL",
        candidate_id: str = "CAND-PROJECTION",
        idempotency_key: str = "idem-projection",
    ) -> tuple[object, bytes]:
        created = core.ingest_text(self.root, f"{title}\n{body}\n")
        garden_ref = str(created["garden_ref"])
        capture_ref = str(created["capture_ref"])
        garden_path = self.root / "garden" / "notes" / Path(garden_ref).name
        content = garden_path.read_bytes()
        candidate_dir = self.root / "governance" / "release-candidates" / candidate_id
        candidate_dir.mkdir(parents=True)
        candidate_path = candidate_dir / "candidate.md"
        candidate_path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        owner_path = candidate_dir / "owner.json"
        owner_path.write_text(
            json.dumps(
                {
                    "schema": "kb2-candidate-owner/v0.1",
                    "owner": "candidate-owner/v0.1",
                    "candidate_id": candidate_id,
                    "content_path": candidate_path.relative_to(self.root).as_posix(),
                    "content_sha256": digest,
                    "media_type": "text/plain",
                    "title": title,
                    "security": "public",
                    "source_capture_ref": capture_ref,
                    "source_garden_ref": garden_ref,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        candidate = Candidate(
            path=candidate_path,
            owner_path=owner_path,
            candidate_id=candidate_id,
            media_type="text/plain",
            title=title,
            content_sha256=digest,
            idempotency_key=idempotency_key,
        )
        return release_candidate(self.root, candidate), content

    def test_empty_library_builds_minimal_generation(self) -> None:
        result = bootstrap.build(self.root)
        self.assertEqual(result["entry_count"], 0)
        generation = self.root / "generated" / "bootstrap" / result["generation"]
        self.assertTrue((generation / "registry.jsonl").is_file())
        self.assertTrue((generation / "HOME.md").is_file())
        self.assertTrue((generation / "build.json").is_file())
        self.assertEqual((generation / "registry.jsonl").read_text(encoding="utf-8"), "")
        self.assertIn("PROTOCOL.md", (generation / "HOME.md").read_text(encoding="utf-8"))

    def test_released_artifact_is_projected_and_find_home_are_metadata_only(self) -> None:
        published, content = self._publish_artifact()
        built = bootstrap.build(self.root)
        generation = self.root / "generated" / "bootstrap" / built["generation"]
        rows = [json.loads(line) for line in (generation / "registry.jsonl").read_text(encoding="utf-8").splitlines()]
        artifact = next(row for row in rows if row["type"] == "artifact")
        body = content.decode("utf-8")
        self.assertEqual(artifact["uri"], f"artifact://{published.artifact_id}")
        self.assertEqual(artifact["title"], "Released Artifact Title")
        self.assertEqual(artifact["lifecycle"], "released")
        self.assertEqual(artifact["current"]["status"], "current")
        self.assertEqual(artifact["security"], "public")
        self.assertEqual(artifact["revision_id"], published.revision_id)
        self.assertEqual(artifact["receipt_id"], published.receipt_id)
        self.assertNotIn("ARTIFACT_BODY_SENTINEL", json.dumps(artifact, ensure_ascii=False))
        home = (generation / "HOME.md").read_text(encoding="utf-8")
        self.assertIn("## Released Artifacts", home)
        self.assertIn(artifact["uri"], home)
        self.assertIn("Released Artifact Title", home)
        self.assertIn(artifact["canonical_path"], home)
        self.assertNotIn("ARTIFACT_BODY_SENTINEL", home)
        for query in (published.artifact_id, "Released Artifact Title"):
            found = bootstrap.find(self.root, query)["matches"]
            self.assertEqual(found[0]["uri"], artifact["uri"])
        self.assertNotIn(body, json.dumps(artifact, ensure_ascii=False) + home)

    def test_search_contains_verified_public_artifact_body_and_shared_identity(self) -> None:
        published, content = self._publish_artifact(title="Searchable Title", body="公开 Search Body 中文连续子串")
        core.ingest_text(self.root, "Garden-only BODY MUST NOT BE SEARCHED")
        core.ingest_text(self.root, f"secret {_EXAMPLE_OPENAI_SECRET}")
        built = bootstrap.build(self.root)
        generation = self.root / "generated" / "bootstrap" / built["generation"]
        search_path = generation / "search.jsonl"
        self.assertTrue(search_path.is_file())
        search_text = search_path.read_text(encoding="utf-8")
        search_rows = [json.loads(line) for line in search_text.splitlines() if line]
        self.assertEqual([row["uri"] for row in search_rows], [f"artifact://{published.artifact_id}"])
        self.assertEqual(search_rows[0]["content"], content.decode("utf-8"))
        for row in search_rows:
            for field in ("uri", "artifact_id", "title", "content", "content_sha256", "build_id", "canonical_path"):
                self.assertIn(field, row)
            self.assertEqual(row["build_id"], built["build_id"])
            self.assertEqual(row["canonical_path"], built["entries"][0]["canonical_path"])
        registry = (generation / "registry.jsonl").read_text(encoding="utf-8")
        home = (generation / "HOME.md").read_text(encoding="utf-8")
        self.assertNotIn(content.decode("utf-8"), registry + home)
        self.assertNotIn(_EXAMPLE_OPENAI_SECRET, search_text + registry + home)

    def test_find_searches_id_uri_title_chinese_substring_and_english_terms(self) -> None:
        published, _ = self._publish_artifact(
            title="中文发布标题 Alpha",
            body="Public Search English Phrase with 中文连续子串",
        )
        bootstrap.build(self.root)
        exact = bootstrap.find(self.root, published.artifact_id)["matches"]
        self.assertEqual(exact[0]["uri"], f"artifact://{published.artifact_id}")
        self.assertEqual(exact[0]["matched_field"], "artifact_id")
        self.assertEqual(exact[0]["score"], 1000)
        self.assertEqual(bootstrap.find(self.root, f"artifact://{published.artifact_id}")["matches"][0]["uri"], exact[0]["uri"])
        chinese = bootstrap.find(self.root, "中文连续子串")["matches"][0]
        self.assertEqual(chinese["uri"], exact[0]["uri"])
        self.assertEqual(chinese["matched_field"], "body")
        self.assertEqual(chinese["build_id"], exact[0]["build_id"])
        for query in ("search english", "SEARCH PHRASE", "English Phrase"):
            self.assertEqual(bootstrap.find(self.root, query)["matches"][0]["uri"], exact[0]["uri"])
        self.assertEqual(bootstrap.find(self.root, "中文发布标题")["matches"][0]["uri"], exact[0]["uri"])

    def test_search_ranking_prefers_title_and_exact_artifact_id_is_top_one(self) -> None:
        title_hit, _ = self._publish_artifact(
            title="Priority Signal",
            body="unrelated title-hit body",
            candidate_id="CAND-TITLEHIT",
            idempotency_key="idem-titlehit",
        )
        body_hit, _ = self._publish_artifact(
            title="Other Document",
            body="Priority Signal appears in the body",
            candidate_id="CAND-BODYHIT",
            idempotency_key="idem-bodyhit",
        )
        bootstrap.build(self.root)
        ranked = bootstrap.find(self.root, "Priority Signal")["matches"]
        self.assertGreaterEqual(len(ranked), 2)
        self.assertEqual(ranked[0]["uri"], f"artifact://{title_hit.artifact_id}")
        title_result = next(item for item in ranked if item["uri"] == f"artifact://{title_hit.artifact_id}")
        body_result = next(item for item in ranked if item["uri"] == f"artifact://{body_hit.artifact_id}")
        self.assertEqual(title_result["matched_field"], "title")
        self.assertEqual(body_result["matched_field"], "body")
        self.assertGreater(title_result["score"], body_result["score"])
        exact = bootstrap.find(self.root, body_hit.artifact_id)["matches"]
        self.assertEqual(exact[0]["uri"], f"artifact://{body_hit.artifact_id}")

    def test_find_rejects_stale_projection_and_malformed_or_drifting_search(self) -> None:
        self._publish_artifact()
        bootstrap.build(self.root)
        note = next((self.root / "garden" / "notes").glob("CAP-*.md"))
        note.write_text(note.read_text(encoding="utf-8") + "\nstale source\n", encoding="utf-8")
        with self.assertRaises(KbError):
            bootstrap.find(self.root, "Released Artifact Title")

        note.write_text(note.read_text(encoding="utf-8").replace("\nstale source\n", "\n"), encoding="utf-8")
        rebuilt = bootstrap.build(self.root)
        search_path = self.root / "generated" / "bootstrap" / rebuilt["generation"] / "search.jsonl"
        search_path.write_text("{not-json}\n", encoding="utf-8")
        with self.assertRaises(KbError):
            bootstrap.find(self.root, "Released Artifact Title")

    def test_search_rejects_identity_drift_and_relative_absolute_roots_match(self) -> None:
        published, _ = self._publish_artifact()
        absolute = bootstrap.build(self.root)
        relative = Path(os.path.relpath(self.root, Path.cwd()))
        self.assertEqual(
            bootstrap.find(relative, published.artifact_id)["matches"],
            bootstrap.find(self.root, published.artifact_id)["matches"],
        )
        search_path = self.root / "generated" / "bootstrap" / absolute["generation"] / "search.jsonl"
        row = json.loads(search_path.read_text(encoding="utf-8").splitlines()[0])
        original = dict(row)
        row["content"] += "forged"
        search_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(KbError):
            bootstrap.find(self.root, published.artifact_id)
        search_path.write_text(json.dumps(original) + "\n", encoding="utf-8")
        row = dict(original)
        row["build_id"] = "BLD-01KZPQC53JGD8174JZEEVACPJK"
        search_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaises(KbError):
            bootstrap.find(self.root, published.artifact_id)

    def test_search_and_registry_rows_are_exactly_bound_to_canonical_records(self) -> None:
        published, _ = self._publish_artifact()
        built = bootstrap.build(self.root)
        generation = self.root / "generated" / "bootstrap" / built["generation"]
        search_path = generation / "search.jsonl"
        registry_path = generation / "registry.jsonl"
        original_search = search_path.read_bytes()
        original_registry = registry_path.read_bytes()
        row = json.loads(original_search.decode("utf-8").splitlines()[0])

        forged = dict(row)
        forged["uri"] = "artifact://ART-FORGED"
        forged["artifact_id"] = "ART-FORGED"
        search_path.write_text(
            json.dumps(row, ensure_ascii=False) + "\n" + json.dumps(forged, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(KbError):
            bootstrap.find(self.root, published.artifact_id)

        search_path.write_bytes(original_search + original_search)
        with self.assertRaises(KbError):
            bootstrap.find(self.root, published.artifact_id)

        search_path.write_bytes(b"")
        with self.assertRaises(KbError):
            bootstrap.find(self.root, published.artifact_id)

        search_path.write_bytes(original_search)
        registry_row = json.loads(original_registry.decode("utf-8").splitlines()[0])
        registry_row.pop("canonical_path")
        registry_path.write_text(json.dumps(registry_row, ensure_ascii=False) + "\n", encoding="utf-8")
        with self.assertRaises(KbError) as raised:
            bootstrap.find(self.root, published.artifact_id)
        self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_GENERATION_INVALID")

        search_path.write_bytes(original_search)
        registry_path.write_bytes(original_registry)

    def test_deleted_generation_rebuilds_search_in_same_deterministic_order(self) -> None:
        self._publish_artifact(title="First Search", body="same content", candidate_id="CAND-FIRST", idempotency_key="idem-first")
        self._publish_artifact(title="Second Search", body="same content", candidate_id="CAND-SECOND", idempotency_key="idem-second")
        first = bootstrap.build(self.root)
        first_generation = self.root / "generated" / "bootstrap" / first["generation"]
        first_rows = [json.loads(line) for line in (first_generation / "search.jsonl").read_text(encoding="utf-8").splitlines()]
        shutil.rmtree(self.root / "generated" / "bootstrap")
        second = bootstrap.build(self.root)
        second_generation = self.root / "generated" / "bootstrap" / second["generation"]
        second_rows = [json.loads(line) for line in (second_generation / "search.jsonl").read_text(encoding="utf-8").splitlines()]
        for rows in (first_rows, second_rows):
            for row in rows:
                row.pop("build_id", None)
                row.pop("generated_at", None)
        self.assertEqual(first_rows, second_rows)

    def test_tampered_or_drifting_release_fails_closed_without_projection_repair(self) -> None:
        published, _ = self._publish_artifact()
        first = bootstrap.build(self.root)
        bootstrap_root = self.root / "generated" / "bootstrap"
        current_before = (bootstrap_root / "CURRENT.json").read_bytes()
        generations_before = sorted(path.name for path in (bootstrap_root / "generations").iterdir())
        bundle = self.root / published.bundle_path
        leaves = {name: (bundle / name).read_bytes() for name in ("artifact.bin", "revision.json", "receipt.json")}

        artifact_path = bundle / "artifact.bin"
        artifact_path.write_bytes(b"tampered artifact\n")
        tampered = {name: (bundle / name).read_bytes() for name in leaves}
        with self.assertRaises(KbError) as raised:
            bootstrap.build(self.root)
        self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_RELEASE_INVALID")
        self.assertEqual(current_before, (bootstrap_root / "CURRENT.json").read_bytes())
        self.assertEqual(generations_before, sorted(path.name for path in (bootstrap_root / "generations").iterdir()))
        self.assertEqual(tampered, {name: (bundle / name).read_bytes() for name in tampered})

        artifact_path.write_bytes(leaves["artifact.bin"])
        receipt_path = bundle / "receipt.json"
        original_receipt = receipt_path.read_bytes()

        def drift(_: Path) -> None:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["projection_probe"] = "source-drift"
            receipt_path.write_bytes(core._json_bytes(receipt))

        with self.assertRaises(KbError) as raised:
            bootstrap.build(self.root, before_commit=drift)
        self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_SOURCE_DRIFT")
        self.assertEqual(current_before, (bootstrap_root / "CURRENT.json").read_bytes())
        self.assertEqual(generations_before, sorted(path.name for path in (bootstrap_root / "generations").iterdir()))
        self.assertNotEqual(original_receipt, receipt_path.read_bytes())
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["projection_probe"], "source-drift")

    def test_garden_and_context_registry_are_safe_summaries(self) -> None:
        garden = core.ingest_text(self.root, "一个普通 Garden 条目，用于验证读启动。")
        context = core.ingest_text(self.root, "这个项目需要跨会话持续推进并交接。")
        result = bootstrap.build(self.root)
        generation = self.root / "generated" / "bootstrap" / result["generation"]
        rows = [json.loads(line) for line in (generation / "registry.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({row["type"] for row in rows}, {"garden-note", "context"})
        self.assertEqual({row["uri"] for row in rows}, {garden["garden_ref"], context["context_ref"]})
        for row in rows:
            self.assertIn("canonical_path", row)
            self.assertIn("current", row)
            self.assertNotIn("为什么", json.dumps(row, ensure_ascii=False))
            self.assertNotIn("当前输入未表达", json.dumps(row, ensure_ascii=False))
            self.assertTrue(row["generated"])
            self.assertTrue(row["do-not-edit"])

    def test_secret_and_restricted_content_never_enters_outputs(self) -> None:
        core.ingest_text(self.root, f"secret {_EXAMPLE_OPENAI_SECRET}")
        result = bootstrap.build(self.root)
        generation = self.root / "generated" / "bootstrap" / result["generation"]
        output = "\n".join(
            path.read_text(encoding="utf-8")
            for path in generation.iterdir()
            if path.suffix in {".md", ".json", ".jsonl"}
        )
        self.assertNotIn(_EXAMPLE_OPENAI_SECRET, output)
        self.assertEqual(result["entry_count"], 0)

    def test_deleted_generation_rebuilds_same_source_and_rows(self) -> None:
        core.ingest_text(self.root, "可重建的 Garden 条目。")
        first = bootstrap.build(self.root)
        first_generation = self.root / "generated" / "bootstrap" / first["generation"]
        first_rows = (first_generation / "registry.jsonl").read_text(encoding="utf-8")
        shutil.rmtree(self.root / "generated" / "bootstrap")
        second = bootstrap.build(self.root)
        second_generation = self.root / "generated" / "bootstrap" / second["generation"]
        self.assertNotEqual(first["build_id"], second["build_id"])
        self.assertEqual(first["source_digest"], second["source_digest"])
        def normalize(rows: str) -> list[dict[str, object]]:
            result = []
            for line in rows.splitlines():
                row = json.loads(line)
                row.pop("build_id", None)
                row.pop("generated_at", None)
                result.append(row)
            return result
        self.assertEqual(normalize(first_rows), normalize((second_generation / "registry.jsonl").read_text(encoding="utf-8")))

    def test_source_drift_fails_closed_and_preserves_last_good(self) -> None:
        core.ingest_text(self.root, "先构建一个 last-good Garden 条目。")
        first = bootstrap.build(self.root)
        current_before = (self.root / "generated" / "bootstrap" / "CURRENT.json").read_bytes()
        note = next((self.root / "garden" / "notes").glob("CAP-*.md"))

        def drift(_: Path) -> None:
            note.write_text(note.read_text(encoding="utf-8") + "\n构建中漂移。\n", encoding="utf-8")

        with self.assertRaises(KbError) as raised:
            bootstrap.build(self.root, before_commit=drift)
        self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_SOURCE_DRIFT")
        self.assertEqual(current_before, (self.root / "generated" / "bootstrap" / "CURRENT.json").read_bytes())
        with self.assertRaises(KbError) as raised:
            bootstrap.find(self.root, "先构建一个 last-good Garden 条目")
        self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_PROJECTION_STALE")

    def test_status_recomputes_live_source_after_add_change_and_delete(self) -> None:
        bootstrap.build(self.root)
        self.assertTrue(bootstrap.status(self.root)["fresh"])

        ingested = core.ingest_text(self.root, "status freshness add")
        self.assertFalse(bootstrap.status(self.root)["fresh"])
        note = self.root / "garden" / "notes" / Path(str(ingested["garden_ref"])).name
        note.write_text(note.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        self.assertFalse(bootstrap.status(self.root)["fresh"])
        note.unlink()
        self.assertFalse(bootstrap.status(self.root)["fresh"])

    def test_status_config_identity_drift_is_stale_and_restores_fresh(self) -> None:
        bootstrap.build(self.root)
        original_config_digest = bootstrap._CONFIG_DIGEST
        bootstrap._CONFIG_DIGEST = "sha256:" + ("f" * 64)
        try:
            self.assertFalse(bootstrap.status(self.root)["fresh"])
        finally:
            bootstrap._CONFIG_DIGEST = original_config_digest
        self.assertTrue(bootstrap.status(self.root)["fresh"])

    def test_existing_malformed_current_never_falls_back_to_last_good(self) -> None:
        bootstrap.build(self.root)
        bootstrap_root = self.root / "generated" / "bootstrap"
        current_path = bootstrap_root / "CURRENT.json"
        last_good_path = bootstrap_root / "last-good.json"
        last_good = last_good_path.read_bytes()
        current_path.unlink()
        self.assertTrue(bootstrap.status(self.root)["fresh"])
        current_path.write_bytes(last_good)

        malformed_values = (
            b"{}",
            b"[]",
            b"null",
            f'{{"secret":"{_EXAMPLE_MALFORMED_SECRET}"'.encode("utf-8"),
            b'{"build_id":"not-a-build"}',
        )
        for malformed in malformed_values:
            current_path.write_bytes(malformed)
            with self.assertRaises(KbError) as raised:
                bootstrap.status(self.root)
            self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_GENERATION_INVALID")
            self.assertNotIn(_EXAMPLE_MALFORMED_SECRET, str(raised.exception))

        target = bootstrap_root / "generations" / json.loads(last_good)["build_id"]
        current_path.unlink()
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(current_path), str(target)],
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            try:
                with self.assertRaises(KbError):
                    bootstrap.status(self.root)
            finally:
                os.rmdir(current_path)

    def test_pointer_identity_tampering_is_rejected(self) -> None:
        bootstrap.build(self.root)
        pointer_path = self.root / "generated" / "bootstrap" / "CURRENT.json"
        original = json.loads(pointer_path.read_text(encoding="utf-8"))
        tampered_values = {
            "source_digest": "sha256:" + ("0" * 64),
            "config_digest": "sha256:" + ("1" * 64),
            "generated": False,
            "do-not-edit": False,
            "schema": "kb2-bootstrap-pointer/v9",
        }
        for field, value in tampered_values.items():
            tampered = dict(original)
            tampered[field] = value
            pointer_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(KbError) as raised:
                bootstrap.status(self.root)
            self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_GENERATION_INVALID")
        pointer_path.write_text(json.dumps(original), encoding="utf-8")

    def test_current_promotion_failure_preserves_pointers_and_cleans_generation(self) -> None:
        core.ingest_text(self.root, "promotion baseline")
        first = bootstrap.build(self.root)
        current_path = self.root / "generated" / "bootstrap" / "CURRENT.json"
        last_good_path = self.root / "generated" / "bootstrap" / "last-good.json"
        current_before = current_path.read_bytes()
        last_good_before = last_good_path.read_bytes()
        generations = self.root / "generated" / "bootstrap" / "generations"
        generations_before = sorted(path.name for path in generations.iterdir())
        note = next((self.root / "garden" / "notes").glob("CAP-*.md"))
        note.write_text(note.read_text(encoding="utf-8") + "\nnew source\n", encoding="utf-8")
        original_replace = bootstrap.core._replace_file_after_sync

        def fail_current(path: Path, data: bytes) -> None:
            if Path(path).name == "CURRENT.json":
                raise OSError("injected CURRENT write failure")
            original_replace(path, data)

        with mock.patch.object(bootstrap.core, "_replace_file_after_sync", side_effect=fail_current):
            with self.assertRaises(OSError):
                bootstrap.build(self.root)
        self.assertEqual(current_before, current_path.read_bytes())
        self.assertEqual(last_good_before, last_good_path.read_bytes())
        self.assertEqual(generations_before, sorted(path.name for path in generations.iterdir()))
        with self.assertRaises(KbError) as raised:
            bootstrap.find(self.root, "promotion baseline")
        self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_PROJECTION_STALE")

        with tempfile.TemporaryDirectory(prefix="kb2-bootstrap-first-failure-", dir=r"D:\tmp") as name:
            first_root = Path(name)
            self._write_anchor(first_root)
            with mock.patch.object(bootstrap.core, "_replace_file_after_sync", side_effect=fail_current):
                with self.assertRaises(OSError):
                    bootstrap.build(first_root)
            bootstrap_root = first_root / "generated" / "bootstrap"
            self.assertFalse((bootstrap_root / "CURRENT.json").exists())
            self.assertFalse((bootstrap_root / "last-good.json").exists())
            generations_root = bootstrap_root / "generations"
            self.assertFalse(generations_root.exists() and any(generations_root.iterdir()))

    def test_reparse_fact_directory_is_rejected_before_external_read(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="kb2-bootstrap-outside-", dir=r"D:\tmp"))
        notes = self.root / "garden" / "notes"
        try:
            (outside / "CAP-01KZPQC53JGD8174JZEEVACPJK.md").write_text("external", encoding="utf-8")
            notes.parent.mkdir(parents=True)
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(notes), str(outside)],
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                self.skipTest("junction creation is unavailable")
            with self.assertRaises(KbError) as raised:
                bootstrap.build(self.root)
            self.assertEqual(raised.exception.code, "KB2_REPARSE_REJECTED")
        finally:
            if notes.exists() and getattr(notes.stat(), "st_file_attributes", 0) & 0x400:
                os.rmdir(notes)
            shutil.rmtree(outside, ignore_errors=True)

    def test_build_identity_and_find_use_registry_only(self) -> None:
        core.ingest_text(self.root, "精确命中项。")
        built = bootstrap.build(self.root)
        generation = self.root / "generated" / "bootstrap" / built["generation"]
        build = json.loads((generation / "build.json").read_text(encoding="utf-8"))
        current = json.loads((self.root / "generated" / "bootstrap" / "CURRENT.json").read_text(encoding="utf-8"))
        self.assertEqual(build["build_id"], current["build_id"])
        self.assertEqual(build["source_digest"], current["source_digest"])
        self.assertEqual(build["config_digest"], current["config_digest"])
        self.assertEqual(
            bootstrap.find(self.root, "精确命中项")["matches"][0]["canonical_path"].replace("\\", "/"),
            built["entries"][0]["canonical_path"],
        )
        self.assertEqual(bootstrap.find(self.root, "不存在的项")["matches"], [])
        fake = self.root / "generated" / "bootstrap" / "fake.md"
        fake.write_text("generated canonical fake", encoding="utf-8")
        self.assertEqual(bootstrap.find(self.root, "generated canonical fake")["matches"], [])

    def test_single_current_context_home_has_verified_handoff_binding(self) -> None:
        context = core.ingest_text(self.root, "跨会话持续推进并交接。\nBODY-DO-NOT-COPY-UNIQUE")
        built = bootstrap.build(self.root)
        home = self.root / "generated" / "bootstrap" / built["generation"] / "HOME.md"
        frontmatter = self._home_frontmatter(home)
        context_path = next((self.root / "contexts").glob("CTX-*/CONTEXT.md"))
        self.assertEqual(frontmatter["handoff_schema"], "kb2-handoff/v0.1-stage1")
        self.assertEqual(frontmatter["handoff_protocol_path"], "PROTOCOL.md")
        self.assertEqual(frontmatter["handoff_protocol_sha256"], bootstrap.core._digest((self.root / "PROTOCOL.md").read_bytes()))
        self.assertEqual(frontmatter["handoff_context_uri"], context["context_ref"])
        self.assertEqual(frontmatter["handoff_context_path"], context_path.relative_to(self.root).as_posix())
        self.assertEqual(frontmatter["handoff_context_sha256"], bootstrap.core._digest(context_path.read_bytes()))
        self.assertEqual(frontmatter["handoff_context_count_at_build"], "1")
        self.assertEqual(frontmatter["handoff_selection"], "explicit-single-active-context")
        self.assertEqual(frontmatter["handoff_inputs_verified"], "true")
        self.assertEqual(frontmatter["handoff_verified_scope"], "protocol+selected-context+owner-chain+source+config")
        self.assertEqual(frontmatter["handoff_verified_at"], frontmatter["generated_at"])
        self.assertEqual(frontmatter["handoff_binding_freshness"], "valid-if-bound-files-match")
        self.assertEqual(bootstrap._verify_handoff_binding(self.root, home), {"valid": True})
        home_text = home.read_text(encoding="utf-8")
        self.assertNotIn("BODY-DO-NOT-COPY-UNIQUE", home_text)
        self.assertNotIn("owner.json", home_text)

    def test_handoff_binding_rejects_protocol_and_context_byte_mutation(self) -> None:
        core.ingest_text(self.root, "这个项目需要跨会话持续推进并交接。")
        built = bootstrap.build(self.root)
        home = self.root / "generated" / "bootstrap" / built["generation"] / "HOME.md"
        protocol = self.root / "PROTOCOL.md"
        protocol.write_bytes(protocol.read_bytes() + b"mutation\n")
        with self.assertRaises(KbError) as raised:
            bootstrap._verify_handoff_binding(self.root, home)
        self.assertEqual(raised.exception.code, "KB2_HANDOFF_BINDING_INVALID")

        protocol.write_text("# Protocol\n", encoding="utf-8")
        context_path = next((self.root / "contexts").glob("CTX-*/CONTEXT.md"))
        context_path.write_bytes(context_path.read_bytes() + b"mutation\n")
        with self.assertRaises(KbError) as raised:
            bootstrap._verify_handoff_binding(self.root, home)
        self.assertEqual(raised.exception.code, "KB2_HANDOFF_BINDING_INVALID")

    def test_handoff_binding_rejects_path_uri_and_digest_tampering(self) -> None:
        context = core.ingest_text(self.root, "这个项目需要跨会话持续推进并交接。")
        built = bootstrap.build(self.root)
        home = self.root / "generated" / "bootstrap" / built["generation"] / "HOME.md"
        original = home.read_text(encoding="utf-8")
        tampered = {
            "handoff_protocol_path": "../PROTOCOL.md",
            "handoff_context_path": "contexts/../PROTOCOL.md",
            "handoff_context_uri": "context://CTX-01KZPQC53JGD8174JZEEVACPJK",
            "handoff_context_sha256": "sha256:" + ("0" * 64),
        }
        for field, value in tampered.items():
            lines = original.splitlines()
            for index, line in enumerate(lines):
                if line.startswith(field + ": "):
                    lines[index] = f"{field}: {value}"
                    break
            else:
                lines.insert(lines.index("---", 1), f"{field}: {value}")
            home.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(KbError) as raised:
                bootstrap._verify_handoff_binding(self.root, home)
            self.assertEqual(raised.exception.code, "KB2_HANDOFF_BINDING_INVALID")
        self.assertEqual(context["context_ref"].split("://", 1)[0], "context")

    def test_zero_or_multiple_current_contexts_never_emit_a_valid_binding(self) -> None:
        empty = bootstrap.build(self.root)
        empty_home = self.root / "generated" / "bootstrap" / empty["generation"] / "HOME.md"
        empty_frontmatter = self._home_frontmatter(empty_home)
        self.assertEqual(empty_frontmatter["handoff_selection"], "unavailable-no-binding")
        self.assertEqual(empty_frontmatter["handoff_context_count_at_build"], "0")
        self.assertEqual(empty_frontmatter["handoff_inputs_verified"], "false")
        self.assertNotIn("handoff_context_uri", empty_frontmatter)

        core.ingest_text(self.root, "第一个跨会话 Context。需要持续推进并交接。")
        core.ingest_text(self.root, "第二个跨会话 Context。需要持续推进并交接。")
        multiple = bootstrap.build(self.root)
        multiple_home = self.root / "generated" / "bootstrap" / multiple["generation"] / "HOME.md"
        multiple_frontmatter = self._home_frontmatter(multiple_home)
        self.assertEqual(multiple_frontmatter["handoff_selection"], "unavailable-no-binding")
        self.assertEqual(multiple_frontmatter["handoff_context_count_at_build"], "2")
        self.assertEqual(multiple_frontmatter["handoff_inputs_verified"], "false")
        self.assertNotIn("handoff_context_uri", multiple_frontmatter)
        with self.assertRaises(KbError) as raised:
            bootstrap._verify_handoff_binding(self.root, multiple_home)
        self.assertEqual(raised.exception.code, "KB2_HANDOFF_BINDING_INVALID")

    def test_applied_candidate_digest_tamper_fails_build_before_pointer_promotion(self) -> None:
        created = core.ingest_text(self.root, "跨会话持续推进并交接。")
        context_ref = str(created["context_ref"])
        context_path = next((self.root / "contexts").glob("CTX-*/CONTEXT.md"))
        core.ingest_text(
            self.root,
            "继续推进：产生一个 applied update。",
            context_ref=context_ref,
            base_digest=core._digest(context_path.read_bytes()),
        )
        first = bootstrap.build(self.root)
        bootstrap_root = self.root / "generated" / "bootstrap"
        current_before = (bootstrap_root / "CURRENT.json").read_bytes()
        last_good_before = (bootstrap_root / "last-good.json").read_bytes()
        generations = bootstrap_root / "generations"
        generations_before = sorted(path.name for path in generations.iterdir())

        update_path = next((self.root / "governance" / "context-updates").glob("*/update.json"))
        update = json.loads(update_path.read_text(encoding="utf-8"))
        update["candidate_digest"] = "sha256:" + ("0" * 64)
        update_path.write_bytes(core._json_bytes(update))

        with self.assertRaises(KbError) as raised:
            bootstrap.build(self.root)
        self.assertEqual(raised.exception.code, "KB2_CONTEXT_UPDATE_INVALID")
        self.assertEqual(current_before, (bootstrap_root / "CURRENT.json").read_bytes())
        self.assertEqual(last_good_before, (bootstrap_root / "last-good.json").read_bytes())
        self.assertEqual(generations_before, sorted(path.name for path in generations.iterdir()))
        with self.assertRaises(KbError) as raised:
            bootstrap.find(self.root, first["entries"][0]["uri"])
        self.assertEqual(raised.exception.code, "KB2_CONTEXT_UPDATE_INVALID")

    def test_handoff_verifier_requires_canonical_generated_home_and_identity(self) -> None:
        core.ingest_text(self.root, "跨会话持续推进并交接。")
        built = bootstrap.build(self.root)
        generation = self.root / "generated" / "bootstrap" / built["generation"]
        home = generation / "HOME.md"
        home_bytes = home.read_bytes()

        foreign = self.root / "HOME.md"
        foreign.write_bytes(home_bytes)
        with self.assertRaises(KbError) as raised:
            bootstrap._verify_handoff_binding(self.root, foreign)
        self.assertEqual(raised.exception.code, "KB2_HANDOFF_BINDING_INVALID")

        marker = generation / "HOME-copy.md"
        marker.write_bytes(home_bytes)
        with self.assertRaises(KbError) as raised:
            bootstrap._verify_handoff_binding(self.root, marker)
        self.assertEqual(raised.exception.code, "KB2_HANDOFF_BINDING_INVALID")

        original = home.read_text(encoding="utf-8")
        identity_tampering = {
            "schema": "kb2-home/v9",
            "build_id": "BLD-01KZPQC53JGD8174JZEEVACPJK",
            "source_digest": "not-a-digest",
            "config_digest": "not-a-digest",
            "generated": "false",
            "do-not-edit": "false",
        }
        for field, value in identity_tampering.items():
            lines = [f"{field}: {value}" if line.startswith(field + ": ") else line for line in original.splitlines()]
            home.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(KbError) as raised:
                bootstrap._verify_handoff_binding(self.root, home)
            self.assertEqual(raised.exception.code, "KB2_HANDOFF_BINDING_INVALID")
        home.write_text(original, encoding="utf-8")

    def test_context_frontmatter_id_and_schema_mismatch_fail_owner_contract(self) -> None:
        created = core.ingest_text(self.root, "跨会话持续推进并交接。")
        context_id = str(created["context_ref"]).removeprefix("context://")
        context_path = next((self.root / "contexts").glob("CTX-*/CONTEXT.md"))
        state_dir = self.root / "governance" / "context-state" / context_id
        state_path = state_dir / "state.json"
        base_path = state_dir / "base.md"
        intent_dir = next((self.root / "governance" / "context-intents").iterdir())
        intent_path = intent_dir / "intent.json"
        intent_candidate_path = intent_dir / "candidate.md"
        original_context = context_path.read_bytes()
        original_state = state_path.read_bytes()
        original_base = base_path.read_bytes()
        original_intent = intent_path.read_bytes()
        original_intent_candidate = intent_candidate_path.read_bytes()

        mutations = {
            f"id: {context_id}": "id: CTX-01KZPQC53JGD8174JZEEVACPJK",
            "schema: context-current/v0.1-pilot": "schema: context-current/v9",
        }
        for old, new in mutations.items():
            mutated = original_context.replace(old.encode("utf-8"), new.encode("utf-8"), 1)
            context_path.write_bytes(mutated)
            base_path.write_bytes(mutated)
            intent_candidate_path.write_bytes(mutated)
            intent = json.loads(original_intent.decode("utf-8"))
            intent["candidate_digest"] = core._digest(mutated)
            intent_path.write_bytes(core._json_bytes(intent))
            state = json.loads(original_state.decode("utf-8"))
            state["base_digest"] = core._digest(mutated)
            state_path.write_bytes(core._json_bytes(state))
            with self.assertRaises(KbError) as raised:
                bootstrap.build(self.root)
            self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_OWNER_INVALID")
            context_path.write_bytes(original_context)
            base_path.write_bytes(original_base)
            state_path.write_bytes(original_state)
            intent_path.write_bytes(original_intent)
            intent_candidate_path.write_bytes(original_intent_candidate)

    def test_generated_home_protocol_link_resolves_to_root_protocol(self) -> None:
        built = bootstrap.build(self.root)
        generation = self.root / "generated" / "bootstrap" / built["generation"]
        home = generation / "HOME.md"
        link = next(line for line in home.read_text(encoding="utf-8").splitlines() if line.startswith("- Protocol: "))
        target = link.split("](", 1)[1].removesuffix(")")
        self.assertEqual((home.parent / target).resolve(), (self.root / "PROTOCOL.md").resolve())

    def test_applied_update_metadata_is_in_source_digest_and_restores_fresh(self) -> None:
        created = core.ingest_text(self.root, "跨会话持续推进并交接。")
        context_ref = str(created["context_ref"])
        context_path = next((self.root / "contexts").glob("CTX-*/CONTEXT.md"))
        core.ingest_text(
            self.root,
            "继续推进：产生一个 applied update。",
            context_ref=context_ref,
            base_digest=core._digest(context_path.read_bytes()),
        )
        bootstrap.build(self.root)
        self.assertTrue(bootstrap.status(self.root)["fresh"])
        update_path = next((self.root / "governance" / "context-updates").glob("*/update.json"))
        original = update_path.read_bytes()
        update = json.loads(original.decode("utf-8"))
        update["created_at"] = "2026-08-13T12:34:56+08:00"
        update_path.write_bytes(core._json_bytes(update))
        self.assertFalse(bootstrap.status(self.root)["fresh"])
        update_path.write_bytes(original)
        self.assertTrue(bootstrap.status(self.root)["fresh"])

    def test_chain_owner_leaves_are_source_covered_and_unrelated_files_are_not(self) -> None:
        created = core.ingest_text(self.root, "跨会话持续推进并交接。")
        context_ref = str(created["context_ref"])
        context_path = next((self.root / "contexts").glob("CTX-*/CONTEXT.md"))
        core.ingest_text(
            self.root,
            "继续推进：覆盖 candidate expected claim provenance。",
            context_ref=context_ref,
            base_digest=core._digest(context_path.read_bytes()),
        )
        bootstrap.build(self.root)
        update_path = next((self.root / "governance" / "context-updates").glob("*/update.json"))
        update = json.loads(update_path.read_text(encoding="utf-8"))
        claim_path = self.root / "ingress" / "context-quarantine" / update["claim_id"] / "claim.json"
        intent_path = next((self.root / "governance" / "context-intents").glob("*/intent.json"))
        candidate_path = update_path.parent / "candidate.md"
        expected_path = update_path.parent / "expected.md"
        for path in (candidate_path, expected_path, claim_path):
            original = path.read_bytes()
            path.write_bytes(original + b"\ncovered-leaf-drift\n")
            with self.assertRaises(KbError):
                bootstrap.status(self.root)
            path.write_bytes(original)
            self.assertTrue(bootstrap.status(self.root)["fresh"], path.name)

        original_intent = intent_path.read_bytes()
        intent = json.loads(original_intent.decode("utf-8"))
        intent["created_at"] = "2026-08-13T12:34:58+08:00"
        intent_path.write_bytes(core._json_bytes(intent))
        self.assertFalse(bootstrap.status(self.root)["fresh"])
        intent_path.write_bytes(original_intent)
        self.assertTrue(bootstrap.status(self.root)["fresh"])

        unrelated = self.root / "evidence" / "unrelated.md"
        unrelated.parent.mkdir()
        unrelated.write_text("does not belong to source closure\n", encoding="utf-8")
        self.assertTrue(bootstrap.status(self.root)["fresh"])

    def test_checked_manifest_covers_update_metadata_race_and_preserves_last_good(self) -> None:
        created = core.ingest_text(self.root, "跨会话持续推进并交接。")
        context_ref = str(created["context_ref"])
        context_path = next((self.root / "contexts").glob("CTX-*/CONTEXT.md"))
        core.ingest_text(
            self.root,
            "继续推进：测试 checked manifest race。",
            context_ref=context_ref,
            base_digest=core._digest(context_path.read_bytes()),
        )
        first = bootstrap.build(self.root)
        current_before = (self.root / "generated" / "bootstrap" / "CURRENT.json").read_bytes()
        update_path = next((self.root / "governance" / "context-updates").glob("*/update.json"))
        original = update_path.read_bytes()

        def drift(_: Path) -> None:
            update = json.loads(update_path.read_text(encoding="utf-8"))
            update["created_at"] = "2026-08-13T12:34:57+08:00"
            update_path.write_bytes(core._json_bytes(update))

        with self.assertRaises(KbError) as raised:
            bootstrap.build(self.root, before_commit=drift)
        self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_SOURCE_DRIFT")
        self.assertEqual(current_before, (self.root / "generated" / "bootstrap" / "CURRENT.json").read_bytes())
        with self.assertRaises(KbError) as raised:
            bootstrap.find(self.root, first["entries"][0]["uri"])
        self.assertEqual(raised.exception.code, "KB2_BOOTSTRAP_PROJECTION_STALE")
        update_path.write_bytes(original)

    def test_lifecycle_groups_completed_and_blocks_active_handoff(self) -> None:
        created = core.ingest_text(self.root, "跨会话持续推进并交接。")
        ref = str(created["context_ref"])
        first = bootstrap.build(self.root)
        first_home = (self.root / "generated" / "bootstrap" / first["generation"] / "HOME.md").read_text(encoding="utf-8")
        self.assertIn("handoff_inputs_verified: true", first_home)
        state_path = self.root / "governance" / "context-state" / ref.removeprefix("context://") / "state.json"
        before = bootstrap.status(self.root)
        self.assertTrue(before["fresh"])
        context_core.close_context(self.root, ref, status="completed")
        self.assertFalse(bootstrap.status(self.root)["fresh"])
        rebuilt = bootstrap.build(self.root)
        rebuilt_home = (self.root / "generated" / "bootstrap" / rebuilt["generation"] / "HOME.md").read_text(encoding="utf-8")
        self.assertIn("handoff_inputs_verified: false", rebuilt_home)
        registry = (self.root / "generated" / "bootstrap" / rebuilt["generation"] / "registry.jsonl").read_text(encoding="utf-8")
        row = next(json.loads(line) for line in registry.splitlines() if json.loads(line)["uri"] == ref)
        self.assertEqual(row["lifecycle"], "completed")
        self.assertIn("## Recently Completed", rebuilt_home)
        self.assertIn(ref, rebuilt_home)
        self.assertNotIn("## Active Context\n\n- " + ref, rebuilt_home)
        self.assertTrue(state_path.is_file())


if __name__ == "__main__":
    unittest.main()
