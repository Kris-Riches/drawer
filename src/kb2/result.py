"""Stable JSON result envelope for the pilot CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class KbError(Exception):
    code: str
    message: str
    exit_code: int = 4
    data: dict[str, Any] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.message


def envelope(
    *,
    ok: bool,
    code: str,
    message: str,
    data: dict[str, Any] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    changed: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "kb2-result/v0.1",
        "ok": ok,
        "code": code,
        "message": message,
        "data": data or {},
        "diagnostics": diagnostics or [],
        "changed": changed or [],
    }
