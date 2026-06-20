from __future__ import annotations

import secrets
from collections.abc import Iterable, Sequence
from functools import lru_cache

import discord

from utils.assets import asset_path, has_asset
from utils.embeds import BRAND_LOGO_FILENAME, brand_embed, branded_files

GifPool = str | Sequence[str]
GIF_ASSET_FOLDER = "gifs"
OPTIMIZED_GIF_ASSET_FOLDER = "gifs_optimized"

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


@lru_cache(maxsize=128)
def has_gif_asset(filename: str) -> bool:
    return has_asset(GIF_ASSET_FOLDER, filename) or has_asset(OPTIMIZED_GIF_ASSET_FOLDER, filename)


@lru_cache(maxsize=128)
def gif_asset_path(filename: str):
    # Prefer originals: re-encoding GIFs can break hand-timed frames and palettes.
    if has_asset(GIF_ASSET_FOLDER, filename):
        return asset_path(GIF_ASSET_FOLDER, filename)
    return asset_path(OPTIMIZED_GIF_ASSET_FOLDER, filename)


@lru_cache(maxsize=64)
def _available_gifs_cached(candidates: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in candidates if has_gif_asset(name))


def available_gifs(candidates: GifPool | None) -> tuple[str, ...]:
    return _available_gifs_cached(normalize_gif_pool(candidates))


def choose_gif(
    candidates: GifPool | None,
    attachments: Iterable[discord.Attachment] = (),
    *,
    force_new: bool = False,
) -> str | None:
    pool = available_gifs(candidates)
    if not pool:
        return None

    existing = [] if force_new else [attachment.filename for attachment in attachments if attachment.filename in pool]
    if existing:
        return existing[0]

    current = {attachment.filename for attachment in attachments if attachment.filename in pool}
    choices = tuple(name for name in pool if name not in current) if force_new and len(pool) > 1 else pool
    return secrets.choice(choices or pool)


def gif_file(filename: str | None) -> discord.File | None:
    if not filename or not has_gif_asset(filename):
        return None
    return discord.File(str(gif_asset_path(filename)), filename=filename)


def random_embed_gif_kwargs(embed: discord.Embed, candidates: GifPool) -> dict:
    filename = choose_gif(candidates)
    file = gif_file(filename)
    if filename is None or file is None:
        files = branded_files()
        if files:
            return {"embed": brand_embed(embed), "files": files}
        return {"embed": brand_embed(embed)}
    embed.set_image(url=f"attachment://{filename}")
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
    filename = choose_gif(candidates, message.attachments, force_new=force_new)
    if filename is None:
        update["embed"] = brand_embed(embed)
        return update
    embed.set_image(url=f"attachment://{filename}")
    if (
        not any(attachment.filename == filename for attachment in message.attachments)
        or not any(attachment.filename == BRAND_LOGO_FILENAME for attachment in message.attachments)
    ):
        file = gif_file(filename)
        attachments = branded_files(file)
        if attachments:
            update["attachments"] = attachments
    update["embed"] = brand_embed(embed)
    return update
