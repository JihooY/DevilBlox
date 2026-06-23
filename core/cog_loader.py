from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field

from discord.ext import commands


@dataclass(slots=True)
class CogLoadFailure:
    extension: str
    error: BaseException


@dataclass(slots=True)
class CogLoadReport:
    loaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[CogLoadFailure] = field(default_factory=list)


async def load_cogs(
    bot: commands.Bot,
    *,
    package_name: str,
    disabled_cogs: set[str] | frozenset[str],
) -> CogLoadReport:
    report = CogLoadReport()

    for extension in discover_extensions(package_name):
        short_name = extension.rsplit(".", 1)[-1]
        if short_name in disabled_cogs or extension in disabled_cogs:
            report.skipped.append(extension)
            continue

        try:
            await bot.load_extension(extension)
        except Exception as exc:
            report.failed.append(CogLoadFailure(extension=extension, error=exc))
        else:
            report.loaded.append(extension)

    return report


def discover_extensions(package_name: str) -> list[str]:
    package = importlib.import_module(package_name)
    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return []

    return sorted(
        module.name
        for module in pkgutil.iter_modules(package_paths, f"{package_name}.")
        if not module.ispkg and not module.name.rsplit(".", 1)[-1].startswith("_")
    )

