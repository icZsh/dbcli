from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbcli.cli import app
from dbcli.core.errors import DbcliError
from dbcli.project.profiles import add_profile, default_database_name, default_password_key, ensure_default_profile, resolve_profile
from dbcli.project import (
    CONFIG_TOML,
    PROFILES_EXAMPLE_TOML,
    init_project,
    load_project_config,
    resolve_settings,
)


runner = CliRunner()


def test_init_creates_dbcli_tree_and_default_keychain_profile(fake_keychain) -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "--json", "--ci"])

        assert result.exit_code == 0
        paths, _ = init_project(Path.cwd())
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
        assert payload["profile_created"] is True
        assert payload["profile_data"] == {
            "name": "default",
            "host": "localhost",
            "port": 3306,
            "user": "dbcli",
            "database": default_database_name(Path.cwd()),
            "password_provider": "keychain",
            "password_key": default_password_key(paths, "default"),
        }
        assert payload["mysql_provisioned"] is False
        assert payload["mysql_verified"] is False
        assert payload["mysql_provision_skipped"] is False
        assert payload["mysql_provision_command"] == "dbcli mysql provision --admin-user root"
        assert payload["mysql_provision_diagnostic"]["code"] == "mysql.provision_failed"

        assert _read(".dbcli/config.toml") == CONFIG_TOML
        assert 'password_provider = "keychain"' in _read(".dbcli/profiles.toml")
        assert "password =" not in _read(".dbcli/profiles.toml")
        assert _read(".dbcli/profiles.example.toml") == PROFILES_EXAMPLE_TOML
        assert _read(".dbcli/runs.jsonl") == ""
        assert fake_keychain.get_password("dbcli", payload["profile_data"]["password_key"])


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
        profiles = {profile["name"]: profile for profile in payload["profiles"]}
        assert profiles["dev"] == {
            "name": "dev",
            "host": "localhost",
            "port": 3306,
            "user": "dbcli",
            "database": "ecom_dev",
            "password_provider": "env",
            "password_env": "DBCLI_DEV_PW",
        }
        assert profiles["default"]["password_provider"] == "keychain"
        assert "password" not in profiles["dev"]

        remove_result = runner.invoke(app, ["profile", "remove", "dev", "--json", "--ci"])
        assert remove_result.exit_code == 0
        remaining = _read(".dbcli/profiles.toml")
        assert "[dev]" not in remaining
        assert "[default]" in remaining


def test_init_accepts_explicit_default_profile_overrides() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(
            app,
            [
                "init",
                "--host",
                "127.0.0.1",
                "--port",
                "3307",
                "--user",
                "loader",
                "--database",
                "warehouse",
                "--json",
                "--ci",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["profile_data"] == {
            "name": "default",
            "host": "127.0.0.1",
            "port": 3307,
            "user": "loader",
            "database": "warehouse",
            "password_provider": "keychain",
            "password_key": payload["profile_data"]["password_key"],
        }


def test_init_can_skip_mysql_provisioning() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "--no-provision", "--json", "--ci"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["mysql_provisioned"] is False
        assert payload["mysql_verified"] is False
        assert payload["mysql_provision_skipped"] is True
        assert payload["mysql_provision_command"] == "dbcli mysql provision --admin-user root"


def test_init_human_output_includes_mysql_follow_up() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init"])

        assert result.exit_code == 0
        assert "profile: default" in result.stdout
        assert "mysql: not provisioned" in result.stdout
        assert "next: dbcli mysql provision --admin-user root" in result.stdout
        assert "reason: test" in result.stdout
        assert "hint: Start MySQL with `brew services start mysql`" in result.stdout


def test_resolve_default_keychain_profile_reads_password(tmp_path, fake_keychain) -> None:
    paths, _ = init_project(tmp_path)
    profile, created = ensure_default_profile(paths)

    assert created is True
    resolved = resolve_profile("default", paths=paths)

    assert resolved.to_connection_dict() == {
        "host": "localhost",
        "port": 3306,
        "user": "dbcli",
        "password": fake_keychain.get_password("dbcli", profile.password_key or ""),
        "database": default_database_name(tmp_path),
    }


def test_default_database_name_sanitizes_directory_name() -> None:
    assert default_database_name(Path("/tmp/Company Data")) == "company_data"
    assert default_database_name(Path("/tmp/2026 Reports")) == "db_2026_reports"


def test_init_repairs_existing_default_profile_with_unsafe_database_name() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init", "--no-provision"]).exit_code == 0
        profiles = Path(".dbcli/profiles.toml")
        profiles.write_text(
            profiles.read_text(encoding="utf-8").replace(f'database = "{Path.cwd().name}"', 'database = "Company Data"'),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["init", "--no-provision", "--json", "--ci"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["profile_created"] is False
        assert payload["profile_data"]["database"] == default_database_name(Path.cwd())
        assert 'database = "Company Data"' not in profiles.read_text(encoding="utf-8")


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
