from __future__ import annotations

import math
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

from database.mongo import MongoConfig


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class OperationsConfig:
    enabled: bool
    channel_id: int | None
    sample_interval: float
    panel_interval: float
    auxiliary_interval: float
    cpu_critical_percent: float
    memory_critical_percent: float
    disk_critical_percent: float
    gpu_critical_percent: float
    network_download_critical_mbps: float
    network_upload_critical_mbps: float
    latency_critical_ms: float
    trigger_samples: int
    recovery_samples: int
    recovery_ratio: float
    mitigation_cooldown_seconds: float
    alert_cooldown_seconds: float
    probe_enabled: bool
    probe_host: str | None
    probe_port: int
    probe_timeout: float
    network_interface: str | None = None

    @classmethod
    def from_env(cls) -> "OperationsConfig":
        config = cls(
            enabled=_bool_env("OPERATIONS_MONITOR_ENABLED", True),
            channel_id=_optional_int_env("OPERATIONS_CHANNEL_ID"),
            sample_interval=_float_env("MONITOR_SAMPLE_INTERVAL", 5.0),
            panel_interval=_float_env("MONITOR_PANEL_INTERVAL", 30.0),
            auxiliary_interval=_float_env("MONITOR_AUXILIARY_INTERVAL", 30.0),
            cpu_critical_percent=_float_env("MONITOR_CPU_CRITICAL_PERCENT", 90.0),
            memory_critical_percent=_float_env("MONITOR_MEMORY_CRITICAL_PERCENT", 90.0),
            disk_critical_percent=_float_env("MONITOR_DISK_CRITICAL_PERCENT", 95.0),
            gpu_critical_percent=_float_env("MONITOR_GPU_CRITICAL_PERCENT", 95.0),
            network_download_critical_mbps=_float_env(
                "MONITOR_NETWORK_DOWNLOAD_CRITICAL_MBPS",
                100.0,
            ),
            network_upload_critical_mbps=_float_env(
                "MONITOR_NETWORK_UPLOAD_CRITICAL_MBPS",
                100.0,
            ),
            latency_critical_ms=_float_env("MONITOR_LATENCY_CRITICAL_MS", 1_000.0),
            trigger_samples=_int_env("MONITOR_TRIGGER_SAMPLES", 3),
            recovery_samples=_int_env("MONITOR_RECOVERY_SAMPLES", 6),
            recovery_ratio=_float_env("MONITOR_RECOVERY_RATIO", 0.70),
            mitigation_cooldown_seconds=_float_env(
                "MONITOR_MITIGATION_COOLDOWN_SECONDS",
                300.0,
            ),
            alert_cooldown_seconds=_float_env("MONITOR_ALERT_COOLDOWN_SECONDS", 300.0),
            probe_enabled=_bool_env("MONITOR_PROBE_ENABLED", True),
            probe_host=(
                _optional_env("MONITOR_PROBE_HOST") or "discord.com"
                if _bool_env("MONITOR_PROBE_ENABLED", True)
                else None
            ),
            probe_port=_int_env("MONITOR_PROBE_PORT", 443),
            probe_timeout=_float_env("MONITOR_PROBE_TIMEOUT", 3.0),
            network_interface=_optional_env("MONITOR_NETWORK_INTERFACE"),
        )
        _validate_operations_config(config)
        return config


