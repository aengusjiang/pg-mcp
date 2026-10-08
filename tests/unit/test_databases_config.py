"""Unit tests for multi-database configuration resolution.

Covers DatabaseEntry validation, DatabasesFile invariants, JSON file
loading errors, env fallback, and conversion to DatabaseConfig.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pg_mcp.config.databases import (
    DatabaseEntry,
    DatabasePoolSettings,
    DatabaseRegistry,
    DatabaseSecurityOverrides,
    DatabasesFile,
    load_databases_file,
    resolve_database_entries,
)
from pg_mcp.config.settings import DatabaseConfig, OpenAIConfig, Settings


def make_settings(**overrides) -> Settings:
    """Build a Settings instance with a valid API key for tests."""
    defaults: dict = {"openai": OpenAIConfig(api_key="sk-test")}
    defaults.update(overrides)
    return Settings(**defaults)


class TestDatabaseEntry:
    """Tests for DatabaseEntry validation and defaults."""

    def test_minimal_entry(self) -> None:
        """A name-only entry fills connection defaults and database=name."""
        entry = DatabaseEntry(name="blog_small")
        assert entry.host == "localhost"
        assert entry.port == 5432
        assert entry.user == "postgres"
        assert entry.database == "blog_small"
        assert entry.pool.min_size == 5
        assert entry.security.blocked_tables is None

    def test_name_pattern_rejected(self) -> None:
        """Names with spaces, dots, or leading digits are rejected."""
        for bad_name in ["my db", "1db", "db.name", "", "a" * 100]:
            with pytest.raises(ValidationError):
                DatabaseEntry(name=bad_name)

    def test_name_pattern_accepted(self) -> None:
        """Valid identifier-style names are accepted."""
        for good_name in ["db1", "blog_small", "saas-crm-large", "_internal"]:
            entry = DatabaseEntry(name=good_name)
            assert entry.name == good_name

    def test_pool_size_consistency(self) -> None:
        """min_size > max_size is rejected."""
        with pytest.raises(ValidationError, match="min_size"):
            DatabaseEntry(
                name="db",
                pool=DatabasePoolSettings(min_size=10, max_size=5),
            )

    def test_port_bounds(self) -> None:
        """Out-of-range ports are rejected."""
        with pytest.raises(ValidationError):
            DatabaseEntry(name="db", port=0)
        with pytest.raises(ValidationError):
            DatabaseEntry(name="db", port=70000)

    def test_extra_fields_forbidden(self) -> None:
        """Unknown keys are rejected to catch typos in databases.json."""
        with pytest.raises(ValidationError):
            DatabaseEntry(name="db", hostname="localhost")  # type: ignore[call-arg]

    def test_safe_dsn_masks_password(self) -> None:
        """safe_dsn never exposes the password; dsn does."""
        entry = DatabaseEntry(name="db", password="s3cret")
        assert "s3cret" not in entry.safe_dsn
        assert "***" in entry.safe_dsn
        assert "s3cret" in entry.dsn

    def test_to_database_config_round_trip(self) -> None:
        """Conversion produces a DatabaseConfig with all values carried over."""
        entry = DatabaseEntry(
            name="blog_small",
            host="db.internal",
            port=5433,
            database="blog_prod",
            user="reader",
            password="pw",
            pool=DatabasePoolSettings(
                min_size=2, max_size=7, timeout=11.0, command_timeout=22.0
            ),
        )
        config = entry.to_database_config()
        assert isinstance(config, DatabaseConfig)
        assert config.host == "db.internal"
        assert config.port == 5433
        assert config.name == "blog_prod"
        assert config.user == "reader"
        assert config.password == "pw"
        assert config.min_pool_size == 2
        assert config.max_pool_size == 7
        assert config.pool_timeout == 11.0
        assert config.command_timeout == 22.0

    def test_to_database_config_ignores_ambient_env(self, monkeypatch) -> None:
        """Ambient DATABASE_* env vars cannot leak into a JSON entry."""
        monkeypatch.setenv("DATABASE_HOST", "evil.host")
        monkeypatch.setenv("DATABASE_MAX_POOL_SIZE", "999")
        entry = DatabaseEntry(name="db", host="explicit.host")
        config = entry.to_database_config()
        assert config.host == "explicit.host"
        assert config.max_pool_size == 20


class TestDatabasesFile:
    """Tests for DatabasesFile invariants."""

    def test_duplicate_names_rejected(self) -> None:
        """Duplicate database names are rejected."""
        with pytest.raises(ValidationError, match="Duplicate"):
            DatabasesFile(
                databases=[DatabaseEntry(name="a"), DatabaseEntry(name="a")]
            )

    def test_missing_default_rejected(self) -> None:
        """default_database must reference a defined entry."""
        with pytest.raises(ValidationError, match="default_database"):
            DatabasesFile(
                default_database="missing",
                databases=[DatabaseEntry(name="a")],
            )

    def test_empty_databases_rejected(self) -> None:
        """At least one database is required."""
        with pytest.raises(ValidationError):
            DatabasesFile(databases=[])

    def test_valid_file(self) -> None:
        """A valid file parses with defaults resolved."""
        parsed = DatabasesFile.model_validate(
            {
                "default_database": "a",
                "databases": [
                    {"name": "a", "database": "physical_a"},
                    {"name": "b", "security": {"blocked_tables": ["x"]}},
                ],
            }
        )
        assert parsed.default_database == "a"
        assert parsed.databases[0].database == "physical_a"
        assert parsed.databases[1].database == "b"
        assert parsed.databases[1].security.blocked_tables == ["x"]


class TestLoadDatabasesFile:
    """Tests for JSON file loading and error context."""

    def _write(self, tmp_path: Path, payload: object) -> Path:
        path = tmp_path / "databases.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_happy_path(self, tmp_path: Path) -> None:
        """A valid JSON file loads and validates."""
        path = self._write(
            tmp_path,
            {
                "_comment": "Underscore-prefixed keys are ignored (JSON has no comments).",
                "default_database": "blog_small",
                "databases": [
                    {"name": "blog_small", "security": {"allow_explain": True}},
                    {"name": "ecommerce_medium"},
                ],
            },
        )
        parsed = load_databases_file(path)
        assert parsed.default_database == "blog_small"
        assert parsed.databases[0].security.allow_explain is True

    def test_missing_file_error_includes_path(self, tmp_path: Path) -> None:
        """A missing file raises ValueError naming the path."""
        with pytest.raises(ValueError, match="not found"):
            load_databases_file(tmp_path / "nope.json")

    def test_invalid_json_error(self, tmp_path: Path) -> None:
        """Malformed JSON raises ValueError with parse context."""
        path = tmp_path / "databases.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_databases_file(path)

    def test_schema_violation_error(self, tmp_path: Path) -> None:
        """A structurally invalid file raises ValueError with the path."""
        path = self._write(tmp_path, {"databases": [{"name": "bad name"}]})
        with pytest.raises(ValueError, match="Invalid databases configuration"):
            load_databases_file(path)


class TestResolveDatabaseEntries:
    """Tests for registry resolution (JSON vs env fallback)."""

    def test_env_fallback_single_database(self, monkeypatch) -> None:
        """Without DATABASES_FILE, a single env-based entry becomes default."""
        monkeypatch.delenv("DATABASES_FILE", raising=False)
        monkeypatch.setenv("DATABASE_NAME", "env_db")
        monkeypatch.setenv("DATABASE_HOST", "env.host")
        settings = make_settings()
        registry = resolve_database_entries(settings)
        assert registry.names == ["env_db"]
        assert registry.default_database == "env_db"
        entry = registry.entries["env_db"]
        assert entry.host == "env.host"
        assert entry.database == "env_db"

    def test_json_file_takes_precedence(self, tmp_path: Path, monkeypatch) -> None:
        """With DATABASES_FILE set, the JSON file wins over DATABASE_* env."""
        path = tmp_path / "databases.json"
        path.write_text(
            json.dumps(
                {
                    "default_database": "second",
                    "databases": [{"name": "first"}, {"name": "second"}],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("DATABASES_FILE", str(path))
        monkeypatch.setenv("DATABASE_NAME", "env_db_ignored")
        settings = make_settings()
        registry = resolve_database_entries(settings)
        assert sorted(registry.names) == ["first", "second"]
        assert registry.default_database == "second"

    def test_unreadable_json_fails_fast(self, tmp_path: Path, monkeypatch) -> None:
        """An unreadable DATABASES_FILE raises ValueError at resolution time."""
        monkeypatch.setenv("DATABASES_FILE", str(tmp_path / "missing.json"))
        settings = make_settings()
        with pytest.raises(ValueError, match="not found"):
            resolve_database_entries(settings)


class TestDatabaseRegistry:
    """Tests for the registry container."""

    def test_entries_defensively_copied(self) -> None:
        """Mutating the returned entries dict does not affect the registry."""
        registry = DatabaseRegistry(entries={"a": DatabaseEntry(name="a")}, default_database="a")
        registry.entries["b"] = DatabaseEntry(name="b")
        assert registry.names == ["a"]

    def test_repr(self) -> None:
        """Repr includes names and default."""
        registry = DatabaseRegistry(
            entries={"a": DatabaseEntry(name="a")}, default_database="a"
        )
        assert "a" in repr(registry)


class TestDatabaseSecurityOverrides:
    """Tests for the overrides model."""

    def test_defaults_are_none(self) -> None:
        """All overrides default to None (= inherit global)."""
        overrides = DatabaseSecurityOverrides()
        assert overrides.blocked_tables is None
        assert overrides.blocked_columns is None
        assert overrides.allow_explain is None

    def test_extra_forbidden(self) -> None:
        """Typos in the security block are rejected."""
        with pytest.raises(ValidationError):
            DatabaseSecurityOverrides(blocked_table=["x"])  # type: ignore[call-arg]
