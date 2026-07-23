from __future__ import annotations

import secrets
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote, urlparse

import discord

from utils.assets import asset_path, has_asset
from utils.embeds import BRAND_LOGO_FILENAME, brand_embed, branded_files

GifPool = str | Sequence[str]
GIF_ASSET_FOLDER = "gifs"
OPTIMIZED_GIF_ASSET_FOLDER = "gifs_optimized"


@dataclass(frozen=True, slots=True)
class GifDeliveryStatus:
    configured_mode: str
    effective_mode: str
    cdn_base_url: str | None
    suppressed: bool
    suppression_reason: str | None
    rotation_enabled: bool
    local_variant: str
    recovering: bool


_configured_mode = "local"
_cdn_base_url: str | None = None
_suppressed = False
_suppression_reason: str | None = None
_rotation_enabled = False
_local_variant = "original"
_recovery_upload_interval = 0.0
_recovery_enabled = False
_next_recovery_upload_at = 0.0

ALL_GIFS = (
    "abyss_ticket.gif",
    "aqua_motion.gif",
    "aqua_step.gif",
    "blue_room.gif",
    "blue_spark.gif",
    "bubble_bath.gif",
    "candy_room.gif",
    "card_bite.gif",
    "city_bridge.gif",
    "crimson_umbrella.gif",
    "dance_hall.gif",
    "denied.gif",
    "festival_pair.gif",
    "forest_motion.gif",
    "gamer_focus.gif",
    "golden_eclipse.gif",
    "moon_rabbit.gif",
    "neon_corridor.gif",
    "pastel_signal.gif",
    "phone_closeup.gif",
    "red_alert.gif",
    "shadow_gate.gif",
    "singer_closeup.gif",
    "stage_walk.gif",
    "starlight_panel.gif",
    "success.gif",
    "sunlit_ruins.gif",
    "team_sunset.gif",
    "torii_glow.gif",
    "verify1.gif",
    "verify2.gif",
    "verify3.gif",
)

PANEL_GIFS = (
    "city_bridge.gif",
    "sunlit_ruins.gif",
    "team_sunset.gif",
    "torii_glow.gif",
    "blue_room.gif",
    "crimson_umbrella.gif",
    "golden_eclipse.gif",
    "forest_motion.gif",
    "dance_hall.gif",
    "pastel_signal.gif",
)

TICKET_OPEN_GIFS = (
    "abyss_ticket.gif",
    "aqua_motion.gif",
    "neon_corridor.gif",
    "shadow_gate.gif",
    "stage_walk.gif",
    "gamer_focus.gif",
    "phone_closeup.gif",
)

TICKET_CLOSE_GIFS = (
    "blue_spark.gif",
    "moon_rabbit.gif",
    "aqua_step.gif",
    "festival_pair.gif",
    "success.gif",
)

SUCCESS_GIFS = (
    "success.gif",
    "blue_spark.gif",
    "candy_room.gif",
    "bubble_bath.gif",
    "dance_hall.gif",
    "pastel_signal.gif",
    "singer_closeup.gif",
)

DENIED_GIFS = (
    "denied.gif",
    "card_bite.gif",
    "red_alert.gif",
    "gamer_focus.gif",
)

TICKET_STATE_GIFS = (
    "card_bite.gif",
    "red_alert.gif",
    "crimson_umbrella.gif",
    "singer_closeup.gif",
)

TICKET_CONDITION_GIFS = (
    "team_sunset.gif",
    "festival_pair.gif",
    "city_bridge.gif",
    "stage_walk.gif",
    "dance_hall.gif",
)

STOCK_CONDITION_GIFS = (
    "golden_eclipse.gif",
    "red_alert.gif",
    "forest_motion.gif",
    "pastel_signal.gif",
)

STOCK_CONTROL_GIFS = (
    "blue_room.gif",
    "bubble_bath.gif",
    "candy_room.gif",
    "aqua_step.gif",
)

VENDING_PANEL_GIFS = (
    "abyss_ticket.gif",
    "city_bridge.gif",
    "gamer_focus.gif",
    "phone_closeup.gif",
    "dance_hall.gif",
)

ARCHIVE_PANEL_GIFS = (
    "red_alert.gif",
    "forest_motion.gif",
    "moon_rabbit.gif",
    "aqua_step.gif",
)

VERIFY_GIFS = (
    "verify1.gif",
    "verify2.gif",
    "verify3.gif",
    "festival_pair.gif",
    "starlight_panel.gif",
    "pastel_signal.gif",
    "singer_closeup.gif",
    "dance_hall.gif",
)

