"""Multi-database configuration resolution.

This module resolves the set of databases the server should serve from
either a JSON configuration file (``DATABASES_FILE``) or the single-database
``DATABASE_*`` environment variables as a backward-compatible fallback.

Each database entry carries its own connection parameters, pool settings,
and optional per-database security overrides (blocked tables/columns and
EXPLAIN policy). Overrides left unset (``None``) inherit the global
``SECURITY_*`` defaults at construction time in the server lifespan.
"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from pg_mcp.config.settings import DatabaseConfig, Settings

# Logical database names are used as pool keys, request targets, and
# Prometheus metric labels, so restrict them to a conservative identifier
# charset (letters, digits, underscore, dash).
_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,62}$")


class DatabaseSecurityOverrides(BaseModel):
    """Per-database security rule overrides.

    ``None`` means "inherit the global ``SECURITY_*`` default".
    """

    model_config = ConfigDict(extra="forbid")

    blocked_tables: list[str] | None = Field(
        default=None, description="Tables blocked for this database (None = inherit global)"
    )
    blocked_columns: list[str] | None = Field(
        default=None, description="Columns blocked for this database (None = inherit global)"
    )
    allow_explain: bool | None = Field(
        default=None, description="Whether EXPLAIN is allowed (None = inherit global)"
    )


class DatabasePoolSettings(BaseModel):
    """Per-database connection pool settings."""

    model_config = ConfigDict(extra="forbid")

    min_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )


class DatabaseEntry(BaseModel):
    """A single database entry from ``databases.json`` (or the env fallback)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Logical database name (pool key, request target)")
    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    database: str | None = Field(
        default=None, description="Physical PostgreSQL database name (defaults to name)"
    )
    user: str = Field(default="postgres", description="Database user")
    password: SecretStr = Field(default=SecretStr(""), description="Database password")
    pool: DatabasePoolSettings = Field(
        default_factory=DatabasePoolSettings, description="Connection pool settings"
    )
    security: DatabaseSecurityOverrides = Field(
        default_factory=DatabaseSecurityOverrides,
        description="Per-database security overrides (None values inherit global)",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Ensure the logical name is a safe identifier.

        Args:
            v: Candidate logical name.

        Returns:
            str: The validated name.

        Raises:
            ValueError: If the name does not match the allowed pattern.
        """
        if not _NAME_PATTERN.match(v):
            raise ValueError(
                f"Database name '{v}' is invalid: must match "
                "'^[a-zA-Z_][a-zA-Z0-9_-]{0,62}$'"
            )
        return v

    @model_validator(mode="after")
    def resolve_defaults(self) -> "DatabaseEntry":
        """Resolve derived defaults after field validation.

        Fills in ``database`` from ``name`` when omitted and rejects pool
        configurations where ``min_size`` exceeds ``max_size``.

        Returns:
            DatabaseEntry: The validated entry with defaults resolved.

        Raises:
            ValueError: If pool sizes are inconsistent.
        """
        if self.database is None:
            # model_validators run in-after mode on a mutable model copy
            object.__setattr__(self, "database", self.name)
        if self.pool.min_size > self.pool.max_size:
            raise ValueError(
                f"Database '{self.name}': pool.min_size ({self.pool.min_size}) "
                f"cannot exceed pool.max_size ({self.pool.max_size})"
            )
        return self

    @property
    def dsn(self) -> str:
        """Build the PostgreSQL DSN connection string."""
        return (
            f"postgresql://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def safe_dsn(self) -> str:
        """Build the DSN with a masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"

    def to_database_config(self) -> DatabaseConfig:
        """Convert to a ``DatabaseConfig`` for ``db.pool.create_pool``.

        Every field is passed explicitly so that ambient ``DATABASE_*``
        environment variables cannot leak into a JSON-configured entry.

        Returns:
            DatabaseConfig: Connection configuration for this entry.
        """
        return DatabaseConfig(
            host=self.host,
            port=self.port,
            name=self.database or self.name,
            user=self.user,
            password=self.password.get_secret_value(),
            min_pool_size=self.pool.min_size,
            max_pool_size=self.pool.max_size,
            pool_timeout=self.pool.timeout,
            command_timeout=self.pool.command_timeout,
        )


class DatabasesFile(BaseModel):
    """Root schema of ``databases.json``."""

    model_config = ConfigDict(extra="forbid")

    default_database: str | None = Field(
        default=None,
        description="Database used when a request omits the database parameter",
    )
    databases: list[DatabaseEntry] = Field(
        ..., min_length=1, description="Non-empty list of database entries"
    )

    @model_validator(mode="after")
    def validate_entries(self) -> "DatabasesFile":
        """Ensure database names are unique and the default exists.

        Returns:
            DatabasesFile: The validated configuration.

        Raises:
            ValueError: On duplicate names or a missing default database.
        """
        names = [entry.name for entry in self.databases]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"Duplicate database names in databases file: {sorted(duplicates)}")
        if self.default_database is not None and self.default_database not in names:
            raise ValueError(
                f"default_database '{self.default_database}' is not defined in databases"
            )
        return self


