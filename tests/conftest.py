from __future__ import annotations

import pytest

from dbcli.core.errors import DbcliError, Diagnostic, ExitCode
from dbcli.project.keychain import InMemoryPasswordStore


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> InMemoryPasswordStore:
    store = InMemoryPasswordStore()
    monkeypatch.setattr("dbcli.project.profiles.default_password_store", lambda: store)
    return store


@pytest.fixture(autouse=True)
def fake_init_mysql_provision(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_provision(*_args: object, **_kwargs: object) -> object:
        raise DbcliError(
            Diagnostic(
                code="mysql.provision_failed",
                message="Local MySQL provisioning is not available in tests.",
                details={
                    "error": "test",
                    "reason": "test",
                    "hint": "Start MySQL with `brew services start mysql`, then run `dbcli mysql provision --admin-user root`.",
                },
            ),
            ExitCode.DB_OR_PROFILE_ERROR,
        )

    monkeypatch.setattr("dbcli.cli.provision_mysql_profile", fail_provision)
