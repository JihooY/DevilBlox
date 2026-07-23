from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import error_embed, info_embed, success_embed
from utils.gifs import PANEL_GIFS, TICKET_CLOSE_GIFS, TICKET_OPEN_GIFS, random_embed_gif_kwargs
from utils.panels import restore_panel_message, save_panel_location
from utils.permissions import allow_ticket_access, deny_ticket_access
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

    async def cog_load(self):
        self.restore_support_panel_loop.start()

    async def cog_unload(self):
        self.restore_support_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def refresh_support_panel(self, guild: discord.Guild, *, rotate_image: bool = False):
        await restore_panel_message(
            self.repos,
            guild,
            "support",
            "support_panel_message_id",
            embed=info_embed("SUPPORT", "문의를 시작하려면 아래 버튼을 눌러주세요."),
            view=SupportView(self),
            image_attachment_filename=PANEL_GIFS,
            rotate_image=rotate_image,
        )

    @tasks.loop(minutes=1)
    async def restore_support_panel_loop(self):
        for guild in self.bot.guilds:
            await self.refresh_support_panel(guild, rotate_image=True)

    @restore_support_panel_loop.before_loop
    async def before_restore_support_panel_loop(self):
        await self.bot.wait_until_ready()

    async def _admin_allowed(self, interaction: discord.Interaction) -> bool:
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("admin"))

    async def _collect_transcript(self, channel: discord.TextChannel) -> list[dict]:
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

        channel = await guild.create_text_channel(
            name=safe_channel_name("문의", interaction.user.display_name),
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
            reason="DevilBlox support ticket opened",
        )
        await deny_ticket_access(channel, guild.default_role)
        await allow_ticket_access(channel, interaction.user)
        await allow_ticket_access(channel, admin_role)
        if guild.me is not None:
            await allow_ticket_access(channel, guild.me)
        await self.repos.tickets.create(guild.id, "support", interaction.user.id, channel.id)
        embed = info_embed("SUPPORT", f"{interaction.user.mention}님이 문의를 시작했습니다.")
        ticket_message = await channel.send(
            content=f"{interaction.user.mention} {admin_role.mention}",
            **random_embed_gif_kwargs(embed, TICKET_OPEN_GIFS),
        )
        await self.repos.tickets.set_panel_message(guild.id, channel.id, ticket_message.id)
        await interaction.followup.send(embed=success_embed("문의 티켓 생성 완료", channel.mention), ephemeral=True)

    @app_commands.command(name="문의패널", description="현재 채널에 문의 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def support_panel(self, interaction: discord.Interaction):
        embed = info_embed("SUPPORT", "문의를 시작하려면 아래 버튼을 눌러주세요.")
        message = await interaction.channel.send(
            **random_embed_gif_kwargs(embed, PANEL_GIFS),
            view=SupportView(self),
        )
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "support",
            "support_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("문의 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="문의종료", description="현재 문의 티켓을 종료합니다.")
    @app_commands.describe(채널삭제="종료 처리 후 티켓 채널을 삭제할지 여부")
    async def close_support(self, interaction: discord.Interaction, 채널삭제: bool = True):
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
        delete_notice = "대화 기록을 저장한 뒤 10초 후 채널이 자동 삭제됩니다." if 채널삭제 else "대화 기록을 저장하고 채널은 유지됩니다."
        embed = success_embed("문의 종료", delete_notice)
        await interaction.channel.send(**random_embed_gif_kwargs(embed, TICKET_CLOSE_GIFS))
        transcript = await self._collect_transcript(interaction.channel)
        await self.repos.tickets.save_transcript(ticket, transcript)
        await self.repos.tickets.close(interaction.guild.id, interaction.channel_id, closed_by=interaction.user.id)
        followup_notice = "10초 후 채널이 삭제됩니다." if 채널삭제 else "채널은 삭제하지 않고 유지됩니다."
        await interaction.followup.send(embed=success_embed("문의 티켓 종료 완료", followup_notice), ephemeral=True)
        if 채널삭제:
            await asyncio.sleep(10)
            await interaction.channel.delete(reason="DevilBlox support ticket closed and transcript saved")

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
