from __future__ import annotations

import re

import discord


def safe_channel_name(prefix: str, *parts: str) -> str:
    raw = "-".join([prefix, *[part for part in parts if part]])
    raw = raw.lower()
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"[^0-9a-z가-힣_-]", "", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    return raw[:90] or prefix


async def collect_channel_transcript(channel: discord.TextChannel) -> list[dict]:
    messages = []
    async for message in channel.history(limit=None, oldest_first=True):
        messages.append(
            {
                "message_id": message.id,
                "created_at": message.created_at,
                "edited_at": message.edited_at,
                "jump_url": message.jump_url,
                "type": str(message.type),
                "pinned": message.pinned,
                "tts": message.tts,
                "content": message.content,
                "clean_content": message.clean_content,
                "author": {
                    "id": message.author.id,
                    "name": str(message.author),
                    "display_name": getattr(message.author, "display_name", message.author.name),
                    "bot": message.author.bot,
                },
                "mentions": [user.id for user in message.mentions],
                "role_mentions": [role.id for role in message.role_mentions],
                "attachments": [
                    {
                        "id": attachment.id,
                        "filename": attachment.filename,
                        "url": attachment.url,
                        "proxy_url": attachment.proxy_url,
                        "content_type": attachment.content_type,
                        "size": attachment.size,
                    }
                    for attachment in message.attachments
                ],
                "embeds": [embed.to_dict() for embed in message.embeds],
                "stickers": [
                    {
                        "id": sticker.id,
                        "name": sticker.name,
                        "format": str(sticker.format),
                    }
                    for sticker in message.stickers
                ],
                "reference": {
                    "message_id": message.reference.message_id,
                    "channel_id": message.reference.channel_id,
                    "guild_id": message.reference.guild_id,
                }
                if message.reference
                else None,
            }
        )
    return messages
