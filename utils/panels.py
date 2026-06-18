from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


async def save_panel_location(
    repos,
    guild_id: int,
    channel_key: str,
    meta_key: str,
    channel_id: int,
    message_id: int,
):
    await repos.settings.set_value(guild_id, "channels", channel_key, channel_id)
    await repos.settings.set_value(guild_id, "meta", meta_key, message_id)


async def restore_panel_message(
    repos,
    guild: discord.Guild,
    channel_key: str,
    meta_key: str,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    image_attachment_filename: str | None = None,
) -> bool:
    try:
        settings = await repos.settings.get(guild.id)
    except Exception:
        log.exception("Failed to read panel settings: guild_id=%s meta_key=%s", guild.id, meta_key)
        return False

    channel_id = settings["channels"].get(channel_key)
    message_id = settings["meta"].get(meta_key)
    if not channel_id or not message_id:
        return False

    channel = guild.get_channel(channel_id)
    if channel is None:
        return False
    if not hasattr(channel, "fetch_message"):
        return False

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        await repos.settings.set_value(guild.id, "meta", meta_key, None)
        return False
    except discord.HTTPException:
        return False
    except Exception:
        log.exception("Failed to fetch panel message: guild_id=%s message_id=%s", guild.id, message_id)
        return False

    update = {}
    if embed is not None:
        if image_attachment_filename and any(
            attachment.filename == image_attachment_filename for attachment in message.attachments
        ):
            embed.set_image(url=f"attachment://{image_attachment_filename}")
        update["embed"] = embed
    if view is not None:
        update["view"] = view
    if not update:
        return True

    try:
        await message.edit(**update)
    except discord.NotFound:
        await repos.settings.set_value(guild.id, "meta", meta_key, None)
        return False
    except discord.HTTPException:
        return False
    except Exception:
        log.exception("Failed to restore panel message: guild_id=%s message_id=%s", guild.id, message_id)
        return False
    return True
