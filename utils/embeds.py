from __future__ import annotations

from functools import lru_cache

import discord

from utils.assets import asset_path, has_asset

COLOR_ERROR = 0xE5484D
COLOR_SUCCESS = 0x2ECC71
COLOR_INFO = 0x5865F2
COLOR_DARK = 0x111111
BRAND_NAME = "DEVIL BLOX"
BRAND_LOGO_FOLDER = "logos"
BRAND_LOGO_FILENAME = "devilblox_icon.png"
BRAND_LOGO_URL = f"attachment://{BRAND_LOGO_FILENAME}"
_BRANDING_HOOKS_INSTALLED = False


def brand_embed(embed: discord.Embed) -> discord.Embed:
    embed.set_author(name=BRAND_NAME, icon_url=BRAND_LOGO_URL)
    return embed


@lru_cache(maxsize=1)
def _brand_logo_path() -> str | None:
    if not has_asset(BRAND_LOGO_FOLDER, BRAND_LOGO_FILENAME):
        return None
    return str(asset_path(BRAND_LOGO_FOLDER, BRAND_LOGO_FILENAME))


def brand_logo_file() -> discord.File | None:
    path = _brand_logo_path()
    if path is None:
        return None
    return discord.File(path, filename=BRAND_LOGO_FILENAME)


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

    if _payload_has_logo_file(kwargs):
        return kwargs

    logo = brand_logo_file()
    if logo is None:
        return kwargs

    if kwargs.get("files"):
        kwargs["files"] = [logo, *list(kwargs["files"])]
    elif kwargs.get("file") is not None:
        kwargs["files"] = [logo, kwargs.pop("file")]
    else:
        kwargs["file"] = logo
    return kwargs


def _componentize_send_payload(kwargs: dict) -> dict:
    if kwargs.get("embed") is None:
        return kwargs
    if kwargs.get("embeds") or kwargs.get("view") is not None:
        return kwargs

    from utils.components import embed_to_layout_view

    kwargs = dict(kwargs)
    embed = kwargs.pop("embed")
    content = kwargs.pop("content", None)
    kwargs["view"] = embed_to_layout_view(
        embed,
        include_brand_thumbnail=_payload_has_logo_file(kwargs),
        leading_content=content,
        attachment_filenames=_payload_attachment_filenames(kwargs),
    )
    return kwargs


def _componentize_edit_payload(
    kwargs: dict,
    *,
    has_brand_logo: bool,
    existing_attachments=None,
) -> dict:
    if kwargs.get("embed") is None:
        return kwargs
    if kwargs.get("embeds") or kwargs.get("view") is not None:
        return kwargs

    from utils.components import embed_to_layout_view

    kwargs = dict(kwargs)
    embed = brand_embed(kwargs.pop("embed"))
    content = kwargs.get("content")
    if "attachments" not in kwargs and existing_attachments is not None:
        kwargs["attachments"] = list(existing_attachments)
    kwargs["content"] = None
    kwargs["embeds"] = []
    kwargs["view"] = embed_to_layout_view(
        embed,
        include_brand_thumbnail=has_brand_logo,
        leading_content=content,
        attachment_filenames=_payload_attachment_filenames(kwargs),
    )
    return kwargs


def _attachments_have_logo(value) -> bool:
    return any(
        getattr(item, "filename", None) == BRAND_LOGO_FILENAME
        for item in value or ()
    )


def _payload_attachment_filenames(kwargs: dict) -> tuple[str, ...]:
    values = []
    if kwargs.get("file") is not None:
        values.append(kwargs["file"])
    values.extend(kwargs.get("files") or ())
    values.extend(kwargs.get("attachments") or ())
    return tuple(
        str(filename)
        for item in values
        if (filename := getattr(item, "filename", None))
    )


