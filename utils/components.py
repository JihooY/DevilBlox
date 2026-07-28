from __future__ import annotations

from datetime import datetime

import discord

from utils.embeds import BRAND_LOGO_URL, COLOR_INFO

MAX_COMPONENT_TEXT = 3_800
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def embed_to_layout_view(
    embed: discord.Embed,
    *,
    include_brand_thumbnail: bool = True,
    leading_content: str | None = None,
    attachment_filenames: tuple[str, ...] = (),
) -> discord.ui.LayoutView:
    """Render a legacy embed as one consistent Components V2 container."""
    view = discord.ui.LayoutView(timeout=None)
    color = getattr(getattr(embed, "colour", None), "value", None) or COLOR_INFO
    container = discord.ui.Container(accent_color=color)
    text = _embed_markdown(embed, leading_content=leading_content)

    if include_brand_thumbnail:
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(text),
                accessory=discord.ui.Thumbnail(
                    BRAND_LOGO_URL,
                    description="DevilBlox logo",
                ),
            )
        )
    else:
        container.add_item(discord.ui.TextDisplay(text))

    media_items = []
    image_url = getattr(getattr(embed, "image", None), "url", None)
    thumbnail_url = getattr(getattr(embed, "thumbnail", None), "url", None)
    referenced_urls: set[str] = set()
    if image_url:
        referenced_urls.add(str(image_url))
        media_items.append(
            discord.MediaGalleryItem(str(image_url), description=embed.title or "DevilBlox image")
        )
    if thumbnail_url and thumbnail_url != BRAND_LOGO_URL and thumbnail_url != image_url:
        referenced_urls.add(str(thumbnail_url))
        media_items.append(
            discord.MediaGalleryItem(str(thumbnail_url), description="Related image")
        )
    for filename in attachment_filenames:
        attachment_url = f"attachment://{filename}"
        normalized = filename.removeprefix("SPOILER_").casefold()
        if (
            filename == "devilblox_icon.png"
            or not normalized.endswith(IMAGE_EXTENSIONS)
            or attachment_url in referenced_urls
            or len(media_items) >= 10
        ):
            continue
        referenced_urls.add(attachment_url)
        media_items.append(
            discord.MediaGalleryItem(attachment_url, description=filename)
        )
    if media_items:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.MediaGallery(*media_items))

    view.add_item(container)
    return view


def _embed_markdown(embed: discord.Embed, *, leading_content: str | None = None) -> str:
    lines: list[str] = []
    if leading_content:
        lines.extend((str(leading_content), ""))
    author_name = getattr(getattr(embed, "author", None), "name", None)
    if author_name and str(author_name).casefold() != "devil blox":
        lines.append(f"-# {author_name}")

    title = str(embed.title or "DEVILBLOX")
    if embed.url:
        lines.append(f"## [{title}]({embed.url})")
    else:
        lines.append(f"## {title}")
    if embed.description:
        lines.append(str(embed.description))

    for field in embed.fields:
        lines.extend(("", f"### {field.name}", str(field.value)))

    footer_text = getattr(getattr(embed, "footer", None), "text", None)
    if footer_text:
        lines.extend(("", f"-# {footer_text}"))
    if isinstance(embed.timestamp, datetime):
        timestamp = int(embed.timestamp.timestamp())
        lines.append(f"-# <t:{timestamp}:F>")

    text = "\n".join(lines).strip() or "## DEVILBLOX"
    if len(text) <= MAX_COMPONENT_TEXT:
        return text
    suffix = "\n-# 일부 내용은 Discord 길이 제한으로 생략되었습니다."
    return text[: MAX_COMPONENT_TEXT - len(suffix)].rstrip() + suffix
