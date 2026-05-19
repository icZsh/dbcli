"""Connection profile loading, linting, and environment interpolation."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dbcli.errors import DbcliError, Diagnostic, ExitCode
from dbcli.project import ProjectPaths, find_project


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
PROFILE_KEYS = frozenset({"host", "port", "user", "password", "database"})


@dataclass(frozen=True)
class Profile:
    name: str
    host: str
    port: int
    user: str
    password: str
    database: str

    @property
    def password_env(self) -> str:
        match = ENV_REFERENCE_RE.fullmatch(self.password)
        return match.group(1) if match else ""

    def to_public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "password_env": self.password_env,
        }


@dataclass(frozen=True)
class ResolvedProfile:
    name: str
    host: str
    port: int
    user: str
    password: str
    database: str

    def to_connection_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
        }


def load_profiles(paths: ProjectPaths | None = None) -> dict[str, Profile]:
    paths = paths or find_project()
    if not paths.profiles.exists():
        return {}

    try:
        raw = tomllib.loads(paths.profiles.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DbcliError(
            Diagnostic(
                code="profile.invalid_toml",
                message=f"Could not parse .dbcli/profiles.toml: {exc}",
                path=str(paths.profiles),
                details={"error": str(exc)},
            ),
            ExitCode.VALIDATION_ERROR,
        ) from exc

    profiles: dict[str, Profile] = {}
    for name, values in raw.items():
        profiles[name] = _profile_from_mapping(name, values, paths.profiles)
    return profiles


def list_profiles(paths: ProjectPaths | None = None) -> list[Profile]:
    return list(load_profiles(paths).values())


def add_profile(
    name: str,
    *,
    host: str,
    port: int,
    user: str,
    database: str,
    password_env: str,
    paths: ProjectPaths | None = None,
) -> Profile:
    paths = paths or find_project()
    _validate_profile_name(name, paths.profiles)
    password_reference = normalize_password_env(password_env, paths.profiles)
    profile = Profile(
        name=name,
        host=_require_non_empty("host", host, name, paths.profiles),
        port=_validate_port(port, name, paths.profiles),
        user=_require_non_empty("user", user, name, paths.profiles),
        password=password_reference,
        database=_require_non_empty("database", database, name, paths.profiles),
    )
    profiles = load_profiles(paths)
    profiles[name] = profile
    write_profiles(paths, profiles)
    return profile


def remove_profile(name: str, paths: ProjectPaths | None = None) -> Profile:
    paths = paths or find_project()
    profiles = load_profiles(paths)
    if name not in profiles:
        raise DbcliError(
            Diagnostic(
                code="profile.not_found",
                message=f"Profile `{name}` does not exist.",
                path=str(paths.profiles),
                details={"profile": name},
            ),
            ExitCode.DB_OR_PROFILE_ERROR,
        )

    removed = profiles.pop(name)
    write_profiles(paths, profiles)
    return removed


def resolve_profile(
    name: str,
    *,
    paths: ProjectPaths | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedProfile:
    paths = paths or find_project()
    profiles = load_profiles(paths)
    if name not in profiles:
        raise DbcliError(
            Diagnostic(
                code="profile.not_found",
                message=f"Profile `{name}` does not exist.",
                path=str(paths.profiles),
                details={"profile": name},
            ),
            ExitCode.DB_OR_PROFILE_ERROR,
        )

    profile = profiles[name]
    env_var = profile.password_env
    env_values = env if env is not None else os.environ
    if env_var not in env_values:
        raise DbcliError(
            Diagnostic(
                code="profile.missing_env_var",
                message=f"Profile `{name}` requires environment variable `{env_var}`.",
                path=f"{paths.profiles}:{name}.password",
                details={"profile": name, "env_var": env_var},
            ),
            ExitCode.DB_OR_PROFILE_ERROR,
        )

    return ResolvedProfile(
        name=profile.name,
        host=profile.host,
        port=profile.port,
        user=profile.user,
        password=env_values[env_var],
        database=profile.database,
    )


def write_profiles(paths: ProjectPaths, profiles: Mapping[str, Profile]) -> None:
    lines: list[str] = []
    for profile in sorted(profiles.values(), key=lambda item: item.name):
        if lines:
            lines.append("")
        lines.append(f"[{profile.name}]")
        lines.append(f"host = {_toml_string(profile.host)}")
        lines.append(f"port = {profile.port}")
        lines.append(f"user = {_toml_string(profile.user)}")
        lines.append(f"password = {_toml_string(profile.password)}")
        lines.append(f"database = {_toml_string(profile.database)}")

    content = "\n".join(lines)
    if content:
        content += "\n"
    paths.profiles.write_text(content, encoding="utf-8")


def normalize_password_env(password_env: str, path: Path | None = None) -> str:
    env_name = password_env.strip()
    match = ENV_REFERENCE_RE.fullmatch(env_name)
    if match:
        env_name = match.group(1)

    if not ENV_NAME_RE.fullmatch(env_name):
        raise DbcliError(
            Diagnostic(
                code="profile.invalid_env_var",
                message="Password environment variable names must look like DBCLI_DEV_PW.",
                path=str(path) if path else None,
                details={},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    return f"${{{env_name}}}"


def _profile_from_mapping(name: str, values: object, path: Path) -> Profile:
    _validate_profile_name(name, path)
    if not isinstance(values, dict):
        raise DbcliError(
            Diagnostic(
                code="profile.invalid",
                message=f"Profile `{name}` must be a TOML table.",
                path=f"{path}:{name}",
                details={"profile": name},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    unknown = sorted(set(values) - PROFILE_KEYS)
    if unknown:
        raise DbcliError(
            Diagnostic(
                code="profile.unknown_key",
                message=f"Unknown key in profile `{name}`: {unknown[0]}.",
                path=f"{path}:{name}.{unknown[0]}",
                details={"profile": name, "key": unknown[0]},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    missing = sorted(PROFILE_KEYS - set(values))
    if missing:
        raise DbcliError(
            Diagnostic(
                code="profile.invalid",
                message=f"Profile `{name}` is missing required key `{missing[0]}`.",
                path=f"{path}:{name}.{missing[0]}",
                details={"profile": name, "key": missing[0]},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    password = values["password"]
    if not isinstance(password, str) or not ENV_REFERENCE_RE.fullmatch(password):
        raise DbcliError(
            Diagnostic(
                code="profile.literal_password",
                message=f"Profile `{name}` must store password as ${{ENV_VAR}}, not a literal value.",
                path=f"{path}:{name}.password",
                details={"profile": name},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    return Profile(
        name=name,
        host=_require_non_empty("host", values["host"], name, path),
        port=_validate_port(values["port"], name, path),
        user=_require_non_empty("user", values["user"], name, path),
        password=password,
        database=_require_non_empty("database", values["database"], name, path),
    )


def _validate_profile_name(name: str, path: Path) -> None:
    if not PROFILE_NAME_RE.fullmatch(name):
        raise DbcliError(
            Diagnostic(
                code="profile.invalid_name",
                message="Profile names may contain only letters, numbers, underscores, and dashes.",
                path=str(path),
                details={"profile": name},
            ),
            ExitCode.VALIDATION_ERROR,
        )


def _require_non_empty(field: str, value: object, profile: str, path: Path) -> str:
    if not isinstance(value, str) or not value:
        raise DbcliError(
            Diagnostic(
                code="profile.invalid",
                message=f"Profile `{profile}` key `{field}` must be a non-empty string.",
                path=f"{path}:{profile}.{field}",
                details={"profile": profile, "key": field},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    return value


def _validate_port(value: object, profile: str, path: Path) -> int:
    if not isinstance(value, int) or value <= 0 or value > 65535:
        raise DbcliError(
            Diagnostic(
                code="profile.invalid",
                message=f"Profile `{profile}` key `port` must be an integer from 1 to 65535.",
                path=f"{path}:{profile}.port",
                details={"profile": profile, "key": "port"},
            ),
            ExitCode.VALIDATION_ERROR,
        )
    return value


def _toml_string(value: str) -> str:
    return json.dumps(value)
