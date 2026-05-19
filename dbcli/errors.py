from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    SUCCESS = 0
    INTERNAL_ERROR = 1
    USAGE_OR_DRIFT = 2
    VALIDATION_ERROR = 10
    REJECT_THRESHOLD = 20
    DB_OR_PROFILE_ERROR = 30


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "details": self.details,
        }


class DbcliError(Exception):
    def __init__(
        self,
        diagnostic: Diagnostic,
        exit_code: ExitCode = ExitCode.INTERNAL_ERROR,
    ) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
        self.exit_code = exit_code
