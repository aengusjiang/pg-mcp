"""Unit tests for configuration management.

Tests for all configuration classes to ensure proper validation,
defaults, and environment variable parsing.
"""

import os

import pytest
from pydantic import ValidationError

from pg_mcp.config.settings import (
    CacheConfig,
    DatabaseConfig,
    ObservabilityConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
    ValidationConfig,
    get_settings,
    reset_settings,
)


class TestDatabaseConfig:
    """Tests for DatabaseConfig."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = DatabaseConfig()
        assert config.host == "localhost"
        assert config.port == 5432
        assert config.name == "postgres"
        assert config.user == "postgres"
        assert config.min_pool_size == 5
        assert config.max_pool_size == 20

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = DatabaseConfig(
            host="db.example.com",
            port=5433,
            name="mydb",
            user="myuser",
            password="secret",
        )
        assert config.host == "db.example.com"
        assert config.port == 5433
        assert config.name == "mydb"
        assert config.user == "myuser"
        assert config.password == "secret"

    def test_dsn_generation(self) -> None:
        """Test DSN string generation."""
        config = DatabaseConfig(
            host="localhost",
            port=5432,
            name="testdb",
            user="testuser",
            password="testpass",
        )
        dsn = config.dsn
        assert dsn == "postgresql://testuser:testpass@localhost:5432/testdb"

    def test_safe_dsn_masks_password(self) -> None:
        """Test safe DSN masks password."""
        config = DatabaseConfig(
            host="localhost",
            port=5432,
            name="testdb",
            user="testuser",
            password="secret123",
        )
        safe_dsn = config.safe_dsn
        assert "secret123" not in safe_dsn
        assert "***" in safe_dsn
        assert "testuser" in safe_dsn

    def test_invalid_port(self) -> None:
        """Test invalid port number is rejected."""
        with pytest.raises(ValidationError):
            DatabaseConfig(port=0)

        with pytest.raises(ValidationError):
            DatabaseConfig(port=99999)

    def test_invalid_pool_size(self) -> None:
        """Test invalid pool size is rejected."""
        with pytest.raises(ValidationError):
            DatabaseConfig(min_pool_size=0)

        with pytest.raises(ValidationError):
            DatabaseConfig(max_pool_size=101)


class TestOpenAIConfig:
    """Tests for OpenAIConfig."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = OpenAIConfig(api_key="sk-test123")
        assert config.model == "gpt-4o-mini"
        assert config.max_tokens == 2000
        assert config.temperature == 0.0
        assert config.timeout == 30.0

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = OpenAIConfig(
            api_key="sk-custom",
            model="gpt-4",
            max_tokens=4000,
            temperature=0.7,
            timeout=60.0,
        )
        assert config.model == "gpt-4"
        assert config.max_tokens == 4000
        assert config.temperature == 0.7
        assert config.timeout == 60.0

    def test_empty_api_key_rejected(self) -> None:
        """Test empty API key is rejected."""
        with pytest.raises(ValidationError, match="must not be empty"):
            OpenAIConfig(api_key="")

    def test_whitespace_api_key_rejected(self) -> None:
        """Test whitespace-only API key is rejected."""
        with pytest.raises(ValidationError, match="must not be empty"):
            OpenAIConfig(api_key="   ")

    def test_invalid_api_key_format(self) -> None:
        """Test API key must start with sk-."""
        with pytest.raises(ValidationError, match="must start with 'sk-'"):
            OpenAIConfig(api_key="invalid-key")

    def test_base_url_allows_gateway_key_format(self) -> None:
        """A custom base_url lifts the 'sk-' prefix requirement."""
        config = OpenAIConfig(
            api_key="e07119c.gw-style-key",
            base_url="https://open.bigmodel.cn/api/paas/v4",
        )
        assert config.base_url == "https://open.bigmodel.cn/api/paas/v4"
        assert config.api_key.get_secret_value() == "e07119c.gw-style-key"

    def test_base_url_with_empty_key_still_rejected(self) -> None:
        """base_url does not waive the non-empty key requirement."""
        with pytest.raises(ValidationError, match="must not be empty"):
            OpenAIConfig(api_key="", base_url="https://gw.example.com/v4")

    def test_base_url_with_invalid_scheme_rejected(self) -> None:
        """base_url must be an http(s) URL."""
        with pytest.raises(ValidationError, match="http://' or 'https://'"):
            OpenAIConfig(api_key="gw-key", base_url="ftp://gw.example.com/v4")

    def test_base_url_defaults_to_none(self) -> None:
        """Without a gateway the key must keep the official sk- format."""
        config = OpenAIConfig(api_key="sk-test123")
        assert config.base_url is None

    def test_base_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPENAI_BASE_URL env var is picked up like other settings."""
        monkeypatch.setenv("OPENAI_BASE_URL", "https://gw.example.com/v4")
        monkeypatch.setenv("OPENAI_API_KEY", "gw-key")
        config = OpenAIConfig()
        assert config.base_url == "https://gw.example.com/v4"

    def test_invalid_max_tokens(self) -> None:
        """Test invalid max_tokens is rejected."""
        with pytest.raises(ValidationError):
            OpenAIConfig(api_key="sk-test", max_tokens=50)

        with pytest.raises(ValidationError):
            OpenAIConfig(api_key="sk-test", max_tokens=5000)

    def test_invalid_temperature(self) -> None:
        """Test invalid temperature is rejected."""
        with pytest.raises(ValidationError):
            OpenAIConfig(api_key="sk-test", temperature=-0.1)

        with pytest.raises(ValidationError):
            OpenAIConfig(api_key="sk-test", temperature=2.1)


class TestSecurityConfig:
    """Tests for SecurityConfig."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = SecurityConfig()
        assert config.max_rows == 10000
        assert config.max_execution_time == 30.0
        assert "pg_sleep" in config.blocked_functions
        assert "pg_read_file" in config.blocked_functions
        assert config.blocked_tables == []
        assert config.blocked_columns == []
        assert config.allow_explain is False

    def test_custom_blocked_functions(self) -> None:
        """Test custom blocked functions."""
        config = SecurityConfig(
            blocked_functions=["func1", "func2"],
        )
        assert config.blocked_functions == ["func1", "func2"]

    def test_parse_blocked_functions_from_string(self) -> None:
        """Test parsing blocked functions from comma-separated string."""
        config = SecurityConfig(
            blocked_functions="func1, func2, func3",  # type: ignore
        )
        assert "func1" in config.blocked_functions
        assert "func2" in config.blocked_functions
        assert "func3" in config.blocked_functions

    def test_blocked_tables_and_columns_from_string(self) -> None:
        """Test parsing blocked tables/columns from comma-separated strings."""
        config = SecurityConfig(
            blocked_tables="audit_log, secrets",  # type: ignore
            blocked_columns="users.email, tokens.value",  # type: ignore
        )
        assert config.blocked_tables == ["audit_log", "secrets"]
        assert config.blocked_columns == ["users.email", "tokens.value"]

    def test_allow_explain_flag(self) -> None:
        """Test enabling the EXPLAIN policy."""
        config = SecurityConfig(allow_explain=True)
        assert config.allow_explain is True

    def test_stale_env_vars_are_ignored(self) -> None:
        """Test that removed settings (e.g. allow_write_operations) don't break."""
        config = SecurityConfig(allow_write_operations=True)  # type: ignore[call-arg]
        assert not hasattr(config, "allow_write_operations")

    def test_invalid_max_rows(self) -> None:
        """Test invalid max_rows is rejected."""
        with pytest.raises(ValidationError):
            SecurityConfig(max_rows=0)

        with pytest.raises(ValidationError):
            SecurityConfig(max_rows=100001)


