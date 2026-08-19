from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import kb2.core as core
import kb2.context as context_core
import kb2.cli as cli
from kb2.core import KbError, correct, explain, ingest_text, organize


class ContextCurrentStateSliceTests(unittest.TestCase):
    def setUp(self) -> None:
        Path(r"D:\tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kb2-context-root-", dir=r"D:\tmp")
        self.root = Path(self.temp.name)
        (self.root / "kb.yaml").write_text(
            "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create(self, text: str = "这项恢复工作需要跨会话持续推进，并在验证后交接。") -> dict[str, object]:
        result = ingest_text(self.root, text)
        self.assertEqual(result["route"], "context-created")
        return result

    def _context_path(self, ref: str) -> Path:
        context_id = ref.removeprefix("context://")
        matches = list((self.root / "contexts").glob(f"{context_id}-*/CONTEXT.md"))
        self.assertEqual(len(matches), 1)
        return matches[0]

    def _create_applied_chain(self, count: int = 3) -> tuple[str, Path, list[Path]]:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        current = path.read_bytes()
        for index in range(count):
            result = ingest_text(
                self.root,
                f"继续推进：链式更新 {index + 1}。",
                context_ref=ref,
                base_digest=core._digest(current),
            )
            self.assertEqual(result["route"], "context-updated")
            current = path.read_bytes()
        updates = list((self.root / "governance" / "context-updates").glob("*/update.json"))
        self.assertEqual(len(updates), count)
        return ref, path, [item.parent for item in updates]

    def test_explicit_intent_creates_one_minimal_context_while_ordinary_text_stays_garden(self) -> None:
        ordinary = ingest_text(self.root, "记录一个已经验证过的命令技巧，当前会话即可完成。")
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        body = path.read_text(encoding="utf-8")

        self.assertEqual(ordinary["route"], "garden-organized")
        self.assertEqual(created["user_structured_fields"], 0)
        self.assertRegex(ref, r"^context://CTX-[0-9A-HJKMNP-TV-Z]{26}$")
        self.assertRegex(body, r"created_at: .+[+-]\d\d:\d\d")
        self.assertEqual([item.name for item in path.parent.iterdir()], ["CONTEXT.md"])
        for heading in ("## 为什么", "## 现在", "## 下一步", "## 阻塞与等待", "## 最近验证", "## 接手注意"):
            self.assertIn(heading, body)
        next_section = body.split("## 下一步", 1)[1].split("\n## ", 1)[0]
        self.assertLessEqual(len(re.findall(r"(?m)^\d+\. ", next_section)), 3)

    def test_create_failure_keeps_capture_and_exact_replays_share_one_canonical(self) -> None:
        intent = "这个项目需要跨会话恢复、持续推进并形成正式产出。"
        with self.assertRaises(KbError) as raised:
            ingest_text(self.root, intent, fail_after_context_intent=True)
        self.assertEqual(raised.exception.code, "KB2_INJECTED_CONTEXT_INTENT")
        captures = list((self.root / "ingress" / "pending").glob("CAP-*"))
        self.assertEqual(len(captures), 1)
        self.assertEqual((captures[0] / "payload.bin").read_text(encoding="utf-8"), intent)

        first = ingest_text(self.root, intent)
        replay = ingest_text(self.root, intent)
        self.assertEqual(first["context_ref"], replay["context_ref"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(list((self.root / "contexts").glob("CTX-*"))), 1)
        self.assertEqual(len(list((self.root / "contexts").glob("CTX-*/CONTEXT.md"))), 1)

    def test_same_create_intent_concurrent_claim_has_one_winner_and_one_context(self) -> None:
        text = "这个并发任务需要跨会话持续推进并交接。"
        captures = [
            core._capture_bytes(self.root, text.encode("utf-8"), source={"kind": "direct-stdin"})
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)
        original_promote = context_core._write_directory_bundle

        def synchronize_intent(staging: Path, final: Path) -> bool:
            if final.parent.name == "context-intents":
                barrier.wait(timeout=10)
            return original_promote(staging, final)

        results: list[tuple[Path, bool]] = []

        def prepare(item: tuple[str, Path, dict[str, object]]) -> None:
            _, capture_dir, metadata = item
            results.append(
                context_core._prepare_intent(
                    self.root,
                    capture_dir,
                    metadata,
                    text.encode("utf-8"),
                    text,
                )
            )

        with mock.patch.object(context_core, "_write_directory_bundle", side_effect=synchronize_intent):
            threads = [threading.Thread(target=prepare, args=(item,)) for item in captures]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sum(created for _, created in results), 1)
        first = context_core._advance_intent(self.root, results[0][0])
        second = context_core._advance_intent(self.root, results[1][0])
        self.assertEqual(first["context_ref"], second["context_ref"])
        self.assertEqual(len(list((self.root / "contexts").glob("CTX-*/CONTEXT.md"))), 1)

    def test_context_update_requires_base_digest_replays_once_and_conflict_retains_both_sides(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        initial = path.read_bytes()

        with self.assertRaises(KbError) as missing:
            ingest_text(self.root, "继续推进：先完成本地验证。", context_ref=ref)
        self.assertEqual(missing.exception.code, "KB2_BASE_DIGEST_REQUIRED")
        self.assertEqual(path.read_bytes(), initial)

        base = str(explain(self.root, ref)["base_digest"])
        update = "继续推进：先完成本地验证，再整理交接说明。"
        applied = ingest_text(self.root, update, context_ref=ref, base_digest=base)
        applied_bytes = path.read_bytes()
        replay = ingest_text(self.root, update, context_ref=ref, base_digest=base)
        self.assertEqual(applied["route"], "context-updated")
        self.assertEqual(replay["context_ref"], ref)
        self.assertTrue(replay["replayed"])
        self.assertEqual(path.read_bytes(), applied_bytes)
        self.assertEqual(len(list((self.root / "governance" / "context-updates").glob("*/update.json"))), 1)

        current_base = str(explain(self.root, ref)["base_digest"])
        external = applied_bytes + "\n外部 Agent 的并发安全编辑。\n".encode("utf-8")
        path.write_bytes(external)
        with self.assertRaises(KbError) as conflict:
            ingest_text(
                self.root,
                "继续推进：生成另一份候选状态。",
                context_ref=ref,
                base_digest=current_base,
            )
        self.assertEqual(conflict.exception.code, "KB2_CONTEXT_CONFLICT")
        self.assertEqual(path.read_bytes(), external)
        owner = next((self.root / "governance" / "context-conflicts").glob("CCF-*"))
        self.assertIn("另一份候选状态", (owner / "candidate.md").read_text(encoding="utf-8"))
        self.assertEqual((owner / "observed.md").read_bytes(), external)
        first_recovery = core.recover_all(self.root)
        second_recovery = core.recover_all(self.root)
        self.assertTrue(first_recovery["unresolved"])
        self.assertEqual(second_recovery["unresolved"], first_recovery["unresolved"])

    def test_context_direct_edit_override_is_explainable_and_survives_three_organize_runs(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        marker = "用户直接修改：接手前必须先核对现场。"
        path.write_text(path.read_text(encoding="utf-8") + "\n" + marker + "\n", encoding="utf-8")

        outcomes = [organize(self.root, ref) for _ in range(3)]
        detail = explain(self.root, ref)
        override = detail["human_override"]
        self.assertTrue(outcomes[0]["override"]["created"])
        self.assertFalse(outcomes[1]["override"]["created"])
        self.assertFalse(outcomes[2]["override"]["created"])
        self.assertEqual(override["actor"], "human-direct-edit")
        self.assertIn(marker, override["diff"])
        self.assertIn("base digest", override["reason"])
        self.assertIn(marker, path.read_text(encoding="utf-8"))

    def test_context_natural_correction_is_captured_explainable_and_survives_three_runs(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        correction = "纠正：下一步先验证恢复结果，不要先写正式结论。"
        result = correct(self.root, ref, correction)
        outcomes = [organize(self.root, ref) for _ in range(3)]
        detail = explain(self.root, ref)
        capture = self.root / "ingress" / "pending" / result["correction_capture_ref"].removeprefix("capture://")
        metadata = json.loads((capture / "capture.json").read_text(encoding="utf-8"))

        self.assertEqual(metadata["source"], {"kind": "human-correction", "target": ref})
        self.assertEqual(detail["human_override"]["actor"], "human-natural-language-correction")
        self.assertEqual(detail["human_override"]["correction_capture_ref"], result["correction_capture_ref"])
        self.assertIn(correction, detail["human_override"]["diff"])
        self.assertTrue(all(not item["override"]["created"] for item in outcomes))
        self.assertIn(correction, path.read_text(encoding="utf-8"))

    def test_applied_update_chain_recovery_is_noop_and_keeps_active_correction_override(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)

        correction = correct(self.root, ref, "纠正：当前 Context 标题使用持续搭建标签。")
        active_override = str(correction["override"]["ref"])
        current = path.read_bytes()
        for text in (
            "继续推进：完成第一轮校验。",
            "继续推进：完成第二轮校验。",
            "继续推进：完成第三轮校验。",
        ):
            result = ingest_text(
                self.root,
                text,
                context_ref=ref,
                base_digest=core._digest(current),
            )
            self.assertEqual(result["route"], "context-updated")
            current = path.read_bytes()

        recovery = core.recover_all(self.root)
        self.assertEqual(recovery["unresolved"], [])
        detail = explain(self.root, ref)
        self.assertEqual(detail["current_digest"], core._digest(current))
        self.assertEqual(detail["human_override"]["ref"], active_override)
        self.assertEqual(detail["human_override"]["correction_capture_ref"], correction["correction_capture_ref"])

    def test_applied_update_chain_fork_and_gap_fail_closed(self) -> None:
        ref, _, update_dirs = self._create_applied_chain()
        intent_dir = next((self.root / "governance" / "context-intents").iterdir())
        intent_digest = json.loads((intent_dir / "intent.json").read_text(encoding="utf-8"))["candidate_digest"]
        update_records = {
            item: json.loads((item / "update.json").read_text(encoding="utf-8"))
            for item in update_dirs
        }
        first = next(item for item, record in update_records.items() if record["base_digest"] == intent_digest)
        clone = self.root / "governance" / "context-updates" / ("f" * 64)
        shutil.copytree(first, clone)
        duplicate = json.loads((clone / "update.json").read_text(encoding="utf-8"))
        duplicate["operation_key"] = f"sha256:{clone.name}"
        (clone / "update.json").write_bytes(core._json_bytes(duplicate))

        recovery = core.recover_all(self.root)
        self.assertTrue(any(item["code"] == "KB2_CONTEXT_UPDATE_INVALID" for item in recovery["unresolved"]))

        # The same owner evidence remains valid, but changing one predecessor digest creates a gap/orphan.
        shutil.rmtree(clone)
        first_candidate = update_records[first]["candidate_digest"]
        middle = next(item for item, record in update_records.items() if record["base_digest"] == first_candidate)
        update_path = middle / "update.json"
        update = json.loads(update_path.read_text(encoding="utf-8"))
        gap = "sha256:" + ("0" * 64)
        expected = middle / "expected.md"
        claimed = self.root / "ingress" / "context-quarantine" / update["claim_id"] / "claimed.bin"
        expected_bytes = b"gap predecessor\n"
        expected.write_bytes(expected_bytes)
        claimed.write_bytes(expected_bytes)
        claim_path = claimed.parent / "claim.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        update["base_digest"] = gap
        update["expected_digest"] = gap
        update["claimed_digest"] = gap
        claim["expected_digest"] = gap
        claim["claimed_digest"] = gap
        update_path.write_bytes(core._json_bytes(update))
        claim_path.write_bytes(core._json_bytes(claim))

        recovery = core.recover_all(self.root)
        self.assertTrue(any(item["code"] == "KB2_CONTEXT_UPDATE_INVALID" for item in recovery["unresolved"]))
        with self.assertRaises(KbError) as raised:
            explain(self.root, ref)
        self.assertEqual(raised.exception.code, "KB2_RECOVERY_UNRESOLVED")

    def test_applied_update_chain_tampered_candidate_fails_closed(self) -> None:
        _, _, update_dirs = self._create_applied_chain(count=1)
        update_path = update_dirs[0] / "update.json"
        candidate_path = update_path.parent / "candidate.md"
        candidate_path.write_bytes(candidate_path.read_bytes() + b"tampered\n")
        recovery = core.recover_all(self.root)
        self.assertTrue(any(item["code"] == "KB2_CONTEXT_UPDATE_INVALID" for item in recovery["unresolved"]))

    def test_applied_update_chain_tampered_claim_fails_closed(self) -> None:
        _, _, update_dirs = self._create_applied_chain(count=1)
        update = json.loads((update_dirs[0] / "update.json").read_text(encoding="utf-8"))
        claim_path = self.root / "ingress" / "context-quarantine" / update["claim_id"] / "claim.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim["applied_digest"] = "sha256:" + ("0" * 64)
        claim_path.write_bytes(core._json_bytes(claim))
        recovery = core.recover_all(self.root)
        self.assertTrue(any(item["code"] == "KB2_CONTEXT_UPDATE_INVALID" for item in recovery["unresolved"]))

    def test_applied_update_chain_latest_current_mismatch_fails_closed(self) -> None:
        ref, path, _ = self._create_applied_chain(count=1)
        current = path.read_bytes()
        path.write_bytes(current + b"current drift\n")
        recovery = core.recover_all(self.root)
        self.assertTrue(any(item["code"] == "KB2_CONTEXT_UPDATE_INVALID" for item in recovery["unresolved"]))

    def test_applied_update_chain_isolated_per_context(self) -> None:
        first = self._create()
        first_ref = str(first["context_ref"])
        first_path = self._context_path(first_ref)
        first_result = ingest_text(
            self.root,
            "继续推进：第一个 Context 的独立更新。",
            context_ref=first_ref,
            base_digest=core._digest(first_path.read_bytes()),
        )
        self.assertEqual(first_result["route"], "context-updated")

        second = self._create("另一个 Context 需要跨会话持续推进并独立验证。")
        second_ref = str(second["context_ref"])
        second_path = self._context_path(second_ref)
        second_result = ingest_text(
            self.root,
            "继续推进：第二个 Context 的独立更新。",
            context_ref=second_ref,
            base_digest=core._digest(second_path.read_bytes()),
        )
        self.assertEqual(second_result["route"], "context-updated")
        self.assertEqual(core.recover_all(self.root)["unresolved"], [])
        self.assertEqual(explain(self.root, first_ref)["ref"], first_ref)
        self.assertEqual(explain(self.root, second_ref)["ref"], second_ref)

    def test_applied_claim_boundary_recovery_is_idempotent_without_overwrite(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        base = core._digest(path.read_bytes())
        update = "继续推进：验证 applied claim 边界恢复。"
        original_replace = context_core.core._replace_file_after_sync
        interrupted = False

        def interrupt_after_claim(path_arg: Path, content: bytes) -> None:
            nonlocal interrupted
            original_replace(path_arg, content)
            if path_arg.name == "claim.json" and json.loads(content.decode("utf-8")).get("stage") == "applied" and not interrupted:
                interrupted = True
                raise KbError("KB2_TEST_INTERRUPT", "applied claim boundary interruption", 4)

        with mock.patch.object(context_core.core, "_replace_file_after_sync", side_effect=interrupt_after_claim):
            with self.assertRaises(KbError) as raised:
                ingest_text(self.root, update, context_ref=ref, base_digest=base)
        self.assertEqual(raised.exception.code, "KB2_TEST_INTERRUPT")
        before = path.read_bytes()
        override_count = len(list((self.root / "governance" / "overrides").glob("OVR-*.yaml")))
        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertEqual(first["unresolved"], [])
        self.assertEqual(second["unresolved"], [])
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list((self.root / "governance" / "overrides").glob("OVR-*.yaml"))), override_count)
        self.assertEqual(explain(self.root, ref)["current_digest"], core._digest(before))

    def test_secret_like_context_update_race_is_claimed_and_removed_from_ordinary_context(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        safe = path.read_bytes()
        base = str(explain(self.root, ref)["base_digest"])
        marker = b"CONTEXT-RACE-SECRET"
        secret = safe + b"\n" + marker + b" sk-" + (b"Z" * 48) + b"\n"

        def inject() -> None:
            path.write_bytes(secret)

        with self.assertRaises(KbError) as raised:
            ingest_text(
                self.root,
                "继续推进：完成安全更新。",
                context_ref=ref,
                base_digest=base,
                before_context_claim=inject,
            )
        self.assertEqual(raised.exception.code, "KB2_RESTRICTED_EDIT")
        self.assertEqual(path.read_bytes(), safe)
        occurrences = [
            item.relative_to(self.root).as_posix()
            for item in self.root.rglob("*")
            if item.is_file() and marker in item.read_bytes()
        ]
        self.assertEqual(len(occurrences), 1)
        self.assertTrue(occurrences[0].startswith("ingress/context-quarantine/"))
        self.assertEqual(explain(self.root, ref)["security"]["precheck"], "rejected")

    def test_context_installed_update_recovery_finishes_without_second_canonical(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        base = str(explain(self.root, ref)["base_digest"])
        update = "继续推进：验证 installed 阶段恢复。"
        original_mark = context_core._mark_update_capture
        interrupted = False

        def interrupt_once(*args: object, **kwargs: object) -> None:
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KbError("KB2_TEST_INTERRUPT", "installed update interruption", 4)
            original_mark(*args, **kwargs)

        with mock.patch.object(context_core, "_mark_update_capture", side_effect=interrupt_once):
            with self.assertRaises(KbError) as raised:
                ingest_text(self.root, update, context_ref=ref, base_digest=base)
        self.assertEqual(raised.exception.code, "KB2_TEST_INTERRUPT")
        transaction = next((self.root / "governance" / "context-updates").glob("*/update.json"))
        self.assertEqual(json.loads(transaction.read_text(encoding="utf-8"))["stage"], "installed")
        self.assertIn(update, path.read_text(encoding="utf-8"))

        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertEqual(first["unresolved"], [])
        self.assertGreaterEqual(first["recovered"], 1)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(second["unresolved"], [])
        self.assertEqual(len(list((self.root / "contexts").glob("CTX-*/CONTEXT.md"))), 1)
        self.assertEqual(explain(self.root, ref)["route"]["result"], "context-updated")

    def test_context_base_owner_drift_fails_closed_before_update(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        path = self._context_path(ref)
        base = str(explain(self.root, ref)["base_digest"])
        state_dir = self.root / "governance" / "context-state" / ref.removeprefix("context://")
        base_path = state_dir / "base.md"
        original_current = path.read_bytes()
        drifted_base = base_path.read_bytes() + "\n外部修改了 organizer base owner。\n".encode("utf-8")
        base_path.write_bytes(drifted_base)

        with self.assertRaises(KbError) as raised:
            ingest_text(
                self.root,
                "继续推进：不得在 base owner 漂移时提交更新。",
                context_ref=ref,
                base_digest=base,
            )

        self.assertEqual(raised.exception.code, "KB2_CONTEXT_OWNER_INVALID")
        self.assertEqual(path.read_bytes(), original_current)
        self.assertEqual(base_path.read_bytes(), drifted_base)
        self.assertEqual(list((self.root / "governance" / "context-updates").glob("*/update.json")), [])

    def test_context_recovery_rejects_applied_update_with_unapplied_claim(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        base = str(explain(self.root, ref)["base_digest"])
        ingest_text(
            self.root,
            "继续推进：验证 applied owner 的完整恢复链。",
            context_ref=ref,
            base_digest=base,
        )
        update_path = next((self.root / "governance" / "context-updates").glob("*/update.json"))
        update = json.loads(update_path.read_text(encoding="utf-8"))
        claim_path = self.root / "ingress" / "context-quarantine" / update["claim_id"] / "claim.json"
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim["stage"] = "claimed"
        claim.pop("applied_digest", None)
        claim_path.write_bytes(core._json_bytes(claim))

        recovery = core.recover_all(self.root)

        self.assertTrue(
            any(
                item.get("context_update") == update_path.parent.name
                and item.get("code") == "KB2_CONTEXT_UPDATE_INVALID"
                for item in recovery["unresolved"]
            )
        )

    def test_recovery_normalizes_malformed_intent_owner_documents(self) -> None:
        self._create()
        intent_path = next((self.root / "governance" / "context-intents").glob("*/intent.json"))
        context_path = next((self.root / "contexts").glob("CTX-*/CONTEXT.md"))
        original_context = context_path.read_bytes()
        malformed_documents = (
            b'{"schema":',
            b"[]",
            b"null",
            b'"not-an-owner"',
            b"\xff\xfe\xfd",
        )
        for document in malformed_documents:
            with self.subTest(document=document):
                intent_path.write_bytes(document)
                recovery = core.recover_all(self.root)
                self.assertTrue(
                    any(
                        item.get("context_intent") == intent_path.parent.name
                        and item.get("code") == "KB2_CONTEXT_INTENT_INVALID"
                        for item in recovery["unresolved"]
                    )
                )
                self.assertEqual(context_path.read_bytes(), original_context)
                self.assertNotIn(b"not-an-owner", context_path.read_bytes())

    def test_recovery_normalizes_malformed_update_owner_documents(self) -> None:
        _, context_path, update_dirs = self._create_applied_chain(count=1)
        update_path = update_dirs[0] / "update.json"
        original_context = context_path.read_bytes()
        malformed_documents = (
            b'{"schema":',
            b"[]",
            b"null",
            b'"not-an-update"',
            b"\xff\xfe\xfd",
        )
        for document in malformed_documents:
            with self.subTest(document=document):
                update_path.write_bytes(document)
                recovery = core.recover_all(self.root)
                self.assertTrue(
                    any(
                        item.get("context_update") == update_path.parent.name
                        and item.get("code") == "KB2_CONTEXT_UPDATE_INVALID"
                        for item in recovery["unresolved"]
                    )
                )
                self.assertEqual(context_path.read_bytes(), original_context)
                self.assertEqual(update_path.read_bytes(), document)

    def test_cli_malformed_update_owner_is_structured_recovery_error(self) -> None:
        _, _, update_dirs = self._create_applied_chain(count=1)
        update_path = update_dirs[0] / "update.json"
        update_path.write_bytes(b'{"schema":')
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-B", "-m", "kb2.cli", "--root", str(self.root), "--json", "recover"],
            capture_output=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 3)
        envelope = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(envelope["code"], "KB2_RECOVERY_UNRESOLVED")
        self.assertTrue(
            any(
                item.get("context_update") == update_path.parent.name
                and item.get("code") == "KB2_CONTEXT_UPDATE_INVALID"
                for item in envelope["data"]["unresolved"]
            )
        )
        self.assertNotIn(b"JSONDecodeError", result.stdout)
        self.assertNotIn(str(self.root), result.stdout.decode("utf-8"))

    def test_recovery_normalizes_malformed_claim_without_reinterpreting_update(self) -> None:
        _, context_path, update_dirs = self._create_applied_chain(count=1)
        update_path = update_dirs[0] / "update.json"
        update = json.loads(update_path.read_text(encoding="utf-8"))
        claim_path = self.root / "ingress" / "context-quarantine" / update["claim_id"] / "claim.json"
        original_context = context_path.read_bytes()
        claim_path.write_bytes(b'{"schema":')

        recovery = core.recover_all(self.root)

        self.assertTrue(
            any(
                item.get("context_update") == update_path.parent.name
                and item.get("code") == "KB2_CONTEXT_CLAIM_INVALID"
                for item in recovery["unresolved"]
            )
        )
        self.assertEqual(context_path.read_bytes(), original_context)
        self.assertEqual(update["stage"], "applied")

    def test_duplicate_context_update_claims_leave_only_canonical_owner(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        base = str(explain(self.root, ref)["base_digest"])
        text = "继续推进：并发更新只能保留一个 canonical owner。"
        captures = [
            core._capture_bytes(
                self.root,
                text.encode("utf-8"),
                source={"kind": "direct-stdin", "target": ref},
            )
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)
        original_promote = context_core._write_directory_bundle

        def synchronize_update(staging: Path, final: Path) -> bool:
            if final.parent.name == "context-updates":
                barrier.wait(timeout=10)
            return original_promote(staging, final)

        results: list[tuple[Path, bool]] = []

        def prepare(item: tuple[str, Path, dict[str, object]]) -> None:
            _, capture_dir, metadata = item
            results.append(
                context_core._prepare_update(
                    self.root,
                    ref,
                    capture_dir,
                    metadata,
                    text.encode("utf-8"),
                    text,
                    base,
                    correction=False,
                )
            )

        with mock.patch.object(context_core, "_write_directory_bundle", side_effect=synchronize_update):
            threads = [threading.Thread(target=prepare, args=(item,)) for item in captures]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len(list((self.root / "ingress" / "context-quarantine").glob("CCL-*"))), 1)

    def test_context_update_expected_owner_digest_must_match_base_digest(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        base = str(explain(self.root, ref)["base_digest"])
        text = "继续推进：expected owner 必须绑定旧 base digest。"
        _, capture_dir, metadata = core._capture_bytes(
            self.root,
            text.encode("utf-8"),
            source={"kind": "direct-stdin", "target": ref},
        )
        update_dir, _ = context_core._prepare_update(
            self.root,
            ref,
            capture_dir,
            metadata,
            text.encode("utf-8"),
            text,
            base,
            correction=False,
        )
        update_path = update_dir / "update.json"
        update = json.loads(update_path.read_text(encoding="utf-8"))
        expected_path = update_dir / "expected.md"
        expected_path.write_bytes(expected_path.read_bytes() + "\nexpected owner drift\n".encode("utf-8"))
        update["expected_digest"] = core._digest(expected_path.read_bytes())
        update_path.write_bytes(core._json_bytes(update))

        with self.assertRaises(KbError) as raised:
            context_core._advance_update(self.root, update_dir)

        self.assertEqual(raised.exception.code, "KB2_CONTEXT_UPDATE_INVALID")
        self.assertEqual(json.loads(update_path.read_text(encoding="utf-8"))["stage"], "prepared")
        self.assertEqual(self._context_path(ref).read_bytes(), (self.root / "governance" / "context-state" / ref.removeprefix("context://") / "base.md").read_bytes())

        recovery = core.recover_all(self.root)
        self.assertTrue(
            any(
                item.get("context_update") == update_dir.name
                and item.get("code") == "KB2_CONTEXT_UPDATE_INVALID"
                for item in recovery["unresolved"]
            )
        )

    def test_context_secret_reappearance_after_claim_is_restricted_in_normal_and_recovery_paths(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        base = str(explain(self.root, ref)["base_digest"])
        text = "继续推进：处理 claim 后重新出现的 secret。"
        context_path = self._context_path(ref)
        marker = b"CONTEXT-REAPPEARED-SECRET"
        secret = marker + b" sk-" + (b"Z" * 48) + b"\n"
        original_replace = core._replace_file_after_sync
        injected = False

        def inject_after_claim(path: Path, data: bytes) -> None:
            nonlocal injected
            value = json.loads(data.decode("utf-8"))
            if path.name == "update.json" and value.get("stage") == "claimed" and not injected:
                injected = True
                context_path.write_bytes(secret)
            original_replace(path, data)

        with mock.patch.object(core, "_replace_file_after_sync", side_effect=inject_after_claim):
            with self.assertRaises(KbError) as raised:
                ingest_text(self.root, text, context_ref=ref, base_digest=base)

        self.assertEqual(raised.exception.code, "KB2_RESTRICTED_EDIT")
        self.assertEqual(self._context_path(ref).read_bytes(), (self.root / "governance" / "context-state" / ref.removeprefix("context://") / "base.md").read_bytes())
        occurrences = [
            item.relative_to(self.root).as_posix()
            for item in self.root.rglob("*")
            if item.is_file() and marker in item.read_bytes()
        ]
        self.assertEqual(len(occurrences), 1)
        self.assertTrue(occurrences[0].startswith("ingress/context-quarantine/"))

        recovery = core.recover_all(self.root)
        self.assertTrue(any(item.get("code") == "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED" for item in recovery["unresolved"]))

    def test_context_safe_reappearance_after_claim_keeps_both_sides_and_sticky_conflict(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        base = str(explain(self.root, ref)["base_digest"])
        text = "继续推进：处理 claim 后重新出现的安全编辑。"
        context_path = self._context_path(ref)
        safe = "安全的重新出现编辑\n".encode("utf-8")
        original_replace = core._replace_file_after_sync
        injected = False

        def inject_after_claim(path: Path, data: bytes) -> None:
            nonlocal injected
            value = json.loads(data.decode("utf-8"))
            if path.name == "update.json" and value.get("stage") == "claimed" and not injected:
                injected = True
                context_path.write_bytes(safe)
            original_replace(path, data)

        with mock.patch.object(core, "_replace_file_after_sync", side_effect=inject_after_claim):
            with self.assertRaises(KbError) as raised:
                ingest_text(self.root, text, context_ref=ref, base_digest=base)

        self.assertEqual(raised.exception.code, "KB2_CONTEXT_CONFLICT")
        self.assertEqual(self._context_path(ref).read_bytes(), safe)
        conflicts = list((self.root / "governance" / "context-conflicts").glob("CCF-*"))
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual((conflict / "observed.md").read_bytes(), safe)
        self.assertIn(text, (conflict / "candidate.md").read_text(encoding="utf-8"))
        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertTrue(first["unresolved"])
        self.assertEqual(second["unresolved"], first["unresolved"])

    def test_context_second_secret_reappearance_is_claimed_in_normal_and_recovery(self) -> None:
        def run_normal_second_reappearance() -> tuple[Path, bytes, bytes]:
            created = self._create()
            ref = str(created["context_ref"])
            base = str(explain(self.root, ref)["base_digest"])
            context_path = self._context_path(ref)
            first_marker = b"CONTEXT-SECOND-SECRET-ONE"
            second_marker = b"CONTEXT-SECOND-SECRET-TWO"
            first_secret = first_marker + b" sk-" + (b"A" * 48) + b"\n"
            second_secret = second_marker + b" sk-" + (b"B" * 48) + b"\n"
            original_replace = core._replace_file_after_sync
            injected_first = False
            injected_second = False

            def inject_twice(path: Path, data: bytes) -> None:
                nonlocal injected_first, injected_second
                value = json.loads(data.decode("utf-8"))
                if path.name == "update.json" and value.get("stage") == "claimed" and not injected_first:
                    injected_first = True
                    context_path.write_bytes(first_secret)
                elif path.name == "claim.json" and value.get("reappeared_entry") == "reappeared.bin" and not injected_second:
                    injected_second = True
                    context_path.write_bytes(second_secret)
                original_replace(path, data)

            with mock.patch.object(core, "_replace_file_after_sync", side_effect=inject_twice):
                with self.assertRaises(KbError) as raised:
                    ingest_text(self.root, "继续推进：验证第二次 secret reappearance。", context_ref=ref, base_digest=base)
            self.assertEqual(raised.exception.code, "KB2_RESTRICTED_EDIT")
            self.assertFalse(core._secret_reasons(context_path.read_bytes()))
            occurrences = [
                item.relative_to(self.root).as_posix()
                for item in self.root.rglob("*")
                if item.is_file() and (first_marker in item.read_bytes() or second_marker in item.read_bytes())
            ]
            self.assertEqual(len(occurrences), 2)
            self.assertTrue(all(item.startswith("ingress/context-quarantine/") for item in occurrences))
            return context_path, first_secret, second_secret

        context_path, first_secret, second_secret = run_normal_second_reappearance()
        self.assertNotEqual(first_secret, second_secret)
        self.assertFalse(core._secret_reasons(context_path.read_bytes()))

        self.temp.cleanup()
        Path(r"D:\tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kb2-context-recovery-reappearance-", dir=r"D:\tmp")
        self.root = Path(self.temp.name)
        (self.root / "kb.yaml").write_text(
            "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            encoding="utf-8",
        )
        created = self._create()
        ref = str(created["context_ref"])
        base = str(explain(self.root, ref)["base_digest"])
        context_path = self._context_path(ref)
        first_marker = b"CONTEXT-RECOVERY-SECRET-ONE"
        second_marker = b"CONTEXT-RECOVERY-SECRET-TWO"
        first_secret = first_marker + b" sk-" + (b"C" * 48) + b"\n"
        second_secret = second_marker + b" sk-" + (b"D" * 48) + b"\n"
        original_replace = core._replace_file_after_sync
        injected = False

        def inject_first(path: Path, data: bytes) -> None:
            nonlocal injected
            value = json.loads(data.decode("utf-8"))
            if path.name == "update.json" and value.get("stage") == "claimed" and not injected:
                injected = True
                context_path.write_bytes(first_secret)
            original_replace(path, data)

        with mock.patch.object(core, "_replace_file_after_sync", side_effect=inject_first):
            with self.assertRaises(KbError):
                ingest_text(self.root, "继续推进：为 recovery 准备第一份 secret。", context_ref=ref, base_digest=base)
        self.assertFalse(core._secret_reasons(context_path.read_bytes()))
        context_path.write_bytes(second_secret)

        recovery = core.recover_all(self.root)

        self.assertTrue(any(item.get("code") == "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED" for item in recovery["unresolved"]))
        self.assertFalse(core._secret_reasons(context_path.read_bytes()))
        occurrences = [
            item.relative_to(self.root).as_posix()
            for item in self.root.rglob("*")
            if item.is_file() and (first_marker in item.read_bytes() or second_marker in item.read_bytes())
        ]
        self.assertEqual(len(occurrences), 2)
        self.assertTrue(all(item.startswith("ingress/context-quarantine/") for item in occurrences))

    def test_context_bounded_reappearance_normal_claims_latest_before_unresolved(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        base = str(explain(self.root, ref)["base_digest"])
        context_path = self._context_path(ref)
        expected = context_path.read_bytes()
        secrets = [
            f"CONTEXT-BOUND-NORMAL-{index}".encode("ascii") + b" sk-" + (bytes([65 + index]) * 48) + b"\n"
            for index in range(10)
        ]
        original_replace = core._replace_file_after_sync
        original_install = core._install_bytes_to_absent
        injected = 0

        def inject_after_claim(path: Path, data: bytes) -> None:
            nonlocal injected
            value = json.loads(data.decode("utf-8"))
            if path.name == "update.json" and value.get("stage") == "claimed" and injected == 0:
                context_path.write_bytes(secrets[injected])
                injected += 1
            original_replace(path, data)

        def inject_after_safe_install(path: Path, content: bytes) -> None:
            nonlocal injected
            original_install(path, content)
            if path == context_path and content == expected and injected < len(secrets):
                context_path.write_bytes(secrets[injected])
                injected += 1

        with mock.patch.object(core, "_replace_file_after_sync", side_effect=inject_after_claim):
            with mock.patch.object(core, "_install_bytes_to_absent", side_effect=inject_after_safe_install):
                with self.assertRaises(KbError) as raised:
                    ingest_text(self.root, "继续推进：验证 normal bounded reappearance。", context_ref=ref, base_digest=base)

        self.assertEqual(raised.exception.code, "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED")
        self.assertEqual(injected, 9)
        self.assertFalse(core._secret_reasons(context_path.read_bytes()))
        occurrences = [
            item.relative_to(self.root).as_posix()
            for item in self.root.rglob("*")
            if item.is_file() and any(marker in item.read_bytes() for marker in secrets)
        ]
        self.assertEqual(len(occurrences), injected)
        self.assertTrue(all(item.startswith("ingress/context-quarantine/") for item in occurrences))
        claim_paths = list((self.root / "ingress" / "context-quarantine").glob("CCL-*/claim.json"))
        self.assertEqual(len(claim_paths), 1)
        claim = json.loads(claim_paths[0].read_text(encoding="utf-8"))
        records = claim["reappeared_entries"]
        self.assertEqual(len(records), injected)
        for record in records:
            retained = claim_paths[0].parent / record["entry"]
            self.assertEqual(core._digest(retained.read_bytes()), record["digest"])

        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertTrue(any(item.get("code") == "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED" for item in first["unresolved"]))
        self.assertEqual(second["unresolved"], first["unresolved"])

    def test_context_bounded_reappearance_recovery_claims_latest_before_unresolved(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        base = str(explain(self.root, ref)["base_digest"])
        context_path = self._context_path(ref)
        expected = context_path.read_bytes()
        secrets = [
            f"CONTEXT-BOUND-RECOVERY-{index}".encode("ascii") + b" sk-" + (bytes([75 + index]) * 48) + b"\n"
            for index in range(10)
        ]
        original_replace = core._replace_file_after_sync
        injected = False

        def inject_first(path: Path, data: bytes) -> None:
            nonlocal injected
            value = json.loads(data.decode("utf-8"))
            if path.name == "update.json" and value.get("stage") == "claimed" and not injected:
                context_path.write_bytes(secrets[0])
                injected = True
            original_replace(path, data)

        with mock.patch.object(core, "_replace_file_after_sync", side_effect=inject_first):
            with self.assertRaises(KbError) as raised:
                ingest_text(self.root, "继续推进：为 recovery bounded reappearance 准备状态。", context_ref=ref, base_digest=base)
        self.assertEqual(raised.exception.code, "KB2_RESTRICTED_EDIT")
        self.assertFalse(core._secret_reasons(context_path.read_bytes()))

        context_path.write_bytes(secrets[1])
        injected_count = 2
        original_install = core._install_bytes_to_absent

        def inject_after_safe_install(path: Path, content: bytes) -> None:
            nonlocal injected_count
            original_install(path, content)
            if path == context_path and content == expected and injected_count < len(secrets):
                context_path.write_bytes(secrets[injected_count])
                injected_count += 1

        with mock.patch.object(core, "_install_bytes_to_absent", side_effect=inject_after_safe_install):
            recovery = core.recover_all(self.root)

        self.assertTrue(any(item.get("code") == "KB2_CONTEXT_REAPPEARANCE_UNRESOLVED" for item in recovery["unresolved"]))
        self.assertEqual(injected_count, 9)
        self.assertFalse(core._secret_reasons(context_path.read_bytes()))
        occurrences = [
            item.relative_to(self.root).as_posix()
            for item in self.root.rglob("*")
            if item.is_file() and any(marker in item.read_bytes() for marker in secrets)
        ]
        self.assertEqual(len(occurrences), injected_count)
        self.assertTrue(all(item.startswith("ingress/context-quarantine/") for item in occurrences))
        claim_paths = list((self.root / "ingress" / "context-quarantine").glob("CCL-*/claim.json"))
        self.assertEqual(len(claim_paths), 1)
        claim = json.loads(claim_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(len(claim["reappeared_entries"]), injected_count)
        for record in claim["reappeared_entries"]:
            retained = claim_paths[0].parent / record["entry"]
            self.assertEqual(core._digest(retained.read_bytes()), record["digest"])

        second = core.recover_all(self.root)
        self.assertEqual(second["unresolved"], recovery["unresolved"])

    def test_cli_context_body_is_stdin_only_and_machine_update_uses_ref_and_base(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        create_text = "这个排障任务需要跨会话持续推进并交接。"
        created = subprocess.run(
            [sys.executable, "-B", "-m", "kb2.cli", "--root", str(self.root), "--json", "ingest"],
            input=create_text.encode("utf-8"),
            capture_output=True,
            env=environment,
        )
        self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
        response = json.loads(created.stdout.decode("utf-8"))
        ref = response["data"]["context_ref"]
        base = str(explain(self.root, ref)["base_digest"])

        rejected = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "kb2.cli",
                "--root",
                str(self.root),
                "--json",
                "ingest",
                "argv-body",
            ],
            capture_output=True,
            env=environment,
        )
        self.assertEqual(rejected.returncode, 2)

        update_text = "继续推进：验证 CLI Context 更新。"
        updated = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "kb2.cli",
                "--root",
                str(self.root),
                "--json",
                "ingest",
                "--context",
                ref,
                "--base-digest",
                base,
            ],
            input=update_text.encode("utf-8"),
            capture_output=True,
            env=environment,
        )
        self.assertEqual(updated.returncode, 0, updated.stderr.decode(errors="replace"))
        self.assertNotIn(update_text, updated.stdout.decode("utf-8"))
        self.assertIn(update_text, self._context_path(ref).read_text(encoding="utf-8"))

    def test_new_context_persists_active_lifecycle_and_close_is_idempotent(self) -> None:
        created = self._create()
        ref = str(created["context_ref"])
        state_path = self.root / "governance" / "context-state" / ref.removeprefix("context://") / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["lifecycle"]["schema"], "context-lifecycle/v0.2")
        self.assertEqual(state["lifecycle"]["owner"], "context-organizer/v0.2")
        self.assertEqual(state["lifecycle"]["context_ref"], ref)
        self.assertEqual(state["lifecycle"]["status"], "active")
        context_bytes = self._context_path(ref).read_bytes()
        first = context_core.close_context(self.root, ref, status="completed")
        after_close = state_path.read_bytes()
        second = context_core.close_context(self.root, ref, status="completed")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["changed"], [])
        self.assertEqual(after_close, state_path.read_bytes())
        self.assertEqual(context_bytes, self._context_path(ref).read_bytes())

    def test_context_close_rejects_terminal_conflict_and_lifecycle_drift(self) -> None:
        ref = str(self._create()["context_ref"])
        context_core.close_context(self.root, ref, status="completed")
        with self.assertRaises(KbError) as conflict:
            context_core.close_context(self.root, ref, status="blocked")
        self.assertEqual(conflict.exception.code, "KB2_CONTEXT_LIFECYCLE_CONFLICT")
        state_path = self.root / "governance" / "context-state" / ref.removeprefix("context://") / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["lifecycle"]["context_entry"] = "contexts/escape/CONTEXT.md"
        state_path.write_bytes(core._json_bytes(state))
        with self.assertRaises(KbError) as drift:
            context_core.close_context(self.root, ref)
        self.assertEqual(drift.exception.code, "KB2_CONTEXT_LIFECYCLE_INVALID")

    def test_legacy_state_close_migrates_to_complete_lifecycle_owner(self) -> None:
        ref = str(self._create()["context_ref"])
        state_path = self.root / "governance" / "context-state" / ref.removeprefix("context://") / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        legacy_updated_at = state["updated_at"]
        state.pop("lifecycle")
        state_path.write_bytes(core._json_bytes(state))
        result = context_core.close_context(self.root, ref, status="completed")
        self.assertEqual(result["status"], "completed")
        _, reloaded, _ = context_core._load_state(self.root, ref)
        lifecycle = reloaded["lifecycle"]
        self.assertEqual(lifecycle["schema"], "context-lifecycle/v0.2")
        self.assertEqual(lifecycle["owner"], "context-organizer/v0.2")
        self.assertEqual(lifecycle["context_ref"], ref)
        self.assertEqual(lifecycle["context_entry"], state["context_entry"])
        self.assertEqual(lifecycle["status"], "completed")
        self.assertEqual(lifecycle["created_at"], legacy_updated_at)

    def test_close_context_revalidates_applied_update_chain(self) -> None:
        ref, _, update_dirs = self._create_applied_chain(1)
        candidate = update_dirs[0] / "candidate.md"
        candidate.write_bytes(candidate.read_bytes() + b"\ntampered\n")
        with self.assertRaises(KbError) as raised:
            context_core.close_context(self.root, ref, status="completed")
        self.assertEqual(raised.exception.code, "KB2_CONTEXT_UPDATE_INVALID")

    def test_close_context_cli_returns_structured_result(self) -> None:
        ref = "context://CTX-01KZPQC53JGD8174JZEEVACPJK"
        with mock.patch.object(cli.context_core, "close_context", return_value={"context_ref": ref, "status": "blocked", "changed": []}):
            with mock.patch("sys.stdout") as output:
                self.assertEqual(cli.main(["--root", str(self.root), "--json", "close-context", ref, "--status", "blocked"]), 0)
                rendered = "".join(call.args[0] for call in output.write.call_args_list if call.args)
        self.assertIn('"code": "KB2_OK"', rendered)
        self.assertIn('"status": "blocked"', rendered)


if __name__ == "__main__":
    unittest.main()
