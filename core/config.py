from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from database.mongo import MongoConfig


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    discord_token: str
    command_prefix: str
    sync_commands: bool
    message_content_intent: bool
    log_level: str
    discord_log_level: str
    cogs_package: str
    disabled_cogs: frozenset[str]
    mongo: MongoConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv(override=False)

        token = _optional_env("DISCORD_TOKEN") or _optional_env("TOKEN")
        if not token:
            raise ConfigError("DISCORD_TOKEN is required.")

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
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            discord_log_level=_env("DISCORD_LOG_LEVEL", "WARNING").upper(),
            cogs_package=_env("COGS_PACKAGE", "cogs"),
            disabled_cogs=frozenset(_csv_env("DISABLED_COGS")),
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


def _csv_env(key: str) -> list[str]:
    value = _optional_env(key)
    if value is None:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]

