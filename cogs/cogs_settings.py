from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from database.settings import CATEGORY_KEYS, CHANNEL_KEYS, ROLE_KEYS
from utils.embeds import error_embed, success_embed

ROLE_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in ROLE_KEYS.items()]
CHANNEL_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in CHANNEL_KEYS.items()]
CATEGORY_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in CATEGORY_KEYS.items()]


class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def settings(self):
        return self.bot.repos.settings

    @app_commands.command(name="역할설정", description="봇에서 사용할 역할을 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(종류=ROLE_CHOICES)
    async def set_role(
        self,
        interaction: discord.Interaction,
        종류: app_commands.Choice[str],
        역할: discord.Role,
    ):
        await self.settings.set_value(interaction.guild.id, "roles", 종류.value, 역할.id)
        await interaction.response.send_message(
            embed=success_embed("역할 설정 완료", f"{종류.name}: {역할.mention}"),
            ephemeral=True,
        )

    @app_commands.command(name="채널설정", description="봇에서 사용할 채널을 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(종류=CHANNEL_CHOICES)
    async def set_channel(
        self,
        interaction: discord.Interaction,
        종류: app_commands.Choice[str],
        채널: discord.TextChannel,
    ):
        await self.settings.set_value(interaction.guild.id, "channels", 종류.value, 채널.id)
        await interaction.response.send_message(
            embed=success_embed("채널 설정 완료", f"{종류.name}: {채널.mention}"),
            ephemeral=True,
        )

    @app_commands.command(name="카테고리설정", description="티켓을 만들거나 종료할 때 사용할 카테고리를 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(종류=CATEGORY_CHOICES)
    async def set_category(
        self,
        interaction: discord.Interaction,
        종류: app_commands.Choice[str],
        카테고리: discord.CategoryChannel,
    ):
        await self.settings.set_value(interaction.guild.id, "categories", 종류.value, 카테고리.id)
        await interaction.response.send_message(
            embed=success_embed("카테고리 설정 완료", f"{종류.name}: {카테고리.name}"),
            ephemeral=True,
        )

    @app_commands.command(name="설정확인", description="현재 저장된 봇 설정을 확인합니다.")
    @app_commands.default_permissions(administrator=True)
    async def show_settings(self, interaction: discord.Interaction):
        doc = await self.settings.get(interaction.guild.id)
        embed = discord.Embed(title="DevilBlox 설정", color=0x5865F2)

        role_lines = []
        for key, label in ROLE_KEYS.items():
            role = interaction.guild.get_role(doc["roles"].get(key) or 0)
            role_lines.append(f"{label}: {role.mention if role else '`미설정`'}")

        channel_lines = []
        for key, label in CHANNEL_KEYS.items():
            channel = interaction.guild.get_channel(doc["channels"].get(key) or 0)
            channel_lines.append(f"{label}: {channel.mention if channel else '`미설정`'}")

        category_lines = []
        for key, label in CATEGORY_KEYS.items():
            category = interaction.guild.get_channel(doc["categories"].get(key) or 0)
            category_lines.append(f"{label}: {category.name if category else '`미설정`'}")

        embed.add_field(name="역할", value="\n".join(role_lines)[:1024], inline=False)
        embed.add_field(name="채널", value="\n".join(channel_lines)[:1024], inline=False)
        embed.add_field(name="카테고리", value="\n".join(category_lines)[:1024], inline=False)
        if len("\n".join(channel_lines)) > 1024:
            embed.set_footer(text="채널 설정이 길어 일부만 표시되었습니다.")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))
