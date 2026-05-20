"""Connection profile loading, linting, and environment interpolation."""

from __future__ import annotations

import json
import os
import re
import secrets
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Mapping

from dbcli.core.errors import DbcliError, Diagnostic, ExitCode
from dbcli.project.keychain import PasswordStore, default_password_store
from dbcli.project import ProjectPaths, find_project


DEFAULT_PROFILE_NAME = "default"
DEFAULT_PROFILE_HOST = "localhost"
DEFAULT_PROFILE_PORT = 3306
DEFAULT_PROFILE_USER = "dbcli"
KEYCHAIN_SERVICE = "dbcli"
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DATABASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
PROFILE_KEYS = frozenset({"host", "port", "user", "password", "password_provider", "password_key", "database"})
PASSWORD_PROVIDERS = frozenset({"env", "keychain"})


@dataclass(frozen=True)
class Profile:
    name: str
    host: str
    port: int
    user: str
    database: str
    password_provider: Literal["env", "keychain"]
    password: str | None = None
    password_key: str | None = None

    @property
    def password_env(self) -> str:
        if self.password_provider != "env" or self.password is None:
            return ""
        match = ENV_REFERENCE_RE.fullmatch(self.password)
        return match.group(1) if match else ""

    def to_public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "password_provider": self.password_provider,
        }
        if self.password_provider == "env":
            payload["password_env"] = self.password_env
        if self.password_provider == "keychain":
            payload["password_key"] = self.password_key
        return payload


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
        database=_require_non_empty("database", database, name, paths.profiles),
        password_provider="env",
        password=password_reference,
    )
    profiles = load_profiles(paths)
    profiles[name] = profile
    write_profiles(paths, profiles)
    return profile


def ensure_default_profile(
    paths: ProjectPaths,
    *,
    host: str = DEFAULT_PROFILE_HOST,
    port: int = DEFAULT_PROFILE_PORT,
    user: str = DEFAULT_PROFILE_USER,
    database: str | None = None,
    password_store: PasswordStore | None = None,
) -> tuple[Profile, bool]:
    profiles = load_profiles(paths)
    profile_database = database or default_database_name(paths.root)
    if DEFAULT_PROFILE_NAME in profiles:
        profile = profiles[DEFAULT_PROFILE_NAME]
        if database is not None and profile.database != profile_database:
            profile = replace(profile, database=_require_non_empty("database", profile_database, DEFAULT_PROFILE_NAME, paths.profiles))
            profiles[DEFAULT_PROFILE_NAME] = profile
            write_profiles(paths, profiles)
        elif not is_safe_database_name(profile.database):
            profile = replace(profile, database=profile_database)
            profiles[DEFAULT_PROFILE_NAME] = profile
            write_profiles(paths, profiles)
        return profile, False

    profile_host = _require_non_empty("host", host, DEFAULT_PROFILE_NAME, paths.profiles)
    profile_port = _validate_port(port, DEFAULT_PROFILE_NAME, paths.profiles)
    profile_user = _require_non_empty("user", user, DEFAULT_PROFILE_NAME, paths.profiles)
    profile_database = _require_non_empty("database", profile_database, DEFAULT_PROFILE_NAME, paths.profiles)
    password_key = default_password_key(paths, DEFAULT_PROFILE_NAME)
    generated_password = secrets.token_urlsafe(32)
    store = password_store or default_password_store()
    store.set_password(KEYCHAIN_SERVICE, password_key, generated_password)

    profile = Profile(
        name=DEFAULT_PROFILE_NAME,
        host=profile_host,
        port=profile_port,
        user=profile_user,
        database=profile_database,
        password_provider="keychain",
        password_key=password_key,
    )
    profiles[DEFAULT_PROFILE_NAME] = profile
    write_profiles(paths, profiles)
    return profile, True


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
    password_store: PasswordStore | None = None,
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
    if profile.password_provider == "env":
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
        password = env_values[env_var]
    else:
        if not profile.password_key:
            raise DbcliError(
                Diagnostic(
                    code="profile.invalid",
                    message=f"Profile `{name}` is missing a keychain password key.",
                    path=f"{paths.profiles}:{name}.password_key",
                    details={"profile": name, "key": "password_key"},
                ),
                ExitCode.VALIDATION_ERROR,
            )
        store = password_store or default_password_store()
        password = store.get_password(KEYCHAIN_SERVICE, profile.password_key)

    return ResolvedProfile(
        name=profile.name,
        host=profile.host,
        port=profile.port,
        user=profile.user,
        password=password,
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
        if profile.password_provider == "env":
            lines.append(f"password = {_toml_string(profile.password or '')}")
        else:
            lines.append('password_provider = "keychain"')
            lines.append(f"password_key = {_toml_string(profile.password_key or '')}")
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


def default_database_name(root: Path) -> str:
    return normalize_database_name(root.resolve().name)


def normalize_database_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        normalized = "dbcli"
    if not re.match(r"^[a-z_]", normalized):
        normalized = f"db_{normalized}"
    normalized = normalized[:64].rstrip("_")
    return normalized or "dbcli"


def is_safe_database_name(value: str) -> bool:
    return DATABASE_NAME_RE.fullmatch(value) is not None


def default_password_key(paths: ProjectPaths, profile_name: str) -> str:
    return f"{KEYCHAIN_SERVICE}:{paths.root}:{profile_name}"


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

    missing = sorted({"host", "port", "user", "database"} - set(values))
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

    password_provider = values.get("password_provider")
    if password_provider is None or password_provider == "env":
        password = values.get("password")
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
        provider: Literal["env", "keychain"] = "env"
        password_key = None
    elif password_provider == "keychain":
        if "password" in values:
            raise DbcliError(
                Diagnostic(
                    code="profile.invalid",
                    message=f"Profile `{name}` cannot define both password and password_provider.",
                    path=f"{path}:{name}.password",
                    details={"profile": name, "key": "password"},
                ),
                ExitCode.VALIDATION_ERROR,
            )
        password = None
        password_key = values.get("password_key")
        if not isinstance(password_key, str) or not password_key:
            raise DbcliError(
                Diagnostic(
                    code="profile.invalid",
                    message=f"Profile `{name}` key `password_key` must be a non-empty string.",
                    path=f"{path}:{name}.password_key",
                    details={"profile": name, "key": "password_key"},
                ),
                ExitCode.VALIDATION_ERROR,
            )
        provider = "keychain"
    else:
        raise DbcliError(
            Diagnostic(
                code="profile.invalid",
                message=f"Profile `{name}` has unsupported password_provider.",
                path=f"{path}:{name}.password_provider",
                details={"profile": name, "key": "password_provider", "allowed": sorted(PASSWORD_PROVIDERS)},
            ),
            ExitCode.VALIDATION_ERROR,
        )

    return Profile(
        name=name,
        host=_require_non_empty("host", values["host"], name, path),
        port=_validate_port(values["port"], name, path),
        user=_require_non_empty("user", values["user"], name, path),
        database=_require_non_empty("database", values["database"], name, path),
        password_provider=provider,
        password=password,
        password_key=password_key,
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
