"""Configuration management for PostgreSQL MCP Server.

This module defines all configuration settings using Pydantic for validation
and type safety. Configuration is loaded from environment variables with
sensible defaults.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class DatabaseConfig(BaseSettings):
    """PostgreSQL database connection configuration."""

    # env_file is required on every nested section: pydantic-settings does
    # not propagate the parent's dotenv source, so without it a plain .env
    # file is silently ignored for DATABASE_*/OPENAI_*/... variables.
    model_config = SettingsConfigDict(
        env_prefix="DATABASE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(default="postgres", description="Database name")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")

    # Connection pool settings
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    @property
    def dsn(self) -> str:
        """Build PostgreSQL DSN connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


class OpenAIConfig(BaseSettings):
    """OpenAI API configuration."""

    model_config = SettingsConfigDict(
        env_prefix="OPENAI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    model: str = Field(default="gpt-4o-mini", description="Model to use for SQL generation")
    max_tokens: int = Field(default=2000, ge=100, le=4096, description="Maximum tokens in response")
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Temperature for response randomness"
    )
    timeout: float = Field(
        default=30.0, ge=5.0, le=120.0, description="API request timeout in seconds"
    )
    base_url: str | None = Field(
        default=None,
        description="Base URL of an OpenAI-compatible API gateway "
        "(e.g. a self-hosted or third-party endpoint). When set, the API key "
        "may use the gateway's own format instead of the 'sk-' prefix",
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Validate API key is not empty."""
        api_key_str = v.get_secret_value()
        if not api_key_str or not api_key_str.strip():
            raise ValueError("OpenAI API key must not be empty")
        return v

    @model_validator(mode="after")
    def validate_key_format_for_endpoint(self) -> Self:
        """Cross-field validation of key format and base URL scheme.

        Rules:
        - base_url must be an http(s) URL when provided;
        - against the official OpenAI API (no base_url) the key must start
          with 'sk-'; a custom gateway may use its own key format, so the
          prefix requirement only applies to the official endpoint.
        """
        if self.base_url is not None:
            if not self.base_url.startswith(("http://", "https://")):
                raise ValueError("OPENAI_BASE_URL must start with 'http://' or 'https://'")
        else:
            api_key_str = self.api_key.get_secret_value()
            if not api_key_str.startswith("sk-"):
                raise ValueError(
                    "OpenAI API key must start with 'sk-' "
                    "(or set OPENAI_BASE_URL when using an OpenAI-compatible gateway)"
                )
        return self


class SecurityConfig(BaseSettings):
    """Security and access control configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SECURITY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # NoDecode keeps env/dotenv values as raw strings so the comma-separated
    # format works: without it pydantic-settings would try json.loads() on
    # "pg_sleep, pg_read_file" and fail before the field validator runs.
    blocked_functions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "pg_sleep",
            "pg_read_file",
            "pg_write_file",
            "lo_import",
            "lo_export",
        ],
        description="List of blocked PostgreSQL functions",
    )
    blocked_tables: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="List of blocked tables (global default; per-database overrides exist)",
    )
    blocked_columns: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="List of blocked columns, optionally as 'table.column' "
        "(global default; per-database overrides exist)",
    )
    allow_explain: bool = Field(
        default=False,
        description="Whether EXPLAIN statements are allowed (global default)",
    )
    max_rows: int = Field(default=10000, ge=1, le=100000, description="Maximum rows to return")
    max_execution_time: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Maximum query execution time in seconds"
    )
    readonly_role: str | None = Field(
        default=None, description="PostgreSQL role to switch to for read-only access"
    )
    safe_search_path: str = Field(
        default="public", description="Safe search_path to set during query execution"
    )

    @field_validator("blocked_functions", "blocked_tables", "blocked_columns", mode="before")
    @classmethod
    def parse_string_list(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string or list."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


class ValidationConfig(BaseSettings):
    """Query validation configuration."""

    model_config = SettingsConfigDict(
        env_prefix="VALIDATION_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Result validation settings
    enabled: bool = Field(default=True, description="Enable result validation using LLM")
    sample_rows: int = Field(
        default=5, ge=1, le=100, description="Number of sample rows to include in validation"
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Result validation timeout in seconds"
    )
    confidence_threshold: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence for acceptable results"
    )


class CacheConfig(BaseSettings):
    """Schema cache configuration."""

    model_config = SettingsConfigDict(
        env_prefix="CACHE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    schema_ttl: int = Field(
        default=3600, ge=60, le=86400, description="Schema cache TTL in seconds"
    )
    max_size: int = Field(default=100, ge=1, le=1000, description="Maximum cache entries")
    enabled: bool = Field(default=True, description="Enable schema caching")


class ResilienceConfig(BaseSettings):
    """Resilience and fault tolerance configuration."""

    model_config = SettingsConfigDict(
        env_prefix="RESILIENCE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")
    retry_delay: float = Field(
        default=1.0, ge=0.1, le=10.0, description="Initial retry delay in seconds"
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Exponential backoff factor"
    )
    circuit_breaker_threshold: int = Field(
        default=5, ge=1, le=100, description="Failures before circuit opens"
    )
    circuit_breaker_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="Circuit breaker timeout in seconds"
    )
    query_concurrency: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Maximum concurrent database queries (semaphore-based)",
    )
    llm_concurrency: int = Field(
        default=5,
        ge=1,
        le=1000,
        description="Maximum concurrent LLM API calls (semaphore-based)",
    )
    rate_limit_timeout: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Seconds a request may wait for a concurrency slot "
        "before failing with rate_limit_exceeded",
    )


class ObservabilityConfig(BaseSettings):
    """Observability and monitoring configuration."""

    model_config = SettingsConfigDict(
        env_prefix="OBSERVABILITY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    metrics_port: int = Field(
        default=9090, ge=1024, le=65535, description="Metrics HTTP server port"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(default="json", description="Log format")


class Settings(BaseSettings):
    """Main application settings aggregating all config sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Application environment"
    )
    databases_file: str | None = Field(
        default=None,
        description="Path to databases.json for multi-database configuration; "
        "when unset, the single DATABASE_* configuration is used",
    )

    # Nested configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create global settings instance.

    Returns:
        Settings: The global settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset global settings instance. Useful for testing."""
    global _settings
    _settings = None
