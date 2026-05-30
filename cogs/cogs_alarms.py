from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import error_embed, info_embed, success_embed
from utils.panels import restore_panel_message, save_panel_location


class AlarmView(discord.ui.View):
    def __init__(self, cog: "AlarmCog"):
        super().__init__(timeout=None)
        self.cog = cog

    async def toggle_role(self, interaction: discord.Interaction, role_key: str, label: str):
        settings = await self.cog.repos.settings.get(interaction.guild.id)
        role = interaction.guild.get_role(settings["roles"].get(role_key) or 0)
        if role is None:
            await interaction.response.send_message(
                embed=error_embed("알림 역할 미설정", f"`/역할설정`으로 {label} 역할을 먼저 설정해주세요."),
                ephemeral=True,
            )
            return

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="DevilBlox alarm toggle")
            await interaction.response.send_message(embed=success_embed("알림 해제", f"{label}을 비활성화했습니다."), ephemeral=True)
        else:
            await interaction.user.add_roles(role, reason="DevilBlox alarm toggle")
            await interaction.response.send_message(embed=success_embed("알림 설정", f"{label}을 활성화했습니다."), ephemeral=True)

    @discord.ui.button(label="공지 알림", style=discord.ButtonStyle.primary, custom_id="devilblox:alarm:announcement")
    async def announcement(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.toggle_role(interaction, "alarm_announcement", "공지 알림")

    @discord.ui.button(label="티켓 상태 알림", style=discord.ButtonStyle.primary, custom_id="devilblox:alarm:seller")
    async def seller(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.toggle_role(interaction, "alarm_seller", "티켓 상태 알림")

    @discord.ui.button(label="입고 알림", style=discord.ButtonStyle.primary, custom_id="devilblox:alarm:stock")
    async def stock(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.toggle_role(interaction, "alarm_stock", "입고 알림")


class AlarmCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(AlarmView(self))

    async def cog_load(self):
        self.restore_alarm_panel_loop.start()

    async def cog_unload(self):
        self.restore_alarm_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def refresh_alarm_panel(self, guild: discord.Guild):
        await restore_panel_message(
            self.repos,
            guild,
            "alarm",
            "alarm_panel_message_id",
            embed=info_embed("ALARM SETTING", "받고 싶은 알림 역할을 켜거나 끌 수 있습니다."),
            view=AlarmView(self),
        )

    @tasks.loop(seconds=1, count=1)
    async def restore_alarm_panel_loop(self):
        for guild in self.bot.guilds:
            await self.refresh_alarm_panel(guild)

    @restore_alarm_panel_loop.before_loop
    async def before_restore_alarm_panel_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="알림패널", description="현재 채널에 알림 설정 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def alarm_panel(self, interaction: discord.Interaction):
        message = await interaction.channel.send(
            embed=info_embed("ALARM SETTING", "받고 싶은 알림 역할을 켜거나 끌 수 있습니다."),
            view=AlarmView(self),
        )
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "alarm",
            "alarm_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("알림 패널 생성 완료"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AlarmCog(bot))
