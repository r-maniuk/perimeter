"""Runtime configuration.

Every setting is read from the environment, grouped by prefix (``DATABASE_``, ``NATS_``, ...).
Secrets can also be provided as files: for any field, ``<PREFIX><FIELD>_FILE`` names a file whose
content becomes the value. This is how the compose stack delivers credentials, so they never
appear in ``docker inspect`` output or in the process environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class FileSecretsSource(PydanticBaseSettingsSource):
    """Resolve ``<ENV_NAME>_FILE`` variables into field values.

    Setting both ``X`` and ``X_FILE`` is rejected: silently preferring one of them is exactly the
    kind of ambiguity that ends with a service using a stale credential.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        prefix = str(self.config.get("env_prefix", ""))
        env_name = f"{prefix}{field_name}".upper()
        file_var = f"{env_name}_FILE"
        path = _getenv_ci(file_var)
        if path is None:
            return None, field_name, False
        if _getenv_ci(env_name) is not None:
            msg = f"set either {env_name} or {file_var}, not both"
            raise ValueError(msg)
        try:
            value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            msg = f"{file_var} points to {path!r}, which cannot be read: {exc.strerror}"
            raise ValueError(msg) from exc
        return value, field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name, field in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field, field_name)
            if value is not None:
                values[key] = value
        return values


def _getenv_ci(name: str) -> str | None:
    value = os.environ.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in os.environ.items():
        if key.lower() == lowered:
            return candidate
    return None


class _Group(BaseSettings):
    """Base for one settings group: environment first, then ``*_FILE`` secrets, then defaults."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, FileSecretsSource(settings_cls))


class DatabaseSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    host: str = "db"
    port: int = 5432
    name: str = "perimeter"
    user: str = "perimeter"
    password: SecretStr = SecretStr("")
    pool_size: int = Field(default=10, ge=1, le=200)
    pool_timeout_s: float = Field(default=2.0, gt=0)
    statement_timeout_ms: int = Field(default=5_000, ge=100)
    ssl: bool = False


class NatsSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="NATS_")

    url: str = "nats://nats:4222"
    user: str | None = None
    password: SecretStr | None = None
    connect_timeout_s: float = Field(default=5.0, gt=0)
    request_timeout_s: float = Field(default=5.0, gt=0)


class SecuritySettings(_Group):
    model_config = SettingsConfigDict(env_prefix="")

    session_secret: SecretStr = SecretStr("")
    session_ttl_s: int = Field(default=43_200, ge=60)
    ingest_token: SecretStr = SecretStr("")
    allowed_origins: str = "http://localhost:8080"
    login_rate_per_minute: int = Field(default=20, ge=1)
    secure_cookies: bool = False

    @property
    def origins(self) -> frozenset[str]:
        parts = (origin.strip().rstrip("/") for origin in self.allowed_origins.split(","))
        return frozenset(origin for origin in parts if origin)


class TelemetrySettings(_Group):
    model_config = SettingsConfigDict(env_prefix="TELEMETRY_")

    partitions: int = Field(default=16, ge=1, le=1024)
    max_age_s: int = Field(default=7_200, ge=60)
    max_bytes: int = Field(default=2 * 1024**3, ge=1024**2)
    dedup_window_s: int = Field(default=30, ge=1)


class IngestSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="INGEST_")

    max_batch: int = Field(default=1_000, ge=1, le=10_000)
    max_body_bytes: int = Field(default=1024**2, ge=1024)
    max_inflight: int = Field(default=20_000, ge=1)
    max_skew_s: float = Field(default=30.0, ge=0)
    admission_high: int = Field(default=150_000, ge=1)
    admission_low: int = Field(default=50_000, ge=0)
    admission_sample_ms: int = Field(default=500, ge=50)
    ws_initial_credit: int = Field(default=2_000, ge=1)


class EngineSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="ENGINE_")

    batch_max: int = Field(default=1_000, ge=1, le=10_000)
    fetch_wait_s: float = Field(default=1.0, gt=0)
    linger_ms: int = Field(default=25, ge=0, le=1_000)
    lease_ttl_s: float = Field(default=6.0, ge=1)
    metrics_port: int = 9102
    instance_id: str | None = None


class LiveSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="LIVE_")

    tile_zoom: int = Field(default=12, ge=1, le=16)
    tile_flush_ms: int = Field(default=100, ge=10)
    send_timeout_s: float = Field(default=5.0, gt=0)
    event_queue_max: int = Field(default=1_024, ge=16)
    position_budget_bytes: int = Field(default=1024**2, ge=4096)
    max_sessions_per_user: int = Field(default=16, ge=1)
    max_viewport_tiles: int = Field(default=16, ge=1, le=256)
    device_stale_s: int = Field(default=600, ge=10)
    snapshot_cache_s: float = Field(default=1.0, ge=0)


class ObservabilitySettings(_Group):
    model_config = SettingsConfigDict(env_prefix="")

    perimeter_env: Literal["production", "development", "test"] = "production"
    log_level: str = "INFO"
    otel_exporter_otlp_endpoint: str | None = None
    otel_sample_ratio: float = Field(default=0.01, ge=0, le=1)


class Settings:
    """All configuration groups of one process. Construct with :func:`load_settings`."""

    __slots__ = (
        "database",
        "engine",
        "ingest",
        "live",
        "nats",
        "observability",
        "security",
        "telemetry",
    )

    def __init__(
        self,
        *,
        database: DatabaseSettings,
        nats: NatsSettings,
        security: SecuritySettings,
        telemetry: TelemetrySettings,
        ingest: IngestSettings,
        engine: EngineSettings,
        live: LiveSettings,
        observability: ObservabilitySettings,
    ) -> None:
        self.database = database
        self.nats = nats
        self.security = security
        self.telemetry = telemetry
        self.ingest = ingest
        self.engine = engine
        self.live = live
        self.observability = observability

    @property
    def is_development(self) -> bool:
        return self.observability.perimeter_env == "development"


def load_settings(**overrides: Any) -> Settings:
    """Read every group from the environment; ``overrides`` replaces whole groups (tests)."""
    groups: dict[str, Any] = {
        "database": DatabaseSettings,
        "nats": NatsSettings,
        "security": SecuritySettings,
        "telemetry": TelemetrySettings,
        "ingest": IngestSettings,
        "engine": EngineSettings,
        "live": LiveSettings,
        "observability": ObservabilitySettings,
    }
    built = {name: overrides.get(name) or factory() for name, factory in groups.items()}
    return Settings(**built)
