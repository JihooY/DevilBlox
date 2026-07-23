from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import error_embed, info_embed, success_embed
from utils.gifs import PANEL_GIFS, TICKET_CLOSE_GIFS, TICKET_OPEN_GIFS, random_embed_gif_kwargs
from utils.panels import restore_panel_message, save_panel_location
from utils.permissions import deny_ticket_access, move_to_category
from utils.roles import has_role
from utils.tickets import collect_channel_transcript, safe_channel_name


async def fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


class MiddlemanInfoSelect(discord.ui.Select):
    def __init__(self, cog: "MiddlemanCog", middlemen: list[dict]):
        self.cog = cog
        options = [
            discord.SelectOption(label=doc.get("user_name") or str(doc["user_id"]), value=str(doc["user_id"]))
            for doc in middlemen[:25]
        ]
        super().__init__(placeholder="중개자를 선택하세요.", options=options, custom_id="devilblox:mm:info_select")

    async def callback(self, interaction: discord.Interaction):
        doc = await self.cog.repos.middlemen.get(interaction.guild.id, int(self.values[0]))
        if doc is None:
            await interaction.response.send_message(embed=error_embed("중개자 없음"), ephemeral=True)
            return
        member = await fetch_member(interaction.guild, doc["user_id"])
        embed = info_embed("MIDDLEMAN INFO")
        embed.add_field(name="이름", value=member.mention if member else doc.get("user_name", str(doc["user_id"])), inline=False)
        embed.add_field(name="누적 중개 횟수", value=f"{doc.get('accrued_trade_count', 0)}회", inline=True)
        embed.add_field(name="누적 중개 금액", value=f"{doc.get('accrued_trade_money', 0):,}원", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MiddlemanInfoView(discord.ui.View):
    def __init__(self, cog: "MiddlemanCog", middlemen: list[dict]):
        super().__init__(timeout=180)
        self.add_item(MiddlemanInfoSelect(cog, middlemen))


class MiddlemanRequestModal(discord.ui.Modal, title="중개 정보 입력"):
    counterparty = discord.ui.TextInput(label="거래 대상자 ID", placeholder="상대방 Discord ID")
    middleman = discord.ui.TextInput(label="중개자 ID", placeholder="중개자 Discord ID")

    def __init__(self, cog: "MiddlemanCog", view: "MiddlemanDraftView"):
        super().__init__()
        self.cog = cog
        self.draft_view = view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            counterparty_id = int(str(self.counterparty.value).strip())
            middleman_id = int(str(self.middleman.value).strip())
        except ValueError:
            await interaction.followup.send(embed=error_embed("입력 오류", "Discord ID는 숫자만 입력해주세요."), ephemeral=True)
            return

        middleman_doc = await self.cog.repos.middlemen.get(interaction.guild.id, middleman_id)
        if middleman_doc is None:
            await interaction.followup.send(embed=error_embed("중개자 오류", "등록되지 않은 중개자입니다."), ephemeral=True)
            return

        counterparty = await fetch_member(interaction.guild, counterparty_id)
        middleman = await fetch_member(interaction.guild, middleman_id)
        if counterparty is None or middleman is None:
            await interaction.followup.send(embed=error_embed("유저 오류", "상대방 또는 중개자를 서버에서 찾을 수 없습니다."), ephemeral=True)
            return

        self.draft_view.counterparty_id = counterparty_id
        self.draft_view.middleman_id = middleman_id
        self.draft_view.open_button.disabled = False

        embed = self.draft_view.build_embed(interaction.user, counterparty, middleman)
        await self.draft_view.message.edit(embed=embed, view=self.draft_view)
        await interaction.followup.send(embed=success_embed("중개 정보 입력 완료"), ephemeral=True)


class MiddlemanDraftView(discord.ui.View):
    def __init__(self, cog: "MiddlemanCog", requester: discord.Member):
        super().__init__(timeout=300)
        self.cog = cog
        self.requester_id = requester.id
        self.counterparty_id: int | None = None
        self.middleman_id: int | None = None
        self.message: discord.WebhookMessage | None = None

        self.open_button = discord.ui.Button(label="중개 티켓 열기", style=discord.ButtonStyle.success, disabled=True)
        self.open_button.callback = self.open_ticket
        self.add_item(self.open_button)

    def build_embed(
        self,
        requester: discord.Member,
        counterparty: discord.Member | None = None,
        middleman: discord.Member | None = None,
    ) -> discord.Embed:
        embed = info_embed("MIDDLEMAN SERVICE START", "세부정보 입력 후 티켓을 열 수 있습니다.")
        embed.add_field(name="본인", value=f"{requester.mention} (`{requester.id}`)", inline=False)
        embed.add_field(name="상대방", value=f"{counterparty.mention} (`{counterparty.id}`)" if counterparty else "미입력", inline=False)
        embed.add_field(name="중개자", value=f"{middleman.mention} (`{middleman.id}`)" if middleman else "미입력", inline=False)
        return embed

    @discord.ui.button(label="세부정보 입력", style=discord.ButtonStyle.secondary)
    async def details(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("신청자만 수정할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.send_modal(MiddlemanRequestModal(self.cog, self))

    async def open_ticket(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.requester_id:
            await interaction.followup.send("신청자만 열 수 있습니다.", ephemeral=True)
            return
        if self.counterparty_id is None or self.middleman_id is None:
            await interaction.followup.send(embed=error_embed("정보 부족", "세부정보를 먼저 입력해주세요."), ephemeral=True)
            return
        await self.cog.open_middleman_ticket(interaction, self.counterparty_id, self.middleman_id, self)


class MiddlemanPanelView(discord.ui.View):
    def __init__(self, cog: "MiddlemanCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="중개 시작하기", style=discord.ButtonStyle.success, custom_id="devilblox:mm:start")
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = MiddlemanDraftView(self.cog, interaction.user)
        embed = view.build_embed(interaction.user)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @discord.ui.button(label="중개자 정보", style=discord.ButtonStyle.primary, custom_id="devilblox:mm:info")
    async def info(self, interaction: discord.Interaction, _: discord.ui.Button):
        middlemen = await self.cog.repos.middlemen.list_all(interaction.guild.id)
        if not middlemen:
            await interaction.response.send_message(embed=error_embed("중개자 없음", "등록된 중개자가 없습니다."), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=info_embed("MIDDLEMAN INFO", "확인할 중개자를 선택하세요."),
            view=MiddlemanInfoView(self.cog, middlemen),
            ephemeral=True,
        )


class MiddlemanCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(MiddlemanPanelView(self))

    async def cog_load(self):
        self.restore_middleman_panel_loop.start()

    async def cog_unload(self):
        self.restore_middleman_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def refresh_middleman_panel(self, guild: discord.Guild, *, rotate_image: bool = False):
        await restore_panel_message(
            self.repos,
            guild,
            "middleman",
            "middleman_panel_message_id",
            embed=info_embed("MIDDLEMAN SERVICE", "중개 시작 또는 중개자 정보를 확인할 수 있습니다."),
            view=MiddlemanPanelView(self),
            image_attachment_filename=PANEL_GIFS,
            rotate_image=rotate_image,
        )

    @tasks.loop(minutes=1)
    async def restore_middleman_panel_loop(self):
        for guild in self.bot.guilds:
            await self.refresh_middleman_panel(guild, rotate_image=True)

    @restore_middleman_panel_loop.before_loop
    async def before_restore_middleman_panel_loop(self):
        await self.bot.wait_until_ready()

    async def _middleman_allowed(self, interaction: discord.Interaction) -> bool:
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("middleman")) or has_role(
            interaction.user, settings["roles"].get("admin")
        )

    async def open_middleman_ticket(
        self,
        interaction: discord.Interaction,
        counterparty_id: int,
        middleman_id: int,
        draft_view: MiddlemanDraftView,
    ):
        guild = interaction.guild
        settings = await self.repos.settings.get(guild.id)
        category = guild.get_channel(settings["categories"].get("middleman") or 0)
        counterparty = await fetch_member(guild, counterparty_id)
        middleman = await fetch_member(guild, middleman_id)
        if counterparty is None or middleman is None:
            await interaction.followup.send(embed=error_embed("유저 오류", "상대방 또는 중개자를 찾을 수 없습니다."), ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            counterparty: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            middleman: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        channel = await guild.create_text_channel(
            name=safe_channel_name("중개", interaction.user.display_name, counterparty.display_name),
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason="DevilBlox middleman ticket opened",
        )
        await self.repos.tickets.create(
            guild.id,
            "middleman",
            interaction.user.id,
            channel.id,
            counterparty_id=counterparty.id,
            middleman_id=middleman.id,
        )
        embed = info_embed("MIDDLEMAN SERVICE", "중개 티켓이 시작되었습니다.")
        ticket_message = await channel.send(
            content=f"{interaction.user.mention} {counterparty.mention} {middleman.mention}",
            **random_embed_gif_kwargs(embed, TICKET_OPEN_GIFS),
        )
        await self.repos.tickets.set_panel_message(guild.id, channel.id, ticket_message.id)
        draft_view.open_button.disabled = True
        if draft_view.message:
            await draft_view.message.edit(embed=success_embed("중개 티켓 생성 완료", channel.mention), view=None)
        await interaction.followup.send(embed=success_embed("중개 티켓 생성 완료", channel.mention), ephemeral=True)

    @app_commands.command(name="중개자등록", description="중개 서비스에 표시할 중개자를 등록합니다.")
    @app_commands.default_permissions(administrator=True)
    async def register_middleman(self, interaction: discord.Interaction, 중개자: discord.Member):
        await self.repos.middlemen.upsert(interaction.guild.id, 중개자.id, 중개자.display_name)
        await interaction.response.send_message(embed=success_embed("중개자 등록 완료", 중개자.mention), ephemeral=True)

    @app_commands.command(name="중개패널", description="현재 채널에 중개 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def middleman_panel(self, interaction: discord.Interaction):
        embed = info_embed("MIDDLEMAN SERVICE", "중개 시작 또는 중개자 정보를 확인할 수 있습니다.")
        message = await interaction.channel.send(
            **random_embed_gif_kwargs(embed, PANEL_GIFS),
            view=MiddlemanPanelView(self),
        )
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "middleman",
            "middleman_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("중개 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="중개종료", description="현재 중개 티켓을 종료하고 거래 기록을 저장합니다.")
    @app_commands.describe(채널삭제="종료 처리 후 티켓 채널을 삭제할지 여부")
    async def close_middleman(self, interaction: discord.Interaction, 금액: int, 채널삭제: bool = False):
        await interaction.response.defer(ephemeral=True)
        if not await self._middleman_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "중개자 권한이 필요합니다."), ephemeral=True)
            return
        if 금액 < 0:
            await interaction.followup.send(embed=error_embed("금액 오류", "금액은 0 이상이어야 합니다."), ephemeral=True)
            return
        ticket = await self.repos.tickets.get_by_channel(interaction.guild.id, interaction.channel_id, "middleman")
        if not ticket or ticket.get("status") != "open":
            await interaction.followup.send(embed=error_embed("티켓 오류", "열려있는 중개 티켓이 아닙니다."), ephemeral=True)
            return

        user = await fetch_member(interaction.guild, ticket["user_id"])
        counterparty = await fetch_member(interaction.guild, ticket["counterparty_id"])
        if user:
            await deny_ticket_access(interaction.channel, user)
        if counterparty:
            await deny_ticket_access(interaction.channel, counterparty)

        settings = await self.repos.settings.get(interaction.guild.id)
        closed = interaction.guild.get_channel(settings["categories"].get("middleman_closed") or 0)
        await move_to_category(interaction.channel, closed if isinstance(closed, discord.CategoryChannel) else None)
        await self.repos.tickets.close(interaction.guild.id, interaction.channel_id, amount=금액, closed_by=interaction.user.id)
        await self.repos.middlemen.add_trade(interaction.guild.id, interaction.user.id, 금액)

        log_channel = interaction.guild.get_channel(settings["channels"].get("middleman_log") or 0)
        if log_channel:
            user_doc = await self.repos.users.ensure_user(interaction.guild.id, ticket["user_id"])
            counter_doc = await self.repos.users.ensure_user(interaction.guild.id, ticket["counterparty_id"])
            embed = success_embed("MIDDLEMAN LOG")
            embed.add_field(name="거래 금액", value=f"{금액:,}원", inline=False)
            embed.add_field(
                name="유저",
                value="`익명`" if user_doc.get("middleman_anonymous") else (user.mention if user else str(ticket["user_id"])),
                inline=False,
            )
            embed.add_field(
                name="상대방",
                value="`익명`" if counter_doc.get("middleman_anonymous") else (counterparty.mention if counterparty else str(ticket["counterparty_id"])),
                inline=False,
            )
            embed.add_field(name="중개자", value=interaction.user.mention, inline=False)
            await log_channel.send(embed=embed)

        delete_notice = "\n10초 후 채널이 삭제됩니다." if 채널삭제 else "\n채널은 삭제하지 않고 유지됩니다."
        embed = success_embed("중개 종료", f"금액: {금액:,}원{delete_notice}")
        await interaction.channel.send(**random_embed_gif_kwargs(embed, TICKET_CLOSE_GIFS))
        if 채널삭제:
            transcript = await collect_channel_transcript(interaction.channel)
            await self.repos.tickets.save_transcript(ticket, transcript)
        followup_notice = "10초 후 채널이 삭제됩니다." if 채널삭제 else "채널은 삭제하지 않고 유지됩니다."
        await interaction.followup.send(embed=success_embed("중개 티켓 종료 완료", followup_notice), ephemeral=True)
        if 채널삭제:
            await asyncio.sleep(10)
            await interaction.channel.delete(reason="DevilBlox middleman ticket closed")


async def setup(bot: commands.Bot):
    await bot.add_cog(MiddlemanCog(bot))
