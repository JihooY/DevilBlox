from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import discord
from discord.ext import commands
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from database import Repositories
from database.mongo import MongoRuntime
from utils.embeds import install_branding_hooks
from utils.gifs import configure_gif_delivery, gif_delivery_status

from .cog_loader import CogLoadReport, load_cogs
from .config import AppConfig
from .error_reporting import DiscordExceptionReporter, DiscordTracebackHandler

log = logging.getLogger("devilblox")


class DevilBloxBot(commands.Bot):
    def __init__(self, *, config: AppConfig, console: Console) -> None:
        configure_gif_delivery(
            mode=config.gif_delivery_mode,
            cdn_base_url=config.gif_cdn_base_url,
            rotation_enabled=config.gif_rotation_enabled,
            local_variant=config.gif_local_variant,
        )
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
        self._previous_loop_exception_handler = None
        self._loop_handler_installed = False
        self._devilblox_close_task: asyncio.Task[None] | None = None
        self.error_reporter = DiscordExceptionReporter(self)
        self._traceback_handler = DiscordTracebackHandler(self.error_reporter)
        logging.getLogger().addHandler(self._traceback_handler)
        self.tree.on_error = self._on_app_command_error

    async def setup_hook(self) -> None:
        loop = asyncio.get_running_loop()
        self._previous_loop_exception_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._handle_asyncio_exception)
        self._loop_handler_installed = True
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
        task = self._devilblox_close_task
        if task is None:
            task = asyncio.create_task(self._close_resources(), name="devilblox-close")
            self._devilblox_close_task = task
        await asyncio.shield(task)

    async def _close_resources(self) -> None:
        if self._loop_handler_installed:
            try:
                asyncio.get_running_loop().set_exception_handler(self._previous_loop_exception_handler)
            except Exception:
                log.exception("Failed to restore the previous asyncio exception handler")
            finally:
                self._loop_handler_installed = False
        logging.getLogger().removeHandler(self._traceback_handler)

        cancelled: asyncio.CancelledError | None = None
        stages = (
            ("Discord exception reporter", self.error_reporter.close),
            ("Discord client", self._close_discord_client),
            ("MongoDB runtime", self.mongo_runtime.close),
        )
        for label, closer in stages:
            try:
                await closer()
            except asyncio.CancelledError as exc:
                cancelled = exc
                log.warning("Cancellation interrupted %s cleanup; continuing shutdown", label)
            except Exception:
                log.exception("Failed to close %s", label)

        if cancelled is not None:
            raise cancelled

    async def _close_discord_client(self) -> None:
        await super().close()

    async def on_ready(self) -> None:
        self.error_reporter.start()
        if self._printed_ready_summary:
            log.info("Reconnected as %s.", self.user)
            return

        self._printed_ready_summary = True
        self._print_startup_summary()

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        error_type, error, error_traceback = sys.exc_info()
        if error is None:
            return
        guild_id = _event_guild_id(args, kwargs)
        error_id = self.error_reporter.enqueue_exception(
            f"Discord event: {event_method}",
            error,
            guild_id=guild_id,
        )
        log.error(
            "Unhandled Discord event exception: event=%s error_id=%s",
            event_method,
            error_id,
            exc_info=(error_type, error, error_traceback),
            extra={"skip_discord_report": True},
        )

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        original = getattr(error, "original", error)
        expected_errors = (
            discord.app_commands.CheckFailure,
            discord.app_commands.CommandOnCooldown,
            discord.app_commands.TransformerError,
        )
        if isinstance(error, expected_errors):
            await self._send_interaction_error(interaction, "이 명령을 실행할 수 없거나 입력값이 올바르지 않습니다.")
            return

        command_name = interaction.command.qualified_name if interaction.command else "unknown"
        context = (
            f"slash command={command_name} guild={interaction.guild_id} "
            f"channel={interaction.channel_id} user={interaction.user.id}"
        )
        error_id = self.error_reporter.enqueue_exception(
            context,
            original,
            guild_id=interaction.guild_id,
        )
        log.error(
            "Unhandled application command exception: command=%s guild_id=%s error_id=%s",
            command_name,
            interaction.guild_id,
            error_id,
            exc_info=(type(original), original, original.__traceback__),
            extra={"skip_discord_report": True},
        )
        await self._send_interaction_error(
            interaction,
            f"처리 중 오류가 발생했습니다. 관리자에게 오류 ID `{error_id}`를 전달해 주세요.",
        )

    async def on_command_error(self, context: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(
            error,
            (commands.CheckFailure, commands.CommandOnCooldown, commands.UserInputError),
        ):
            await context.send("이 명령을 실행할 수 없거나 입력값이 올바르지 않습니다.")
            return

        original = getattr(error, "original", error)
        command_name = context.command.qualified_name if context.command else "unknown"
        report_context = (
            f"prefix command={command_name} guild={getattr(context.guild, 'id', None)} "
            f"channel={getattr(context.channel, 'id', None)} user={context.author.id}"
        )
        guild_id = getattr(context.guild, "id", None)
        error_id = self.error_reporter.enqueue_exception(
            report_context,
            original,
            guild_id=guild_id,
        )
        log.error(
            "Unhandled prefix command exception: command=%s error_id=%s",
            command_name,
            error_id,
            exc_info=(type(original), original, original.__traceback__),
            extra={"skip_discord_report": True},
        )
        await context.send(f"처리 중 오류가 발생했습니다. 오류 ID: `{error_id}`")

    def _handle_asyncio_exception(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        error = context.get("exception")
        if not isinstance(error, BaseException):
            error = RuntimeError(str(context.get("message") or "Unhandled asyncio exception"))
        error_id = self.error_reporter.enqueue_exception(
            "asyncio event loop",
            error,
            guild_id=None,
        )
        log.error(
            "Unhandled asyncio exception: error_id=%s details=%s",
            error_id,
            context.get("message"),
            exc_info=(type(error), error, error.__traceback__),
            extra={"skip_discord_report": True},
        )

    async def _send_interaction_error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

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
        media = gif_delivery_status()
        summary.add_row("GIF delivery", media.effective_mode)
        summary.add_row("GIF rotation", _enabled_text(media.rotation_enabled))
        summary.add_row("Operations monitor", _enabled_text(self.config.operations.enabled))
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


def _event_guild_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    for value in (*args, *kwargs.values()):
        if isinstance(value, dict):
            guild_id = value.get("guild_id")
        else:
            guild_id = getattr(value, "guild_id", None)
        if isinstance(guild_id, int) and not isinstance(guild_id, bool) and guild_id > 0:
            return guild_id

        if isinstance(value, discord.Guild):
            return value.id
        guild = getattr(value, "guild", None)
        guild_id = getattr(guild, "id", None)
        if isinstance(guild_id, int) and not isinstance(guild_id, bool) and guild_id > 0:
            return guild_id
    return None
