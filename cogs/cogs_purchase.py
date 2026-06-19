from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import error_embed, info_embed, success_embed
from utils.gifs import (
    PANEL_GIFS,
    SUCCESS_GIFS,
    TICKET_CLOSE_GIFS,
    TICKET_OPEN_GIFS,
    TICKET_STATE_GIFS,
    random_embed_gif_kwargs,
)
from utils.panels import restore_panel_message, save_panel_location
from utils.permissions import allow_ticket_access, deny_ticket_access
from utils.roles import has_role
from utils.tickets import collect_channel_transcript, safe_channel_name

log = logging.getLogger(__name__)

GRADE_THRESHOLDS = (
    (200_000, "svip"),
    (100_000, "vvip"),
    (50_000, "vip"),
    (1, "customer"),
)


async def fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


class PurchaseSelect(discord.ui.Select):
    def __init__(self, cog: "PurchaseCog", sellers: list[dict]):
        self.cog = cog
        options = []
        for seller in sellers[:25]:
            state = "비활성화" if seller.get("ticket_disabled") else "활성화"
            description = (
                f"{state} | 누적 {seller.get('accrued_sell_money', 0)}원 "
                f"| {seller.get('accrued_sell_count', 0)}회"
            )
            options.append(
                discord.SelectOption(
                    label=seller.get("user_name") or str(seller["user_id"]),
                    value=str(seller["user_id"]),
                    description=description[:100],
                )
            )
        disabled = not options
        if not options:
            options.append(discord.SelectOption(label="등록된 셀러가 없습니다.", value="none"))
        super().__init__(
            placeholder="구매할 셀러를 선택하세요.",
            options=options,
            custom_id="devilblox:purchase:select",
            min_values=1,
            max_values=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(embed=error_embed("셀러 없음", "등록된 셀러가 없습니다."), ephemeral=True)
            return
        await self.cog.open_purchase_ticket(interaction, int(self.values[0]))


class PurchasePanelView(discord.ui.View):
    def __init__(self, cog: "PurchaseCog", sellers: list[dict]):
        super().__init__(timeout=None)
        self.add_item(PurchaseSelect(cog, sellers))


class PurchaseCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(PurchasePanelView(self, []))

    async def cog_load(self):
        self.purchase_panel_loop.start()

    async def cog_unload(self):
        self.purchase_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def _seller_allowed(self, interaction: discord.Interaction) -> bool:
        settings = await self.repos.settings.get(interaction.guild.id)
        seller_role = settings["roles"].get("seller")
        admin_role = settings["roles"].get("admin")
        return has_role(interaction.user, seller_role) or has_role(interaction.user, admin_role)

    async def _admin_allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("admin"))

    async def refresh_ticket_condition_panel(self, guild: discord.Guild):
        events_cog = self.bot.get_cog("EventsCog")
        if events_cog is None or not hasattr(events_cog, "build_ticket_condition_embed"):
            return

        try:
            settings = await self.repos.settings.get(guild.id)
            channel_id = settings["channels"].get("ticket_condition")
            message_id = settings["meta"].get("ticket_condition_message_id")
            if not channel_id or not message_id:
                return

            channel = guild.get_channel(channel_id)
            if channel is None:
                return

            message = await channel.fetch_message(message_id)
            embed = await events_cog.build_ticket_condition_embed(guild)
            if hasattr(events_cog, "ticket_condition_edit_kwargs"):
                await message.edit(**events_cog.ticket_condition_edit_kwargs(embed, message))
            else:
                await message.edit(embed=embed)
        except discord.HTTPException:
            return
        except Exception:
            log.exception("Failed to refresh ticket condition panel: guild_id=%s", guild.id)

    async def _refresh_seller_current_ticket_count(self, guild_id: int, seller_id: int):
        doc = await self.repos.sellers.collection.find_one(
            {"_id": f"{guild_id}:{seller_id}"},
            {"current_ticket_channel_ids": 1},
        )
        channel_ids = (doc.get("current_ticket_channel_ids") or []) if doc else []
        await self.repos.sellers.collection.update_one(
            {"_id": f"{guild_id}:{seller_id}"},
            {"$set": {"current_ticket_count": len(channel_ids), "updated_at": datetime.now(timezone.utc)}},
        )

    async def add_seller_current_ticket(self, guild_id: int, seller_id: int, channel_id: int):
        if hasattr(self.repos.sellers, "add_current_ticket"):
            await self.repos.sellers.add_current_ticket(guild_id, seller_id, channel_id)
            return

        now = datetime.now(timezone.utc)
        await self.repos.sellers.collection.update_one(
            {"_id": f"{guild_id}:{seller_id}"},
            {
                "$set": {"guild_id": guild_id, "user_id": seller_id, "updated_at": now},
                "$setOnInsert": {
                    "_id": f"{guild_id}:{seller_id}",
                    "user_name": str(seller_id),
                    "accrued_sell_money": 0,
                    "accrued_sell_count": 0,
                    "current_ticket_count": 0,
                    "ticket_disabled": False,
                    "disabled_reason": "",
                    "created_at": now,
                },
                "$addToSet": {"current_ticket_channel_ids": channel_id},
            },
            upsert=True,
        )
        await self._refresh_seller_current_ticket_count(guild_id, seller_id)

    async def remove_seller_current_ticket(self, guild_id: int, seller_id: int, channel_id: int):
        if hasattr(self.repos.sellers, "remove_current_ticket"):
            await self.repos.sellers.remove_current_ticket(guild_id, seller_id, channel_id)
            return

        await self.repos.sellers.collection.update_one(
            {"_id": f"{guild_id}:{seller_id}"},
            {
                "$pull": {"current_ticket_channel_ids": channel_id},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )
        await self._refresh_seller_current_ticket_count(guild_id, seller_id)

    async def open_purchase_ticket(self, interaction: discord.Interaction, seller_id: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        seller_doc = await self.repos.sellers.get(guild.id, seller_id)
        if seller_doc is None:
            await interaction.followup.send(embed=error_embed("셀러 오류", "등록되지 않은 셀러입니다."), ephemeral=True)
            return
        if seller_doc.get("ticket_disabled"):
            reason = seller_doc.get("disabled_reason") or "사유 없음"
            await interaction.followup.send(embed=error_embed("티켓 비활성화", reason), ephemeral=True)
            return

        existing = await self.repos.tickets.get_open_purchase_for_user_seller(
            guild.id,
            interaction.user.id,
            seller_id,
        )
        if existing:
            channel = guild.get_channel(existing["channel_id"])
            mention = channel.mention if channel else f"`{existing['channel_id']}`"
            await interaction.followup.send(embed=error_embed("이미 열린 티켓", f"이 셀러와 열린 티켓이 있습니다: {mention}"), ephemeral=True)
            return

        settings = await self.repos.settings.get(guild.id)
        category = guild.get_channel(settings["categories"].get("purchase") or 0)
        seller = await fetch_member(guild, seller_id)
        if seller is None:
            await interaction.followup.send(embed=error_embed("셀러 오류", "셀러 멤버를 찾을 수 없습니다."), ephemeral=True)
            return

        admin_role = guild.get_role(settings["roles"].get("admin") or 0)
        channel = await guild.create_text_channel(
            name=safe_channel_name("구매", interaction.user.display_name, seller.display_name),
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
            reason="DevilBlox purchase ticket opened",
        )
        await deny_ticket_access(channel, guild.default_role)
        await allow_ticket_access(channel, interaction.user)
        await allow_ticket_access(channel, seller)
        if admin_role is not None:
            await allow_ticket_access(channel, admin_role)
        if guild.me is not None:
            await allow_ticket_access(channel, guild.me)
        await self.repos.tickets.create(
            guild.id,
            "purchase",
            interaction.user.id,
            channel.id,
            seller_id=seller.id,
        )
        
        await self.add_seller_current_ticket(guild.id, seller.id, channel.id)
        await self.refresh_ticket_condition_panel(guild)

        account = (seller_doc.get("payment_account") or "").strip()
        account_value = account or "아직 등록된 셀러 계좌가 없습니다. 셀러에게 계좌 안내를 요청해주세요."
        embed = info_embed("PURCHASE", f"{interaction.user.mention}님이 {seller.mention} 셀러 구매 티켓을 열었습니다.")
        embed.add_field(name="셀러", value=seller.mention, inline=True)
        embed.add_field(name="구매자", value=interaction.user.mention, inline=True)
        embed.add_field(name="입금 계좌", value=account_value[:1024], inline=False)
        await channel.send(
            content=f"{interaction.user.mention} {seller.mention}",
            **random_embed_gif_kwargs(embed, TICKET_OPEN_GIFS),
        )
        await interaction.followup.send(embed=success_embed("구매 티켓 생성 완료", channel.mention), ephemeral=True)

    async def upgrade_user_grade(self, guild: discord.Guild, member: discord.Member, accrued_spent: int):
        settings = await self.repos.settings.get(guild.id)
        for amount, role_key in GRADE_THRESHOLDS:
            if accrued_spent < amount:
                continue
            role = guild.get_role(settings["roles"].get(role_key) or 0)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="DevilBlox purchase grade upgrade")
                except discord.Forbidden:
                    return
            if role:
                await self.repos.users.set_grade(guild.id, member.id, role.id)
            return

    async def refresh_purchase_panel(self, guild: discord.Guild):
        try:
            settings = await self.repos.settings.get(guild.id)
            channel_id = settings["channels"].get("purchase")
            message_id = settings["meta"].get("purchase_panel_message_id")
            if not channel_id or not message_id:
                return

            channel = guild.get_channel(channel_id)
            if channel is None:
                return

            sellers = await self.repos.sellers.list_active_options(guild.id)
            embed = info_embed("PURCHASE", "원하는 셀러를 선택하면 개인 구매 티켓이 열립니다.")
            await restore_panel_message(
                self.repos,
                guild,
                "purchase",
                "purchase_panel_message_id",
                embed=embed,
                view=PurchasePanelView(self, sellers),
                image_attachment_filename=PANEL_GIFS,
            )
        except Exception:
            log.exception("Failed to refresh purchase panel: guild_id=%s", guild.id)

    @tasks.loop(minutes=1)
    async def purchase_panel_loop(self):
        for guild in self.bot.guilds:
            await self.refresh_purchase_panel(guild)

    @purchase_panel_loop.before_loop
    async def before_purchase_panel_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="셀러등록", description="구매 패널에 표시할 셀러를 등록합니다.")
    @app_commands.default_permissions(administrator=True)
    async def register_seller(self, interaction: discord.Interaction, 셀러: discord.Member):
        await interaction.response.defer(ephemeral=True)
        await self.repos.sellers.upsert(interaction.guild.id, 셀러.id, 셀러.display_name)
        embed = success_embed("셀러 등록 완료", f"{셀러.mention}")
        await interaction.followup.send(
            **random_embed_gif_kwargs(embed, SUCCESS_GIFS),
            ephemeral=True,
        )
        await self.refresh_purchase_panel(interaction.guild)

    @app_commands.command(name="구매패널", description="현재 채널에 구매 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def purchase_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sellers = await self.repos.sellers.list_active_options(interaction.guild.id)
        if not sellers:
            await interaction.followup.send(
                embed=error_embed("셀러 없음", "`/셀러등록`으로 셀러를 먼저 등록해주세요."),
                ephemeral=True,
            )
            return

        embed = info_embed("PURCHASE", "원하는 셀러를 선택하면 개인 구매 티켓이 열립니다.")
        message = await interaction.channel.send(**random_embed_gif_kwargs(embed, PANEL_GIFS), view=PurchasePanelView(self, sellers))
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "purchase",
            "purchase_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.followup.send(embed=success_embed("구매 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="구매티켓종료", description="현재 구매 티켓을 종료하고 판매 기록을 저장합니다.")
    @app_commands.default_permissions(send_messages=True)
    async def close_purchase_ticket(self, interaction: discord.Interaction, 상품명: str, 금액: int):
        await interaction.response.defer(ephemeral=True)
        if not await self._seller_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 권한이 필요합니다."), ephemeral=True)
            return
        if 금액 < 0:
            await interaction.followup.send(embed=error_embed("금액 오류", "금액은 0 이상이어야 합니다."), ephemeral=True)
            return

        ticket = await self.repos.tickets.get_by_channel(interaction.guild.id, interaction.channel_id, "purchase")
        if not ticket or ticket.get("status") != "open":
            await interaction.followup.send(embed=error_embed("티켓 오류", "열려있는 구매 티켓이 아닙니다."), ephemeral=True)
            return
        if not await self._admin_allowed(interaction) and ticket.get("seller_id") != interaction.user.id:
            await interaction.followup.send(embed=error_embed("권한 없음", "이 티켓의 담당 셀러만 종료할 수 있습니다."), ephemeral=True)
            return

        buyer = await fetch_member(interaction.guild, ticket["user_id"])
        seller = await fetch_member(interaction.guild, ticket["seller_id"])
        channel = interaction.channel
        settings = await self.repos.settings.get(interaction.guild.id)

        if buyer:
            await deny_ticket_access(channel, buyer)
        if seller:
            await deny_ticket_access(channel, seller)

        embed = success_embed(
            "구매 티켓 종료",
            f"상품명: {상품명}\n금액: {금액:,}원\n대화 기록을 저장한 뒤 10초 후 채널이 자동 삭제됩니다.",
        )
        await channel.send(**random_embed_gif_kwargs(embed, TICKET_CLOSE_GIFS))
        transcript = await collect_channel_transcript(channel)
        await self.repos.tickets.save_transcript(ticket, transcript)
        await self.repos.tickets.close(
            interaction.guild.id,
            channel.id,
            product_name=상품명,
            amount=금액,
            closed_by=interaction.user.id,
        )
        await self.remove_seller_current_ticket(interaction.guild.id, ticket["seller_id"], channel.id)
        await self.refresh_ticket_condition_panel(interaction.guild)

        if 금액 > 0:
            buyer_doc = await self.repos.users.add_spent(interaction.guild.id, ticket["user_id"], 금액)
            await self.repos.users.add_points(interaction.guild.id, ticket["user_id"], 금액 // 1000)
            await self.repos.sellers.add_sale(interaction.guild.id, ticket["seller_id"], 금액)
            if buyer:
                await self.upgrade_user_grade(interaction.guild, buyer, buyer_doc.get("accrued_spent", 0))
            await self.refresh_purchase_panel(interaction.guild)

            log_channel = interaction.guild.get_channel(settings["channels"].get("purchase_log") or 0)
            if log_channel:
                buyer_label = buyer.mention if buyer else str(ticket["user_id"])
                seller_label = seller.mention if seller else str(ticket["seller_id"])
                embed = info_embed("PURCHASE LOG", f"{buyer_label}님 {상품명} 구매 감사합니다!")
                embed.add_field(name="판매자", value=seller_label, inline=False)
                await log_channel.send(**random_embed_gif_kwargs(embed, SUCCESS_GIFS))

        await interaction.followup.send(embed=success_embed("구매 티켓 종료 완료", "10초 후 채널이 삭제됩니다."), ephemeral=True)
        await asyncio.sleep(10)
        await channel.delete(reason="DevilBlox purchase ticket closed and transcript saved")

    @app_commands.command(name="티켓설정", description="본인 셀러 티켓의 생성 가능 여부를 바꿉니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.choices(상태=[
        app_commands.Choice(name="켜기", value="on"),
        app_commands.Choice(name="끄기", value="off"),
    ])
    async def seller_ticket_state(
        self,
        interaction: discord.Interaction,
        상태: app_commands.Choice[str],
        사유: str = "",
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self._seller_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 권한이 필요합니다."), ephemeral=True)
            return
        await self.repos.sellers.upsert(interaction.guild.id, interaction.user.id, interaction.user.display_name)
        disabled = 상태.value == "off"
        await self.repos.sellers.set_ticket_state(interaction.guild.id, interaction.user.id, disabled, 사유)
        await self.refresh_purchase_panel(interaction.guild)
        await self.send_ticket_state_log(interaction, disabled, 사유)
        await interaction.followup.send(
            embed=success_embed("티켓 상태 변경", "비활성화" if disabled else "활성화"),
            ephemeral=True,
        )

    async def send_ticket_state_log(self, interaction: discord.Interaction, disabled: bool, reason: str):
        settings = await self.repos.settings.get(interaction.guild.id)
        channel_id = settings["channels"].get("ticket_state_log") or settings["channels"].get("ticket_condition")
        channel = interaction.guild.get_channel(channel_id or 0)
        if channel is None:
            return

        mention_role = interaction.guild.get_role(settings["roles"].get("alarm_seller") or 0)
        state = "비활성화" if disabled else "활성화"
        embed = error_embed("TICKET STATE", f"{interaction.user.mention} 셀러 티켓이 {state}되었습니다.")
        if not disabled:
            embed = success_embed("TICKET STATE", f"{interaction.user.mention} 셀러 티켓이 {state}되었습니다.")
        embed.add_field(name="셀러", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="상태", value=state, inline=True)
        embed.add_field(name="처리자", value=interaction.user.mention, inline=True)
        if reason.strip():
            embed.add_field(name="사유", value=reason.strip()[:1024], inline=False)

        await channel.send(
            content=mention_role.mention if mention_role else None,
            **random_embed_gif_kwargs(embed, TICKET_STATE_GIFS),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )

    @app_commands.command(name="계좌등록", description="구매 티켓에 자동 표시할 셀러 계좌를 등록하거나 수정합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(
        계좌정보="예: 국민 123456-78-901234 홍길동",
        셀러="관리자가 다른 셀러 계좌를 대신 등록할 때 선택합니다.",
    )
    async def seller_payment_account(
        self,
        interaction: discord.Interaction,
        계좌정보: str,
        셀러: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self._seller_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 권한이 필요합니다."), ephemeral=True)
            return
        if not 계좌정보.strip() or len(계좌정보.strip()) > 500:
            await interaction.followup.send(embed=error_embed("계좌 오류", "계좌 정보는 1~500자로 입력해주세요."), ephemeral=True)
            return

        is_admin = await self._admin_allowed(interaction)
        target = 셀러 if 셀러 is not None and is_admin else interaction.user
        if 셀러 is not None and not is_admin and 셀러.id != interaction.user.id:
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러는 본인 계좌만 등록할 수 있습니다."), ephemeral=True)
            return

        await self.repos.sellers.upsert(interaction.guild.id, target.id, target.display_name)
        await self.repos.sellers.set_payment_account(interaction.guild.id, target.id, 계좌정보)
        embed = success_embed("계좌 등록 완료", f"{target.mention} 티켓에 자동 표시됩니다.")
        await interaction.followup.send(
            **random_embed_gif_kwargs(embed, SUCCESS_GIFS),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PurchaseCog(bot))