# Keep the context names for existing callers, but let every place draw from the
# full GIF set so panels and logs feel less repetitive.
PANEL_GIFS = ALL_GIFS
TICKET_OPEN_GIFS = ALL_GIFS
TICKET_CLOSE_GIFS = ALL_GIFS
SUCCESS_GIFS = ALL_GIFS
DENIED_GIFS = ALL_GIFS
TICKET_STATE_GIFS = ALL_GIFS
TICKET_CONDITION_GIFS = ALL_GIFS
STOCK_CONDITION_GIFS = ALL_GIFS
STOCK_CONTROL_GIFS = ALL_GIFS
VENDING_PANEL_GIFS = ALL_GIFS
ARCHIVE_PANEL_GIFS = ALL_GIFS
VERIFY_GIFS = ALL_GIFS


def normalize_gif_pool(candidates: GifPool | None) -> tuple[str, ...]:
    if candidates is None:
        return ()
    if isinstance(candidates, str):
        return (candidates,)
    return tuple(str(candidate) for candidate in candidates)


def configure_gif_delivery(
    *,
    mode: str,
    cdn_base_url: str | None,
    rotation_enabled: bool = False,
    local_variant: str = "original",
) -> None:
    global _configured_mode, _cdn_base_url, _rotation_enabled, _local_variant

    normalized_mode = mode.casefold()
    if normalized_mode not in {"auto", "local", "cdn"}:
        raise ValueError("GIF delivery mode must be auto, local, or cdn")
    normalized_variant = local_variant.casefold()
    if normalized_variant not in {"original", "optimized"}:
        raise ValueError("GIF local variant must be original or optimized")

    _configured_mode = normalized_mode
    _cdn_base_url = cdn_base_url.rstrip("/") if cdn_base_url else None
    _rotation_enabled = rotation_enabled
    _local_variant = normalized_variant
    gif_asset_path.cache_clear()
    end_gif_recovery()


def set_gif_suppressed(enabled: bool, reason: str | None = None) -> bool:
    global _suppressed, _suppression_reason

    changed = _suppressed != enabled
    _suppressed = enabled
    _suppression_reason = reason if enabled else None
    return changed


def begin_gif_recovery(upload_interval: float, *, duration: float = 3_600.0) -> None:
    """Throttle new local GIF uploads until :func:`end_gif_recovery` is called.

    ``duration`` remains accepted for compatibility with existing callers, but
    recovery protection no longer expires on a timer. A timed expiry could
    release every still-pending panel upload in the same refresh wave.
    """

    global _recovery_upload_interval, _recovery_enabled, _next_recovery_upload_at

    interval = max(float(upload_interval), 0.0)
    now = time.monotonic()
    _ = duration
    _recovery_upload_interval = interval
    _recovery_enabled = interval > 0
    _next_recovery_upload_at = now


def end_gif_recovery() -> None:
    global _recovery_upload_interval, _recovery_enabled, _next_recovery_upload_at

    _recovery_upload_interval = 0.0
    _recovery_enabled = False
    _next_recovery_upload_at = 0.0


def gif_recovery_active() -> bool:
    resolved_local = _configured_mode == "local" or (
        _configured_mode == "auto" and not _cdn_base_url
    )
    return (
        resolved_local
        and not _suppressed
        and _recovery_enabled
        and _recovery_upload_interval > 0
    )


def claim_local_gif_upload_slot() -> bool:
    global _next_recovery_upload_at

    if gif_delivery_status().effective_mode != "local" or not gif_recovery_active():
        return True
    now = time.monotonic()
    if now < _next_recovery_upload_at:
        return False
    _next_recovery_upload_at = now + _recovery_upload_interval
    return True


def gif_delivery_status() -> GifDeliveryStatus:
    resolved_mode = "cdn" if _configured_mode == "auto" and _cdn_base_url else _configured_mode
    if resolved_mode == "auto":
        resolved_mode = "local"
    return GifDeliveryStatus(
        configured_mode=_configured_mode,
        effective_mode="disabled" if _suppressed else resolved_mode,
        cdn_base_url=_cdn_base_url,
        suppressed=_suppressed,
        suppression_reason=_suppression_reason,
        rotation_enabled=_rotation_enabled,
        local_variant=_local_variant,
        recovering=gif_recovery_active(),
    )


def gifs_suppressed() -> bool:
    return _suppressed


def gif_media_url(filename: str | None) -> str | None:
    if not filename or _suppressed:
        return None
    status = gif_delivery_status()
    if status.effective_mode == "cdn":
        if not _cdn_base_url:
            return None
        return f"{_cdn_base_url}/{quote(filename)}"
    return f"attachment://{filename}"


def is_gif_filename(filename: str | None) -> bool:
    return bool(filename and filename.casefold().endswith(".gif"))


def is_gif_media_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(str(url))
    target = parsed.path or parsed.netloc
    return target.casefold().endswith(".gif")


@lru_cache(maxsize=128)
def has_gif_asset(filename: str) -> bool:
    return has_asset(GIF_ASSET_FOLDER, filename) or has_asset(OPTIMIZED_GIF_ASSET_FOLDER, filename)


