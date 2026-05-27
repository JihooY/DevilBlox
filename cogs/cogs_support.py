from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.embeds import error_embed, info_embed, success_embed
from utils.permissions import allow_ticket_access, deny_ticket_access, move_to_category
from utils.roles import has_role
from utils.tickets import safe_channel_name


class SupportView(discord.ui.View):
    def __init__(self, cog: "SupportCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="문의하기",
        style=discord.ButtonStyle.success,
        custom_id="devilblox:support:open",
    )
    async def open_support(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.open_support_ticket(interaction)


class SupportCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(SupportView(self))

    @property
    def repos(self):
        return self.bot.repos

    async def _admin_allowed(self, interaction: discord.Interaction) -> bool:
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("admin"))

    async def open_support_ticket(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        existing = await self.repos.tickets.get_open_for_user(guild.id, interaction.user.id, "support")
        if existing:
            channel = guild.get_channel(existing["channel_id"])
            await interaction.followup.send(
                embed=error_embed("이미 열린 문의", channel.mention if channel else str(existing["channel_id"])),
                ephemeral=True,
            )
            return

        settings = await self.repos.settings.get(guild.id)
        admin_role = guild.get_role(settings["roles"].get("admin") or 0)
        category = guild.get_channel(settings["categories"].get("support") or 0)
        if admin_role is None:
            await interaction.followup.send(embed=error_embed("설정 오류", "관리자 역할을 먼저 설정해주세요."), ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            admin_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        channel = await guild.create_text_channel(
            name=safe_channel_name("문의", interaction.user.display_name),
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason="DevilBlox support ticket opened",
        )
        await self.repos.tickets.create(guild.id, "support", interaction.user.id, channel.id)
        await channel.send(
            content=f"{interaction.user.mention} {admin_role.mention}",
            embed=info_embed("SUPPORT", f"{interaction.user.mention}님이 문의를 시작했습니다."),
        )
        await interaction.followup.send(embed=success_embed("문의 티켓 생성 완료", channel.mention), ephemeral=True)

    @app_commands.command(name="문의패널", description="현재 채널에 문의 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def support_panel(self, interaction: discord.Interaction):
        await interaction.channel.send(
            embed=info_embed("SUPPORT", "문의를 시작하려면 아래 버튼을 눌러주세요."),
            view=SupportView(self),
        )
        await interaction.response.send_message(embed=success_embed("문의 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="문의종료", description="현재 문의 티켓을 종료합니다.")
    async def close_support(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self._admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return
        ticket = await self.repos.tickets.get_by_channel(interaction.guild.id, interaction.channel_id, "support")
        if not ticket or ticket.get("status") != "open":
            await interaction.followup.send(embed=error_embed("티켓 오류", "열려있는 문의 티켓이 아닙니다."), ephemeral=True)
            return

        member = interaction.guild.get_member(ticket["user_id"])
        if member:
            await deny_ticket_access(interaction.channel, member)
        settings = await self.repos.settings.get(interaction.guild.id)
        closed = interaction.guild.get_channel(settings["categories"].get("support_closed") or 0)
        await move_to_category(interaction.channel, closed if isinstance(closed, discord.CategoryChannel) else None)
        await self.repos.tickets.close(interaction.guild.id, interaction.channel_id, closed_by=interaction.user.id)
        await interaction.channel.send(embed=success_embed("문의 종료"))
        await interaction.followup.send(embed=success_embed("문의 티켓 종료 완료"), ephemeral=True)

    @app_commands.command(name="유저추가", description="현재 티켓 채널에 유저를 추가합니다.")
    async def add_user(self, interaction: discord.Interaction, 유저: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if not await self._admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return
        ticket = await self.repos.tickets.get_by_channel(interaction.guild.id, interaction.channel_id)
        if not ticket or ticket.get("status") != "open":
            await interaction.followup.send(embed=error_embed("티켓 오류", "열려있는 티켓 채널에서 사용해주세요."), ephemeral=True)
            return
        await allow_ticket_access(interaction.channel, 유저)
        await interaction.followup.send(embed=success_embed("유저 추가 완료", 유저.mention), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SupportCog(bot))
