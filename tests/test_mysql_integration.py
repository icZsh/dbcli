from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url

from dbcli.core.errors import ExitCode
from dbcli.project.history import read_run_records
from dbcli.db.load import run_load
from dbcli.project.profiles import add_profile
from dbcli.project import ProjectPaths, init_project


pytestmark = pytest.mark.mysql


@pytest.fixture()
def mysql_admin_url() -> URL:
    raw_url = os.getenv("DBCLI_MYSQL_INTEGRATION_URL")
    if not raw_url:
        pytest.skip("DBCLI_MYSQL_INTEGRATION_URL is not set")
    return make_url(raw_url)


@pytest.fixture()
def mysql_database(mysql_admin_url: URL) -> Iterator[tuple[str, URL]]:
    db_name = f"dbcli_it_{uuid4().hex[:12]}"
    server_url = mysql_admin_url.set(database=None)
    engine = create_engine(server_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        yield db_name, mysql_admin_url.set(database=db_name)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS `{db_name}`"))
        engine.dispose()


@pytest.fixture()
def mysql_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mysql_database: tuple[str, URL]) -> tuple[ProjectPaths, Engine]:
    db_name, url = mysql_database
    paths, _ = init_project(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    add_profile(
        "it",
        host=str(url.host),
        port=int(url.port or 3306),
        user=str(url.username),
        database=db_name,
        password_env="DBCLI_IT_PW",
        paths=paths,
    )
    monkeypatch.setenv("DBCLI_IT_PW", str(url.password or ""))
    engine = create_engine(url)
    try:
        yield paths, engine
    finally:
        engine.dispose()


def test_append_create_then_append_again(mysql_project: tuple[ProjectPaths, Engine]) -> None:
    paths, engine = mysql_project
    _write_recipe_project(paths, mode="append", rows=[("001", "A"), ("002", "B")])

    first = run_load("sellers", paths=paths)

    assert first.exit_code == ExitCode.SUCCESS
    assert first.rows["loaded"] == 2
    assert _table_rows(engine, "dim_sellers") == [(1, "A"), (2, "B")]

    _write_source(paths.root, [("003", "C")])
    second = run_load("sellers", paths=paths)

    assert second.exit_code == ExitCode.SUCCESS
    assert second.rows["loaded"] == 1
    assert _table_rows(engine, "dim_sellers") == [(1, "A"), (2, "B"), (3, "C")]
    assert [record["exit_code"] for record in read_run_records(paths=paths)] == [0, 0]


def test_append_detects_live_schema_drift(mysql_project: tuple[ProjectPaths, Engine]) -> None:
    paths, engine = mysql_project
    _write_recipe_project(paths, mode="append", rows=[("001", "A")])
    assert run_load("sellers", paths=paths).exit_code == ExitCode.SUCCESS

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE `dim_sellers` MODIFY `tier` VARCHAR(8) NOT NULL"))

    result = run_load("sellers", paths=paths)

    assert result.exit_code == ExitCode.USAGE_OR_DRIFT
    assert result.diagnostics[0].code == "mysql.schema_drift"
    assert any(diff["check"] == "type" and diff["column"] == "tier" for diff in result.diagnostics[0].details["diffs"])


def test_append_sql_error_rolls_back_batch(mysql_project: tuple[ProjectPaths, Engine]) -> None:
    paths, engine = mysql_project
    _write_recipe_project(paths, mode="append", rows=[("001", "GOOD"), ("002", "BAD")])
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE `dim_sellers` (
                  `seller_id` BIGINT NOT NULL,
                  `tier` VARCHAR(4) NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TRIGGER `dim_sellers_fail_bad`
                BEFORE INSERT ON `dim_sellers`
                FOR EACH ROW
                BEGIN
                  IF NEW.`tier` = 'BAD' THEN
                    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'intentional test failure';
                  END IF;
                END
                """
            )
        )

    result = run_load("sellers", paths=paths)

    assert result.exit_code == ExitCode.DB_OR_PROFILE_ERROR
    assert result.diagnostics[0].code == "mysql.load_failed"
    assert _table_rows(engine, "dim_sellers") == []
    assert read_run_records(paths=paths)[0]["exit_code"] == 30


def test_replace_resolves_drift_and_cleans_companion_tables(mysql_project: tuple[ProjectPaths, Engine]) -> None:
    paths, engine = mysql_project
    _write_recipe_project(paths, mode="replace", rows=[("010", "A"), ("011", "B")])
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE `dim_sellers` (
                  `seller_id` VARCHAR(8) NOT NULL,
                  `old_value` VARCHAR(8) NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        )
        connection.execute(text("INSERT INTO `dim_sellers` (`seller_id`, `old_value`) VALUES ('old', 'row')"))

    result = run_load("sellers", paths=paths)

    assert result.exit_code == ExitCode.SUCCESS
    assert result.rows["loaded"] == 2
    assert _table_rows(engine, "dim_sellers") == [(10, "A"), (11, "B")]
    assert _companion_tables(engine) == []


def _write_recipe_project(paths: ProjectPaths, *, mode: str, rows: list[tuple[str, str]]) -> None:
    _write_source(paths.root, rows)
    (paths.recipes_dir / "sellers.yaml").write_text(
        f"""name: sellers
source:
  path: data/sellers.csv
  encoding: utf-8
  delimiter: ","
target:
  profile: it
  table: dim_sellers
  mode: {mode}
  charset: utf8mb4
  collation: utf8mb4_unicode_ci
  engine: InnoDB
schema:
  - {{name: seller_id, type: BIGINT, nullable: false}}
  - {{name: tier, type: VARCHAR(4), nullable: false}}
edits:
  - cast: {{seller_id: {{type: int}}}}
options:
  reject_threshold: 1.0
  batch_size: 2
""",
        encoding="utf-8",
    )


def _write_source(root: Path, rows: list[tuple[str, str]]) -> None:
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    lines = ["seller_id,tier", *[f"{seller_id},{tier}" for seller_id, tier in rows]]
    (data_dir / "sellers.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _table_rows(engine: Engine, table: str) -> list[tuple[int, str]]:
    with engine.connect() as connection:
        result = connection.execute(text(f"SELECT `seller_id`, `tier` FROM `{table}` ORDER BY `seller_id`"))
        return [(int(row[0]), str(row[1])) for row in result]


def _companion_tables(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        result = connection.execute(
            text(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND LOCATE('__dbcli_', TABLE_NAME) > 0
                ORDER BY TABLE_NAME
                """
            )
        )
        return [str(row[0]) for row in result]
