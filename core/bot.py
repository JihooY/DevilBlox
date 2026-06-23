from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from database import Repositories
from database.mongo import MongoRuntime
from utils.embeds import install_branding_hooks

from .cog_loader import CogLoadReport, load_cogs
from .config import AppConfig

log = logging.getLogger("devilblox")


class DevilBloxBot(commands.Bot):
    def __init__(self, *, config: AppConfig, console: Console) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = config.message_content_intent

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
        self.console = console
        self.mongo_runtime = MongoRuntime(config.mongo)
        self.db: Any | None = None
        self.repos: Repositories | None = None
        self.cog_report = CogLoadReport()
        self.synced_command_count = 0
        self._printed_ready_summary = False

    async def setup_hook(self) -> None:
        install_branding_hooks()
        self.db = await self.mongo_runtime.connect()
        self.repos = Repositories(self.db)
        await self.repos.ensure_indexes()

        self.cog_report = await load_cogs(
            self,
            package_name=self.config.cogs_package,
            disabled_cogs=self.config.disabled_cogs,
        )
        self._log_cog_report()

        if self.config.sync_commands:
            synced = await self.tree.sync()
            self.synced_command_count = len(synced)
            log.info("Synced %s application commands.", self.synced_command_count)

    async def close(self) -> None:
        await super().close()
        await self.mongo_runtime.close()

    async def on_ready(self) -> None:
        if self._printed_ready_summary:
            log.info("Reconnected as %s.", self.user)
            return

        self._printed_ready_summary = True
        self._print_startup_summary()

    def _log_cog_report(self) -> None:
        for extension in self.cog_report.loaded:
            log.info("Loaded extension: %s", extension)
        for extension in self.cog_report.skipped:
            log.info("Skipped disabled extension: %s", extension)
        for failure in self.cog_report.failed:
            log.error(
                "Failed to load extension: %s",
                failure.extension,
                exc_info=(
                    type(failure.error),
                    failure.error,
                    failure.error.__traceback__,
                ),
            )

    def _print_startup_summary(self) -> None:
        guilds = sorted(self.guilds, key=lambda guild: guild.name.lower())

        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="bold cyan", no_wrap=True)
        summary.add_column()
        summary.add_row("Bot", _bot_label(self.user))
        summary.add_row("MongoDB", f"database={self.config.mongo.db_name}")
        summary.add_row("Command prefix", self.config.command_prefix)
        summary.add_row("Message content", _enabled_text(self.config.message_content_intent))
        summary.add_row(
            "Slash commands",
            _sync_text(self.config.sync_commands, self.synced_command_count),
        )
        summary.add_row("Cogs", _cog_text(self.cog_report))
        summary.add_row("Guilds", str(len(guilds)))

        self.console.print(Panel(summary, title="DevilBlox Online", border_style="cyan"))

        if guilds:
            guild_table = Table(title="Connected Guilds", show_lines=False)
            guild_table.add_column("#", justify="right", style="dim", no_wrap=True)
            guild_table.add_column("Name", overflow="fold")
            guild_table.add_column("Guild ID", no_wrap=True)
            guild_table.add_column("Members", justify="right", no_wrap=True)

            for index, guild in enumerate(guilds, start=1):
                guild_table.add_row(
                    str(index),
                    guild.name,
                    str(guild.id),
                    str(guild.member_count or "unknown"),
                )
            self.console.print(guild_table)
        else:
            self.console.print("[yellow]No connected guilds yet.[/yellow]")

        if self.cog_report.loaded:
            self.console.print(
                "[dim]Loaded cogs: " + ", ".join(self.cog_report.loaded) + "[/dim]",
            )


def _bot_label(user: discord.ClientUser | None) -> str:
    if user is None:
        return "unknown"
    return f"{user} ({user.id})"


def _enabled_text(enabled: bool) -> str:
    return "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"


def _sync_text(enabled: bool, synced_count: int) -> str:
    if not enabled:
        return "[dim]disabled[/dim]"
    return f"{synced_count} synced"


def _cog_text(report: CogLoadReport) -> str:
    parts = [f"{len(report.loaded)} loaded"]
    if report.skipped:
        parts.append(f"{len(report.skipped)} skipped")
    if report.failed:
        parts.append(f"[red]{len(report.failed)} failed[/red]")
    return ", ".join(parts)