@dataclass(frozen=True, slots=True)
class AppConfig:
    discord_token: str
    command_prefix: str
    sync_commands: bool
    message_content_intent: bool
    log_level: str
    discord_log_level: str
    log_file: str
    log_max_bytes: int
    log_backup_count: int
    cogs_package: str
    disabled_cogs: frozenset[str]
    gif_delivery_mode: str
    gif_cdn_base_url: str | None
    gif_rotation_enabled: bool
    gif_local_variant: str
    gif_recovery_upload_interval: float
    operations: OperationsConfig
    mongo: MongoConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv(override=False)

        token = _optional_env("DISCORD_TOKEN") or _optional_env("TOKEN")
        if not token:
            raise ConfigError("DISCORD_TOKEN is required.")

        gif_delivery_mode = _env("GIF_DELIVERY_MODE", "auto").casefold()
        if gif_delivery_mode not in {"auto", "local", "cdn"}:
            raise ConfigError("GIF_DELIVERY_MODE must be one of: auto, local, cdn.")
        gif_cdn_base_url = _optional_env("GIF_CDN_BASE_URL")
        if gif_delivery_mode == "cdn" and not gif_cdn_base_url:
            raise ConfigError("GIF_CDN_BASE_URL is required when GIF_DELIVERY_MODE=cdn.")
        if gif_cdn_base_url:
            parsed_cdn_url = urlparse(gif_cdn_base_url)
            if parsed_cdn_url.scheme not in {"http", "https"} or not parsed_cdn_url.netloc:
                raise ConfigError("GIF_CDN_BASE_URL must be an absolute HTTP(S) URL.")
        gif_local_variant = _env("GIF_LOCAL_VARIANT", "original").casefold()
        if gif_local_variant not in {"original", "optimized"}:
            raise ConfigError("GIF_LOCAL_VARIANT must be one of: original, optimized.")
        log_level = _env("LOG_LEVEL", "INFO").upper()
        discord_log_level = _env("DISCORD_LOG_LEVEL", "WARNING").upper()
        valid_log_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if log_level not in valid_log_levels:
            raise ConfigError("LOG_LEVEL is not a valid Python logging level.")
        if discord_log_level not in valid_log_levels:
            raise ConfigError("DISCORD_LOG_LEVEL is not a valid Python logging level.")
        log_max_bytes = _int_env("LOG_MAX_BYTES", 5 * 1024 * 1024)
        log_backup_count = _int_env("LOG_BACKUP_COUNT", 5)
        if log_max_bytes <= 0:
            raise ConfigError("LOG_MAX_BYTES must be greater than zero.")
        if log_backup_count < 1:
            raise ConfigError("LOG_BACKUP_COUNT must be at least 1.")
        gif_recovery_upload_interval = _float_env("GIF_RECOVERY_UPLOAD_INTERVAL", 5.0)
        if not math.isfinite(gif_recovery_upload_interval) or gif_recovery_upload_interval < 0:
            raise ConfigError("GIF_RECOVERY_UPLOAD_INTERVAL must be zero or greater.")

        mongo = MongoConfig(
            uri=_optional_env("MONGO_URI"),
            db_name=_env("MONGO_DB_NAME", "devilblox"),
            host=_env("MONGO_HOST", "127.0.0.1"),
            port=_int_env("MONGO_PORT", 27017),
            user=_optional_env("MONGO_USER"),
            password=_optional_env("MONGO_PASSWORD"),
            auth_db=_env("MONGO_AUTH_DB", "admin"),
            use_ssh=_bool_env("MONGO_USE_SSH", False),
            ssh_host=_optional_env("SSH_HOST"),
            ssh_port=_int_env("SSH_PORT", 22),
            ssh_user=_optional_env("SSH_USER"),
            ssh_key=_optional_env("SSH_KEY"),
        )

        return cls(
            discord_token=token,
            command_prefix=_env("COMMAND_PREFIX", "!"),
            sync_commands=_bool_env("SYNC_COMMANDS", True),
            message_content_intent=_bool_env(
                "DISCORD_MESSAGE_CONTENT_INTENT",
                _bool_env("MESSAGE_CONTENT_INTENT", False),
            ),
            log_level=log_level,
            discord_log_level=discord_log_level,
            log_file=_env("LOG_FILE", "logs/devilblox.log"),
            log_max_bytes=log_max_bytes,
            log_backup_count=log_backup_count,
            cogs_package=_env("COGS_PACKAGE", "cogs"),
            disabled_cogs=frozenset(_csv_env("DISABLED_COGS")),
            gif_delivery_mode=gif_delivery_mode,
            gif_cdn_base_url=gif_cdn_base_url,
            gif_rotation_enabled=_bool_env("GIF_ROTATION_ENABLED", False),
            gif_local_variant=gif_local_variant,
            gif_recovery_upload_interval=gif_recovery_upload_interval,
            operations=OperationsConfig.from_env(),
            mongo=mongo,
        )


