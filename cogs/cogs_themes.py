from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands

from cogs.theme_views import ThemeManagerView
from utils.panels import save_panel_location

log = logging.getLogger(__name__)


def theme_panel_edit_kwargs(view: ThemeManagerView) -> dict:
    return {
        "content": None,
        "embeds": [],
        "attachments": [],
        "view": view,
        "allowed_mentions": discord.AllowedMentions.none(),
    }


class ThemesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks = defaultdict(asyncio.Lock)
        self._restore_task: asyncio.Task | None = None

    async def cog_load(self):
        self._restore_task = asyncio.create_task(self.restore_saved_panels(), name="devilblox-theme-panels")

    async def cog_unload(self):
        if self._restore_task is not None:
            self._restore_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._restore_task

    async def restore_saved_panels(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self.refresh_panel(guild.id)
            except Exception:
                log.exception("Failed to restore theme panel: guild_id=%s", guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.refresh_panel(guild.id)

    async def build_panel_view(self, guild_id: int, settings: dict, *, selected: str | None = None):
        state = await self.bot.themes.state(guild_id)
        selected = selected or settings["meta"].get("theme_panel_selected")
        return ThemeManagerView(self.bot, guild_id, None, state, selected)

    async def refresh_panel(self, guild_id: int, *, selected: str | None = None):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        async with self._locks[guild_id]:
            settings = await self.bot.repos.settings.get(guild_id)
            message_id = settings["meta"].get("theme_panel_message_id")
            channel = guild.get_channel(settings["channels"].get("themes") or 0)
            if channel is None or not message_id or not hasattr(channel, "fetch_message"):
                return
            view = await self.build_panel_view(guild_id, settings, selected=selected)
            # Register before the HTTP request so saved controls also recover
            # when Discord temporarily rejects the startup refresh.
            self.bot.add_view(view, message_id=message_id)
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(**theme_panel_edit_kwargs(view))
                await self.bot.repos.settings.set_value(guild_id, "meta", "theme_panel_selected", view.selected)
            except discord.NotFound:
                view.stop()
                await self.bot.repos.settings.set_value(guild_id, "meta", "theme_panel_message_id", None)
                await self.bot.repos.settings.unregister_panel(guild_id, message_id)
            except discord.HTTPException:
                log.exception("Failed to refresh theme panel: guild_id=%s", guild_id)

    async def ensure_panel(self, guild: discord.Guild, channel: discord.TextChannel):
        async with self._locks[guild.id]:
            settings = await self.bot.repos.settings.get(guild.id)
            view = await self.build_panel_view(guild.id, settings)
            message_id = settings["meta"].get("theme_panel_message_id")
            message = None
            if message_id and settings["channels"].get("themes") == channel.id:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(**theme_panel_edit_kwargs(view))
                except discord.NotFound:
                    message = None
            if message is None:
                message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            await save_panel_location(
                self.bot.repos, guild.id, "themes", "theme_panel_message_id", channel.id, message.id,
            )
            await self.bot.repos.settings.set_value(guild.id, "meta", "theme_panel_selected", view.selected)
            self.bot.add_view(view, message_id=message.id)
            return message

    @app_commands.command(name="테마관리패널", description="지정한 채널에 GIF 테마 관리 컨테이너를 설치합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(채널="패널을 보낼 채널 (생략하면 설정된 테마 관리 채널 또는 현재 채널)")
    async def theme_panel(self, interaction: discord.Interaction, 채널: discord.TextChannel | None = None):
        permissions = getattr(interaction.user, "guild_permissions", None)
        if interaction.guild is None or permissions is None or not permissions.administrator:
            await interaction.response.send_message("서버 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await self.bot.repos.settings.get(interaction.guild.id)
        channel = 채널 or interaction.guild.get_channel(settings["channels"].get("themes") or 0) or interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != interaction.guild.id:
            await interaction.followup.send("서버의 텍스트 채널을 지정해주세요.", ephemeral=True)
            return
        try:
            message = await self.ensure_panel(interaction.guild, channel)
        except discord.Forbidden:
            await interaction.followup.send(
                "지정한 채널의 채널 보기·메시지 보내기·메시지 기록 보기 권한을 확인해주세요.", ephemeral=True,
            )
            return
        await interaction.followup.send(f"테마 관리패널을 설치했습니다: {message.jump_url}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ThemesCog(bot))