class TestValidationConfig:
    """Tests for ValidationConfig."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = ValidationConfig()
        assert config.enabled is True
        assert config.sample_rows == 5
        assert config.timeout_seconds == 10.0
        assert config.confidence_threshold == 70

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = ValidationConfig(
            enabled=False,
            sample_rows=10,
            timeout_seconds=20.0,
            confidence_threshold=85,
        )
        assert config.enabled is False
        assert config.sample_rows == 10
        assert config.timeout_seconds == 20.0
        assert config.confidence_threshold == 85

    def test_invalid_sample_rows(self) -> None:
        """Test invalid sample rows is rejected."""
        with pytest.raises(ValidationError):
            ValidationConfig(sample_rows=0)

        with pytest.raises(ValidationError):
            ValidationConfig(sample_rows=101)


class TestCacheConfig:
    """Tests for CacheConfig."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = CacheConfig()
        assert config.schema_ttl == 3600
        assert config.max_size == 100
        assert config.enabled is True

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = CacheConfig(
            schema_ttl=7200,
            max_size=200,
            enabled=False,
        )
        assert config.schema_ttl == 7200
        assert config.max_size == 200
        assert config.enabled is False

    def test_invalid_ttl(self) -> None:
        """Test invalid TTL is rejected."""
        with pytest.raises(ValidationError):
            CacheConfig(schema_ttl=30)

        with pytest.raises(ValidationError):
            CacheConfig(schema_ttl=90000)