class DatabaseRegistry:
    """Resolved set of databases the server should serve."""

    def __init__(self, entries: dict[str, DatabaseEntry], default_database: str | None) -> None:
        """Initialize the registry.

        Args:
            entries: Mapping of logical name to database entry.
            default_database: Logical name used when a request omits
                the database parameter (may be None).
        """
        self._entries = dict(entries)
        self._default_database = default_database

    @property
    def entries(self) -> dict[str, DatabaseEntry]:
        """Get the database entries keyed by logical name."""
        return dict(self._entries)

    @property
    def default_database(self) -> str | None:
        """Get the default database name, if any."""
        return self._default_database

    @property
    def names(self) -> list[str]:
        """Get the list of configured database names."""
        return list(self._entries.keys())

    def __repr__(self) -> str:
        """Return a string representation of the registry."""
        return (
            f"DatabaseRegistry(databases={self.names}, "
            f"default_database={self._default_database!r})"
        )


def load_databases_file(path: str | Path) -> DatabasesFile:
    """Load and validate a ``databases.json`` file.

    Args:
        path: Path to the JSON configuration file.

    Returns:
        DatabasesFile: The validated configuration.

    Raises:
        ValueError: If the file is missing, unparsable, or fails validation.
            The message always includes the file path for troubleshooting.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"Databases file not found: {file_path}")

    try:
        raw: Any = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in databases file {file_path}: {e}") from e

    # Allow "_"-prefixed top-level keys (e.g. "_comment") so the JSON file can
    # carry usage notes despite JSON having no comment syntax.
    if isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if not key.startswith("_")}

    try:
        return DatabasesFile.model_validate(raw)
    except Exception as e:
        raise ValueError(f"Invalid databases configuration in {file_path}: {e}") from e


def resolve_database_entries(settings: Settings) -> DatabaseRegistry:
    """Resolve the database registry from settings.

    Prefers the JSON file referenced by ``settings.databases_file``; falls
    back to a single entry built from the ``DATABASE_*`` environment
    configuration (which also becomes the default database).

    Args:
        settings: Application settings.

    Returns:
        DatabaseRegistry: The resolved registry.

    Raises:
        ValueError: If the configured databases file cannot be loaded
            or validated (fail fast at startup).
    """
    if settings.databases_file:
        databases_file = load_databases_file(settings.databases_file)
        entries = {entry.name: entry for entry in databases_file.databases}
        return DatabaseRegistry(
            entries=entries,
            default_database=databases_file.default_database,
        )

    # Backward-compatible single-database fallback from DATABASE_* env vars.
    db = settings.database
    entry = DatabaseEntry(
        name=db.name,
        host=db.host,
        port=db.port,
        database=db.name,
        user=db.user,
        password=SecretStr(db.password),
        pool=DatabasePoolSettings(
            min_size=db.min_pool_size,
            max_size=db.max_pool_size,
            timeout=db.pool_timeout,
            command_timeout=db.command_timeout,
        ),
    )
    return DatabaseRegistry(entries={entry.name: entry}, default_database=entry.name)
