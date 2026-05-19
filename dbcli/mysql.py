"""MySQL connectivity and identifier safety helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from dbcli.errors import DbcliError, Diagnostic, ExitCode
from dbcli.profiles import ResolvedProfile


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_IDENTIFIER_LENGTH = 64


@dataclass(frozen=True)
class QualifiedIdentifier:
    schema: str | None
    name: str

    def to_dict(self) -> dict[str, str | None]:
        return {"schema": self.schema, "name": self.name}


def mysql_url(profile: ResolvedProfile) -> URL:
    return URL.create(
        "mysql+pymysql",
        username=profile.user,
        password=profile.password,
        host=profile.host,
        port=profile.port,
        database=profile.database,
        query={"charset": "utf8mb4"},
    )


def create_mysql_engine(profile: ResolvedProfile) -> Engine:
    return create_engine(mysql_url(profile), pool_pre_ping=True)


def check_profile_connection(profile: ResolvedProfile, *, engine: Engine | None = None) -> None:
    owned_engine = engine is None
    engine = engine or create_mysql_engine(profile)
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            result.scalar_one()
    except SQLAlchemyError as exc:
        raise DbcliError(
            Diagnostic(
                code="mysql.connection_failed",
                message=f"Could not connect to MySQL profile `{profile.name}`.",
                path=None,
                details={"profile": profile.name, "error": exc.__class__.__name__},
            ),
            ExitCode.DB_OR_PROFILE_ERROR,
        ) from exc
    finally:
        if owned_engine:
            engine.dispose()


def parse_identifier(value: str) -> QualifiedIdentifier:
    if not isinstance(value, str) or not value:
        raise _identifier_error(value)
    if "`" in value or ";" in value:
        raise _identifier_error(value)
    parts = value.split(".")
    if len(parts) > 2 or any(not part for part in parts):
        raise _identifier_error(value)
    for part in parts:
        if len(part) > MAX_IDENTIFIER_LENGTH or not IDENTIFIER_RE.fullmatch(part):
            raise _identifier_error(value)
    if len(parts) == 2:
        return QualifiedIdentifier(schema=parts[0], name=parts[1])
    return QualifiedIdentifier(schema=None, name=parts[0])


def quote_identifier(value: str) -> str:
    parsed = parse_identifier(value)
    if parsed.schema:
        return f"`{parsed.schema}`.`{parsed.name}`"
    return f"`{parsed.name}`"


def _identifier_error(value: Any) -> DbcliError:
    return DbcliError(
        Diagnostic(
            code="mysql.invalid_identifier",
            message="Identifier must be a safe MySQL identifier or schema.table pair.",
            path=None,
            details={"identifier": str(value)},
        ),
        ExitCode.USAGE_OR_DRIFT,
    )
