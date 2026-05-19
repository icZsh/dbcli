from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.errors import DbcliError
from dbcli.profiles import add_profile, resolve_profile
from dbcli.project import (
    CONFIG_TOML,
    PROFILES_EXAMPLE_TOML,
    init_project,
    load_project_config,
    resolve_settings,
)


runner = CliRunner()


def test_init_creates_dbcli_tree() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "--json", "--ci"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "success"
        assert payload["command"] == "init"
        assert payload["created"] == [
            ".dbcli",
            ".dbcli/recipes",
            ".dbcli/rejects",
            ".dbcli/config.toml",
            ".dbcli/profiles.toml",
            ".dbcli/profiles.example.toml",
            ".dbcli/runs.jsonl",
        ]

        assert _read(".dbcli/config.toml") == CONFIG_TOML
        assert _read(".dbcli/profiles.toml") == ""
        assert _read(".dbcli/profiles.example.toml") == PROFILES_EXAMPLE_TOML
        assert _read(".dbcli/runs.jsonl") == ""


def test_profile_add_list_and_remove_manage_env_password_references_only() -> None:
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
                "--json",
                "--ci",
            ],
        )

        assert add_result.exit_code == 0
        assert 'password = "${DBCLI_DEV_PW}"' in _read(".dbcli/profiles.toml")
        assert "secret" not in _read(".dbcli/profiles.toml")

        list_result = runner.invoke(app, ["profile", "list", "--json", "--ci"])
        assert list_result.exit_code == 0
        payload = json.loads(list_result.stdout)
        assert payload["profiles"] == [
            {
                "name": "dev",
                "host": "localhost",
                "port": 3306,
                "user": "dbcli",
                "database": "ecom_dev",
                "password_env": "DBCLI_DEV_PW",
            }
        ]
        assert "password" not in payload["profiles"][0]

        remove_result = runner.invoke(app, ["profile", "remove", "dev", "--json", "--ci"])
        assert remove_result.exit_code == 0
        assert _read(".dbcli/profiles.toml") == ""


def test_literal_password_file_fails_profile_lint() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        _write(
            ".dbcli/profiles.toml",
            """[dev]
host = "localhost"
port = 3306
user = "dbcli"
password = "literal-secret"
database = "ecom_dev"
""",
        )

        result = runner.invoke(app, ["profile", "list", "--json", "--ci"])

        assert result.exit_code == 10
        payload = json.loads(result.stdout)
        assert payload["diagnostics"][0]["code"] == "profile.literal_password"
        assert "literal-secret" not in result.stdout
        assert "literal-secret" not in result.stderr


def test_invalid_password_env_diagnostic_does_not_echo_input() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0

        result = runner.invoke(
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
                "literal-secret!",
                "--json",
                "--ci",
            ],
        )

        assert result.exit_code == 10
        payload = json.loads(result.stdout)
        assert payload["diagnostics"][0]["code"] == "profile.invalid_env_var"
        assert "literal-secret!" not in result.stdout
        assert "literal-secret!" not in result.stderr


def test_resolve_profile_missing_env_var_is_profile_error(tmp_path) -> None:
    paths, _ = init_project(tmp_path)
    add_profile(
        "dev",
        host="localhost",
        port=3306,
        user="dbcli",
        database="ecom_dev",
        password_env="DBCLI_DEV_PW",
        paths=paths,
    )

    with pytest.raises(DbcliError) as exc_info:
        resolve_profile("dev", paths=paths, env={})

    assert int(exc_info.value.exit_code) == 30
    assert exc_info.value.diagnostic.code == "profile.missing_env_var"
    assert exc_info.value.diagnostic.details == {"profile": "dev", "env_var": "DBCLI_DEV_PW"}


def test_resolve_settings_precedence_chain(tmp_path) -> None:
    paths, _ = init_project(tmp_path)
    paths.config.write_text(
        """[defaults]
profile = "project"
batch_size = 100
reject_threshold = 0.25
charset = "latin1"
collation = "latin1_swedish_ci"
engine = "InnoDB"
""",
        encoding="utf-8",
    )
    config = load_project_config(paths)

    settings = resolve_settings(
        config,
        recipe_target={"profile": "recipe", "charset": "utf8mb4"},
        recipe_options={"batch_size": 250},
        cli_values={"profile": "cli"},
    )

    assert settings.to_dict() == {
        "profile": "cli",
        "batch_size": 250,
        "reject_threshold": 0.25,
        "charset": "utf8mb4",
        "collation": "latin1_swedish_ci",
        "engine": "InnoDB",
    }


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
