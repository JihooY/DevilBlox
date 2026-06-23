from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install as install_rich_traceback


def configure_logging(
    *,
    console: Console,
    level: str,
    discord_level: str,
) -> None:
    install_rich_traceback(console=console, show_locals=False)

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console,
                rich_tracebacks=True,
                show_path=False,
                markup=False,
            ),
        ],
        force=True,
    )

    logging.getLogger("discord").setLevel(discord_level)
    logging.getLogger("discord.http").setLevel(discord_level)
    logging.getLogger("motor").setLevel("WARNING")
    logging.getLogger("pymongo").setLevel("WARNING")
