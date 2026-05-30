from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import info_embed, success_embed

log = logging.getLogger(__name__)


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def repos(self):
        return self.bot.repos

    async def cog_load(self):
        self.ticket_condition_loop.start()

    async def cog_unload(self):
        self.ticket_condition_loop.cancel()

    async def sync_seller_current_tickets(self, guild: discord.Guild):
        tickets_by_seller: dict[int, list[int]] = {}
        tickets = await self.repos.tickets.list_open_purchase_tickets(guild.id)
        for ticket in tickets:
            seller_id = ticket.get("seller_id")
            channel_id = ticket.get("channel_id")
            if not seller_id or not channel_id or guild.get_channel(channel_id) is None:
                continue
            tickets_by_seller.setdefault(seller_id, []).append(channel_id)
        await self.repos.sellers.set_current_tickets(guild.id, tickets_by_seller)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        settings = await self.repos.settings.get(member.guild.id)
        await self.repos.users.ensure_user(member.guild.id, member.id, settings["roles"].get("verified"))
        channel = member.guild.get_channel(settings["channels"].get("join_leave_log") or 0)
        if channel:
            await channel.send(embed=success_embed("입장", f"{member.mention}님이 들어왔습니다."))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        settings = await self.repos.settings.get(member.guild.id)
        channel = member.guild.get_channel(settings["channels"].get("join_leave_log") or 0)
        if channel:
            embed = discord.Embed(title="퇴장", description=f"{member}님이 나갔습니다.", color=0xE5484D)
            await channel.send(embed=embed)

    @app_commands.command(name="티켓현황패널", description="현재 채널에 티켓 현황 메시지를 생성하고 자동 갱신 대상으로 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    async def ticket_condition_panel(self, interaction: discord.Interaction):
        reset_at = int(time.time())
        await self.sync_seller_current_tickets(interaction.guild)
        embed = await self.build_ticket_condition_embed(interaction.guild, reset_at=reset_at)
        message = await interaction.channel.send(embed=embed)
        await self.repos.settings.set_value(interaction.guild.id, "channels", "ticket_condition", interaction.channel.id)
        await self.repos.settings.set_value(interaction.guild.id, "meta", "ticket_condition_message_id", message.id)
        await self.repos.settings.set_value(interaction.guild.id, "meta", "ticket_condition_reset_at", reset_at)
        await interaction.response.send_message(embed=success_embed("티켓 현황 패널 생성 완료"), ephemeral=True)

    async def build_ticket_condition_embed(self, guild: discord.Guild, reset_at: int | None = None) -> discord.Embed:
        sellers = await self.repos.sellers.list_active_options(guild.id)
        embed = info_embed("TICKET CONDITION", "현재 열려있는 구매 티켓 수를 표시합니다.")
        if reset_at is None:
            settings = await self.repos.settings.get(guild.id)
            reset_at = settings["meta"].get("ticket_condition_reset_at")
        if not sellers:
            embed.description = "등록된 셀러가 없습니다."
            if reset_at:
                embed.add_field(name="LAST RESET", value=f"<t:{reset_at}:F> (<t:{reset_at}:R>)", inline=False)
            return embed
        for seller in sellers:
            opened = seller.get("current_ticket_count")
            if opened is None:
                opened = len(seller.get("current_ticket_channel_ids", []))
            suffix = " (비활성화)" if seller.get("ticket_disabled") else ""
            embed.add_field(name=f"{seller.get('user_name', seller['user_id'])}{suffix}", value=f"{opened}개", inline=False)
        if reset_at:
            embed.add_field(name="LAST RESET", value=f"<t:{reset_at}:F> (<t:{reset_at}:R>)", inline=False)
        return embed

    @tasks.loop(minutes=1)
    async def ticket_condition_loop(self):
        for guild in self.bot.guilds:
            try:
                settings = await self.repos.settings.get(guild.id)
                channel_id = settings["channels"].get("ticket_condition")
                message_id = settings["meta"].get("ticket_condition_message_id")
                if not channel_id or not message_id:
                    continue
                channel = guild.get_channel(channel_id)
                if channel is None:
                    continue
                message = await channel.fetch_message(message_id)
                reset_at = int(time.time())
                await self.repos.settings.set_value(guild.id, "meta", "ticket_condition_reset_at", reset_at)
                await message.edit(embed=await self.build_ticket_condition_embed(guild, reset_at=reset_at))
            except discord.HTTPException:
                continue
            except Exception:
                log.exception("Failed to refresh ticket condition panel: guild_id=%s", guild.id)

    @ticket_condition_loop.before_loop
    async def before_ticket_condition_loop(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self.sync_seller_current_tickets(guild)
            except Exception:
                log.exception("Failed to sync seller current tickets: guild_id=%s", guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))
