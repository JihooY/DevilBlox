from __future__ import annotations

import discord

COLOR_ERROR = 0xE5484D
COLOR_SUCCESS = 0x2ECC71
COLOR_INFO = 0x5865F2
COLOR_DARK = 0x111111


def base_embed(title: str, description: str | None = None, color: int = COLOR_INFO) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def success_embed(title: str = "완료", description: str | None = None) -> discord.Embed:
    return base_embed(title, description, COLOR_SUCCESS)


def error_embed(title: str = "오류", description: str | None = None) -> discord.Embed:
    return base_embed(title, description, COLOR_ERROR)


def info_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title, description, COLOR_INFO)