@lru_cache(maxsize=128)
def gif_asset_path(filename: str):
    folders = (
        (OPTIMIZED_GIF_ASSET_FOLDER, GIF_ASSET_FOLDER)
        if _local_variant == "optimized"
        else (GIF_ASSET_FOLDER, OPTIMIZED_GIF_ASSET_FOLDER)
    )
    for folder in folders:
        if has_asset(folder, filename):
            return asset_path(folder, filename)
    return asset_path(folders[0], filename)


@lru_cache(maxsize=64)
def _available_gifs_cached(candidates: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in candidates if has_gif_asset(name))


def available_gifs(candidates: GifPool | None) -> tuple[str, ...]:
    if _suppressed:
        return ()
    normalized = normalize_gif_pool(candidates)
    if gif_delivery_status().effective_mode == "cdn":
        return normalized
    return _available_gifs_cached(normalized)


def choose_gif(
    candidates: GifPool | None,
    attachments: Iterable[discord.Attachment] = (),
    *,
    force_new: bool = False,
    existing_urls: Iterable[str] = (),
) -> str | None:
    pool = available_gifs(candidates)
    if not pool:
        return None

    rotate = force_new and _rotation_enabled
    existing = [] if rotate else [attachment.filename for attachment in attachments if attachment.filename in pool]
    if not existing and not rotate:
        for url in existing_urls:
            parsed_url = urlparse(str(url))
            path_name = (parsed_url.path.rsplit("/", 1)[-1] or parsed_url.netloc)
            if path_name in pool:
                existing.append(path_name)
                break
    if existing:
        return existing[0]

    if gif_delivery_status().effective_mode == "local" and not claim_local_gif_upload_slot():
        return None

    current = {attachment.filename for attachment in attachments if attachment.filename in pool}
    choices = tuple(name for name in pool if name not in current) if rotate and len(pool) > 1 else pool
    return secrets.choice(choices or pool)


def gif_file(filename: str | None) -> discord.File | None:
    if (
        not filename
        or _suppressed
        or gif_delivery_status().effective_mode != "local"
        or not has_gif_asset(filename)
    ):
        return None
    return discord.File(str(gif_asset_path(filename)), filename=filename)


def gif_file_from_folder(filename: str | None, folder: str) -> discord.File | None:
    if (
        not filename
        or _suppressed
        or gif_delivery_status().effective_mode != "local"
        or not has_asset(folder, filename)
    ):
        return None
    return discord.File(str(asset_path(folder, filename)), filename=filename)


def message_media_urls(message: discord.Message) -> tuple[str, ...]:
    urls: list[str] = []
    for embed in message.embeds:
        if embed.image and embed.image.url:
            urls.append(embed.image.url)
        if embed.thumbnail and embed.thumbnail.url:
            urls.append(embed.thumbnail.url)
    try:
        view = discord.ui.LayoutView.from_message(message)
    except (AttributeError, ValueError):
        return tuple(urls)
    if not isinstance(view, discord.ui.LayoutView):
        return tuple(urls)
    for item in view.walk_children():
        if not isinstance(item, discord.ui.MediaGallery):
            continue
        urls.extend(str(media.media.url) for media in item.items if media.media.url)
    return tuple(urls)


def retained_non_gif_attachments(message: discord.Message) -> list[discord.Attachment]:
    return [attachment for attachment in message.attachments if not is_gif_filename(attachment.filename)]


def random_embed_gif_kwargs(embed: discord.Embed, candidates: GifPool) -> dict:
    filename = choose_gif(candidates)
    file = gif_file(filename)
    media_url = gif_media_url(filename)
    if media_url is None:
        files = branded_files()
        if files:
            return {"embed": brand_embed(embed), "files": files}
        return {"embed": brand_embed(embed)}
    embed.set_image(url=media_url)
    files = branded_files(file)
    if files:
        return {"embed": brand_embed(embed), "files": files}
    return {"embed": brand_embed(embed)}


def panel_embed_edit_kwargs(
    embed: discord.Embed,
    message: discord.Message,
    candidates: GifPool,
    *,
    force_new: bool = False,
) -> dict:
    update = {"embed": embed}
    filename = choose_gif(
        candidates,
        message.attachments,
        force_new=force_new,
        existing_urls=message_media_urls(message),
    )
    if filename is None:
        embed.set_image(url=None)
        if any(is_gif_filename(attachment.filename) for attachment in message.attachments):
            update["attachments"] = retained_non_gif_attachments(message)
        update["embed"] = brand_embed(embed)
        return update
    embed.set_image(url=gif_media_url(filename))
    local_mode = gif_delivery_status().effective_mode == "local"
    needs_local_file = local_mode and not any(
        attachment.filename == filename for attachment in message.attachments
    )
    needs_logo = not any(
        attachment.filename == BRAND_LOGO_FILENAME for attachment in message.attachments
    )
    stale_local_gif = not local_mode and any(
        is_gif_filename(attachment.filename) for attachment in message.attachments
    )
    if needs_local_file:
        file = gif_file(filename)
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
    update["embed"] = brand_embed(embed)
    return update
