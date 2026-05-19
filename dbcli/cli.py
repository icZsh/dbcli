from __future__ import annotations

import sys
from typing import Annotated

import typer

from dbcli import __version__
from dbcli.errors import DbcliError, Diagnostic, ExitCode
from dbcli.output import (
    OutputConfig,
    build_output_config,
    emit_result,
    log,
    merge_output_config,
    result_envelope,
)
from dbcli.profiles import add_profile, list_profiles, remove_profile
from dbcli.project import init_project, load_project_config
from dbcli.source import format_inspection, inspect_source


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(
    context_settings=CONTEXT_SETTINGS,
    no_args_is_help=True,
    help="Recipe-first CSV/XLSX to MySQL loading CLI.",
)
profile_app = typer.Typer(help="Manage connection profiles.")
recipes_app = typer.Typer(help="Manage recipes.")
app.add_typer(profile_app, name="profile")
app.add_typer(recipes_app, name="recipes")


JsonOption = Annotated[bool, typer.Option("--json", help="Emit machine-readable result data to stdout.")]
NoProgressOption = Annotated[bool, typer.Option("--no-progress", help="Suppress progress bars/spinners.")]
CiOption = Annotated[bool, typer.Option("--ci", help="Force CI mode.")]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dbcli {__version__}")
        raise typer.Exit(ExitCode.SUCCESS)


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the dbcli version and exit.",
        ),
    ] = False,
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    del version
    ctx.obj = build_output_config(
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
    )


def _resolve_config(
    ctx: typer.Context,
    *,
    json_output: bool,
    no_progress: bool,
    ci: bool,
) -> OutputConfig:
    config = merge_output_config(
        ctx.obj if isinstance(ctx.obj, OutputConfig) else None,
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
    )
    ctx.obj = config
    return config


def _not_implemented(
    ctx: typer.Context,
    command: str,
    *,
    json_output: bool,
    no_progress: bool,
    ci: bool,
) -> None:
    config = _resolve_config(
        ctx,
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
    )
    diagnostic = Diagnostic(
        code="internal.not_implemented",
        message=f"`dbcli {command}` is not implemented yet.",
        details={"command": command},
    )
    payload = result_envelope(
        status="failed",
        command=command,
        diagnostics=[diagnostic.to_dict()],
        exit_code=int(ExitCode.INTERNAL_ERROR),
    )
    emit_result(payload, config)
    log(
        "error",
        "command not implemented",
        config,
        command=command,
        exit_code=int(ExitCode.INTERNAL_ERROR),
    )
    raise typer.Exit(ExitCode.INTERNAL_ERROR)


def _emit_success(
    ctx: typer.Context,
    command: str,
    *,
    json_output: bool,
    no_progress: bool,
    ci: bool,
    profile: str | None = None,
    extra: dict[str, object] | None = None,
    human_stdout: str | None = None,
) -> None:
    config = _resolve_config(
        ctx,
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
    )
    payload = result_envelope(
        status="success",
        command=command,
        profile=profile,
        exit_code=int(ExitCode.SUCCESS),
    )
    if extra:
        payload.update(extra)

    if human_stdout and not config.json_output:
        typer.echo(human_stdout, nl=not human_stdout.endswith("\n"))
    emit_result(payload, config)


def _emit_dbcli_error(
    ctx: typer.Context,
    command: str,
    error: DbcliError,
    *,
    json_output: bool,
    no_progress: bool,
    ci: bool,
) -> None:
    config = _resolve_config(
        ctx,
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
    )
    payload = result_envelope(
        status="failed",
        command=command,
        diagnostics=[error.diagnostic.to_dict()],
        exit_code=int(error.exit_code),
    )
    emit_result(payload, config)
    log(
        "error",
        "command failed",
        config,
        command=command,
        diagnostic=error.diagnostic.code,
        exit_code=int(error.exit_code),
    )
    raise typer.Exit(error.exit_code)


