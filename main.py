from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import Repositories
from database.mongo import MongoConfig, MongoRuntime
from utils.embeds import install_branding_hooks

ROOT_DIR = Path(__file__).resolve().parent
COGS_DIR = ROOT_DIR / "cogs"

log = logging.getLogger("devilblox")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(slots=True)
class AppConfig:
    discord_token: str
    command_prefix: str
    sync_commands: bool
    mongo: MongoConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
        if not token:
            raise RuntimeError("DISCORD_TOKEN is required.")

        mongo = MongoConfig(
            uri=os.getenv("MONGO_URI"),
            db_name=os.getenv("MONGO_DB_NAME", "devilblox"),
            host=os.getenv("MONGO_HOST", "127.0.0.1"),
            port=int(os.getenv("MONGO_PORT", "27017")),
            user=os.getenv("MONGO_USER") or None,
            password=os.getenv("MONGO_PASSWORD") or None,
            auth_db=os.getenv("MONGO_AUTH_DB", "admin"),
            use_ssh=env_bool("MONGO_USE_SSH", False),
            ssh_host=os.getenv("SSH_HOST") or None,
            ssh_port=int(os.getenv("SSH_PORT", "22")),
            ssh_user=os.getenv("SSH_USER") or None,
            ssh_key=os.getenv("SSH_KEY") or None,
        )
        return cls(
            discord_token=token,
            command_prefix=os.getenv("COMMAND_PREFIX", "!"),
            sync_commands=env_bool("SYNC_COMMANDS", True),
            mongo=mongo,
        )


class DevilBloxBot(commands.Bot):
    def __init__(self, config: AppConfig):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = env_bool("MESSAGE_CONTENT_INTENT", False)

        super().__init__(
            command_prefix=config.command_prefix,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=True,
                everyone=False,
                replied_user=False,
            ),
        )
        self.config = config
        self.mongo_runtime = MongoRuntime(config.mongo)
        self.db = None
        self.repos: Repositories | None = None

    async def setup_hook(self):
        install_branding_hooks()
        self.db = await self.mongo_runtime.connect()
        self.repos = Repositories(self.db)
        await self.repos.ensure_indexes()

        for path in sorted(COGS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            extension = f"cogs.{path.stem}"
            try:
                await self.load_extension(extension)
                log.info("Loaded extension: %s", extension)
            except Exception:
                log.exception("Failed to load extension: %s", extension)

        if self.config.sync_commands:
            synced = await self.tree.sync()
            log.info("Synced %s application commands.", len(synced))

    async def close(self):
        await super().close()
        await self.mongo_runtime.close()

    async def on_ready(self):
        guilds = ", ".join(f"{guild.name}({guild.id})" for guild in self.guilds)
        log.info("Logged in as %s. Guilds: %s", self.user, guilds or "none")


async def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    config = AppConfig.from_env()
    bot = DevilBloxBot(config)
    async with bot:
        await bot.start(config.discord_token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
