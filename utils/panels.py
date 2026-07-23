from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from utils.embeds import BRAND_LOGO_FILENAME, brand_embed, branded_files
from utils.gifs import (
    GifPool,
    choose_gif,
    gif_delivery_status,
    gif_file,
    gif_media_url,
    is_gif_filename,
    is_gif_media_url,
    message_media_urls,
    retained_non_gif_attachments,
)

log = logging.getLogger(__name__)

PANEL_LOCATIONS = (
    ("account", "account_panel_message_id"),
    ("alarm", "alarm_panel_message_id"),
    ("archive", "archive_panel_message_id"),
    ("middleman", "middleman_panel_message_id"),
    ("purchase", "purchase_panel_message_id"),
    ("stock_condition", "stock_condition_message_id"),
    ("stock_control", "stock_control_message_id"),
    ("support", "support_panel_message_id"),
    ("ticket_condition", "ticket_condition_message_id"),
    ("vending", "vending_panel_message_id"),
    ("verify", "verify_panel_message_id"),
)


@dataclass(slots=True)
class PanelCleanupResult:
    checked: int = 0
    changed: int = 0
    gif_attachments_removed: int = 0
    media_references_removed: int = 0
    failed: int = 0


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
    await repos.settings.register_panel(
        guild_id,
        channel_key=channel_key,
        meta_key=meta_key,
        channel_id=channel_id,
        message_id=message_id,
    )


