from __future__ import annotations

import asyncio

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .bot import DevilBloxBot
from .config import AppConfig, ConfigError
from .logging import configure_logging


async def async_main() -> None:
    console = Console()
    bot: DevilBloxBot | None = None

    try:
        config = AppConfig.from_env()
        configure_logging(
            console=console,
            level=config.log_level,
            discord_level=config.discord_log_level,
        )
        _print_runtime_plan(console, config)

        bot = DevilBloxBot(config=config, console=console)
        async with bot:
            await bot.start(config.discord_token)
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
    finally:
        if bot is not None and not bot.is_closed():
            await bot.close()


def main() -> None:
    console = Console()
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        console.print("[yellow]Ctrl+C detected. Shutdown complete.[/yellow]")
    except asyncio.CancelledError:
        console.print("[yellow]Shutdown cancelled pending tasks. Shutdown complete.[/yellow]")


def _print_runtime_plan(console: Console, config: AppConfig) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    table.add_row("Discord bot", "[green]enabled[/green]")
    table.add_row("Command sync", _enabled_text(config.sync_commands))
    table.add_row("Cogs package", config.cogs_package)
    table.add_row("Disabled cogs", ", ".join(config.disabled_cogs) or "[dim]none[/dim]")
    table.add_row("MongoDB", config.mongo.db_name)
    table.add_row("SSH tunnel", _enabled_text(config.mongo.use_ssh))
    table.add_row("Log level", config.log_level)

    console.print(Panel(table, title="DevilBlox Runtime", border_style="cyan"))


def _enabled_text(enabled: bool) -> str:
    return "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"

