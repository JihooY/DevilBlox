from __future__ import annotations

import discord


async def allow_ticket_access(channel: discord.TextChannel, target: discord.Member | discord.Role):
    overwrite = discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        use_application_commands=True,
    )
    await channel.set_permissions(target, overwrite=overwrite)


async def deny_ticket_access(channel: discord.TextChannel, target: discord.Member | discord.Role):
    overwrite = discord.PermissionOverwrite(
        view_channel=False,
        read_message_history=False,
        send_messages=False,
        embed_links=False,
        attach_files=False,
        use_application_commands=False,
    )
    await channel.set_permissions(target, overwrite=overwrite)


async def move_to_category(channel: discord.TextChannel, category: discord.CategoryChannel | None):
    if category is not None:
        await channel.edit(category=category)
