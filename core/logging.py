from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install as install_rich_traceback


def configure_logging(
    *,
    console: Console,
    level: str,
    discord_level: str,
    log_file: str,
    log_max_bytes: int,
    log_backup_count: int,
) -> None:
    install_rich_traceback(console=console, show_locals=False)

    handlers: list[logging.Handler] = [
        RichHandler(
            console=console,
            rich_tracebacks=True,
            show_path=False,
            markup=False,
        ),
    ]
    try:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=log_max_bytes,
            backupCount=log_backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s [%(filename)s:%(lineno)d] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        handlers.append(file_handler)
    except OSError as exc:
        console.print(f"[yellow]File logging disabled:[/yellow] {exc}")

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=handlers,
        force=True,
    )

    logging.getLogger("discord").setLevel(discord_level)
    logging.getLogger("discord.http").setLevel(discord_level)
    logging.getLogger("motor").setLevel("WARNING")
    logging.getLogger("pymongo").setLevel("WARNING")
