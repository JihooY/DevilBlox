from __future__ import annotations

import discord

from utils.assets import asset_path, has_asset

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


def embed_asset_kwargs(embed: discord.Embed, folder: str, filename: str) -> dict:
    if not has_asset(folder, filename):
        return {"embed": embed}
    embed.set_image(url=f"attachment://{filename}")
    return {
        "embed": embed,
        "file": discord.File(str(asset_path(folder, filename)), filename=filename),
    }


def embed_gif_kwargs(embed: discord.Embed, filename: str) -> dict:
    return embed_asset_kwargs(embed, "gifs", filename)


def embed_banner_kwargs(embed: discord.Embed, filename: str) -> dict:
    return embed_asset_kwargs(embed, "banners", filename)
