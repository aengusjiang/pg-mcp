"""Pytest configuration and shared fixtures.

This module provides shared fixtures and configuration for all tests:
always-on resets (global settings, metrics off, hermetic working directory)
plus opt-in guards for the environment-dependent integration tests.
"""

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

from pg_mcp.config.settings import Settings, reset_settings

# Fixture databases created by fixtures/Makefile (see docs/DEVELOPMENT.md).
FIXTURE_DATABASES: tuple[str, ...] = ("blog_small", "ecommerce_medium", "saas_crm_large")

# Repository root (two levels above this file: tests/conftest.py).
REPO_ROOT = Path(__file__).resolve().parents[1]

# OPENAI_* variables forwarded from the developer's .env into the test
# process environment so nested config classes see them even under the
# hermetic working directory (OS env beats env_file in pydantic-settings).
_OPENAI_ENV_KEYS: tuple[str, ...] = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")


def _load_repo_dotenv_openai() -> dict[str, str]:
    """Read OPENAI_* values from the repository root .env, if present.

    Returns a dict of variable name -> raw value (never logged). Only the
    handful of keys needed by the LLM guard are extracted; parsing stays
    deliberately simple (KEY=VALUE lines, no interpolation).
    """
    values: dict[str, str] = {}
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _OPENAI_ENV_KEYS:
            values[key] = value.strip().strip("'\"")
    return values


@pytest.fixture(autouse=True)
def hermetic_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in an empty working directory.

    Config classes resolve their env_file (".env") relative to the cwd, so
    without this fixture any developer with a real .env in the repo root
    would leak local settings (OPENAI_MODEL, DATABASES_FILE, ...) into
    default-value assertions. Tests that need a specific cwd (e.g. the
    dotenv propagation tests) chdir themselves afterwards.
    """
    monkeypatch.chdir(tmp_path)


def fixture_connection_kwargs() -> dict[str, object]:
    """Connection parameters for the local fixture PostgreSQL instance.

    Defaults match the docs/DEVELOPMENT.md tutorial (localhost, user
    postgres, password postgres); override with the standard PG*
    environment variables.
    """
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", "postgres"),
    }


async def _database_reachable(name: str) -> bool:
    """Probe one fixture database with a short-lived connection."""
    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(database=name, timeout=3.0, **fixture_connection_kwargs())
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            await conn.close()


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global settings before each test."""
    reset_settings()


@pytest.fixture(autouse=True)
def disable_metrics_for_tests():
    """Disable metrics for tests to avoid port conflicts."""
    os.environ["OBSERVABILITY_METRICS_ENABLED"] = "false"
    yield
    # Clean up
    if "OBSERVABILITY_METRICS_ENABLED" in os.environ:
        del os.environ["OBSERVABILITY_METRICS_ENABLED"]


@pytest.fixture
def fixture_databases() -> dict[str, object]:
    """Skip unless all fixture databases are reachable.

    Returns the connection kwargs so callers can point their own
    databases.json entries (or DATABASE_* variables) at the same instance.
    """
    for name in FIXTURE_DATABASES:
        if not asyncio.run(_database_reachable(name)):
            pytest.skip(
                f"Fixture database '{name}' is not reachable; run "
                "'cd fixtures && make create-all' first (see docs/DEVELOPMENT.md)"
            )
    return fixture_connection_kwargs()


@pytest.fixture
def require_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip unless a usable OpenAI (or compatible gateway) config exists.

    The key may come from OS env vars or the repository root .env; values
    found only in .env are re-exported as OS env vars (which take priority
    over env_file) so Settings() constructions inside the server under
    test see them despite the hermetic working directory. Only keys that
    are absent from the OS environment AND non-empty in .env are set, so
    an empty-string export can never mask a real .env value.

    The real-LLM tests exercise the full generation pipeline; without a
    key the server lifespan fails at startup, so skip instead of failing.
    """
    dotenv_values = _load_repo_dotenv_openai()
    for key, value in dotenv_values.items():
        if value and key not in os.environ:
            monkeypatch.setenv(key, value)
    try:
        settings = Settings()
    except Exception as e:
        pytest.skip(f"Settings failed to load: {e}")
    if not settings.openai.api_key.get_secret_value().strip():
        pytest.skip("OPENAI_API_KEY not configured; skipping real-LLM integration tests")


@pytest.fixture
def blog_environment(
    fixture_databases: dict[str, object],
    require_llm_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configure the single-database DATABASE_* fallback to point at blog_small.

    Combines both integration guards (fixture databases reachable, LLM key
    present) so the server lifespan under test starts against the small
    fixture database.
    """
    for key, value in fixture_databases.items():
        monkeypatch.setenv(f"DATABASE_{key.upper()}", str(value))
    monkeypatch.setenv("DATABASE_NAME", "blog_small")
