"""Password storage for local profile secrets."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Protocol

from dbcli.core.errors import DbcliError, Diagnostic, ExitCode


class PasswordStore(Protocol):
    def set_password(self, service: str, account: str, password: str) -> None:
        ...

    def get_password(self, service: str, account: str) -> str:
        ...


@dataclass
class InMemoryPasswordStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def set_password(self, service: str, account: str, password: str) -> None:
        self.values[(service, account)] = password

    def get_password(self, service: str, account: str) -> str:
        try:
            return self.values[(service, account)]
        except KeyError as exc:
            raise _keychain_error(
                "profile.keychain_missing_password",
                "No password was found in the configured keychain.",
                service=service,
                account=account,
            ) from exc


@dataclass(frozen=True)
class MacOSKeychainPasswordStore:
    security_path: str = "security"

    def set_password(self, service: str, account: str, password: str) -> None:
        self._run(
            [
                self.security_path,
                "add-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
                password,
                "-U",
            ],
            code="profile.keychain_write_failed",
            message="Could not store the generated MySQL password in macOS Keychain.",
            service=service,
            account=account,
        )

    def get_password(self, service: str, account: str) -> str:
        result = self._run(
            [self.security_path, "find-generic-password", "-a", account, "-s", service, "-w"],
            code="profile.keychain_missing_password",
            message="Could not read the MySQL password from macOS Keychain.",
            service=service,
            account=account,
        )
        password = result.stdout.rstrip("\n")
        if not password:
            raise _keychain_error(
                "profile.keychain_missing_password",
                "The configured keychain password is empty.",
                service=service,
                account=account,
            )
        return password

    @staticmethod
    def _run(
        args: list[str],
        *,
        code: str,
        message: str,
        service: str,
        account: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(args, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise _keychain_error(code, message, service=service, account=account) from exc


def default_password_store() -> PasswordStore:
    if sys.platform == "darwin" and shutil.which("security"):
        return MacOSKeychainPasswordStore()
    raise _keychain_error(
        "profile.keychain_unavailable",
        "Automatic password setup requires macOS Keychain on this platform.",
        service="dbcli",
        account=None,
    )


def _keychain_error(code: str, message: str, *, service: str, account: str | None) -> DbcliError:
    details = {"service": service}
    if account is not None:
        details["account"] = account
    return DbcliError(
        Diagnostic(
            code=code,
            message=message,
            details=details,
        ),
        ExitCode.DB_OR_PROFILE_ERROR,
    )