@app.command()
def init(
    ctx: typer.Context,
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    try:
        paths, created = init_project()
        load_project_config(paths)
    except DbcliError as exc:
        _emit_dbcli_error(ctx, "init", exc, json_output=json_output, no_progress=no_progress, ci=ci)
    _emit_success(
        ctx,
        "init",
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
        extra={
            "project": str(paths.root),
            "dbcli_dir": str(paths.dbcli_dir),
            "created": created,
        },
    )


@profile_app.command("add")
def profile_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Profile name.")],
    host: Annotated[str, typer.Option("--host", help="MySQL host.")],
    user: Annotated[str, typer.Option("--user", help="MySQL user.")],
    database: Annotated[str, typer.Option("--database", help="Default MySQL database.")],
    password_env: Annotated[str, typer.Option("--password-env", help="Environment variable containing the password.")],
    port: Annotated[int, typer.Option("--port", help="MySQL port.")] = 3306,
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    try:
        profile = add_profile(
            name,
            host=host,
            port=port,
            user=user,
            database=database,
            password_env=password_env,
        )
    except DbcliError as exc:
        _emit_dbcli_error(ctx, "profile add", exc, json_output=json_output, no_progress=no_progress, ci=ci)
    _emit_success(
        ctx,
        "profile add",
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
        profile=profile.name,
        extra={"profile_data": profile.to_public_dict()},
    )


@profile_app.command("list")
def profile_list(
    ctx: typer.Context,
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    try:
        profiles = [profile.to_public_dict() for profile in list_profiles()]
    except DbcliError as exc:
        _emit_dbcli_error(ctx, "profile list", exc, json_output=json_output, no_progress=no_progress, ci=ci)

    lines = [
        f"{profile['name']}\t{profile['user']}@{profile['host']}:{profile['port']}/{profile['database']}"
        for profile in profiles
    ]
    _emit_success(
        ctx,
        "profile list",
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
        extra={"profiles": profiles},
        human_stdout="\n".join(lines) if lines else "",
    )


@profile_app.command("remove")
def profile_remove(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Profile name.")],
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    try:
        removed = remove_profile(name)
    except DbcliError as exc:
        _emit_dbcli_error(ctx, "profile remove", exc, json_output=json_output, no_progress=no_progress, ci=ci)
    _emit_success(
        ctx,
        "profile remove",
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
        profile=removed.name,
        extra={"removed": removed.to_public_dict()},
    )


@profile_app.command("test")
def profile_test(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Profile name.")],
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    del name
    _not_implemented(ctx, "profile test", json_output=json_output, no_progress=no_progress, ci=ci)


@app.command()
def scan(
    ctx: typer.Context,
    file: Annotated[str, typer.Argument(help="Source CSV/XLSX file.")],
    sheet: Annotated[str | None, typer.Option("--sheet", help="XLSX sheet name.")] = None,
    table: Annotated[str | None, typer.Option("--table", help="Target table name.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Connection profile name.")] = None,
    encoding: Annotated[str | None, typer.Option("--encoding", help="CSV encoding.")] = None,
    delimiter: Annotated[str | None, typer.Option("--delimiter", help="CSV delimiter.")] = None,
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    del file, sheet, table, profile, encoding, delimiter
    _not_implemented(ctx, "scan", json_output=json_output, no_progress=no_progress, ci=ci)


@recipes_app.command("list")
def recipes_list(
    ctx: typer.Context,
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    _not_implemented(ctx, "recipes list", json_output=json_output, no_progress=no_progress, ci=ci)


@recipes_app.command("show")
def recipes_show(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Recipe name or path.")],
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    del name
    _not_implemented(ctx, "recipes show", json_output=json_output, no_progress=no_progress, ci=ci)


@app.command()
def validate(
    ctx: typer.Context,
    recipe: Annotated[str, typer.Argument(help="Recipe name or path.")],
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    del recipe
    _not_implemented(ctx, "validate", json_output=json_output, no_progress=no_progress, ci=ci)


@app.command()
def load(
    ctx: typer.Context,
    recipe: Annotated[str, typer.Argument(help="Recipe name or path.")],
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    del recipe
    _not_implemented(ctx, "load", json_output=json_output, no_progress=no_progress, ci=ci)


@app.command()
def inspect(
    ctx: typer.Context,
    file: Annotated[str, typer.Argument(help="Source CSV/XLSX file.")],
    sheet: Annotated[str | None, typer.Option("--sheet", help="XLSX sheet name.")] = None,
    encoding: Annotated[str | None, typer.Option("--encoding", help="CSV encoding.")] = None,
    delimiter: Annotated[str | None, typer.Option("--delimiter", help="CSV delimiter.")] = None,
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    try:
        inspection = inspect_source(file, sheet=sheet, encoding=encoding, delimiter=delimiter)
    except DbcliError as exc:
        _emit_dbcli_error(ctx, "inspect", exc, json_output=json_output, no_progress=no_progress, ci=ci)
    _emit_success(
        ctx,
        "inspect",
        json_output=json_output,
        no_progress=no_progress,
        ci=ci,
        extra={"inspection": inspection.to_dict()},
        human_stdout=format_inspection(inspection),
    )


@app.command()
def history(
    ctx: typer.Context,
    table: Annotated[str | None, typer.Option("--table", help="Filter by table.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum records to show.")] = None,
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    del table, limit
    _not_implemented(ctx, "history", json_output=json_output, no_progress=no_progress, ci=ci)


@app.command()
def show(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    json_output: JsonOption = False,
    no_progress: NoProgressOption = False,
    ci: CiOption = False,
) -> None:
    del run_id
    _not_implemented(ctx, "show", json_output=json_output, no_progress=no_progress, ci=ci)


def run() -> None:
    try:
        app()
    except Exception:
        raise


if __name__ == "__main__":
    sys.exit(run())
