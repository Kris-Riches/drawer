from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import kb2.core as core
from kb2.core import KbError, correct, explain, ingest_text, organize, recover_security_holds, status


class CaptureGardenSliceTests(unittest.TestCase):
    def setUp(self) -> None:
        Path(r"D:\tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kb2-test-root-", dir=r"D:\tmp")
        self.root = Path(self.temp.name)
        (self.root / "kb.yaml").write_text(
            "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _capture_dirs(self) -> list[Path]:
        pending = self.root / "ingress" / "pending"
        return sorted(p for p in pending.glob("CAP-*") if p.is_dir()) if pending.exists() else []

    @staticmethod
    def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
        snapshot: dict[str, bytes | None] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            snapshot[relative + ("/" if path.is_dir() else "")] = None if path.is_dir() else path.read_bytes()
        return snapshot

    def test_unanchored_old_library_shaped_directory_is_never_initialized_or_changed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="old-library-shaped-", dir=r"D:\tmp") as old_name:
            old = Path(old_name)
            (old / "notes").mkdir()
            (old / "notes" / "legacy.md").write_bytes("旧库内容".encode("utf-8"))
            (old / "README.md").write_bytes(b"legacy root")
            before = self._tree_snapshot(old)

            for operation in (
                lambda: ingest_text(old, "不得写入"),
                lambda: status(old),
                lambda: explain(old, "garden://notes/CAP-01KZPQC53JGD8174JZEEVACPJK.md"),
            ):
                with self.assertRaises(KbError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, "KB2_ROOT_UNANCHORED")
                self.assertEqual(self._tree_snapshot(old), before)

    def test_invalid_root_anchor_schema_or_id_is_read_only_failure(self) -> None:
        invalid_anchors = (
            "schema: kb-root/v9\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
            "schema: kb-root/v0.1\nid: ''\n",
            "schema: kb-root/v0.1\nid: not-a-kb-id\n",
        )
        for index, anchor in enumerate(invalid_anchors):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory(prefix="invalid-kb-root-", dir=r"D:\tmp") as invalid_name:
                    invalid = Path(invalid_name)
                    (invalid / "kb.yaml").write_text(anchor, encoding="utf-8")
                    (invalid / "existing.bin").write_bytes(b"must-not-change")
                    before = self._tree_snapshot(invalid)
                    with self.assertRaises(KbError) as raised:
                        ingest_text(invalid, "不得写入")
                    self.assertEqual(raised.exception.code, "KB2_ROOT_INVALID")
                    self.assertEqual(self._tree_snapshot(invalid), before)

    def _windows_acl(self, path: Path) -> tuple[dict[str, object], str]:
        escaped = str(path).replace("'", "''")
        command = (
            f"$a=Get-Acl -LiteralPath '{escaped}'; "
            "$rules=@($a.Access | ForEach-Object { "
            "$sid=$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value; "
            "[pscustomobject]@{sid=$sid;type=$_.AccessControlType.ToString();rights=$_.FileSystemRights.ToString()} }); "
            "[pscustomobject]@{protected=$a.AreAccessRulesProtected;rules=$rules} | ConvertTo-Json -Compress -Depth 4"
        )
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        acl = json.loads(result.stdout)
        whoami = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
        ).stdout.decode(errors="replace")
        sid_match = re.search(r"S-\d(?:-\d+)+", whoami)
        self.assertIsNotNone(sid_match)
        return acl, sid_match.group(0)

    def test_capture_precedes_organize_and_uses_zero_user_fields(self) -> None:
        text = "阶段一真实流程草稿\n先保存，再整理。"
        result = ingest_text(self.root, text)
        capture_dir = self._capture_dirs()[0]
        metadata = json.loads((capture_dir / "capture.json").read_text(encoding="utf-8"))
        note = self.root / "garden" / "notes" / (capture_dir.name + ".md")

        self.assertEqual(result["route"], "garden-organized")
        self.assertEqual(result["user_structured_fields"], 0)
        self.assertEqual((capture_dir / "payload.bin").read_text(encoding="utf-8"), text)
        self.assertEqual(metadata["state"], "garden-organized")
        self.assertTrue(note.is_file())
        self.assertIn(text, note.read_text(encoding="utf-8"))

    def test_failure_after_capture_keeps_exact_payload(self) -> None:
        text = "保存后立即模拟 Organizer 故障"
        with self.assertRaises(KbError) as raised:
            ingest_text(self.root, text, fail_after_capture=True)

        self.assertEqual(raised.exception.code, "KB2_INJECTED_AFTER_CAPTURE")
        captures = self._capture_dirs()
        self.assertEqual(len(captures), 1)
        self.assertEqual((captures[0] / "payload.bin").read_text(encoding="utf-8"), text)
        self.assertFalse((self.root / "garden").exists())

    def test_secret_fixture_is_only_in_protected_capture(self) -> None:
        token = "sk-" + ("A" * 48)
        result = ingest_text(self.root, "临时凭据 " + token)
        capture_dir = self._capture_dirs()[0]
        hold = next((self.root / "ingress" / "restricted-hold").glob("*.json"))
        hold_text = hold.read_text(encoding="utf-8")

        self.assertEqual(result["route"], "restricted-hold")
        self.assertIn(token, (capture_dir / "payload.bin").read_text(encoding="utf-8"))
        self.assertNotIn(token, hold_text)
        self.assertFalse((self.root / "garden").exists())
        self.assertNotIn(token, json.dumps(result, ensure_ascii=False))
        occurrences: list[str] = []
        for path in self.root.rglob("*"):
            if path.is_file() and token.encode("utf-8") in path.read_bytes():
                occurrences.append(path.relative_to(self.root).as_posix())
        self.assertEqual(occurrences, [f"ingress/pending/{capture_dir.name}/payload.bin"])

    def test_secret_external_edit_is_quarantined_then_removed_from_garden(self) -> None:
        result = ingest_text(self.root, "已验证的安全正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        safe_bytes = note.read_bytes()
        token = "sk-" + ("Q" * 48)
        edited_bytes = safe_bytes + ("\n外部误贴凭据 " + token + "\n").encode("utf-8")
        note.write_bytes(edited_bytes)

        with self.assertRaises(KbError) as raised:
            organize(self.root, ref)

        self.assertEqual(raised.exception.code, "KB2_RESTRICTED_EDIT")
        quarantine_root = self.root / "ingress" / "quarantine"
        hold_bundles = [path for path in quarantine_root.glob("HLD-*") if path.is_dir()]
        self.assertEqual(len(hold_bundles), 1)
        self.assertEqual((hold_bundles[0] / "payload.bin").read_bytes(), edited_bytes)
        self.assertEqual(note.read_bytes(), safe_bytes)
        hold_record = json.loads((hold_bundles[0] / "quarantine.json").read_text(encoding="utf-8"))
        self.assertEqual(hold_record["action"], "restored-last-safe-base")
        self.assertEqual(hold_record["payload_digest"], "sha256:" + __import__("hashlib").sha256(edited_bytes).hexdigest())
        self.assertEqual(hold_record["state"], "externalization_pending")
        self.assertEqual(hold_record["stage"], "decision-recorded")
        retained = hold_bundles[0] / hold_record["retained_observed_entry"]
        self.assertEqual(retained.read_bytes(), edited_bytes)
        self.assertEqual(hold_record["retained_observed_digest"], hold_record["payload_digest"])
        self.assertEqual(
            hold_record["retained_observed_entries"],
            [
                {
                    "entry": retained.name,
                    "digest": hold_record["payload_digest"],
                    "retained_at": hold_record["retained_observed_entries"][0]["retained_at"],
                }
            ],
        )
        summary = self.root / "ingress" / "restricted-hold" / f"{hold_bundles[0].name}.json"
        summary_record = json.loads(summary.read_text(encoding="utf-8"))
        self.assertFalse(summary_record["contains_payload"])
        self.assertTrue(summary_record["externalization_pending"])
        self.assertEqual(
            set(raised.exception.changed),
            {
                hold_bundles[0].relative_to(self.root).as_posix().replace("/", os.sep),
                summary.relative_to(self.root).as_posix().replace("/", os.sep),
                note.relative_to(self.root).as_posix().replace("/", os.sep),
                (self.root / "governance" / "organizer-state" / self._capture_dirs()[0].name / "state.json")
                .relative_to(self.root).as_posix().replace("/", os.sep),
            },
        )

        ordinary_occurrences: list[str] = []
        for surface in (self.root / "garden", self.root / "governance"):
            if surface.exists():
                for path in surface.rglob("*"):
                    if path.is_file() and token.encode("utf-8") in path.read_bytes():
                        ordinary_occurrences.append(path.relative_to(self.root).as_posix())
        self.assertEqual(ordinary_occurrences, [])

        detail = explain(self.root, ref)
        self.assertEqual(detail["base_digest"], "sha256:" + __import__("hashlib").sha256(safe_bytes).hexdigest())
        self.assertEqual(detail["route"]["result"], "restricted-hold")
        self.assertEqual(detail["security"]["precheck"], "rejected")
        self.assertEqual(detail["security"]["profile"], "restricted-summary/v1")
        self.assertEqual(detail["security"]["latest_hold"]["action"], "restored-last-safe-base")
        self.assertEqual(detail["security"]["latest_hold"]["hold_ref"], f"hold://{hold_bundles[0].name}")
        rerun = organize(self.root, ref)
        self.assertEqual(rerun["changed"], [])
        self.assertFalse(rerun["override"]["created"])
        if os.name == "nt":
            for protected_root in (quarantine_root, summary.parent):
                acl, current_sid = self._windows_acl(protected_root)
                self.assertTrue(acl["protected"])
                self.assertEqual({rule["sid"] for rule in acl["rules"]}, {current_sid, "S-1-5-18"})

    def test_direct_edit_creates_one_object_override_and_survives_three_runs(self) -> None:
        result = ingest_text(self.root, "自动标题\n自动正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / (ref.rsplit("/", 1)[1])
        human_text = "用户直接修改后的正文"
        note.write_text(note.read_text(encoding="utf-8") + "\n" + human_text + "\n", encoding="utf-8")

        outcomes = [organize(self.root, ref) for _ in range(3)]
        override_files = list((self.root / "governance" / "overrides").glob("OVR-*.yaml"))
        record = json.loads(override_files[0].read_text(encoding="utf-8"))

        self.assertTrue(outcomes[0]["override"]["created"])
        self.assertFalse(outcomes[1]["override"]["created"])
        self.assertFalse(outcomes[2]["override"]["created"])
        self.assertEqual(len(override_files), 1)
        self.assertEqual(record["scope"], {"kind": "object", "ref": ref})
        self.assertIn(human_text, record["diff"])
        self.assertIn(human_text, note.read_text(encoding="utf-8"))

    def test_direct_override_return_to_old_bytes_creates_new_causal_record(self) -> None:
        initial = ingest_text(self.root, "A")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        original = note.read_bytes()
        version_b = original + b"\nB\n"
        version_c = original + b"\nC\n"

        note.write_bytes(version_b)
        first = organize(self.root, ref)
        note.write_bytes(version_c)
        second = organize(self.root, ref)
        before_third = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).astimezone()
        note.write_bytes(version_b)
        third = organize(self.root, ref)
        replay = organize(self.root, ref)

        paths = sorted((self.root / "governance" / "overrides").glob("OVR-*.yaml"))
        records = {json.loads(path.read_text(encoding="utf-8"))["id"]: json.loads(path.read_text(encoding="utf-8")) for path in paths}
        first_id = first["override"]["ref"].removeprefix("override://")
        second_id = second["override"]["ref"].removeprefix("override://")
        third_id = third["override"]["ref"].removeprefix("override://")
        third_record = records[third_id]
        self.assertEqual(len(paths), 3)
        self.assertNotIn(third_id, {first_id, second_id})
        self.assertEqual(third_record["supersedes"], second_id)
        self.assertEqual(third_record["actor"], "human-direct-edit")
        self.assertEqual(third_record["scope"], {"kind": "object", "ref": ref})
        self.assertNotIn("correction_capture_ref", third_record)
        self.assertEqual(third_record["base_digest"], records[second_id]["observed_digest"])
        self.assertEqual(third_record["observed_digest"], records[first_id]["observed_digest"])
        self.assertIn("-C", third_record["diff"])
        self.assertIn("+B", third_record["diff"])
        created_at = __import__("datetime").datetime.fromisoformat(third_record["created_at"])
        self.assertGreaterEqual(created_at, before_third.replace(microsecond=0))
        self.assertFalse(replay["override"]["created"])
        self.assertEqual(len(list((self.root / "governance" / "overrides").glob("OVR-*.yaml"))), 3)

    def test_direct_override_crash_replay_reuses_only_exact_operation_identity(self) -> None:
        initial = ingest_text(self.root, "override crash replay")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        note.write_bytes(note.read_bytes() + b"\nnew direct bytes\n")
        with self.assertRaises(KbError) as raised:
            organize(self.root, ref, fail_after_override_record=True)
        self.assertEqual(raised.exception.code, "KB2_INJECTED_AFTER_OVERRIDE_RECORD")
        first_path = next((self.root / "governance" / "overrides").glob("OVR-*.yaml"))
        first = json.loads(first_path.read_text(encoding="utf-8"))
        replay = organize(self.root, ref)
        self.assertFalse(replay["override"]["created"])
        self.assertEqual(replay["override"]["ref"], f"override://{first['id']}")
        self.assertEqual(len(list((self.root / "governance" / "overrides").glob("OVR-*.yaml"))), 1)

    def test_direct_edit_never_reuses_correction_linked_override(self) -> None:
        initial = ingest_text(self.root, "correction provenance base")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        correction_result = correct(self.root, ref, "correction-owned bytes")
        correction_bytes = note.read_bytes()
        correction_override = correction_result["override"]["ref"].removeprefix("override://")
        note.write_bytes(correction_bytes + b"\nintermediate direct C\n")
        intermediate = organize(self.root, ref)
        note.write_bytes(correction_bytes)
        returned = organize(self.root, ref)

        returned_id = returned["override"]["ref"].removeprefix("override://")
        intermediate_id = intermediate["override"]["ref"].removeprefix("override://")
        self.assertNotEqual(returned_id, correction_override)
        self.assertTrue(returned["override"]["created"])
        record = json.loads((self.root / "governance" / "overrides" / f"{returned_id}.yaml").read_text(encoding="utf-8"))
        self.assertEqual(record["actor"], "human-direct-edit")
        self.assertEqual(record["supersedes"], intermediate_id)
        self.assertNotIn("correction_capture_ref", record)
        self.assertEqual(len(list((self.root / "governance" / "overrides").glob("OVR-*.yaml"))), 3)

    def test_applied_correction_override_field_drift_blocks_recovery(self) -> None:
        mutations = {
            "schema": "human-override/v9",
            "actor": "wrong-actor",
            "scope": {"kind": "object", "ref": "garden://notes/CAP-01KZPQC53JGD8174JZEEVACPJQ.md"},
            "supersedes": "OVR-01KZPQC53JGD8174JZEEVACPJQ",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory(prefix="kb2-applied-ovr-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text("schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n", encoding="utf-8")
                    initial = ingest_text(root, "applied correction override")
                    correct(root, initial["garden_ref"], "correction bytes")
                    override_path = next((root / "governance" / "overrides").glob("OVR-*.yaml"))
                    override = json.loads(override_path.read_text(encoding="utf-8"))
                    override[field] = value
                    override_path.write_text(json.dumps(override), encoding="utf-8")
                    recovery = core.recover_corrections(root)
                    self.assertEqual(recovery["recovered"], 0)
                    self.assertTrue(recovery["unresolved"])
                    with self.assertRaises(KbError):
                        explain(root, initial["garden_ref"])

    def test_installed_correction_rejects_existing_override_drift(self) -> None:
        initial = ingest_text(self.root, "installed existing override drift")
        ref = initial["garden_ref"]
        with self.assertRaises(KbError):
            correct(self.root, ref, "installed correction", fail_after_install=True)
        transaction_path = next((self.root / "governance" / "corrections").glob("COR-*/correction.json"))
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        candidate = transaction_path.parent / "candidate.md"
        displaced = transaction_path.parent / "displaced.md"
        override_path = self.root / "governance" / "overrides" / f"{transaction['override_id']}.yaml"
        diff = "".join(
            __import__("difflib").unified_diff(
                displaced.read_text(encoding="utf-8").splitlines(keepends=True),
                candidate.read_text(encoding="utf-8").splitlines(keepends=True),
                fromfile="organizer-base",
                tofile="human-correction",
            )
        )
        override_path.write_text(
            json.dumps(
                {
                    "schema": "human-override/v0.1-pilot",
                    "id": transaction["override_id"],
                    "target": ref,
                    "scope": {"kind": "object", "ref": ref},
                    "actor": "wrong-installed-actor",
                    "reason": f"natural-language correction from {transaction['correction_capture_ref']}",
                    "created_at": "2026-08-11T00:00:00+08:00",
                    "base_digest": transaction["target_base_digest"],
                    "observed_digest": transaction["candidate_digest"],
                    "diff_format": "unified",
                    "diff": diff,
                    "supersedes": transaction["supersedes"],
                    "correction_capture_ref": transaction["correction_capture_ref"],
                }
            ),
            encoding="utf-8",
        )
        recovery = core.recover_corrections(self.root)
        self.assertEqual(recovery["recovered"], 0)
        self.assertTrue(recovery["unresolved"])
        self.assertIn(json.loads(transaction_path.read_text(encoding="utf-8"))["stage"], {"installed", "conflict"})

    def test_invalid_correction_override_id_fails_before_candidate_or_garden_mutation(self) -> None:
        initial = ingest_text(self.root, "invalid correction override id")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        before_note = note.read_bytes()
        with self.assertRaises(KbError):
            correct(self.root, ref, "prepared candidate", fail_after_prepare=True)
        transaction_path = next((self.root / "governance" / "corrections").glob("COR-*/correction.json"))
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        candidate = transaction_path.parent / "candidate.md"
        before_candidate = candidate.read_bytes()
        transaction["override_id"] = "not-a-valid-override-id"
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        recovery = core.recover_corrections(self.root)
        self.assertEqual(recovery["recovered"], 0)
        self.assertTrue(recovery["unresolved"])
        self.assertEqual(note.read_bytes(), before_note)
        self.assertEqual(candidate.read_bytes(), before_candidate)
        self.assertFalse((transaction_path.parent / "displaced.md").exists())
        self.assertFalse((self.root / "governance" / "overrides" / "not-a-valid-override-id.yaml").exists())
        self.assertEqual(json.loads(transaction_path.read_text(encoding="utf-8"))["stage"], "prepared")

    def test_active_direct_override_drift_blocks_fast_path_and_explain(self) -> None:
        for operation in ("organize", "explain"):
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory(prefix="kb2-active-ovr-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text("schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n", encoding="utf-8")
                    initial = ingest_text(root, "active direct override")
                    ref = initial["garden_ref"]
                    note = root / "garden" / "notes" / ref.rsplit("/", 1)[1]
                    note.write_bytes(note.read_bytes() + b"\ndirect active\n")
                    organize(root, ref)
                    override_path = next((root / "governance" / "overrides").glob("OVR-*.yaml"))
                    override = json.loads(override_path.read_text(encoding="utf-8"))
                    override["actor"] = "drifted-direct-actor"
                    override_path.write_text(json.dumps(override), encoding="utf-8")
                    with self.assertRaises(KbError) as raised:
                        (organize(root, ref) if operation == "organize" else explain(root, ref))
                    self.assertEqual(raised.exception.code, "KB2_OVERRIDE_ENTRY_INVALID")

    def test_override_scanner_rejects_filename_record_identity_mismatch(self) -> None:
        result = ingest_text(self.root, "override owner identity")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        base = note.read_bytes()
        note.write_bytes(base + b"\nhuman edit\n")
        observed_digest = "sha256:" + __import__("hashlib").sha256(note.read_bytes()).hexdigest()
        overrides = self.root / "governance" / "overrides"
        path_id = "OVR-01KZPQC53JGD8174JZEEVACPJN"
        record_id = "OVR-01KZPQC53JGD8174JZEEVACPJP"
        (overrides / f"{path_id}.yaml").write_text(
            json.dumps(
                {
                    "schema": "human-override/v0.1-pilot",
                    "id": record_id,
                    "target": ref,
                    "observed_digest": observed_digest,
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(KbError) as raised:
            organize(self.root, ref)
        self.assertEqual(raised.exception.code, "KB2_OVERRIDE_ENTRY_INVALID")
        self.assertEqual(note.read_bytes(), base + b"\nhuman edit\n")
        state_path = self.root / "governance" / "organizer-state" / result["capture_ref"].removeprefix("capture://") / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["base_digest"], "sha256:" + __import__("hashlib").sha256(base).hexdigest())
        self.assertIsNone(state["active_override"])

    @unittest.skipUnless(os.name == "nt", "Windows nested junction assertion")
    def test_override_scanner_rejects_reparse_leaf_before_read(self) -> None:
        result = ingest_text(self.root, "override leaf guard")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        note.write_bytes(note.read_bytes() + b"\nhuman edit\n")
        link = self.root / "governance" / "overrides" / "OVR-01KZPQC53JGD8174JZEEVACPJQ.yaml"
        with tempfile.TemporaryDirectory(prefix="kb2-override-leaf-", dir=r"D:\tmp") as outside_name:
            outside = Path(outside_name)
            (outside / "sentinel.json").write_text("external override", encoding="utf-8")
            before = self._tree_snapshot(outside)
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr.decode(errors="replace"))
            original_loader = core._load_json

            def reject_reparse_load(path: Path) -> dict[str, object]:
                if path == link:
                    raise AssertionError("override scanner attempted to load a reparse leaf")
                return original_loader(path)

            try:
                with mock.patch.object(core, "_load_json", side_effect=reject_reparse_load):
                    with self.assertRaises(KbError) as raised:
                        organize(self.root, ref)
                self.assertEqual(raised.exception.code, "KB2_REPARSE_REJECTED")
                self.assertEqual(self._tree_snapshot(outside), before)
            finally:
                os.rmdir(link)

    @unittest.skipUnless(os.name == "nt", "Windows nested junction assertion")
    def test_organize_rejects_reparse_base_leaf_before_read(self) -> None:
        result = ingest_text(self.root, "organizer base leaf guard")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        note.write_bytes(note.read_bytes() + b"\nhuman edit\n")
        state_dir = self.root / "governance" / "organizer-state" / result["capture_ref"].removeprefix("capture://")
        base = state_dir / "base.md"
        base.rename(state_dir / "base-original.md")
        with tempfile.TemporaryDirectory(prefix="kb2-base-leaf-", dir=r"D:\tmp") as outside_name:
            outside = Path(outside_name)
            (outside / "sentinel.bin").write_bytes(b"external base")
            before = self._tree_snapshot(outside)
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(base), str(outside)], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr.decode(errors="replace"))
            original_read = Path.read_bytes

            def reject_reparse_read(path: Path) -> bytes:
                if path == base:
                    raise AssertionError("organize attempted to read a reparse base leaf")
                return original_read(path)

            try:
                with mock.patch.object(Path, "read_bytes", reject_reparse_read):
                    with self.assertRaises(KbError) as raised:
                        organize(self.root, ref)
                self.assertEqual(raised.exception.code, "KB2_REPARSE_REJECTED")
                self.assertEqual(self._tree_snapshot(outside), before)
            finally:
                os.rmdir(base)

    @unittest.skipUnless(os.name == "nt", "Windows nested junction assertion")
    def test_decode_rejects_reparse_capture_payload_leaf_before_read(self) -> None:
        with self.assertRaises(KbError):
            ingest_text(self.root, "capture payload leaf guard", fail_after_capture=True)
        capture = self._capture_dirs()[0]
        metadata = json.loads((capture / "capture.json").read_text(encoding="utf-8"))
        payload = capture / "payload.bin"
        payload.rename(capture / "payload-original.bin")
        with tempfile.TemporaryDirectory(prefix="kb2-payload-leaf-", dir=r"D:\tmp") as outside_name:
            outside = Path(outside_name)
            (outside / "sentinel.bin").write_bytes(b"external payload")
            before = self._tree_snapshot(outside)
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(payload), str(outside)], capture_output=True)
            self.assertEqual(made.returncode, 0, made.stderr.decode(errors="replace"))
            original_read = Path.read_bytes

            def reject_reparse_read(path: Path) -> bytes:
                if path == payload:
                    raise AssertionError("decode attempted to read a reparse payload leaf")
                return original_read(path)

            try:
                with mock.patch.object(Path, "read_bytes", reject_reparse_read):
                    with self.assertRaises(KbError) as raised:
                        core._decode_captured_utf8(self.root, capture, metadata)
                self.assertEqual(raised.exception.code, "KB2_REPARSE_REJECTED")
                self.assertEqual(self._tree_snapshot(outside), before)
            finally:
                os.rmdir(payload)

    @unittest.skipUnless(os.name == "nt", "Windows nested junction assertion")
    def test_explain_rejects_reparse_capture_and_override_leaves_before_read(self) -> None:
        for leaf_kind in ("capture", "override"):
            with self.subTest(leaf_kind=leaf_kind):
                with tempfile.TemporaryDirectory(prefix="kb2-explain-leaf-root-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    result = ingest_text(root, f"explain {leaf_kind} leaf guard")
                    ref = result["garden_ref"]
                    capture = root / "ingress" / "pending" / result["capture_ref"].removeprefix("capture://")
                    if leaf_kind == "capture":
                        link = capture / "capture.json"
                        link.rename(capture / "capture-original.json")
                    else:
                        note = root / "garden" / "notes" / ref.rsplit("/", 1)[1]
                        note.write_bytes(note.read_bytes() + b"\nhuman edit\n")
                        organize(root, ref)
                        link = next((root / "governance" / "overrides").glob("OVR-*.yaml"))
                        link.rename(link.parent / "override-original.yaml")
                    with tempfile.TemporaryDirectory(prefix="kb2-explain-leaf-", dir=r"D:\tmp") as outside_name:
                        outside = Path(outside_name)
                        (outside / "sentinel.json").write_text("external explain leaf", encoding="utf-8")
                        before = self._tree_snapshot(outside)
                        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
                        self.assertEqual(made.returncode, 0, made.stderr.decode(errors="replace"))
                        original_loader = core._load_json

                        def reject_reparse_load(path: Path) -> dict[str, object]:
                            if path == link:
                                raise AssertionError("explain attempted to load a reparse leaf")
                            return original_loader(path)

                        try:
                            with mock.patch.object(core, "_load_json", side_effect=reject_reparse_load):
                                with self.assertRaises(KbError) as raised:
                                    explain(root, ref)
                            self.assertEqual(raised.exception.code, "KB2_REPARSE_REJECTED")
                            self.assertEqual(self._tree_snapshot(outside), before)
                        finally:
                            os.rmdir(link)

    def test_natural_language_correct_records_override(self) -> None:
        result = ingest_text(self.root, "初始内容")
        corrected = correct(self.root, result["garden_ref"], "标题需要强调恢复顺序")
        detail = explain(self.root, result["garden_ref"])
        note = self.root / "garden" / "notes" / result["garden_ref"].rsplit("/", 1)[1]

        self.assertTrue(corrected["correction_recorded"])
        self.assertGreaterEqual(len(corrected["changed"]), 4)
        self.assertTrue(any(item.endswith(".md") for item in corrected["changed"]))
        self.assertTrue(any(item.endswith(".yaml") for item in corrected["changed"]))
        self.assertIn("标题需要强调恢复顺序", note.read_text(encoding="utf-8"))
        self.assertEqual(detail["human_override"]["actor"], "human-natural-language-correction")

    def test_cli_utf8_json_round_trip_preserves_chinese_correction(self) -> None:
        result = ingest_text(self.root, "CLI 中文往返")
        ref = result["garden_ref"]
        correction = "将验收说明保留为合成 fixture，不能计入阶段 3 真实样本。"
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "kb2.cli", "--root", str(self.root), "--json", "correct", ref],
            check=False,
            capture_output=True,
            env=environment,
            input=correction.encode("utf-8"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        cli_result = json.loads(completed.stdout.decode("utf-8"))
        detail = explain(self.root, ref)

        self.assertTrue(cli_result["ok"])
        self.assertEqual(detail["human_override"]["correction_capture_ref"], cli_result["data"]["correction_capture_ref"])
        self.assertNotIn(correction, completed.stdout.decode("utf-8"))
        override_path = next((self.root / "governance" / "overrides").glob("OVR-*.yaml"))
        self.assertIn(correction, override_path.read_text(encoding="utf-8"))

    def test_cli_rejects_bodies_in_argv_and_accepts_utf8_stdin(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        rejected = subprocess.run(
            [sys.executable, "-m", "kb2.cli", "--root", str(self.root), "--json", "ingest", "argv-body"],
            check=False,
            capture_output=True,
            env=environment,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(self._capture_dirs(), [])

        content = "通过标准输入捕获的中文正文"
        accepted = subprocess.run(
            [sys.executable, "-m", "kb2.cli", "--root", str(self.root), "--json", "ingest"],
            check=False,
            capture_output=True,
            env=environment,
            input=content.encode("utf-8"),
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr.decode("utf-8", errors="replace"))
        response = json.loads(accepted.stdout.decode("utf-8"))
        self.assertTrue(response["ok"])
        self.assertNotIn(content, accepted.stdout.decode("utf-8"))
        self.assertEqual((self._capture_dirs()[0] / "payload.bin").read_text(encoding="utf-8"), content)

        ref = response["data"]["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        before_note = note.read_bytes()
        before_captures = len(self._capture_dirs())
        rejected_correction = subprocess.run(
            [
                sys.executable,
                "-m",
                "kb2.cli",
                "--root",
                str(self.root),
                "--json",
                "correct",
                ref,
                "argv-correction-body",
            ],
            check=False,
            capture_output=True,
            env=environment,
        )
        self.assertEqual(rejected_correction.returncode, 2)
        self.assertEqual(len(self._capture_dirs()), before_captures)
        self.assertEqual(note.read_bytes(), before_note)

    def test_correction_is_captured_before_apply_and_linked_from_override(self) -> None:
        initial = ingest_text(self.root, "需要纠正的内容")
        correction = "正文应明确先恢复、再验证"
        result = correct(self.root, initial["garden_ref"], correction)
        captures = self._capture_dirs()
        correction_capture = next(
            path for path in captures
            if json.loads((path / "capture.json").read_text(encoding="utf-8"))["source"]["kind"] == "human-correction"
        )
        correction_metadata = json.loads((correction_capture / "capture.json").read_text(encoding="utf-8"))
        override_path = next((self.root / "governance" / "overrides").glob("OVR-*.yaml"))
        override = json.loads(override_path.read_text(encoding="utf-8"))

        self.assertEqual(len(captures), 2)
        self.assertEqual((correction_capture / "payload.bin").read_text(encoding="utf-8"), correction)
        self.assertEqual(correction_metadata["source"]["target"], initial["garden_ref"])
        self.assertEqual(correction_metadata["route"]["result"], "correction-applied")
        self.assertEqual(override["correction_capture_ref"], f"capture://{correction_capture.name}")
        self.assertEqual(result["correction_capture_ref"], override["correction_capture_ref"])

    def test_secret_correction_is_captured_before_scan_and_never_rewrites_garden(self) -> None:
        initial = ingest_text(self.root, "纠正前安全正文")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        before = note.read_bytes()
        token = "sk-" + ("C" * 48)

        with self.assertRaises(KbError) as raised:
            correct(self.root, ref, "错误粘贴 " + token)

        self.assertEqual(raised.exception.code, "KB2_POLICY_REJECTED")
        correction_capture = next(
            path for path in self._capture_dirs()
            if json.loads((path / "capture.json").read_text(encoding="utf-8"))["source"]["kind"] == "human-correction"
        )
        self.assertIn(token, (correction_capture / "payload.bin").read_text(encoding="utf-8"))
        summary = self.root / "ingress" / "restricted-hold" / f"{correction_capture.name}.json"
        self.assertNotIn(token, summary.read_text(encoding="utf-8"))
        self.assertTrue(json.loads(summary.read_text(encoding="utf-8"))["externalization_pending"])
        self.assertEqual(note.read_bytes(), before)
        self.assertEqual(list((self.root / "governance" / "overrides").glob("OVR-*.yaml")), [])

    def test_correct_conflict_transaction_never_overwrites_injected_edit(self) -> None:
        initial = ingest_text(self.root, "事务前正文")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        injected = "并发外部编辑必须保留"

        def inject() -> None:
            note.write_text(injected, encoding="utf-8")

        with self.assertRaises(KbError) as raised:
            correct(self.root, ref, "本次自然语言纠正", before_claim=inject)

        self.assertEqual(raised.exception.code, "KB2_BASE_DIGEST_MISMATCH")
        self.assertEqual(note.read_text(encoding="utf-8"), injected)
        self.assertEqual(len(self._capture_dirs()), 2)
        correction_capture = max(self._capture_dirs(), key=lambda path: path.stat().st_ctime_ns)
        metadata = json.loads((correction_capture / "capture.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["route"]["result"], "needs-review")
        self.assertEqual(metadata["route"]["reason"], "garden-conflict")
        self.assertEqual(list((self.root / "governance" / "overrides").glob("OVR-*.yaml")), [])

    @unittest.skipUnless(os.name == "nt", "Windows ACL assertion")
    def test_protection_removes_preexisting_everyone_ace(self) -> None:
        pending = self.root / "ingress" / "pending"
        pending.mkdir(parents=True)
        grant = subprocess.run(
            ["icacls", str(pending), "/inheritance:r", "/grant:r", "*S-1-1-0:(OI)(CI)F"],
            check=False,
            capture_output=True,
        )
        self.assertEqual(grant.returncode, 0)
        ingest_text(self.root, "ACL 白名单重建")

        acl, current_sid = self._windows_acl(pending)
        self.assertTrue(acl["protected"])
        self.assertEqual({rule["sid"] for rule in acl["rules"]}, {current_sid, "S-1-5-18"})

    def test_organizer_state_and_base_are_not_owned_by_pending(self) -> None:
        result = ingest_text(self.root, "状态所有权验证")
        capture = self._capture_dirs()[0]
        metadata = json.loads((capture / "capture.json").read_text(encoding="utf-8"))
        state_root = self.root / "governance" / "organizer-state" / capture.name

        self.assertNotIn("organizer", metadata)
        self.assertFalse((capture / "organizer-base.md").exists())
        self.assertTrue((state_root / "state.json").is_file())
        self.assertTrue((state_root / "base.md").is_file())
        state = json.loads((state_root / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["capture_ref"], result["capture_ref"])
        self.assertEqual(state["garden_ref"], result["garden_ref"])

    def test_quarantine_recovery_after_commit_before_garden_restore(self) -> None:
        result = ingest_text(self.root, "恢复前安全正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        safe = note.read_bytes()
        token = "sk-" + ("R" * 48)
        edited = safe + token.encode("utf-8")
        note.write_bytes(edited)

        with self.assertRaises(KbError) as raised:
            organize(self.root, ref, fail_after_quarantine=True)
        self.assertEqual(raised.exception.code, "KB2_INJECTED_AFTER_QUARANTINE")
        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))
        self.assertEqual((quarantine / "payload.bin").read_bytes(), edited)
        transaction = json.loads((quarantine / "quarantine.json").read_text(encoding="utf-8"))
        self.assertEqual(transaction["state"], "externalization_pending")
        self.assertEqual(transaction["stage"], "quarantine-committed")

        recovered = recover_security_holds(self.root)
        self.assertEqual(recovered["recovered"], 1)
        self.assertEqual(note.read_bytes(), safe)
        summary = next((self.root / "ingress" / "restricted-hold").glob("HLD-*.json"))
        self.assertNotIn(token, summary.read_text(encoding="utf-8"))
        self.assertFalse(json.loads(summary.read_text(encoding="utf-8"))["contains_payload"])

    def test_quarantine_recovery_after_restore_before_decision_update(self) -> None:
        result = ingest_text(self.root, "决策恢复前安全正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        safe = note.read_bytes()
        token = "sk-" + ("S" * 48)
        note.write_bytes(safe + token.encode("utf-8"))

        with self.assertRaises(KbError) as raised:
            organize(self.root, ref, fail_after_restore=True)
        self.assertEqual(raised.exception.code, "KB2_INJECTED_AFTER_GARDEN_RESTORE")
        self.assertEqual(note.read_bytes(), safe)
        recovered = recover_security_holds(self.root)
        self.assertEqual(recovered["recovered"], 1)
        detail = explain(self.root, ref)
        self.assertEqual(detail["route"]["result"], "restricted-hold")
        self.assertEqual(detail["security"]["precheck"], "rejected")
        self.assertEqual(detail["security"]["profile"], "restricted-summary/v1")
        self.assertTrue(detail["security"]["latest_hold"]["externalization_pending"])

    def test_quarantine_recovery_conflict_stays_unresolved_across_retries(self) -> None:
        result = ingest_text(self.root, "隔离冲突前安全正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        token = "sk-" + ("T" * 48)
        note.write_bytes(note.read_bytes() + token.encode("utf-8"))

        with self.assertRaises(KbError) as raised:
            organize(self.root, ref, fail_after_quarantine=True)
        self.assertEqual(raised.exception.code, "KB2_INJECTED_AFTER_QUARANTINE")

        concurrent = "隔离提交后的并发安全编辑，绝不能被覆盖。\n".encode("utf-8")
        note.write_bytes(concurrent)
        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))

        first = recover_security_holds(self.root)
        second = recover_security_holds(self.root)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_QUARANTINE_RECOVERY_CONFLICT")
        self.assertEqual(second["unresolved"], first["unresolved"])
        transaction = json.loads((quarantine / "quarantine.json").read_text(encoding="utf-8"))
        self.assertEqual(transaction["stage"], "recovery-conflict")
        self.assertEqual(note.read_bytes(), concurrent)
        self.assertEqual(list((self.root / "governance" / "overrides").glob("OVR-*.yaml")), [])

        for operation in (lambda: organize(self.root, ref), lambda: explain(self.root, ref)):
            with self.assertRaises(KbError) as blocked:
                operation()
            self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")
            self.assertEqual(note.read_bytes(), concurrent)

    def test_quarantine_claims_concurrent_secret_third_digest_before_safe_restore(self) -> None:
        result = ingest_text(self.root, "并发秘密前安全正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        safe = note.read_bytes()
        secret_one = safe + ("sk-" + ("X" * 48)).encode("utf-8")
        note.write_bytes(secret_one)
        with self.assertRaises(KbError):
            organize(self.root, ref, fail_after_quarantine=True)

        sentinel = "S2-CONCURRENT-SECRET-SENTINEL"
        secret_two = safe + ("\n" + sentinel + " sk-" + ("Y" * 48) + "\n").encode("utf-8")
        note.write_bytes(secret_two)
        first = recover_security_holds(self.root)
        second = recover_security_holds(self.root)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(note.read_bytes(), safe)
        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))
        transaction = json.loads((quarantine / "quarantine.json").read_text(encoding="utf-8"))
        self.assertEqual(transaction["stage"], "recovery-conflict")
        retained = [quarantine / item["entry"] for item in transaction["retained_observed_entries"]]
        matching = [path for path in retained if sentinel.encode("utf-8") in path.read_bytes()]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].read_bytes(), secret_two)
        occurrences = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and sentinel.encode("utf-8") in path.read_bytes()
        ]
        self.assertEqual(occurrences, [matching[0].relative_to(self.root).as_posix()])
        for operation in (lambda: organize(self.root, ref), lambda: explain(self.root, ref)):
            with self.assertRaises(KbError) as blocked:
                operation()
            self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")

    def test_quarantine_classifies_claimed_safe_bytes_not_preclaim_sensitive_read(self) -> None:
        result = ingest_text(self.root, "claim classification base")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        safe_base = note.read_bytes()
        note.write_bytes(safe_base + ("sk-" + ("L" * 48)).encode("utf-8"))
        with self.assertRaises(KbError):
            organize(self.root, ref, fail_after_quarantine=True)
        sensitive_s2 = safe_base + ("\nS2-PRECLAIM sk-" + ("M" * 48)).encode("utf-8")
        safe_c = safe_base + b"\nCLAIMED-SAFE-C\n"
        note.write_bytes(sensitive_s2)
        original_move = core._move_file_to_absent
        swapped = False

        def swap_before_claim(source: Path, destination: Path) -> None:
            nonlocal swapped
            if source == note and destination.name.startswith("garden-observed") and not swapped:
                source.write_bytes(safe_c)
                swapped = True
            original_move(source, destination)

        with mock.patch.object(core, "_move_file_to_absent", side_effect=swap_before_claim):
            first = recover_security_holds(self.root)
        self.assertTrue(swapped)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(note.read_bytes(), safe_c)
        second = recover_security_holds(self.root)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(note.read_bytes(), safe_c)

    def test_quarantine_reclaims_late_sensitive_reappearance_after_safe_claim(self) -> None:
        result = ingest_text(self.root, "late sensitive base")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        safe_base = note.read_bytes()
        note.write_bytes(safe_base + ("sk-" + ("N" * 48)).encode("utf-8"))
        with self.assertRaises(KbError):
            organize(self.root, ref, fail_after_quarantine=True)
        safe_c = safe_base + b"\nSAFE-C-BEFORE-LATE-S3\n"
        marker = b"LATE-SENSITIVE-S3"
        sensitive_s3 = safe_base + b"\n" + marker + b" sk-" + (b"P" * 48) + b"\n"
        note.write_bytes(safe_c)
        original_scan = core._secret_reasons
        injected = False

        def inject_after_claimed_safe_scan(data: bytes) -> list[str]:
            nonlocal injected
            result_reasons = original_scan(data)
            if data == safe_c and not injected:
                note.write_bytes(sensitive_s3)
                injected = True
            return result_reasons

        with mock.patch.object(core, "_secret_reasons", side_effect=inject_after_claimed_safe_scan):
            first = recover_security_holds(self.root)
        self.assertTrue(injected)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(note.read_bytes(), safe_base)
        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))
        occurrences = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and marker in path.read_bytes()
        ]
        self.assertEqual(len(occurrences), 1)
        self.assertTrue(occurrences[0].startswith(quarantine.relative_to(self.root).as_posix()))
        second = recover_security_holds(self.root)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(note.read_bytes(), safe_base)

    def test_quarantine_owner_contract_rejects_schema_refs_and_traversal(self) -> None:
        for mutation in ("schema", "refs", "traversal"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory(prefix="kb2-q-owner-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text("schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n", encoding="utf-8")
                    result = ingest_text(root, "quarantine owner safe")
                    ref = result["garden_ref"]
                    note = root / "garden" / "notes" / ref.rsplit("/", 1)[1]
                    note.write_bytes(note.read_bytes() + ("sk-" + ("Z" * 48)).encode("utf-8"))
                    with self.assertRaises(KbError):
                        organize(root, ref, fail_after_quarantine=True)
                    quarantine = next((root / "ingress" / "quarantine").glob("HLD-*"))
                    transaction_path = quarantine / "quarantine.json"
                    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
                    foreign = root / "ingress" / "quarantine" / "foreign.bin"
                    if mutation == "schema":
                        transaction["schema"] = "security-quarantine/v9"
                    elif mutation == "refs":
                        transaction["hold_ref"] = "hold://HLD-01KZPQC53JGD8174JZEEVACPJQ"
                    else:
                        foreign.write_bytes(b"foreign-owner-bytes")
                        transaction["payload_entry"] = "../foreign.bin"
                        transaction["payload_digest"] = "sha256:" + __import__("hashlib").sha256(foreign.read_bytes()).hexdigest()
                    transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
                    original_read = Path.read_bytes

                    def reject_foreign_read(path: Path) -> bytes:
                        if path.name == "foreign.bin":
                            raise AssertionError("quarantine loader read a foreign owner")
                        return original_read(path)

                    with mock.patch.object(Path, "read_bytes", reject_foreign_read):
                        recovery = recover_security_holds(root)
                    self.assertEqual(recovery["recovered"], 0)
                    self.assertEqual(recovery["unresolved"][0]["code"], "KB2_QUARANTINE_ENTRY_INVALID")
                    if foreign.exists():
                        self.assertEqual(foreign.read_bytes(), b"foreign-owner-bytes")

    def test_correction_owner_contract_rejects_schema_leaves_capture_and_source(self) -> None:
        for mutation in (
            "schema",
            "leaves",
            "foreign-capture",
            "capture-schema",
            "capture-payload-entry",
            "source-kind",
            "source-target",
        ):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory(prefix="kb2-c-owner-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text("schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n", encoding="utf-8")
                    initial = ingest_text(root, "correction owner target")
                    foreign_result = ingest_text(root, "foreign ordinary capture")
                    with self.assertRaises(KbError):
                        correct(root, initial["garden_ref"], "owned correction", fail_after_prepare=True)
                    transaction_path = next((root / "governance" / "corrections").glob("COR-*/correction.json"))
                    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
                    correction_capture = root / "ingress" / "pending" / transaction["correction_capture_ref"].removeprefix("capture://")
                    foreign_capture = root / "ingress" / "pending" / foreign_result["capture_ref"].removeprefix("capture://")
                    if mutation == "schema":
                        transaction["schema"] = "correction-transaction/v9"
                    elif mutation == "leaves":
                        transaction["candidate_entry"] = "../foreign.md"
                    elif mutation == "foreign-capture":
                        transaction["correction_capture_ref"] = foreign_result["capture_ref"]
                    elif mutation in {"capture-schema", "capture-payload-entry", "source-kind", "source-target"}:
                        metadata_path = correction_capture / "capture.json"
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        if mutation == "capture-schema":
                            metadata["schema"] = "capture/v9"
                        elif mutation == "capture-payload-entry":
                            metadata["payload_entry"] = "../payload.bin"
                        elif mutation == "source-kind":
                            metadata["source"]["kind"] = "direct-stdin"
                        else:
                            metadata["source"]["target"] = "garden://notes/CAP-01KZPQC53JGD8174JZEEVACPJQ.md"
                        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                    transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
                    original_loader = core._load_json

                    def reject_foreign_capture_read(path: Path) -> dict[str, object]:
                        if foreign_capture in path.parents:
                            raise AssertionError("correction loader read a foreign capture owner")
                        return original_loader(path)

                    with mock.patch.object(core, "_load_json", side_effect=reject_foreign_capture_read):
                        recovery = core.recover_corrections(root)
                    self.assertEqual(recovery["recovered"], 0)
                    self.assertIn(
                        recovery["unresolved"][0]["code"],
                        {"KB2_CORRECTION_ENTRY_INVALID", "KB2_CORRECTION_CAPTURE_INVALID"},
                    )

    def test_cli_recover_unresolved_is_nonzero_error_with_report(self) -> None:
        result = ingest_text(self.root, "CLI unresolved recovery")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        note.write_bytes(note.read_bytes() + ("sk-" + ("R" * 48)).encode("utf-8"))
        with self.assertRaises(KbError):
            organize(self.root, ref, fail_after_quarantine=True)
        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))
        transaction_path = quarantine / "quarantine.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["stage"] = "future-stage"
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "kb2.cli", "--root", str(self.root), "--json", "recover"],
            capture_output=True,
            env=environment,
        )
        response = json.loads(completed.stdout.decode("utf-8"))
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "KB2_RECOVERY_UNRESOLVED")
        self.assertTrue(response["data"]["unresolved"])

        with tempfile.TemporaryDirectory(prefix="kb2-clean-recover-", dir=r"D:\tmp") as clean_name:
            clean_root = Path(clean_name)
            (clean_root / "kb.yaml").write_text(
                "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                encoding="utf-8",
            )
            clean = subprocess.run(
                [sys.executable, "-B", "-m", "kb2.cli", "--root", str(clean_root), "--json", "recover"],
                capture_output=True,
                env=environment,
            )
            clean_response = json.loads(clean.stdout.decode("utf-8"))
            self.assertEqual(clean.returncode, 0)
            self.assertTrue(clean_response["ok"])
            self.assertEqual(clean_response["code"], "KB2_OK")

    def test_unknown_quarantine_stage_fails_closed_across_retries(self) -> None:
        result = ingest_text(self.root, "未知隔离阶段前安全正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        note.write_bytes(note.read_bytes() + ("sk-" + ("U" * 48)).encode("utf-8"))
        with self.assertRaises(KbError):
            organize(self.root, ref, fail_after_quarantine=True)

        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))
        transaction_path = quarantine / "quarantine.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["stage"] = "future-unknown-stage"
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        before = note.read_bytes()

        for _ in range(2):
            replay = recover_security_holds(self.root)
            self.assertEqual(replay["recovered"], 0)
            self.assertEqual(replay["unresolved"][0]["code"], "KB2_QUARANTINE_STAGE_INVALID")
            self.assertEqual(note.read_bytes(), before)
        with self.assertRaises(KbError) as blocked:
            explain(self.root, ref)
        self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")

    def test_quarantine_claim_race_preserves_moved_bytes_and_stays_unresolved(self) -> None:
        result = ingest_text(self.root, "隔离声明竞争前安全正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        safe = note.read_bytes()
        note.write_bytes(safe + ("sk-" + ("V" * 48)).encode("utf-8"))
        with self.assertRaises(KbError):
            organize(self.root, ref, fail_after_quarantine=True)

        marker = b"concurrent-marker-after-digest-check"
        concurrent = safe + b"\n" + marker + b"\n"

        def inject_after_check() -> None:
            note.write_bytes(concurrent)

        first = recover_security_holds(self.root, before_quarantine_claim=inject_after_check)
        second = recover_security_holds(self.root)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_QUARANTINE_RECOVERY_CONFLICT")
        self.assertEqual(second["unresolved"], first["unresolved"])
        self.assertEqual(note.read_bytes(), safe)
        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))
        transaction = json.loads((quarantine / "quarantine.json").read_text(encoding="utf-8"))
        observed = quarantine / transaction["recovery_conflict_entry"]
        self.assertEqual(observed.read_bytes(), concurrent)
        occurrences = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and marker in path.read_bytes()
        ]
        self.assertEqual(occurrences, [observed.relative_to(self.root).as_posix()])
        for operation in (lambda: organize(self.root, ref), lambda: explain(self.root, ref)):
            with self.assertRaises(KbError) as blocked:
                operation()
            self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")

    def test_quarantine_observed_drift_before_cleanup_is_retained_and_fail_closed(self) -> None:
        result = ingest_text(self.root, "隔离保留窗口前安全正文")
        ref = result["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        safe = note.read_bytes()
        note.write_bytes(safe + ("sk-" + ("W" * 48)).encode("utf-8"))
        with self.assertRaises(KbError):
            organize(self.root, ref, fail_after_quarantine=True)

        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))
        marker = b"marker-injected-after-moved-digest-before-cleanup"
        original_loader = core._load_quarantine_transaction
        injected = False

        def inject_in_cleanup_window(root: Path, owner: Path) -> tuple[Path, dict[str, object]]:
            nonlocal injected
            loaded = original_loader(root, owner)
            observed = owner / "garden-observed.bin"
            if observed.is_file() and not injected:
                observed.write_bytes(marker)
                injected = True
            return loaded

        with mock.patch.object(core, "_load_quarantine_transaction", side_effect=inject_in_cleanup_window):
            first = recover_security_holds(self.root)

        self.assertTrue(injected)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_QUARANTINE_RETAINED_DRIFT")
        transaction = json.loads((quarantine / "quarantine.json").read_text(encoding="utf-8"))
        self.assertEqual(transaction["stage"], "recovery-conflict")
        retained = quarantine / transaction["retained_observed_entry"]
        self.assertEqual(retained.read_bytes(), marker)
        self.assertEqual(note.read_bytes(), safe)

        second = recover_security_holds(self.root)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(second["unresolved"][0]["code"], "KB2_QUARANTINE_RETAINED_DRIFT")
        self.assertEqual(retained.read_bytes(), marker)
        occurrences = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and marker in path.read_bytes()
        ]
        self.assertEqual(occurrences, [retained.relative_to(self.root).as_posix()])
        for operation in (lambda: organize(self.root, ref), lambda: explain(self.root, ref)):
            with self.assertRaises(KbError) as blocked:
                operation()
            self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")

    @unittest.skipUnless(os.name == "nt", "Windows nested junction assertion")
    def test_recovery_scanners_reject_nested_junctions_without_touching_targets(self) -> None:
        initial = ingest_text(self.root, "嵌套 junction 守卫目标")
        quarantine_root = self.root / "ingress" / "quarantine"
        quarantine_root.mkdir(parents=True)
        corrections_root = self.root / "governance" / "corrections"
        corrections_root.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix="kb2-q-outside-", dir=r"D:\tmp") as q_name, tempfile.TemporaryDirectory(
            prefix="kb2-c-outside-", dir=r"D:\tmp"
        ) as c_name:
            q_outside = Path(q_name)
            c_outside = Path(c_name)
            (q_outside / "quarantine.json").write_bytes(b"must-not-be-parsed-or-changed")
            (q_outside / "sentinel.bin").write_bytes(b"quarantine-outside")
            (c_outside / "correction.json").write_bytes(b"must-not-be-parsed-or-changed")
            (c_outside / "sentinel.bin").write_bytes(b"correction-outside")
            q_before = self._tree_snapshot(q_outside)
            c_before = self._tree_snapshot(c_outside)
            q_link = quarantine_root / "HLD-01KZPQC53JGD8174JZEEVACPJK"
            c_link = corrections_root / "COR-01KZPQC53JGD8174JZEEVACPJM"
            for link, target in ((q_link, q_outside), (c_link, c_outside)):
                made = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    check=False,
                    capture_output=True,
                )
                self.assertEqual(made.returncode, 0, made.stderr.decode(errors="replace"))
            try:
                original_load_json = core._load_json
                original_read_bytes = Path.read_bytes

                def reject_external_json_read(path: Path) -> dict[str, object]:
                    if path.is_relative_to(q_outside) or path.is_relative_to(c_outside):
                        raise AssertionError(f"recovery read external transaction: {path}")
                    return original_load_json(path)

                def reject_external_payload_read(path: Path) -> bytes:
                    if path.is_relative_to(q_outside) or path.is_relative_to(c_outside):
                        raise AssertionError(f"recovery read external payload: {path}")
                    return original_read_bytes(path)

                with mock.patch.object(core, "_load_json", side_effect=reject_external_json_read), mock.patch.object(
                    Path,
                    "read_bytes",
                    reject_external_payload_read,
                ):
                    for _ in range(2):
                        security = recover_security_holds(self.root)
                        corrections = core.recover_corrections(self.root)
                        self.assertEqual(security["unresolved"][0]["code"], "KB2_REPARSE_REJECTED")
                        self.assertEqual(corrections["unresolved"][0]["code"], "KB2_REPARSE_REJECTED")
                self.assertEqual(self._tree_snapshot(q_outside), q_before)
                self.assertEqual(self._tree_snapshot(c_outside), c_before)
                for operation in (
                    lambda: organize(self.root, initial["garden_ref"]),
                    lambda: explain(self.root, initial["garden_ref"]),
                ):
                    with self.assertRaises(KbError) as blocked:
                        operation()
                    self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")
            finally:
                os.rmdir(q_link)
                os.rmdir(c_link)

    def test_correction_installed_crash_replays_exact_linked_override_once(self) -> None:
        initial = ingest_text(self.root, "安装故障前正文")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]

        with self.assertRaises(KbError) as raised:
            correct(self.root, ref, "安装后必须精确恢复", fail_after_install=True)
        self.assertEqual(raised.exception.code, "KB2_INJECTED_CORRECTION_INSTALLED")

        transaction_dir = next((self.root / "governance" / "corrections").glob("COR-*"))
        transaction_path = transaction_dir / "correction.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        self.assertEqual(transaction["stage"], "installed")
        self.assertEqual(transaction["candidate_digest"], "sha256:" + __import__("hashlib").sha256(note.read_bytes()).hexdigest())
        self.assertEqual(list((self.root / "governance" / "overrides").glob("OVR-*.yaml")), [])

        first = core.recover_corrections(self.root)
        second = core.recover_corrections(self.root)
        self.assertEqual(first, {"recovered": 1, "unresolved": []})
        self.assertEqual(second, {"recovered": 0, "unresolved": []})
        override_paths = list((self.root / "governance" / "overrides").glob("OVR-*.yaml"))
        self.assertEqual(len(override_paths), 1)
        override = json.loads(override_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(override["correction_capture_ref"], transaction["correction_capture_ref"])
        self.assertEqual(json.loads(transaction_path.read_text(encoding="utf-8"))["stage"], "applied")
        self.assertEqual(organize(self.root, ref)["changed"], [])
        self.assertEqual(explain(self.root, ref)["human_override"]["correction_capture_ref"], transaction["correction_capture_ref"])

    def test_correction_prepared_and_claimed_crashes_replay_idempotently(self) -> None:
        for injection, expected_stage in (("fail_after_prepare", "prepared"), ("fail_after_claim", "claimed")):
            with self.subTest(injection=injection):
                with tempfile.TemporaryDirectory(prefix="kb2-correction-replay-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    initial = ingest_text(root, "纠正事务恢复前正文")
                    kwargs = {injection: True}
                    with self.assertRaises(KbError):
                        correct(root, initial["garden_ref"], "恢复必须幂等", **kwargs)
                    transaction_path = next((root / "governance" / "corrections").glob("COR-*/correction.json"))
                    self.assertEqual(json.loads(transaction_path.read_text(encoding="utf-8"))["stage"], expected_stage)
                    self.assertEqual(core.recover_corrections(root), {"recovered": 1, "unresolved": []})
                    self.assertEqual(core.recover_corrections(root), {"recovered": 0, "unresolved": []})
                    self.assertEqual(len(list((root / "governance" / "overrides").glob("OVR-*.yaml"))), 1)

    def test_correction_recovery_conflict_never_genericizes_or_overwrites(self) -> None:
        initial = ingest_text(self.root, "纠正恢复冲突前正文")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        with self.assertRaises(KbError):
            correct(self.root, ref, "候选已安装但尚未建覆盖", fail_after_install=True)

        external = "候选安装后的外部并发编辑必须保留。\n".encode("utf-8")
        note.write_bytes(external)
        transaction_dir = next((self.root / "governance" / "corrections").glob("COR-*"))
        displaced = (transaction_dir / "displaced.md").read_bytes()
        capture_payloads = {
            path.name: (path / "payload.bin").read_bytes()
            for path in self._capture_dirs()
        }

        first = core.recover_corrections(self.root)
        second = core.recover_corrections(self.root)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_CORRECTION_CONFLICT")
        self.assertEqual(second["unresolved"], first["unresolved"])
        self.assertEqual(note.read_bytes(), external)
        self.assertEqual((transaction_dir / "displaced.md").read_bytes(), displaced)
        self.assertEqual(
            {path.name: (path / "payload.bin").read_bytes() for path in self._capture_dirs()},
            capture_payloads,
        )
        self.assertEqual(list((self.root / "governance" / "overrides").glob("OVR-*.yaml")), [])
        for operation in (lambda: organize(self.root, ref), lambda: explain(self.root, ref)):
            with self.assertRaises(KbError) as blocked:
                operation()
            self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")
            self.assertEqual(note.read_bytes(), external)

    def test_unknown_correction_stage_is_sticky_and_blocks_fact_reads(self) -> None:
        initial = ingest_text(self.root, "未知纠正阶段前正文")
        ref = initial["garden_ref"]
        with self.assertRaises(KbError):
            correct(self.root, ref, "未知阶段纠正", fail_after_prepare=True)
        transaction_path = next((self.root / "governance" / "corrections").glob("COR-*/correction.json"))
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        transaction["stage"] = "future-correction-stage"
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        for _ in range(2):
            recovery = core.recover_corrections(self.root)
            self.assertEqual(recovery["recovered"], 0)
            self.assertEqual(recovery["unresolved"][0]["code"], "KB2_CORRECTION_STAGE_INVALID")
        with self.assertRaises(KbError) as blocked:
            explain(self.root, ref)
        self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")

    @unittest.skipUnless(os.name == "nt", "Windows ACL assertion")
    def test_pending_acl_has_inheritance_disabled(self) -> None:
        ingest_text(self.root, "ACL 验证")
        pending = self.root / "ingress" / "pending"
        acl, current_sid = self._windows_acl(pending)
        sids = {rule["sid"] for rule in acl["rules"]}
        self.assertTrue(acl["protected"])
        self.assertEqual(sids, {current_sid, "S-1-5-18"})
        self.assertTrue(all(rule["type"] == "Allow" for rule in acl["rules"]))

    @unittest.skipUnless(os.name == "nt", "Windows junction assertion")
    def test_ingress_junction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kb2-test-outside-", dir=r"D:\tmp") as outside_name:
            outside = Path(outside_name)
            link = self.root / "ingress"
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                check=False,
                capture_output=True,
            )
            self.assertEqual(made.returncode, 0)
            try:
                with self.assertRaises(KbError) as raised:
                    ingest_text(self.root, "不得越过 junction")
                self.assertIn(raised.exception.code, {"KB2_REPARSE_REJECTED", "KB2_PATH_ESCAPE"})
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                os.rmdir(link)

    def test_status_and_explain_are_fact_backed(self) -> None:
        result = ingest_text(self.root, "可解释的捕获")
        state = status(self.root)
        detail = explain(self.root, result["garden_ref"])

        self.assertEqual(state["counts"]["captures"], 1)
        self.assertEqual(state["counts"]["garden_notes"], 1)
        self.assertTrue(state["capabilities"]["release"])
        self.assertTrue(state["capabilities"]["projection"])
        self.assertEqual(state["phase"], 2)
        self.assertIn("phase-2", state["slice"])
        self.assertEqual(
            {key: state["counts"][key] for key in ("contexts", "overrides", "candidates", "artifacts", "revisions", "receipts")},
            {key: 0 for key in ("contexts", "overrides", "candidates", "artifacts", "revisions", "receipts")},
        )
        self.assertFalse(state["projection"]["implemented"])
        self.assertEqual(detail["capture_ref"], result["capture_ref"])
        self.assertEqual(detail["route"]["result"], "garden-organized")
        self.assertIsNone(detail["human_override"])

    def test_status_fails_closed_on_pointer_without_artifact_store(self) -> None:
        pointer_dir = self.root / "released" / "idempotency"
        pointer_dir.mkdir(parents=True)
        (pointer_dir / "idem-invalid.json").write_text("{", encoding="utf-8")

        with self.assertRaises(KbError) as raised:
            status(self.root)

        self.assertEqual(raised.exception.code, "KB2_RELEASE_INVALID")

    def test_status_capabilities_follow_component_availability(self) -> None:
        import kb2.bootstrap as bootstrap
        import kb2.release as release

        with mock.patch.object(release, "release_candidate", None), mock.patch.object(bootstrap, "build", None):
            state = status(self.root)

        self.assertFalse(state["capabilities"]["release"])
        self.assertFalse(state["capabilities"]["projection"])
        self.assertEqual(state["phase"], 1)
        self.assertIn("capture-context", state["slice"])
        self.assertNotIn("projection", state["slice"])


    def test_ingest_detects_post_scan_capture_owner_drift_without_secret_leak(self) -> None:
        for drift_metadata in (False, True):
            with self.subTest(drift_metadata=drift_metadata):
                with tempfile.TemporaryDirectory(prefix="kb2-capture-snapshot-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    original = b"safe capture snapshot"
                    marker = b"POST-SCAN-CAPTURE-DRIFT"
                    observed = marker + b" sk-" + (b"D" * 48)
                    original_scan = core._secret_reasons
                    injected = False

                    def drift_after_scan(data: bytes) -> list[str]:
                        nonlocal injected
                        reasons = original_scan(data)
                        if data == original and not injected:
                            capture = next((root / "ingress" / "pending").glob("CAP-*"))
                            (capture / "payload.bin").write_bytes(observed)
                            if drift_metadata:
                                metadata_path = capture / "capture.json"
                                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                                metadata["payload_digest"] = "sha256:" + __import__("hashlib").sha256(observed).hexdigest()
                                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                            injected = True
                        return reasons

                    with mock.patch.object(core, "_secret_reasons", side_effect=drift_after_scan):
                        with self.assertRaises(KbError) as raised:
                            core.ingest_bytes(root, original)

                    self.assertTrue(injected)
                    self.assertEqual(raised.exception.code, "KB2_CAPTURE_OWNER_DRIFT")
                    capture = next((root / "ingress" / "pending").glob("CAP-*"))
                    self.assertEqual((capture / "payload.bin").read_bytes(), original)
                    self.assertFalse((root / "garden").exists())
                    owner_paths = list(capture.glob("snapshot-drift-OBS-*.json"))
                    self.assertEqual(len(owner_paths), 1)
                    owner = json.loads(owner_paths[0].read_text(encoding="utf-8"))
                    observed_entry = capture / next(
                        item["entry"] for item in owner["retained_entries"] if item["kind"] == "payload-observed"
                    )
                    self.assertEqual(observed_entry.read_bytes(), observed)
                    metadata_entries = [
                        capture / item["entry"]
                        for item in owner["retained_entries"]
                        if item["kind"] == "metadata-observed"
                    ]
                    self.assertEqual(len(metadata_entries), int(drift_metadata))
                    if drift_metadata:
                        self.assertNotEqual(metadata_entries[0].read_bytes(), (capture / "capture.json").read_bytes())
                    occurrences = [
                        path.relative_to(root).as_posix()
                        for path in root.rglob("*")
                        if path.is_file() and marker in path.read_bytes()
                    ]
                    self.assertEqual(occurrences, [observed_entry.relative_to(root).as_posix()])

    def test_quarantine_base_missing_or_drift_is_sticky_and_preserves_owner(self) -> None:
        for mode in ("missing", "drift"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory(prefix="kb2-q-base-owner-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    result = ingest_text(root, "quarantine base owner")
                    ref = result["garden_ref"]
                    note = root / "garden" / "notes" / ref.rsplit("/", 1)[1]
                    garden_marker = b"SECRET-GARDEN-OWNER"
                    note.write_bytes(note.read_bytes() + b"\n" + garden_marker + b" sk-" + (b"B" * 48))
                    with self.assertRaises(KbError):
                        organize(root, ref, fail_after_quarantine=True)
                    quarantine = next((root / "ingress" / "quarantine").glob("HLD-*"))
                    state_dir = root / "governance" / "organizer-state" / result["capture_ref"].removeprefix("capture://")
                    base = state_dir / "base.md"
                    drift = b"OWNER-DRIFT-BASE sk-" + (b"E" * 48)
                    if mode == "missing":
                        base.unlink()
                    else:
                        base.write_bytes(drift)

                    first = recover_security_holds(root)
                    second = recover_security_holds(root)
                    expected_code = "KB2_ORGANIZER_BASE_MISSING" if mode == "missing" else "KB2_ORGANIZER_BASE_DRIFT"
                    self.assertEqual(first["recovered"], 0)
                    self.assertEqual(second["recovered"], 0)
                    self.assertEqual(first["unresolved"][0]["code"], expected_code)
                    self.assertEqual(second["unresolved"], first["unresolved"])
                    transaction = json.loads((quarantine / "quarantine.json").read_text(encoding="utf-8"))
                    self.assertEqual(transaction["stage"], "recovery-conflict")
                    self.assertFalse((root / "ingress" / "restricted-hold" / f"{quarantine.name}.json").exists())
                    self.assertFalse(core._secret_reasons(note.read_bytes()))
                    self.assertIn("# 内容已隔离".encode("utf-8"), note.read_bytes())
                    garden_occurrences = [
                        path.relative_to(root).as_posix()
                        for path in root.rglob("*")
                        if path.is_file() and garden_marker in path.read_bytes()
                    ]
                    self.assertEqual(len(garden_occurrences), 2)
                    self.assertTrue(all(path.startswith(quarantine.relative_to(root).as_posix()) for path in garden_occurrences))
                    if mode == "drift":
                        retained = quarantine / transaction["retained_organizer_base_entries"][0]["entry"]
                        self.assertEqual(retained.read_bytes(), drift)
                        occurrences = [
                            path.relative_to(root).as_posix()
                            for path in root.rglob("*")
                            if path.is_file() and b"OWNER-DRIFT-BASE" in path.read_bytes()
                        ]
                        self.assertEqual(occurrences, [retained.relative_to(root).as_posix()])

                    environment = os.environ.copy()
                    environment["PYTHONUTF8"] = "1"
                    environment["PYTHONDONTWRITEBYTECODE"] = "1"
                    cli = subprocess.run(
                        [sys.executable, "-B", "-m", "kb2.cli", "--root", str(root), "--json", "recover"],
                        check=False,
                        capture_output=True,
                        env=environment,
                    )
                    response = json.loads(cli.stdout.decode("utf-8"))
                    self.assertNotEqual(cli.returncode, 0)
                    self.assertEqual(response["code"], "KB2_RECOVERY_UNRESOLVED")
                    for operation in (lambda: organize(root, ref), lambda: explain(root, ref)):
                        with self.assertRaises(KbError) as blocked:
                            operation()
                        self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")

    def test_organize_detects_post_scan_garden_drift_and_fails_closed(self) -> None:
        for sensitive in (False, True):
            with self.subTest(sensitive=sensitive):
                with tempfile.TemporaryDirectory(prefix="kb2-organize-cas-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    initial = ingest_text(root, "organize CAS base")
                    ref = initial["garden_ref"]
                    note = root / "garden" / "notes" / ref.rsplit("/", 1)[1]
                    base = note.read_bytes()
                    scanned = base + b"\nSCANNED-HUMAN-VERSION\n"
                    marker = b"POST-SCAN-GARDEN-DRIFT"
                    observed = base + b"\n" + marker + (b" sk-" + (b"G" * 48) if sensitive else b" safe\n")
                    note.write_bytes(scanned)
                    original_scan = core._secret_reasons
                    injected = False

                    def drift_after_scan(data: bytes) -> list[str]:
                        nonlocal injected
                        reasons = original_scan(data)
                        if data == scanned and not injected:
                            note.write_bytes(observed)
                            injected = True
                        return reasons

                    with mock.patch.object(core, "_secret_reasons", side_effect=drift_after_scan):
                        with self.assertRaises(KbError) as raised:
                            organize(root, ref)

                    self.assertTrue(injected)
                    self.assertEqual(list((root / "governance" / "overrides").glob("OVR-*.yaml")), [])
                    state_path = root / "governance" / "organizer-state" / initial["capture_ref"].removeprefix("capture://") / "state.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    self.assertIsNone(state["active_override"])
                    if sensitive:
                        self.assertEqual(raised.exception.code, "KB2_RESTRICTED_EDIT")
                        self.assertFalse(core._secret_reasons(note.read_bytes()))
                        quarantine = next((root / "ingress" / "quarantine").glob("HLD-*"))
                        occurrences = [
                            path.relative_to(root).as_posix()
                            for path in root.rglob("*")
                            if path.is_file() and marker in path.read_bytes()
                        ]
                        self.assertEqual(len(occurrences), 2)
                        self.assertTrue(all(path.startswith(quarantine.relative_to(root).as_posix()) for path in occurrences))
                        detail = explain(root, ref)
                        self.assertEqual(detail["security"]["precheck"], "rejected")
                    else:
                        self.assertEqual(raised.exception.code, "KB2_GARDEN_CONFLICT")
                        self.assertEqual(note.read_bytes(), observed)
                        conflict = next((root / "governance" / "organizer-conflicts").glob("CNF-*"))
                        self.assertEqual((conflict / "expected.md").read_bytes(), scanned)
                        for operation in (lambda: organize(root, ref), lambda: explain(root, ref)):
                            with self.assertRaises(KbError) as blocked:
                                operation()
                            self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")

    def test_organize_postcondition_rechecks_garden_after_state_commit(self) -> None:
        for sensitive in (False, True):
            with self.subTest(sensitive=sensitive):
                with tempfile.TemporaryDirectory(prefix="kb2-organize-postcondition-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    initial = ingest_text(root, "organize postcondition base")
                    ref = initial["garden_ref"]
                    note = root / "garden" / "notes" / ref.rsplit("/", 1)[1]
                    committed = note.read_bytes() + b"\nCOMMITTED-HUMAN-VERSION\n"
                    marker = b"POST-COMMIT-GARDEN-DRIFT"
                    observed = committed + b"\n" + marker + (b" sk-" + (b"H" * 48) if sensitive else b" safe\n")
                    note.write_bytes(committed)
                    original_save = core._save_organizer_state
                    injected = False

                    def inject_after_state(state_dir: Path, state: dict[str, object]) -> None:
                        nonlocal injected
                        original_save(state_dir, state)
                        if not injected:
                            note.write_bytes(observed)
                            injected = True

                    with mock.patch.object(core, "_save_organizer_state", side_effect=inject_after_state):
                        with self.assertRaises(KbError) as raised:
                            organize(root, ref)

                    self.assertTrue(injected)
                    self.assertEqual(len(list((root / "governance" / "overrides").glob("OVR-*.yaml"))), 1)
                    if sensitive:
                        self.assertEqual(raised.exception.code, "KB2_RESTRICTED_EDIT")
                        self.assertEqual(note.read_bytes(), committed)
                        self.assertFalse(core._secret_reasons(note.read_bytes()))
                        detail = explain(root, ref)
                        self.assertEqual(detail["security"]["precheck"], "rejected")
                        self.assertTrue(detail["human_override"])
                    else:
                        self.assertEqual(raised.exception.code, "KB2_GARDEN_CONFLICT")
                        self.assertEqual(note.read_bytes(), observed)
                        conflict = next((root / "governance" / "organizer-conflicts").glob("CNF-*"))
                        self.assertEqual((conflict / "expected.md").read_bytes(), committed)
                        with self.assertRaises(KbError) as blocked:
                            explain(root, ref)
                        self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")

    def test_quarantine_missing_base_reappearance_after_stub_is_claimed_and_sticky(self) -> None:
        initial = ingest_text(self.root, "missing base post-stub reappearance")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        note.write_bytes(note.read_bytes() + b" sk-" + (b"J" * 48))
        with self.assertRaises(KbError):
            organize(self.root, ref, fail_after_quarantine=True)
        state_dir = self.root / "governance" / "organizer-state" / initial["capture_ref"].removeprefix("capture://")
        base = state_dir / "base.md"
        base.unlink()
        marker = b"POST-STUB-SECRET-BASE"
        secret_base = marker + b" sk-" + (b"K" * 48)
        original_save = core._save_organizer_state
        injected = False

        def inject_after_stub_state(owner: Path, state: dict[str, object]) -> None:
            nonlocal injected
            original_save(owner, state)
            if owner == state_dir and not injected:
                base.write_bytes(secret_base)
                injected = True

        with mock.patch.object(core, "_save_organizer_state", side_effect=inject_after_stub_state):
            first = recover_security_holds(self.root)
        self.assertTrue(injected)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_ORGANIZER_BASE_MISSING")
        self.assertFalse(core._secret_reasons(base.read_bytes()))
        quarantine = next((self.root / "ingress" / "quarantine").glob("HLD-*"))
        transaction_path = quarantine / "quarantine.json"
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        retained = [quarantine / item["entry"] for item in transaction["retained_organizer_base_entries"]]
        matching = [path for path in retained if marker in path.read_bytes()]
        self.assertEqual(len(matching), 1)
        occurrences = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and marker in path.read_bytes()
        ]
        self.assertEqual(occurrences, [matching[0].relative_to(self.root).as_posix()])

        late_marker = b"REPLAY-SECRET-BASE"
        base.write_bytes(late_marker + b" sk-" + (b"L" * 48))
        second = recover_security_holds(self.root)
        third = recover_security_holds(self.root)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(third["recovered"], 0)
        self.assertEqual(second["unresolved"][0]["code"], "KB2_ORGANIZER_BASE_MISSING")
        self.assertEqual(third["unresolved"], second["unresolved"])
        self.assertFalse(core._secret_reasons(base.read_bytes()))
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        replay_retained = [quarantine / item["entry"] for item in transaction["retained_organizer_base_entries"]]
        self.assertEqual(sum(late_marker in path.read_bytes() for path in replay_retained), 1)

    def test_correction_capture_scan_drift_is_retained_and_never_reaches_garden(self) -> None:
        for drift_payload in (False, True):
            with self.subTest(drift_payload=drift_payload):
                with tempfile.TemporaryDirectory(prefix="kb2-correction-snapshot-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    initial = ingest_text(root, "correction snapshot target")
                    ref = initial["garden_ref"]
                    note = root / "garden" / "notes" / ref.rsplit("/", 1)[1]
                    before = note.read_bytes()
                    correction = b"safe correction snapshot"
                    marker = b"CORRECTION-SCAN-DRIFT"
                    observed = marker + b" sk-" + (b"M" * 48)
                    original_scan = core._secret_reasons
                    injected = False

                    def drift_after_scan(data: bytes) -> list[str]:
                        nonlocal injected
                        reasons = original_scan(data)
                        if data == correction and not injected:
                            capture = max((root / "ingress" / "pending").glob("CAP-*"), key=lambda p: p.stat().st_ctime_ns)
                            metadata_path = capture / "capture.json"
                            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                            metadata["route"] = {"result": "passed-by-drift"}
                            if drift_payload:
                                (capture / "payload.bin").write_bytes(observed)
                                metadata["payload_digest"] = "sha256:" + __import__("hashlib").sha256(observed).hexdigest()
                            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                            injected = True
                        return reasons

                    with mock.patch.object(core, "_secret_reasons", side_effect=drift_after_scan):
                        with self.assertRaises(KbError) as raised:
                            core.correct_bytes(root, ref, correction)
                    self.assertTrue(injected)
                    self.assertEqual(raised.exception.code, "KB2_CAPTURE_OWNER_DRIFT")
                    self.assertEqual(note.read_bytes(), before)
                    self.assertFalse((root / "governance" / "corrections").exists())
                    correction_capture = max((root / "ingress" / "pending").glob("CAP-*"), key=lambda p: p.stat().st_ctime_ns)
                    self.assertEqual((correction_capture / "payload.bin").read_bytes(), correction)
                    drift_owners = list(correction_capture.glob("snapshot-drift-OBS-*.json"))
                    self.assertEqual(len(drift_owners), 1)
                    if drift_payload:
                        occurrences = [
                            path.relative_to(root).as_posix()
                            for path in root.rglob("*")
                            if path.is_file() and marker in path.read_bytes()
                        ]
                        self.assertEqual(len(occurrences), 1)
                        self.assertIn("payload-observed-", occurrences[0])

    def test_capture_drift_owner_blocks_explain_and_global_recovery(self) -> None:
        initial = ingest_text(self.root, "successful ingest before owner drift")
        ref = initial["garden_ref"]
        capture = self.root / "ingress" / "pending" / initial["capture_ref"].removeprefix("capture://")
        marker = b"EXPLAIN-CAPTURE-DRIFT"
        observed = marker + b" sk-" + (b"N" * 48)
        metadata_path = capture / "capture.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        (capture / "payload.bin").write_bytes(observed)
        metadata["payload_digest"] = "sha256:" + __import__("hashlib").sha256(observed).hexdigest()
        metadata["route"] = {"result": "garden-organized"}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with self.assertRaises(KbError) as blocked:
            explain(self.root, ref)
        self.assertEqual(blocked.exception.code, "KB2_RECOVERY_UNRESOLVED")
        self.assertEqual((capture / "payload.bin").read_text(encoding="utf-8"), "successful ingest before owner drift")
        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_CAPTURE_OWNER_DRIFT")
        self.assertEqual(second["unresolved"], first["unresolved"])
        occurrences = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and marker in path.read_bytes()
        ]
        self.assertEqual(len(occurrences), 1)
        self.assertIn("payload-observed-", occurrences[0])

    def test_organize_commit_to_result_boundary_rechecks_after_decision(self) -> None:
        initial = ingest_text(self.root, "commit result boundary base")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        committed = note.read_bytes() + b"\nCOMMIT-RESULT-SAFE\n"
        note.write_bytes(committed)
        marker = b"DECISION-ROUTE-SECRET"
        secret = committed + b"\n" + marker + b" sk-" + (b"P" * 48)
        original_route = core._decision_route
        injected = False

        def inject_at_result(state: dict[str, object]) -> dict[str, object]:
            nonlocal injected
            route = original_route(state)
            if not injected:
                note.write_bytes(secret)
                injected = True
            return route

        with mock.patch.object(core, "_decision_route", side_effect=inject_at_result):
            with self.assertRaises(KbError) as raised:
                organize(self.root, ref)
        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "KB2_RESTRICTED_EDIT")
        self.assertEqual(note.read_bytes(), committed)
        self.assertFalse(core._secret_reasons(note.read_bytes()))
        detail = explain(self.root, ref)
        self.assertEqual(detail["security"]["precheck"], "rejected")

    def test_organizer_conflict_replay_reclassifies_secret_and_keeps_safe_reappearance(self) -> None:
        initial = ingest_text(self.root, "organizer conflict replay base")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        scanned = note.read_bytes() + b"\nEXPECTED-SAFE-SIDE\n"
        observed = note.read_bytes() + b"\nOBSERVED-SAFE-SIDE\n"
        note.write_bytes(scanned)
        original_scan = core._secret_reasons
        injected = False

        def create_safe_conflict(data: bytes) -> list[str]:
            nonlocal injected
            reasons = original_scan(data)
            if data == scanned and not injected:
                note.write_bytes(observed)
                injected = True
            return reasons

        with mock.patch.object(core, "_secret_reasons", side_effect=create_safe_conflict):
            with self.assertRaises(KbError):
                organize(self.root, ref)
        conflict = next((self.root / "governance" / "organizer-conflicts").glob("CNF-*"))
        expected = (conflict / "expected.md").read_bytes()
        safe_reappearance = observed + b"\nSAFE-REAPPEARANCE\n"
        note.write_bytes(safe_reappearance)
        safe_replay = core.recover_all(self.root)
        self.assertEqual(safe_replay["recovered"], 0)
        self.assertEqual(note.read_bytes(), safe_reappearance)
        self.assertEqual((conflict / "expected.md").read_bytes(), expected)

        marker = b"CNF-REPLAY-SECRET"
        secret = safe_reappearance + b"\n" + marker + b" sk-" + (b"Q" * 48)
        note.write_bytes(secret)
        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(second["recovered"], 0)
        self.assertFalse(core._secret_reasons(note.read_bytes()))
        self.assertEqual((conflict / "expected.md").read_bytes(), expected)
        occurrences = [
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and marker in path.read_bytes()
        ]
        self.assertTrue(occurrences)
        self.assertTrue(all(path.startswith("ingress/quarantine/") for path in occurrences))

    def test_ingest_final_capture_update_rejects_metadata_drift(self) -> None:
        original_update = core._update_capture
        marker = "INGEST-FINAL-METADATA-DRIFT"
        injected = False

        def inject_before_final_update(
            root: Path,
            capture_dir: Path,
            metadata: dict[str, object],
            **kwargs: object,
        ) -> None:
            nonlocal injected
            metadata_path = capture_dir / "capture.json"
            current = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("state") == "garden-organized" and not injected:
                current["drift_marker"] = marker
                metadata_path.write_text(json.dumps(current), encoding="utf-8")
                injected = True
            original_update(root, capture_dir, metadata, **kwargs)

        with mock.patch.object(core, "_update_capture", side_effect=inject_before_final_update):
            with self.assertRaises(KbError) as raised:
                ingest_text(self.root, "ingest final update CAS")

        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "KB2_CAPTURE_OWNER_DRIFT")
        capture = self._capture_dirs()[0]
        canonical = json.loads((capture / "capture.json").read_text(encoding="utf-8"))
        self.assertNotIn("drift_marker", canonical)
        owners = list(capture.glob("snapshot-drift-OBS-*.json"))
        self.assertEqual(len(owners), 1)
        observed_metadata = [
            capture / item["entry"]
            for item in json.loads(owners[0].read_text(encoding="utf-8"))["retained_entries"]
            if item["kind"] == "metadata-observed"
        ]
        self.assertEqual(len(observed_metadata), 1)
        self.assertIn(marker, observed_metadata[0].read_text(encoding="utf-8"))
        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_CAPTURE_OWNER_DRIFT")
        self.assertEqual(second["unresolved"], first["unresolved"])

    def test_correction_final_capture_update_rejects_metadata_drift(self) -> None:
        initial = ingest_text(self.root, "correction final update target")
        ref = initial["garden_ref"]
        note = self.root / "garden" / "notes" / ref.rsplit("/", 1)[1]
        original_update = core._update_capture
        marker = "CORRECTION-FINAL-METADATA-DRIFT"
        injected = False

        def inject_before_applied_update(
            root: Path,
            capture_dir: Path,
            metadata: dict[str, object],
            **kwargs: object,
        ) -> None:
            nonlocal injected
            metadata_path = capture_dir / "capture.json"
            current = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("state") == "correction-applied" and not injected:
                current["drift_marker"] = marker
                metadata_path.write_text(json.dumps(current), encoding="utf-8")
                injected = True
            original_update(root, capture_dir, metadata, **kwargs)

        with mock.patch.object(core, "_update_capture", side_effect=inject_before_applied_update):
            with self.assertRaises(KbError) as raised:
                correct(self.root, ref, "correction final update CAS")

        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "KB2_CAPTURE_OWNER_DRIFT")
        correction_capture = max(self._capture_dirs(), key=lambda path: path.stat().st_ctime_ns)
        canonical = json.loads((correction_capture / "capture.json").read_text(encoding="utf-8"))
        self.assertNotIn("drift_marker", canonical)
        owners = list(correction_capture.glob("snapshot-drift-OBS-*.json"))
        self.assertEqual(len(owners), 1)
        observed_metadata = [
            correction_capture / item["entry"]
            for item in json.loads(owners[0].read_text(encoding="utf-8"))["retained_entries"]
            if item["kind"] == "metadata-observed"
        ]
        self.assertEqual(len(observed_metadata), 1)
        self.assertIn(marker, observed_metadata[0].read_text(encoding="utf-8"))
        transaction = next((self.root / "governance" / "corrections").glob("COR-*/correction.json"))
        self.assertNotEqual(json.loads(transaction.read_text(encoding="utf-8"))["stage"], "applied")
        self.assertIn("correction final update CAS", note.read_text(encoding="utf-8"))
        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertEqual(first["recovered"], 0)
        self.assertEqual(second["recovered"], 0)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_CAPTURE_OWNER_DRIFT")
        self.assertEqual(second["unresolved"], first["unresolved"])

    def test_ingest_capture_update_claims_metadata_changed_after_loader_return(self) -> None:
        original_loader = core._load_capture_owner
        marker = "INGEST-AFTER-LOADER-METADATA-DRIFT"
        injected = False

        def inject_after_loader(
            root: Path,
            capture_dir: Path,
            **kwargs: object,
        ) -> tuple[dict[str, object], bytes]:
            nonlocal injected
            result = original_loader(root, capture_dir, **kwargs)
            in_update = any(frame.function == "_update_capture" for frame in __import__("inspect").stack())
            if kwargs.get("expected_metadata") is not None and in_update and not injected:
                metadata_path = capture_dir / "capture.json"
                current = json.loads(metadata_path.read_text(encoding="utf-8"))
                current["drift_marker"] = marker
                metadata_path.write_text(json.dumps(current), encoding="utf-8")
                injected = True
            return result

        with mock.patch.object(core, "_load_capture_owner", side_effect=inject_after_loader):
            with self.assertRaises(KbError) as raised:
                ingest_text(self.root, "ingest loader-return claim")

        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "KB2_CAPTURE_OWNER_DRIFT")
        capture = self._capture_dirs()[0]
        self.assertNotIn(marker, (capture / "capture.json").read_text(encoding="utf-8"))
        owner = json.loads(next(capture.glob("snapshot-drift-OBS-*.json")).read_text(encoding="utf-8"))
        observed = [capture / item["entry"] for item in owner["retained_entries"] if item["kind"] == "metadata-observed"]
        self.assertEqual(len(observed), 1)
        self.assertIn(marker, observed[0].read_text(encoding="utf-8"))
        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_CAPTURE_OWNER_DRIFT")
        self.assertEqual(second["unresolved"], first["unresolved"])

    def test_correction_capture_update_claims_metadata_changed_after_loader_return(self) -> None:
        initial = ingest_text(self.root, "correction loader-return target")
        ref = initial["garden_ref"]
        original_loader = core._load_capture_owner
        marker = "CORRECTION-AFTER-LOADER-METADATA-DRIFT"
        injected = False

        def inject_after_loader(
            root: Path,
            capture_dir: Path,
            **kwargs: object,
        ) -> tuple[dict[str, object], bytes]:
            nonlocal injected
            result = original_loader(root, capture_dir, **kwargs)
            in_update = any(frame.function == "_update_capture" for frame in __import__("inspect").stack())
            if kwargs.get("expected_metadata") is not None and in_update and not injected:
                metadata_path = capture_dir / "capture.json"
                current = json.loads(metadata_path.read_text(encoding="utf-8"))
                current["drift_marker"] = marker
                metadata_path.write_text(json.dumps(current), encoding="utf-8")
                injected = True
            return result

        with mock.patch.object(core, "_load_capture_owner", side_effect=inject_after_loader):
            with self.assertRaises(KbError) as raised:
                correct(self.root, ref, "correction loader-return claim")

        self.assertTrue(injected)
        self.assertEqual(raised.exception.code, "KB2_CAPTURE_OWNER_DRIFT")
        correction_capture = max(self._capture_dirs(), key=lambda path: path.stat().st_ctime_ns)
        self.assertNotIn(marker, (correction_capture / "capture.json").read_text(encoding="utf-8"))
        owner = json.loads(next(correction_capture.glob("snapshot-drift-OBS-*.json")).read_text(encoding="utf-8"))
        observed = [
            correction_capture / item["entry"]
            for item in owner["retained_entries"]
            if item["kind"] == "metadata-observed"
        ]
        self.assertEqual(len(observed), 1)
        self.assertIn(marker, observed[0].read_text(encoding="utf-8"))
        transaction = next((self.root / "governance" / "corrections").glob("COR-*/correction.json"))
        self.assertNotEqual(json.loads(transaction.read_text(encoding="utf-8"))["stage"], "applied")
        first = core.recover_all(self.root)
        second = core.recover_all(self.root)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_CAPTURE_OWNER_DRIFT")
        self.assertEqual(second["unresolved"], first["unresolved"])

    def test_capture_metadata_update_prepared_claimed_installed_recovery_is_idempotent(self) -> None:
        for stage in ("prepared", "claimed", "installed"):
            with self.subTest(stage=stage):
                with tempfile.TemporaryDirectory(prefix="kb2-capture-update-recovery-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    result = ingest_text(root, "capture update recovery")
                    capture = root / "ingress" / "pending" / result["capture_ref"].removeprefix("capture://")
                    metadata, payload = core._capture_update_snapshot(root, capture, core._load_capture_owner(root, capture)[0])
                    updated = json.loads(json.dumps(metadata))
                    updated["recovery_probe"] = stage
                    with mock.patch.object(core, "_advance_capture_metadata_update", return_value=None):
                        core._update_capture(
                            root,
                            capture,
                            updated,
                            expected_metadata=metadata,
                            expected_payload=payload,
                        )
                    transaction_path = max(capture.glob("capture-update-UPD-*.json"), key=lambda path: path.stat().st_ctime_ns)
                    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
                    candidate = capture / transaction["candidate_entry"]
                    claimed = capture / transaction["claimed_entry"]
                    canonical = capture / "capture.json"
                    if stage in {"claimed", "installed"}:
                        core._move_file_to_absent(canonical, claimed)
                        transaction["stage"] = "claimed"
                        transaction["claimed_digest"] = "sha256:" + __import__("hashlib").sha256(claimed.read_bytes()).hexdigest()
                    if stage == "installed":
                        core._move_file_to_absent(candidate, canonical)
                        transaction["stage"] = "installed"
                    transaction_path.write_text(json.dumps(transaction), encoding="utf-8")

                    first = core.recover_capture_owners(root)
                    second = core.recover_capture_owners(root)
                    self.assertEqual(first, {"recovered": 1, "unresolved": []})
                    self.assertEqual(second, {"recovered": 0, "unresolved": []})
                    self.assertEqual(json.loads(canonical.read_text(encoding="utf-8"))["recovery_probe"], stage)
                    self.assertEqual(json.loads(transaction_path.read_text(encoding="utf-8"))["stage"], "applied")
                    owned, owned_payload = core._load_capture_owner(root, capture)
                    self.assertEqual(owned["recovery_probe"], stage)
                    self.assertEqual(owned_payload, payload)

    def test_capture_metadata_update_claimed_drift_interruption_recovers_sticky_owner(self) -> None:
        original_loader = core._load_capture_owner
        marker = "CLAIMED-DRIFT-INTERRUPTION-METADATA"
        injected = False

        def inject_after_loader(
            root: Path,
            capture_dir: Path,
            **kwargs: object,
        ) -> tuple[dict[str, object], bytes]:
            nonlocal injected
            result = original_loader(root, capture_dir, **kwargs)
            in_update = any(frame.function == "_update_capture" for frame in __import__("inspect").stack())
            if kwargs.get("expected_metadata") is not None and in_update and not injected:
                metadata_path = capture_dir / "capture.json"
                current = json.loads(metadata_path.read_text(encoding="utf-8"))
                current["drift_marker"] = marker
                metadata_path.write_text(json.dumps(current), encoding="utf-8")
                injected = True
            return result

        def interrupt_before_drift_retention(*args: object, **kwargs: object) -> None:
            raise KbError("KB2_TEST_INTERRUPT", "simulated interruption before drift retention", 3)

        with mock.patch.object(core, "_load_capture_owner", side_effect=inject_after_loader):
            with mock.patch.object(core, "_mark_capture_update_drift", side_effect=interrupt_before_drift_retention):
                with self.assertRaises(KbError) as interrupted:
                    ingest_text(self.root, "claimed drift interruption")

        self.assertEqual(interrupted.exception.code, "KB2_TEST_INTERRUPT")
        self.assertTrue(injected)
        capture = self._capture_dirs()[0]
        transaction_path = next(capture.glob("capture-update-UPD-*.json"))
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        claimed = capture / transaction["claimed_entry"]
        self.assertEqual(transaction["stage"], "claimed")
        self.assertFalse((capture / "capture.json").exists())
        self.assertIn(marker, claimed.read_text(encoding="utf-8"))

        first = core.recover_capture_owners(self.root)
        second = core.recover_capture_owners(self.root)
        self.assertEqual(first["unresolved"][0]["code"], "KB2_CAPTURE_OWNER_DRIFT")
        self.assertEqual(second["unresolved"], first["unresolved"])
        expected = json.loads((capture / "owner.json").read_text(encoding="utf-8"))["metadata_snapshot"]
        self.assertEqual(json.loads((capture / "capture.json").read_text(encoding="utf-8")), expected)
        drift = json.loads(next(capture.glob("snapshot-drift-OBS-*.json")).read_text(encoding="utf-8"))
        observed = [capture / item["entry"] for item in drift["retained_entries"]]
        self.assertTrue(any(marker in path.read_text(encoding="utf-8") for path in observed if path.suffix == ".json"))

    def test_capture_metadata_update_expected_owner_failures_are_sticky(self) -> None:
        for failure in ("missing", "drift", "invalid-json"):
            with self.subTest(failure=failure):
                with tempfile.TemporaryDirectory(prefix="kb2-capture-update-expected-", dir=r"D:\tmp") as root_name:
                    root = Path(root_name)
                    (root / "kb.yaml").write_text(
                        "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\n",
                        encoding="utf-8",
                    )
                    result = ingest_text(root, "capture expected owner guard")
                    capture = root / "ingress" / "pending" / result["capture_ref"].removeprefix("capture://")
                    metadata, payload = core._load_capture_owner(root, capture)
                    updated = json.loads(json.dumps(metadata))
                    updated["expected_owner_probe"] = failure
                    with mock.patch.object(core, "_advance_capture_metadata_update", return_value=None):
                        core._update_capture(
                            root,
                            capture,
                            updated,
                            expected_metadata=metadata,
                            expected_payload=payload,
                        )
                    transaction_path = max(
                        capture.glob("capture-update-UPD-*.json"),
                        key=lambda path: path.stat().st_ctime_ns,
                    )
                    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
                    expected_path = capture / transaction["expected_entry"]
                    if failure == "missing":
                        expected_path.unlink()
                    elif failure == "drift":
                        changed = json.loads(expected_path.read_text(encoding="utf-8"))
                        changed["drift_marker"] = "EXPECTED-OWNER-DRIFT"
                        expected_path.write_text(json.dumps(changed), encoding="utf-8")
                    else:
                        expected_path.write_text("{not-json", encoding="utf-8")

                    first = core.recover_capture_owners(root)
                    second = core.recover_capture_owners(root)
                    expected_result = {
                        "recovered": 0,
                        "unresolved": [{"capture": capture.name, "code": "KB2_CAPTURE_UPDATE_INVALID"}],
                    }
                    self.assertEqual(first, expected_result)
                    self.assertEqual(second, expected_result)
                    self.assertEqual(json.loads((capture / "capture.json").read_text(encoding="utf-8")), metadata)

    @unittest.skipUnless(os.name == "nt", "Windows expected-owner junction assertion")
    def test_capture_metadata_update_expected_owner_reparse_fails_before_read(self) -> None:
        result = ingest_text(self.root, "capture expected owner reparse guard")
        capture = self.root / "ingress" / "pending" / result["capture_ref"].removeprefix("capture://")
        metadata, payload = core._load_capture_owner(self.root, capture)
        updated = json.loads(json.dumps(metadata))
        updated["expected_owner_probe"] = "reparse"
        with mock.patch.object(core, "_advance_capture_metadata_update", return_value=None):
            core._update_capture(
                self.root,
                capture,
                updated,
                expected_metadata=metadata,
                expected_payload=payload,
            )
        transaction_path = max(
            capture.glob("capture-update-UPD-*.json"),
            key=lambda path: path.stat().st_ctime_ns,
        )
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        expected_path = capture / transaction["expected_entry"]
        expected_path.unlink()
        with tempfile.TemporaryDirectory(prefix="kb2-capture-update-expected-reparse-", dir=r"D:\tmp") as outside_name:
            outside = Path(outside_name)
            (outside / "sentinel.json").write_text("external expected owner", encoding="utf-8")
            before = self._tree_snapshot(outside)
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(expected_path), str(outside)],
                capture_output=True,
            )
            self.assertEqual(made.returncode, 0, made.stderr.decode(errors="replace"))
            original_read = Path.read_bytes

            def reject_external_read(path: Path) -> bytes:
                if path.is_relative_to(outside):
                    raise AssertionError(f"recovery read reparse expected owner: {path}")
                return original_read(path)

            try:
                with mock.patch.object(Path, "read_bytes", reject_external_read):
                    first = core.recover_capture_owners(self.root)
                    second = core.recover_capture_owners(self.root)
                expected_result = {
                    "recovered": 0,
                    "unresolved": [{"capture": capture.name, "code": "KB2_CAPTURE_UPDATE_INVALID"}],
                }
                self.assertEqual(first, expected_result)
                self.assertEqual(second, expected_result)
                self.assertEqual(self._tree_snapshot(outside), before)
                self.assertEqual(json.loads((capture / "capture.json").read_text(encoding="utf-8")), metadata)
            finally:
                os.rmdir(expected_path)


if __name__ == "__main__":
    unittest.main()
