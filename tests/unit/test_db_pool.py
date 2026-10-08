"""Unit tests for database pool management in db/pool.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_mcp.config.settings import DatabaseConfig
from pg_mcp.db import pool as pool_module
from pg_mcp.db.pool import close_pools, create_pool


@pytest.fixture
def db_config() -> DatabaseConfig:
    """Create a database configuration for pool creation."""
    return DatabaseConfig(
        host="localhost",
        port=5432,
        name="testdb",
        user="testuser",
        password="testpass",
        min_pool_size=2,
        max_pool_size=10,
        pool_timeout=30.0,
        command_timeout=15.0,
    )


class TestCreatePool:
    @pytest.mark.asyncio
    async def test_create_pool_passes_config_parameters(
        self, db_config: DatabaseConfig
    ) -> None:
        """create_pool forwards every config field to asyncpg."""
        mock_pool = MagicMock()
        with patch.object(
            pool_module.asyncpg, "create_pool", new=AsyncMock(return_value=mock_pool)
        ) as mock_create:
            pool = await create_pool(db_config)

        assert pool is mock_pool
        kwargs = mock_create.call_args.kwargs
        assert kwargs["host"] == "localhost"
        assert kwargs["port"] == 5432
        assert kwargs["database"] == "testdb"
        assert kwargs["user"] == "testuser"
        assert kwargs["password"] == "testpass"
        assert kwargs["min_size"] == 2
        assert kwargs["max_size"] == 10
        assert kwargs["timeout"] == 30.0
        assert kwargs["command_timeout"] == 15.0

    @pytest.mark.asyncio
    async def test_create_pool_none_result_raises(
        self, db_config: DatabaseConfig
    ) -> None:
        """A None pool from asyncpg fails fast with a clear error."""
        with (
            patch.object(pool_module.asyncpg, "create_pool", new=AsyncMock(return_value=None)),
            pytest.raises(RuntimeError, match="testdb"),
        ):
            await create_pool(db_config)


class TestClosePools:
    def _mock_pool(self, close_side_effect: object = None) -> MagicMock:
        """Build a mock asyncpg pool."""
        mock_pool = MagicMock()
        mock_pool.close = AsyncMock(side_effect=close_side_effect)
        return mock_pool

    @pytest.mark.asyncio
    async def test_close_pools_graceful(self) -> None:
        """Pools are closed gracefully in order."""
        pool = self._mock_pool()

        await close_pools({"mydb": pool})

        pool.close.assert_awaited_once()
        pool.terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_pools_timeout_forces_termination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pool that hangs past the close timeout is force-terminated."""
        monkeypatch.setattr(pool_module, "_CLOSE_TIMEOUT_SECONDS", 0.05)

        async def slow_close() -> None:
            import asyncio

            await asyncio.sleep(1)

        pool = MagicMock()
        pool.close = AsyncMock(side_effect=slow_close)

        await close_pools({"mydb": pool})

        pool.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_pools_error_continues_to_other_pools(self) -> None:
        """An error closing one pool does not stop closing the rest."""
        failing = self._mock_pool(close_side_effect=RuntimeError("boom"))
        healthy = self._mock_pool()

        await close_pools({"bad": failing, "good": healthy})

        failing.terminate.assert_called_once()
        healthy.close.assert_awaited_once()
