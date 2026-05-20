"""Project discovery, initialization, and default config handling."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dbcli.core.errors import DbcliError, Diagnostic, ExitCode


DBCLI_DIR = ".dbcli"

CONFIG_TOML = """[defaults]
profile = "dev"
batch_size = 5000
reject_threshold = 0.0
charset = "utf8mb4"
collation = "utf8mb4_unicode_ci"
engine = "InnoDB"

[ci]
no_progress = true
"""

PROFILES_EXAMPLE_TOML = """[dev]
host = "localhost"
port = 3306
user = "dbcli"
password = "${DBCLI_DEV_PW}"
database = "ecom_dev"
"""

BUILTIN_DEFAULTS: dict[str, Any] = {
    "profile": "dev",
    "batch_size": 5000,
    "reject_threshold": 0.0,
    "charset": "utf8mb4",
    "collation": "utf8mb4_unicode_ci",
    "engine": "InnoDB",
}

CONFIG_DEFAULT_KEYS = frozenset(BUILTIN_DEFAULTS)
CONFIG_CI_KEYS = frozenset({"no_progress"})
TARGET_DEFAULT_KEYS = frozenset({"profile", "charset", "collation", "engine"})
OPTION_DEFAULT_KEYS = frozenset({"batch_size", "reject_threshold"})


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    dbcli_dir: Path
    config: Path
    profiles: Path
    profiles_example: Path
    recipes_dir: Path
    rejects_dir: Path
    runs: Path


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    defaults: dict[str, Any]
    ci: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class ResolvedSettings:
    profile: str
    batch_size: int
    reject_threshold: float
    charset: str
    collation: str
    engine: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "batch_size": self.batch_size,
            "reject_threshold": self.reject_threshold,
            "charset": self.charset,
            "collation": self.collation,
            "engine": self.engine,
        }


def project_paths(root: Path) -> ProjectPaths:
    root = root.resolve()
    dbcli_dir = root / DBCLI_DIR
    return ProjectPaths(
        root=root,
        dbcli_dir=dbcli_dir,
        config=dbcli_dir / "config.toml",
        profiles=dbcli_dir / "profiles.toml",
        profiles_example=dbcli_dir / "profiles.example.toml",
        recipes_dir=dbcli_dir / "recipes",
        rejects_dir=dbcli_dir / "rejects",
        runs=dbcli_dir / "runs.jsonl",
    )


def find_project(start: Path | None = None) -> ProjectPaths:
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / DBCLI_DIR).is_dir():
            return project_paths(candidate)

    raise DbcliError(
        Diagnostic(
            code="project.not_found",
            message="No .dbcli project found. Run `dbcli init` from the project root first.",
            path=str(current),
            details={"start": str(current)},
        ),
        ExitCode.USAGE_OR_DRIFT,
    )


def init_project(root: Path | None = None) -> tuple[ProjectPaths, list[str]]:
    paths = project_paths(root or Path.cwd())
    created: list[str] = []

    for directory in (paths.dbcli_dir, paths.recipes_dir, paths.rejects_dir):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(_relative(paths.root, directory))
        elif not directory.is_dir():
            raise DbcliError(
                Diagnostic(
                    code="project.path_conflict",
                    message=f"{_relative(paths.root, directory)} exists but is not a directory.",
                    path=str(directory),
                    details={"path": str(directory)},
                ),
                ExitCode.USAGE_OR_DRIFT,
            )

    _write_if_missing(paths.config, CONFIG_TOML, paths, created)
    _write_if_missing(paths.profiles, "", paths, created)
    _write_if_missing(paths.profiles_example, PROFILES_EXAMPLE_TOML, paths, created)
    _write_if_missing(paths.runs, "", paths, created)

    return paths, created


def load_project_config(paths: ProjectPaths | None = None) -> ProjectConfig:
    paths = paths or find_project()
    if not paths.config.exists():
        raise DbcliError(
            Diagnostic(
                code="config.not_found",
                message=".dbcli/config.toml is missing.",
                path=str(paths.config),
                details={"path": str(paths.config)},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    try:
        raw = tomllib.loads(paths.config.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DbcliError(
            Diagnostic(
                code="config.invalid_toml",
                message=f"Could not parse .dbcli/config.toml: {exc}",
                path=str(paths.config),
                details={"error": str(exc)},
            ),
            ExitCode.VALIDATION_ERROR,
        ) from exc

    if not isinstance(raw, dict):
        raw = {}

    defaults = _read_section(raw, "defaults", CONFIG_DEFAULT_KEYS, paths.config)
    ci = _read_section(raw, "ci", CONFIG_CI_KEYS, paths.config)
    merged_defaults = dict(BUILTIN_DEFAULTS)
    merged_defaults.update(defaults)
    _validate_defaults(merged_defaults, paths.config)
    _validate_ci(ci, paths.config)

    return ProjectConfig(
        path=paths.config,
        defaults=merged_defaults,
        ci=ci,
        raw=raw,
    )


def resolve_settings(
    project_config: ProjectConfig | None = None,
    *,
    recipe_target: Mapping[str, Any] | None = None,
    recipe_options: Mapping[str, Any] | None = None,
    cli_values: Mapping[str, Any] | None = None,
) -> ResolvedSettings:
    values = dict(BUILTIN_DEFAULTS)
    if project_config is not None:
        values.update(project_config.defaults)

    if recipe_target:
        for key in TARGET_DEFAULT_KEYS:
            if recipe_target.get(key) is not None:
                values[key] = recipe_target[key]

    if recipe_options:
        for key in OPTION_DEFAULT_KEYS:
            if recipe_options.get(key) is not None:
                values[key] = recipe_options[key]

    if cli_values:
        for key, value in cli_values.items():
            if key in values and value is not None:
                values[key] = value

    _validate_defaults(values, project_config.path if project_config else None)
    return ResolvedSettings(
        profile=str(values["profile"]),
        batch_size=int(values["batch_size"]),
        reject_threshold=float(values["reject_threshold"]),
        charset=str(values["charset"]),
        collation=str(values["collation"]),
        engine=str(values["engine"]),
    )


def _write_if_missing(path: Path, content: str, paths: ProjectPaths, created: list[str]) -> None:
    if path.exists():
        if not path.is_file():
            raise DbcliError(
                Diagnostic(
                    code="project.path_conflict",
                    message=f"{_relative(paths.root, path)} exists but is not a file.",
                    path=str(path),
                    details={"path": str(path)},
                ),
                ExitCode.USAGE_OR_DRIFT,
            )
        return

    path.write_text(content, encoding="utf-8")
    created.append(_relative(paths.root, path))


def _read_section(
    raw: Mapping[str, Any],
    name: str,
    allowed_keys: frozenset[str],
    path: Path,
) -> dict[str, Any]:
    section = raw.get(name, {})
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise DbcliError(
            Diagnostic(
                code="config.invalid_section",
                message=f"[{name}] in .dbcli/config.toml must be a table.",
                path=f"{path}:{name}",
                details={"section": name},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    unknown = sorted(set(section) - allowed_keys)
    if unknown:
        raise DbcliError(
            Diagnostic(
                code="config.unknown_key",
                message=f"Unknown config key in [{name}]: {unknown[0]}.",
                path=f"{path}:{name}.{unknown[0]}",
                details={"section": name, "key": unknown[0]},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    return dict(section)


def _validate_defaults(values: Mapping[str, Any], path: Path | None) -> None:
    checks = {
        "profile": lambda value: isinstance(value, str) and bool(value),
        "batch_size": lambda value: isinstance(value, int) and not isinstance(value, bool) and value > 0,
        "reject_threshold": lambda value: (
            isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= float(value) <= 1
        ),
        "charset": lambda value: isinstance(value, str) and bool(value),
        "collation": lambda value: isinstance(value, str) and bool(value),
        "engine": lambda value: isinstance(value, str) and bool(value),
    }
    for key, check in checks.items():
        if not check(values.get(key)):
            raise DbcliError(
                Diagnostic(
                    code="config.invalid_value",
                    message=f"Invalid config value for defaults.{key}.",
                    path=f"{path}:defaults.{key}" if path else f"defaults.{key}",
                    details={"key": key},
                ),
                ExitCode.VALIDATION_ERROR,
            )


def _validate_ci(values: Mapping[str, Any], path: Path) -> None:
    if "no_progress" in values and not isinstance(values["no_progress"], bool):
        raise DbcliError(
            Diagnostic(
                code="config.invalid_value",
                message="Invalid config value for ci.no_progress.",
                path=f"{path}:ci.no_progress",
                details={"key": "no_progress"},
            ),
            ExitCode.VALIDATION_ERROR,
        )


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