class TestResilienceConfig:
    """Tests for ResilienceConfig."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = ResilienceConfig()
        assert config.max_retries == 3
        assert config.retry_delay == 1.0
        assert config.backoff_factor == 2.0
        assert config.circuit_breaker_threshold == 5
        assert config.circuit_breaker_timeout == 60.0
        assert config.query_concurrency == 10
        assert config.llm_concurrency == 5
        assert config.rate_limit_timeout == 30.0

    def test_concurrency_limits(self) -> None:
        """Test custom concurrency limits."""
        config = ResilienceConfig(
            query_concurrency=50,
            llm_concurrency=20,
            rate_limit_timeout=60.0,
        )
        assert config.query_concurrency == 50
        assert config.llm_concurrency == 20
        assert config.rate_limit_timeout == 60.0

    def test_invalid_concurrency(self) -> None:
        """Test invalid concurrency is rejected."""
        with pytest.raises(ValidationError):
            ResilienceConfig(query_concurrency=0)

        with pytest.raises(ValidationError):
            ResilienceConfig(llm_concurrency=-1)

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = ResilienceConfig(
            max_retries=5,
            retry_delay=2.0,
            backoff_factor=3.0,
        )
        assert config.max_retries == 5
        assert config.retry_delay == 2.0
        assert config.backoff_factor == 3.0

    def test_invalid_values(self) -> None:
        """Test invalid values are rejected."""
        with pytest.raises(ValidationError):
            ResilienceConfig(max_retries=-1)

        with pytest.raises(ValidationError):
            ResilienceConfig(backoff_factor=0.5)


class TestObservabilityConfig:
    """Tests for ObservabilityConfig."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = ObservabilityConfig()
        # metrics_enabled 在测试环境可能被禁用以避免启动 HTTP 服务器
        # 生产环境应该通过环境变量显式设置
        assert config.metrics_port == 9090
        assert config.log_level == "INFO"
        assert config.log_format == "json"

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = ObservabilityConfig(
            metrics_enabled=False,
            metrics_port=8080,
            log_level="DEBUG",
            log_format="text",
        )
        assert config.metrics_enabled is False
        assert config.metrics_port == 8080
        assert config.log_level == "DEBUG"
        assert config.log_format == "text"

    def test_invalid_log_level(self) -> None:
        """Test invalid log level is rejected."""
        with pytest.raises(ValidationError):
            ObservabilityConfig(log_level="INVALID")  # type: ignore

    def test_invalid_log_format(self) -> None:
        """Test invalid log format is rejected."""
        with pytest.raises(ValidationError):
            ObservabilityConfig(log_format="xml")  # type: ignore


class TestSettings:
    """Tests for main Settings class."""

    def test_default_settings(self) -> None:
        """Test default settings initialization."""
        settings = Settings(openai=OpenAIConfig(api_key="sk-test"))
        assert settings.environment == "development"
        assert settings.database is not None
        assert settings.openai is not None
        assert settings.security is not None
        assert settings.validation is not None
        assert settings.cache is not None
        assert settings.resilience is not None
        assert settings.observability is not None

    def test_databases_file_default(self) -> None:
        """Test databases_file defaults to None (single-database mode)."""
        settings = Settings(openai=OpenAIConfig(api_key="sk-test"))
        assert settings.databases_file is None

    def test_nested_config_override(self) -> None:
        """Test overriding nested configurations."""
        settings = Settings(
            openai=OpenAIConfig(api_key="sk-test"),
            database=DatabaseConfig(
                host="custom.host",
                port=5433,
            ),
            security=SecurityConfig(
                blocked_tables=["audit_log"],
            ),
        )
        assert settings.database.host == "custom.host"
        assert settings.database.port == 5433
        assert settings.security.blocked_tables == ["audit_log"]


