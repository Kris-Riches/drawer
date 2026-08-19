from __future__ import annotations

import os
from pathlib import Path
import tempfile
import uuid


TEST_TMPDIR_ENV = "DRAWER_TEST_TMPDIR"


def _configured_temp_parent() -> str | None:
    configured = os.environ.get(TEST_TMPDIR_ENV)
    if not configured:
        return None

    parent = Path(configured).expanduser().resolve()
    probe = parent / f".drawer-test-write-probe-{uuid.uuid4().hex}"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(probe)
        os.rmdir(probe)
    except OSError as exc:
        raise RuntimeError(
            f"{TEST_TMPDIR_ENV} is not writable: {parent}. "
            f"Unset {TEST_TMPDIR_ENV} or choose a writable directory."
        ) from exc
    return str(parent)


def temporary_directory(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix=prefix, dir=_configured_temp_parent())


def make_temporary_directory(*, prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=_configured_temp_parent())
