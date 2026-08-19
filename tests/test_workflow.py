from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from kb2 import bootstrap
from kb2.cli import main
from kb2.result import KbError


class PublishTextWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        Path(r"D:\tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="kb2-workflow-root-", dir=r"D:\tmp")
        self.root = Path(self.temp.name)
        (self.root / "kb.yaml").write_text(
            "schema: kb-root/v0.1\nid: KB-01KZPQC53JGD8174JZEEVACPJK\nprotocol: PROTOCOL.md\n",
            encoding="utf-8",
        )
        (self.root / "PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, args: list[str], stdin: bytes = b"") -> tuple[int, dict[str, object]]:
        output = StringIO()
        with mock.patch("sys.stdin") as patched_stdin, redirect_stdout(output):
            patched_stdin.buffer.read.return_value = stdin
            exit_code = main(["--root", str(self.root), "--json", *args])
        return exit_code, json.loads(output.getvalue())

    def test_public_text_runs_zero_form_loop_and_republish_is_idempotent(self) -> None:
        code, result = self.run_cli(["publish-text"], b"Verified local Python fallback runbook\nUse the bundled runtime when system Python is absent.\n")
        self.assertEqual(code, 0)
        self.assertEqual(result["code"], "KB2_OK")
        data = result["data"]
        self.assertEqual(data["user_structured_fields"], 0)
        self.assertEqual(data["route"], "garden-organized")
        self.assertTrue(data["release"]["release_committed"])
        self.assertTrue(data["projection"]["fresh"])
        self.assertEqual(data["find"]["matches"][0]["uri"], f"artifact://{data['release']['artifact_id']}")

        code, repeated = self.run_cli(["publish", "--candidate", data["candidate"]["path"]])
        self.assertEqual(code, 0)
        self.assertEqual(repeated["data"]["release"]["artifact_id"], data["release"]["artifact_id"])
        self.assertEqual(repeated["data"]["release"]["receipt_id"], data["release"]["receipt_id"])
        receipts = list((self.root / "released" / "artifacts").rglob("receipt.json"))
        self.assertEqual(len(receipts), 1)

    def test_projection_failure_keeps_committed_release(self) -> None:
        with mock.patch("kb2.workflow.bootstrap.build", side_effect=KbError("KB2_BUILD_FAILED", "injected", 4)):
            code, result = self.run_cli(["publish-text"], b"Projection failure separation\nPublic operational note.\n")
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "KB2_PUBLISHED_INDEX_STALE")
        self.assertTrue(result["data"]["release"]["release_committed"])
        self.assertFalse(result["data"]["projection"]["fresh"])
        self.assertEqual(len(list((self.root / "released" / "artifacts").rglob("receipt.json"))), 1)

    def test_secret_like_text_is_captured_without_candidate_or_release(self) -> None:
        secret = b"Authorization: Bearer " + (b"A" * 32)
        code, result = self.run_cli(["publish-text"], secret)
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "KB2_POLICY_REJECTED")
        self.assertTrue(any((self.root / "ingress" / "pending").glob("CAP-*")))
        self.assertFalse((self.root / "governance" / "release-candidates").exists())
        self.assertFalse((self.root / "released").exists())

    def test_relative_root_matches_absolute_root(self) -> None:
        previous = Path.cwd()
        try:
            import os
            os.chdir(self.root)
            code, result = self.run_cli(["publish-text"], b"Relative root workflow\nPublic text.\n")
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0)
        self.assertEqual(result["code"], "KB2_OK")
        self.assertTrue(bootstrap.status(self.root)["fresh"])
