from __future__ import annotations

import json

from typer.testing import CliRunner

from dbcli import __version__
from dbcli.cli import app


runner = CliRunner()


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"dbcli {__version__}"


def test_help_lists_core_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "validate" in result.stdout
    assert "load" in result.stdout
    assert "profile" in result.stdout
    assert "recipes" in result.stdout


def test_json_flag_after_command_routes_payload_to_stdout_and_log_to_stderr() -> None:
    result = runner.invoke(app, ["load", "recipe.yaml", "--json", "--ci"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["command"] == "load"
    assert payload["diagnostics"][0]["code"] == "internal.not_implemented"
    assert "level=error" in result.stderr
    assert "command=load" in result.stderr
