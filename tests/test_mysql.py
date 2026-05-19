from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.errors import DbcliError
from dbcli.mysql import check_profile_connection, mysql_url, parse_identifier, quote_identifier
from dbcli.profiles import ResolvedProfile


runner = CliRunner()


def test_mysql_url_hides_password_by_default() -> None:
    profile = _profile(password="super-secret")
    url = mysql_url(profile)

    assert url.render_as_string(hide_password=True) == "mysql+pymysql://dbcli:***@localhost:3306/ecom_dev?charset=utf8mb4"
    assert "super-secret" not in url.render_as_string(hide_password=True)


def test_identifier_validation_and_quoting() -> None:
    assert parse_identifier("schema.table").to_dict() == {"schema": "schema", "name": "table"}
    assert quote_identifier("schema.table") == "`schema`.`table`"
    assert quote_identifier("table_name") == "`table_name`"

    for value in ["bad-name", "schema.table.extra", "evil;drop", "bad`name", "1starts_with_digit", ""]:
        with pytest.raises(DbcliError) as exc_info:
            quote_identifier(value)
        assert exc_info.value.diagnostic.code == "mysql.invalid_identifier"
        assert int(exc_info.value.exit_code) == 2


def test_profile_connection_success_uses_select_one() -> None:
    engine = _FakeEngine()

    check_profile_connection(_profile(), engine=engine)

    assert engine.connection.executed == ["SELECT 1"]
    assert engine.disposed is False


def test_profile_connection_failure_is_sanitized() -> None:
    engine = _FakeEngine(error=OperationalError("SELECT 1", {}, Exception("secret password")))

    with pytest.raises(DbcliError) as exc_info:
        check_profile_connection(_profile(password="secret-password"), engine=engine)

    assert int(exc_info.value.exit_code) == 30
    assert exc_info.value.diagnostic.code == "mysql.connection_failed"
    assert exc_info.value.diagnostic.details == {"profile": "dev", "error": "OperationalError"}
    assert "secret-password" not in exc_info.value.diagnostic.message


def test_profile_test_cli_resolves_env_and_never_prints_password(monkeypatch) -> None:
    calls: list[ResolvedProfile] = []

    def fake_test(profile: ResolvedProfile) -> None:
        calls.append(profile)

    monkeypatch.setattr("dbcli.cli.check_profile_connection", fake_test)
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        add_result = runner.invoke(
            app,
            [
                "profile",
                "add",
                "dev",
                "--host",
                "localhost",
                "--user",
                "dbcli",
                "--database",
                "ecom_dev",
                "--password-env",
                "DBCLI_DEV_PW",
            ],
        )
        assert add_result.exit_code == 0

        result = runner.invoke(
            app,
            ["profile", "test", "dev", "--json", "--ci"],
            env={"DBCLI_DEV_PW": "actual-secret"},
        )

    assert result.exit_code == 0
    assert calls[0].password == "actual-secret"
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["command"] == "profile test"
    assert payload["ok"] is True
    assert "actual-secret" not in result.stdout
    assert "actual-secret" not in result.stderr


def test_profile_test_cli_missing_env_returns_30() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        assert (
            runner.invoke(
                app,
                [
                    "profile",
                    "add",
                    "dev",
                    "--host",
                    "localhost",
                    "--user",
                    "dbcli",
                    "--database",
                    "ecom_dev",
                    "--password-env",
                    "DBCLI_DEV_PW",
                ],
            ).exit_code
            == 0
        )

        result = runner.invoke(app, ["profile", "test", "dev", "--json", "--ci"], env={})

    assert result.exit_code == 30
    payload = json.loads(result.stdout)
    assert payload["diagnostics"][0]["code"] == "profile.missing_env_var"


def _profile(password: str = "pw") -> ResolvedProfile:
    return ResolvedProfile(
        name="dev",
        host="localhost",
        port=3306,
        user="dbcli",
        password=password,
        database="ecom_dev",
    )


class _FakeScalarResult:
    def scalar_one(self) -> int:
        return 1


class _FakeConnection:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.executed: list[str] = []

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object) -> _FakeScalarResult:
        self.executed.append(str(statement))
        if self.error:
            raise self.error
        return _FakeScalarResult()


class _FakeEngine:
    def __init__(self, error: Exception | None = None) -> None:
        self.connection = _FakeConnection(error=error)
        self.disposed = False

    def connect(self) -> _FakeConnection:
        return self.connection

    def dispose(self) -> None:
        self.disposed = True
