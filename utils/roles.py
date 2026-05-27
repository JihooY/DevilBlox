from __future__ import annotations

import discord


def has_role(member: discord.Member, role_id: int | None) -> bool:
    return bool(role_id and any(role.id == role_id for role in member.roles))


def get_configured_role(guild: discord.Guild, role_id: int | None) -> discord.Role | None:
    if not role_id:
        return None
    return guild.get_role(role_id)


async def require_role(interaction: discord.Interaction, role_id: int | None, label: str) -> discord.Role | None:
    role = get_configured_role(interaction.guild, role_id) if interaction.guild else None
    if role is None:
        await interaction.followup.send(f"{label} 역할이 아직 설정되지 않았습니다.", ephemeral=True)
    return role