async def restore_panel_message(
    repos,
    guild: discord.Guild,
    channel_key: str,
    meta_key: str,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    image_attachment_filename: GifPool | None = None,
    rotate_image: bool = False,
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
        image_filename = choose_gif(
            image_attachment_filename,
            message.attachments,
            force_new=rotate_image,
            existing_urls=message_media_urls(message),
        )
        if image_filename:
            embed.set_image(url=gif_media_url(image_filename))
            local_mode = gif_delivery_status().effective_mode == "local"
            needs_local_file = local_mode and not any(
                attachment.filename == image_filename for attachment in message.attachments
            )
            needs_logo = not any(
                attachment.filename == BRAND_LOGO_FILENAME for attachment in message.attachments
            )
            stale_local_gif = not local_mode and any(
                is_gif_filename(attachment.filename) for attachment in message.attachments
            )
            if needs_local_file:
                file = gif_file(image_filename)
                retained = retained_non_gif_attachments(message)
                if needs_logo:
                    attachments = [*branded_files(file), *retained]
                else:
                    attachments = [*retained, *([file] if file is not None else [])]
                if attachments:
                    update["attachments"] = attachments
            elif needs_logo:
                update["attachments"] = [*branded_files(), *message.attachments]
            elif stale_local_gif:
                update["attachments"] = retained_non_gif_attachments(message)
        else:
            embed.set_image(url=None)
            if any(is_gif_filename(attachment.filename) for attachment in message.attachments):
                update["attachments"] = retained_non_gif_attachments(message)
        update["embed"] = brand_embed(embed)
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


async def strip_saved_panel_gifs(
    repos,
    guild: discord.Guild,
    *,
    layout_renderer: Callable[
        [str | None, discord.Message],
        Awaitable[discord.ui.LayoutView | None],
    ]
    | None = None,
) -> PanelCleanupResult:
    """Remove GIF references/attachments from every currently tracked persistent panel."""

    result = PanelCleanupResult()
    try:
        settings = await repos.settings.get(guild.id)
    except Exception:
        log.exception("Failed to read panel settings for GIF cleanup: guild_id=%s", guild.id)
        result.failed += 1
        return result

    panel_records = list(settings["meta"].get("active_panels") or ())
    panel_records.extend(
        {
            "channel_key": channel_key,
            "meta_key": meta_key,
            "channel_id": settings["channels"].get(channel_key),
            "message_id": settings["meta"].get(meta_key),
        }
        for channel_key, meta_key in PANEL_LOCATIONS
    )

    seen_messages: set[int] = set()
    for panel in panel_records:
        channel_key = panel.get("channel_key")
        meta_key = panel.get("meta_key")
        channel_id = panel.get("channel_id") or settings["channels"].get(channel_key)
        message_id = panel.get("message_id")
        if not channel_id or not message_id or message_id in seen_messages:
            continue
        seen_messages.add(message_id)
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            result.failed += 1
            continue

        result.checked += 1
        try:
            message = await channel.fetch_message(message_id)
            replacement_view = (
                await layout_renderer(channel_key, message) if layout_renderer is not None else None
            )
            removed_attachments, removed_references = await strip_message_gifs(
                message,
                replacement_view=replacement_view,
            )
        except discord.NotFound:
            if meta_key and settings["meta"].get(meta_key) == message_id:
                await repos.settings.set_value(guild.id, "meta", meta_key, None)
            await repos.settings.unregister_panel(guild.id, message_id)
            continue
        except discord.HTTPException:
            result.failed += 1
            continue
        except Exception:
            result.failed += 1
            log.exception(
                "Failed to strip GIF from panel: guild_id=%s message_id=%s",
                guild.id,
                message_id,
            )
            continue

        if removed_attachments or removed_references:
            result.changed += 1
            result.gif_attachments_removed += removed_attachments
            result.media_references_removed += removed_references
    return result


async def strip_message_gifs(
    message: discord.Message,
    *,
    replacement_view: discord.ui.LayoutView | None = None,
) -> tuple[int, int]:
    gif_attachments = [
        attachment for attachment in message.attachments if is_gif_filename(attachment.filename)
    ]
    retained_attachments = retained_non_gif_attachments(message)
    embeds: list[discord.Embed] = []
    embed_changed = False
    removed_references = 0
    for original in message.embeds:
        embed = original.copy()
        if embed.image and is_gif_media_url(embed.image.url):
            embed.set_image(url=None)
            embed_changed = True
            removed_references += 1
        if embed.thumbnail and is_gif_media_url(embed.thumbnail.url):
            embed.set_thumbnail(url=None)
            embed_changed = True
            removed_references += 1
        embeds.append(embed)

    layout_view: discord.ui.LayoutView | None = None
    preserve_gif_attachments = False
    try:
        parsed_view = discord.ui.LayoutView.from_message(message)
    except (AttributeError, TypeError, ValueError):
        parsed_view = None
    if isinstance(parsed_view, discord.ui.LayoutView):
        gallery_items = [
            item for item in parsed_view.walk_children() if isinstance(item, discord.ui.MediaGallery)
        ]
        gif_gallery_count = sum(
            1
            for gallery in gallery_items
            for media in gallery.items
            if is_gif_media_url(str(media.media.url))
        )
        has_dispatchable_items = any(
            item.is_dispatchable() for item in parsed_view.walk_children()
        )
        if gif_gallery_count and replacement_view is not None:
            removed_references += gif_gallery_count
            layout_view = replacement_view
        elif gif_gallery_count and not has_dispatchable_items:
            for item in gallery_items:
                retained_media = [
                    media for media in item.items if not is_gif_media_url(str(media.media.url))
                ]
                if len(retained_media) == len(item.items):
                    continue
                if retained_media:
                    item.clear_items()
                    for media in retained_media:
                        item.append_item(media)
                elif item.parent is not None:
                    item.parent.remove_item(item)
                else:
                    parsed_view.remove_item(item)
            removed_references += gif_gallery_count
            layout_view = parsed_view
        elif gif_gallery_count:
            # A generic LayoutView has no original callbacks. Keep its attachment
            # intact unless the owning cog provided a real replacement renderer.
            preserve_gif_attachments = True

    update: dict = {}
    if gif_attachments and not preserve_gif_attachments:
        update["attachments"] = retained_attachments
    if embed_changed:
        update["embeds"] = embeds
    if layout_view is not None:
        update["view"] = layout_view
    if update:
        await message.edit(**update)
    removed_attachment_count = 0 if preserve_gif_attachments else len(gif_attachments)
    return removed_attachment_count, removed_references