class TestSettingsGlobalInstance:
    """Tests for global settings instance management."""

    def teardown_method(self) -> None:
        """Clean up after each test."""
        reset_settings()
        # Clean up environment variables
        for key in list(os.environ.keys()):
            if key.startswith(("DATABASE_", "OPENAI_", "SECURITY_")):
                del os.environ[key]

    def test_get_settings_creates_instance(self) -> None:
        """Test get_settings creates instance."""
        # Set required env var
        os.environ["OPENAI_API_KEY"] = "sk-test123"

        settings = get_settings()
        assert settings is not None
        assert isinstance(settings, Settings)

    def test_get_settings_returns_same_instance(self) -> None:
        """Test get_settings returns singleton."""
        os.environ["OPENAI_API_KEY"] = "sk-test123"

        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_reset_settings(self) -> None:
        """Test reset_settings clears instance."""
        os.environ["OPENAI_API_KEY"] = "sk-test123"

        settings1 = get_settings()
        reset_settings()
        settings2 = get_settings()
        assert settings1 is not settings2

    def test_settings_from_environment(self) -> None:
        """Test loading settings from environment variables."""
        os.environ["OPENAI_API_KEY"] = "sk-env-key"
        os.environ["OPENAI_MODEL"] = "gpt-4"
        os.environ["DATABASE_HOST"] = "env.host.com"
        os.environ["SECURITY_MAX_ROWS"] = "5000"

        reset_settings()
        settings = get_settings()

        # Use get_secret_value() to access SecretStr content
        assert settings.openai.api_key.get_secret_value() == "sk-env-key"
        assert settings.openai.model == "gpt-4"
        assert settings.database.host == "env.host.com"
        assert settings.security.max_rows == 5000


class TestDotEnvPropagation:
    """Tests that nested config sections read the .env file.

    Regression guard: pydantic-settings does not propagate the parent's
    env_file, so every nested section must declare it explicitly. Before
    this fix, a plain .env file was silently ignored for DATABASE_*/,
    /*SECURITY_, ... variables.
    """

    def test_database_config_reads_dotenv(self, tmp_path, monkeypatch) -> None:
        """Nested DatabaseConfig picks up DATABASE_* from a .env file."""
        (tmp_path / ".env").write_text(
            "DATABASE_HOST=dotenv.host\nDATABASE_NAME=blog_small\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        config = DatabaseConfig()
        assert config.host == "dotenv.host"
        assert config.name == "blog_small"

    def test_security_config_reads_dotenv(self, tmp_path, monkeypatch) -> None:
        """Nested SecurityConfig picks up SECURITY_* from a .env file."""
        (tmp_path / ".env").write_text(
            "SECURITY_BLOCKED_TABLES=audit_log,secrets\n"
            "SECURITY_ALLOW_EXPLAIN=true\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        config = SecurityConfig()
        assert config.blocked_tables == ["audit_log", "secrets"]
        assert config.allow_explain is True

    def test_security_lists_from_os_env(self, monkeypatch) -> None:
        """Comma-separated list env vars parse without JSON quoting."""
        monkeypatch.setenv("SECURITY_BLOCKED_FUNCTIONS", "custom_fn, other_fn")
        monkeypatch.setenv("SECURITY_BLOCKED_COLUMNS", "users.email")
        config = SecurityConfig()
        assert config.blocked_functions == ["custom_fn", "other_fn"]
        assert config.blocked_columns == ["users.email"]

    def test_os_env_overrides_dotenv(self, tmp_path, monkeypatch) -> None:
        """Real environment variables take precedence over .env values."""
        (tmp_path / ".env").write_text("DATABASE_PORT=5433\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DATABASE_PORT", "6543")
        config = DatabaseConfig()
        assert config.port == 6543

    def test_settings_databases_file_from_dotenv(self, tmp_path, monkeypatch) -> None:
        """Top-level Settings reads DATABASES_FILE from a .env file."""
        databases_file = tmp_path / "databases.json"
        (tmp_path / ".env").write_text(
            f"DATABASES_FILE={databases_file}\n"
            "OPENAI_API_KEY=sk-test\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        settings = Settings()
        assert settings.databases_file == str(databases_file)
