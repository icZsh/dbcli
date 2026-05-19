from __future__ import annotations

import json

import pytest
import polars as pl
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.errors import DbcliError
from dbcli.mysql import (
    MysqlColumnInfo,
    MysqlTableInfo,
    SqlAlchemyMysqlAdapter,
    check_profile_connection,
    compare_table_info,
    create_table_sql,
    insert_sql,
    mysql_url,
    parse_identifier,
    quote_identifier,
)
from dbcli.profiles import ResolvedProfile
from dbcli.project import ResolvedSettings
from dbcli.schema import parse_schema_columns


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


def test_create_table_and_insert_sql_are_safe_and_ordered() -> None:
    schema = parse_schema_columns(
        [
            {"name": "seller_id", "type": "BIGINT", "nullable": False},
            {"name": "tier", "type": "VARCHAR(16)", "nullable": True},
        ]
    )

    assert create_table_sql("analytics.dim_sellers", schema, charset="utf8mb4", collation="utf8mb4_unicode_ci", engine="InnoDB") == (
        "CREATE TABLE `analytics`.`dim_sellers` (\n"
        "  `seller_id` BIGINT NOT NULL,\n"
        "  `tier` VARCHAR(16) NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )
    assert insert_sql("analytics.dim_sellers", ["seller_id", "tier"]) == (
        "INSERT INTO `analytics`.`dim_sellers` (`seller_id`, `tier`) VALUES (:seller_id, :tier)"
    )


def test_compare_table_info_normalizes_types_and_reports_drift() -> None:
    schema = parse_schema_columns(
        [
            {"name": "seller_id", "type": "BIGINT", "nullable": False},
            {"name": "active", "type": "BOOLEAN", "nullable": False},
            {"name": "tier", "type": "VARCHAR(16)", "nullable": True},
        ]
    )
    matching = MysqlTableInfo(
        schema="ecom_dev",
        name="dim_sellers",
        engine="innodb",
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
        columns=[
            MysqlColumnInfo("seller_id", "bigint(20)", False),
            MysqlColumnInfo("active", "tinyint(1)", False),
            MysqlColumnInfo("tier", "varchar(16)", True, charset="utf8mb4", collation="utf8mb4_unicode_ci"),
        ],
    )

    assert compare_table_info(schema, matching, charset="utf8mb4", collation="utf8mb4_unicode_ci", engine="InnoDB") == []

    drifted = MysqlTableInfo(
        schema="ecom_dev",
        name="dim_sellers",
        engine="MyISAM",
        charset="latin1",
        collation="latin1_swedish_ci",
        columns=[
            MysqlColumnInfo("seller_id", "int(11)", False),
            MysqlColumnInfo("tier", "varchar(32)", False, charset="latin1", collation="latin1_swedish_ci"),
            MysqlColumnInfo("extra", "text", True),
        ],
    )

    diffs = compare_table_info(schema, drifted, charset="utf8mb4", collation="utf8mb4_unicode_ci", engine="InnoDB")

    assert [diff["check"] for diff in diffs] == [
        "engine",
        "charset",
        "collation",
        "missing_column",
        "extra_column",
        "type",
        "type",
        "nullability",
        "column_charset",
        "column_collation",
    ]


def test_sqlalchemy_adapter_rolls_back_insert_failure() -> None:
    schema = parse_schema_columns(
        [
            {"name": "seller_id", "type": "BIGINT", "nullable": False},
            {"name": "tier", "type": "VARCHAR(16)", "nullable": True},
        ]
    )
    engine = _FakeLoadEngine(
        table_rows=[{"ENGINE": "InnoDB", "TABLE_COLLATION": "utf8mb4_unicode_ci"}],
        column_rows=[
            {
                "COLUMN_NAME": "seller_id",
                "COLUMN_TYPE": "bigint(20)",
                "IS_NULLABLE": "NO",
                "CHARACTER_SET_NAME": None,
                "COLLATION_NAME": None,
                "ORDINAL_POSITION": 1,
            },
            {
                "COLUMN_NAME": "tier",
                "COLUMN_TYPE": "varchar(16)",
                "IS_NULLABLE": "YES",
                "CHARACTER_SET_NAME": "utf8mb4",
                "COLLATION_NAME": "utf8mb4_unicode_ci",
                "ORDINAL_POSITION": 2,
            },
        ],
        insert_error=OperationalError("INSERT", {}, Exception("boom")),
    )
    adapter = SqlAlchemyMysqlAdapter(_profile(), engine=engine)  # type: ignore[arg-type]

    with pytest.raises(OperationalError):
        adapter.append_rows(
            "dim_sellers",
            schema,
            pl.DataFrame({"seller_id": [1, 2], "tier": ["A", "B"]}),
            ResolvedSettings(
                profile="dev",
                batch_size=2,
                reject_threshold=0.0,
                charset="utf8mb4",
                collation="utf8mb4_unicode_ci",
                engine="InnoDB",
            ),
        )

    assert engine.transaction.rolled_back is True
    assert engine.transaction.committed is False
    assert engine.connection.insert_batches == [[{"seller_id": 1, "tier": "A"}, {"seller_id": 2, "tier": "B"}]]


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


class _FakeMappingResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> "_FakeMappingResult":
        return self

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _FakeLoadConnection:
    def __init__(
        self,
        *,
        table_rows: list[dict[str, object]],
        column_rows: list[dict[str, object]],
        insert_error: OperationalError,
    ) -> None:
        self.table_rows = table_rows
        self.column_rows = column_rows
        self.insert_error = insert_error
        self.insert_batches: list[list[dict[str, object]]] = []

    def execute(self, statement: object, params: object | None = None) -> _FakeMappingResult:
        sql = str(statement)
        if "information_schema.TABLES" in sql:
            return _FakeMappingResult(self.table_rows)
        if "information_schema.COLUMNS" in sql:
            return _FakeMappingResult(self.column_rows)
        if sql.startswith("INSERT INTO"):
            assert isinstance(params, list)
            self.insert_batches.append(params)
            raise self.insert_error
        return _FakeMappingResult([])


class _FakeTransaction:
    def __init__(self, connection: _FakeLoadConnection) -> None:
        self.connection = connection
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> _FakeLoadConnection:
        return self.connection

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> bool:
        self.rolled_back = exc_type is not None
        self.committed = exc_type is None
        return False


class _FakeLoadEngine:
    def __init__(
        self,
        *,
        table_rows: list[dict[str, object]],
        column_rows: list[dict[str, object]],
        insert_error: OperationalError,
    ) -> None:
        self.connection = _FakeLoadConnection(
            table_rows=table_rows,
            column_rows=column_rows,
            insert_error=insert_error,
        )
        self.transaction = _FakeTransaction(self.connection)

    def begin(self) -> _FakeTransaction:
        return self.transaction