def install_branding_hooks():
    global _BRANDING_HOOKS_INSTALLED
    if _BRANDING_HOOKS_INSTALLED:
        return
    _BRANDING_HOOKS_INSTALLED = True

    original_messageable_send = discord.abc.Messageable.send
    original_response_send_message = discord.InteractionResponse.send_message
    original_response_edit_message = discord.InteractionResponse.edit_message
    original_webhook_send = discord.Webhook.send
    original_message_edit = discord.Message.edit
    original_partial_message_edit = discord.PartialMessage.edit
    original_interaction_message_edit = discord.InteractionMessage.edit
    original_webhook_message_edit = discord.WebhookMessage.edit

    async def branded_messageable_send(self, *args, **kwargs):
        payload = _componentize_send_payload(_brand_embed_payload(kwargs))
        return await original_messageable_send(self, *args, **payload)

    async def branded_response_send_message(self, *args, **kwargs):
        payload = _componentize_send_payload(_brand_embed_payload(kwargs))
        return await original_response_send_message(self, *args, **payload)

    async def branded_response_edit_message(self, *args, **kwargs):
        payload = _componentize_edit_payload(
            kwargs,
            has_brand_logo=_attachments_have_logo(kwargs.get("attachments")),
        )
        return await original_response_edit_message(self, *args, **payload)

    async def branded_webhook_send(self, *args, **kwargs):
        payload = _componentize_send_payload(_brand_embed_payload(kwargs))
        return await original_webhook_send(self, *args, **payload)

    async def branded_message_edit(self, *args, **kwargs):
        existing_attachments = getattr(self, "attachments", ())
        payload = _componentize_edit_payload(
            kwargs,
            has_brand_logo=_attachments_have_logo(
                kwargs.get("attachments", existing_attachments)
            ),
            existing_attachments=existing_attachments,
        )
        return await original_message_edit(self, *args, **payload)

    async def branded_partial_message_edit(self, *args, **kwargs):
        payload = _componentize_edit_payload(kwargs, has_brand_logo=False)
        return await original_partial_message_edit(self, *args, **payload)

    async def branded_interaction_message_edit(self, *args, **kwargs):
        existing_attachments = getattr(self, "attachments", ())
        payload = _componentize_edit_payload(
            kwargs,
            has_brand_logo=_attachments_have_logo(
                kwargs.get("attachments", existing_attachments)
            ),
            existing_attachments=existing_attachments,
        )
        return await original_interaction_message_edit(self, *args, **payload)

    async def branded_webhook_message_edit(self, *args, **kwargs):
        existing_attachments = getattr(self, "attachments", ())
        payload = _componentize_edit_payload(
            kwargs,
            has_brand_logo=_attachments_have_logo(
                kwargs.get("attachments", existing_attachments)
            ),
            existing_attachments=existing_attachments,
        )
        return await original_webhook_message_edit(self, *args, **payload)

    discord.abc.Messageable.send = branded_messageable_send
    discord.InteractionResponse.send_message = branded_response_send_message
    discord.InteractionResponse.edit_message = branded_response_edit_message
    discord.Webhook.send = branded_webhook_send
    discord.Message.edit = branded_message_edit
    discord.PartialMessage.edit = branded_partial_message_edit
    discord.InteractionMessage.edit = branded_interaction_message_edit
    discord.WebhookMessage.edit = branded_webhook_message_edit


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
    from utils.gifs import gif_file, gif_media_url

    media_url = gif_media_url(filename)
    if media_url is None:
        return embed_kwargs(embed)
    embed.set_image(url=media_url)
    files = branded_files(gif_file(filename))
    return {"embed": brand_embed(embed), "files": files} if files else {"embed": brand_embed(embed)}


def embed_banner_kwargs(embed: discord.Embed, filename: str) -> dict:
    if filename.casefold().endswith(".gif"):
        from utils.gifs import gif_file_from_folder, gif_media_url

        media_url = gif_media_url(filename)
        if media_url is None:
            return embed_kwargs(embed)
        embed.set_image(url=media_url)
        files = branded_files(gif_file_from_folder(filename, "banners"))
        return {"embed": brand_embed(embed), "files": files} if files else {"embed": brand_embed(embed)}
    return embed_asset_kwargs(embed, "banners", filename)
