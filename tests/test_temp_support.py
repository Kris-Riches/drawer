from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests._temp_support import (
    TEST_TMPDIR_ENV,
    make_temporary_directory,
    temporary_directory,
)


class TestTemporaryDirectoryContractTests(unittest.TestCase):
    def test_default_uses_system_temporary_directory(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(TEST_TMPDIR_ENV, None)
            with temporary_directory(prefix="drawer-default-") as name:
                self.assertEqual(Path(name).parent, Path(tempfile.gettempdir()))

    def test_configured_parent_is_used_by_both_helpers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="drawer-parent-") as parent_name:
            with mock.patch.dict(os.environ, {TEST_TMPDIR_ENV: parent_name}):
                with temporary_directory(prefix="drawer-managed-") as managed_name:
                    self.assertEqual(Path(managed_name).parent, Path(parent_name))

                unmanaged_name = make_temporary_directory(prefix="drawer-unmanaged-")
                try:
                    self.assertEqual(Path(unmanaged_name).parent, Path(parent_name))
                finally:
                    Path(unmanaged_name).rmdir()

    def test_unwritable_configured_parent_fails_immediately(self) -> None:
        with tempfile.TemporaryDirectory(prefix="drawer-parent-") as parent_name:
            with mock.patch.dict(os.environ, {TEST_TMPDIR_ENV: parent_name}), mock.patch(
                "tests._temp_support.os.mkdir",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaisesRegex(RuntimeError, rf"{TEST_TMPDIR_ENV} is not writable"):
                    temporary_directory(prefix="drawer-denied-")


if __name__ == "__main__":
    unittest.main()