def _optional_env(key: str) -> str | None:
    value = os.getenv(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env(key: str, default: str) -> str:
    return _optional_env(key) or default


def _bool_env(key: str, default: bool) -> bool:
    value = _optional_env(key)
    if value is None:
        return default

    normalized = value.lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean value.")


def _int_env(key: str, default: int) -> int:
    value = _optional_env(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer.") from exc


def _optional_int_env(key: str) -> int | None:
    value = _optional_env(key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer.") from exc


def _float_env(key: str, default: float) -> float:
    value = _optional_env(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number.") from exc


def _csv_env(key: str) -> list[str]:
    value = _optional_env(key)
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _validate_operations_config(config: OperationsConfig) -> None:
    positive_values = {
        "MONITOR_SAMPLE_INTERVAL": config.sample_interval,
        "MONITOR_PANEL_INTERVAL": config.panel_interval,
        "MONITOR_AUXILIARY_INTERVAL": config.auxiliary_interval,
        "MONITOR_TRIGGER_SAMPLES": config.trigger_samples,
        "MONITOR_RECOVERY_SAMPLES": config.recovery_samples,
        "MONITOR_MITIGATION_COOLDOWN_SECONDS": config.mitigation_cooldown_seconds,
        "MONITOR_ALERT_COOLDOWN_SECONDS": config.alert_cooldown_seconds,
        "MONITOR_PROBE_PORT": config.probe_port,
        "MONITOR_PROBE_TIMEOUT": config.probe_timeout,
        "MONITOR_NETWORK_DOWNLOAD_CRITICAL_MBPS": config.network_download_critical_mbps,
        "MONITOR_NETWORK_UPLOAD_CRITICAL_MBPS": config.network_upload_critical_mbps,
    }
    for key, value in positive_values.items():
        if not math.isfinite(value) or value <= 0:
            raise ConfigError(f"{key} must be greater than zero.")

    if config.sample_interval < 1:
        raise ConfigError("MONITOR_SAMPLE_INTERVAL must be at least 1 second.")
    if config.panel_interval < 10:
        raise ConfigError("MONITOR_PANEL_INTERVAL must be at least 10 seconds.")
    if config.auxiliary_interval < 5:
        raise ConfigError("MONITOR_AUXILIARY_INTERVAL must be at least 5 seconds.")

    percent_values = {
        "MONITOR_CPU_CRITICAL_PERCENT": config.cpu_critical_percent,
        "MONITOR_MEMORY_CRITICAL_PERCENT": config.memory_critical_percent,
        "MONITOR_DISK_CRITICAL_PERCENT": config.disk_critical_percent,
        "MONITOR_GPU_CRITICAL_PERCENT": config.gpu_critical_percent,
    }
    for key, value in percent_values.items():
        if not math.isfinite(value) or not 0 < value <= 100:
            raise ConfigError(f"{key} must be greater than 0 and at most 100.")

    scalar_thresholds = {
        "MONITOR_LATENCY_CRITICAL_MS": config.latency_critical_ms,
        "MONITOR_NETWORK_DOWNLOAD_CRITICAL_MBPS": config.network_download_critical_mbps,
        "MONITOR_NETWORK_UPLOAD_CRITICAL_MBPS": config.network_upload_critical_mbps,
    }
    for key, value in scalar_thresholds.items():
        if not math.isfinite(value) or value <= 0:
            raise ConfigError(f"{key} must be greater than zero.")

    if not math.isfinite(config.recovery_ratio) or not 0 < config.recovery_ratio < 1:
        raise ConfigError("MONITOR_RECOVERY_RATIO must be greater than 0 and less than 1.")
    if not 1 <= config.probe_port <= 65535:
        raise ConfigError("MONITOR_PROBE_PORT must be between 1 and 65535.")
    if config.channel_id is not None and config.channel_id <= 0:
        raise ConfigError("OPERATIONS_CHANNEL_ID must be a positive Discord snowflake.")
