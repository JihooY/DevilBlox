from __future__ import annotations

import discord

from utils.assets import asset_path, has_asset

COLOR_ERROR = 0xE5484D
COLOR_SUCCESS = 0x2ECC71
COLOR_INFO = 0x5865F2
COLOR_DARK = 0x111111
BRAND_NAME = "DEVIL BLOX"
BRAND_LOGO_FOLDER = "logos"
BRAND_LOGO_FILENAME = "devilblox_logo.png"
BRAND_LOGO_URL = f"attachment://{BRAND_LOGO_FILENAME}"
_BRANDING_HOOKS_INSTALLED = False


def brand_embed(embed: discord.Embed) -> discord.Embed:
    embed.set_author(name=BRAND_NAME, icon_url=BRAND_LOGO_URL)
    return embed


def brand_logo_file() -> discord.File | None:
    if not has_asset(BRAND_LOGO_FOLDER, BRAND_LOGO_FILENAME):
        return None
    return discord.File(str(asset_path(BRAND_LOGO_FOLDER, BRAND_LOGO_FILENAME)), filename=BRAND_LOGO_FILENAME)


def branded_files(*files: discord.File | None) -> list[discord.File]:
    result = []
    logo = brand_logo_file()
    if logo is not None:
        result.append(logo)
    result.extend(file for file in files if file is not None)
    return result


def embed_kwargs(embed: discord.Embed) -> dict:
    files = branded_files()
    if files:
        return {"embed": brand_embed(embed), "files": files}
    return {"embed": brand_embed(embed)}


def _has_embed_payload(kwargs: dict) -> bool:
    return kwargs.get("embed") is not None or bool(kwargs.get("embeds"))


def _payload_has_logo_file(kwargs: dict) -> bool:
    file = kwargs.get("file")
    if getattr(file, "filename", None) == BRAND_LOGO_FILENAME:
        return True
    return any(getattr(item, "filename", None) == BRAND_LOGO_FILENAME for item in kwargs.get("files") or ())


def _brand_embed_payload(kwargs: dict) -> dict:
    if not _has_embed_payload(kwargs):
        return kwargs

    kwargs = dict(kwargs)
    if kwargs.get("embed") is not None:
        kwargs["embed"] = brand_embed(kwargs["embed"])
    if kwargs.get("embeds"):
        kwargs["embeds"] = [brand_embed(embed) for embed in kwargs["embeds"]]

    logo = brand_logo_file()
    if logo is None or _payload_has_logo_file(kwargs):
        return kwargs

    if kwargs.get("files"):
        kwargs["files"] = [logo, *list(kwargs["files"])]
    elif kwargs.get("file") is not None:
        kwargs["files"] = [logo, kwargs.pop("file")]
    else:
        kwargs["file"] = logo
    return kwargs


def install_branding_hooks():
    global _BRANDING_HOOKS_INSTALLED
    if _BRANDING_HOOKS_INSTALLED:
        return
    _BRANDING_HOOKS_INSTALLED = True

    original_messageable_send = discord.abc.Messageable.send
    original_response_send_message = discord.InteractionResponse.send_message
    original_webhook_send = discord.Webhook.send

    async def branded_messageable_send(self, *args, **kwargs):
        return await original_messageable_send(self, *args, **_brand_embed_payload(kwargs))

    async def branded_response_send_message(self, *args, **kwargs):
        return await original_response_send_message(self, *args, **_brand_embed_payload(kwargs))

    async def branded_webhook_send(self, *args, **kwargs):
        return await original_webhook_send(self, *args, **_brand_embed_payload(kwargs))

    discord.abc.Messageable.send = branded_messageable_send
    discord.InteractionResponse.send_message = branded_response_send_message
    discord.Webhook.send = branded_webhook_send


def base_embed(title: str, description: str | None = None, color: int = COLOR_INFO) -> discord.Embed:
    return brand_embed(discord.Embed(title=title, description=description, color=color))


def success_embed(title: str = "완료", description: str | None = None) -> discord.Embed:
    return base_embed(title, description, COLOR_SUCCESS)


def error_embed(title: str = "오류", description: str | None = None) -> discord.Embed:
    return base_embed(title, description, COLOR_ERROR)


def info_embed(title: str, description: str | None = None) -> discord.Embed:
    return base_embed(title, description, COLOR_INFO)


def embed_asset_kwargs(embed: discord.Embed, folder: str, filename: str) -> dict:
    if not has_asset(folder, filename):
        return embed_kwargs(embed)
    embed.set_image(url=f"attachment://{filename}")
    file = discord.File(str(asset_path(folder, filename)), filename=filename)
    files = branded_files(file)
    if files:
        return {"embed": brand_embed(embed), "files": files}
    return {
        "embed": brand_embed(embed),
    }


def embed_gif_kwargs(embed: discord.Embed, filename: str) -> dict:
    return embed_asset_kwargs(embed, "gifs", filename)


def embed_banner_kwargs(embed: discord.Embed, filename: str) -> dict:
    return embed_asset_kwargs(embed, "banners", filename)
