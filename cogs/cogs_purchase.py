from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import error_embed, info_embed, success_embed
from utils.panels import restore_panel_message, save_panel_location
from utils.permissions import allow_ticket_access, deny_ticket_access, move_to_category
from utils.roles import has_role
from utils.tickets import safe_channel_name

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
            await interaction.response.send_message(embed=error_embed("셀러 없음", "등록된 셀러가 없습니다."), ephemeral=True)
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

        existing = await self.repos.tickets.get_open_for_user(guild.id, interaction.user.id, "purchase")
        if existing:
            channel = guild.get_channel(existing["channel_id"])
            mention = channel.mention if channel else f"`{existing['channel_id']}`"
            await interaction.followup.send(embed=error_embed("이미 열린 티켓", mention), ephemeral=True)
            return

        settings = await self.repos.settings.get(guild.id)
        category = guild.get_channel(settings["categories"].get("purchase") or 0)
        seller = await fetch_member(guild, seller_id)
        if seller is None:
            await interaction.followup.send(embed=error_embed("셀러 오류", "셀러 멤버를 찾을 수 없습니다."), ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            seller: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        channel = await guild.create_text_channel(
            name=safe_channel_name("구매", interaction.user.display_name),
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason="DevilBlox purchase ticket opened",
        )
        await self.repos.tickets.create(
            guild.id,
            "purchase",
            interaction.user.id,
            channel.id,
            seller_id=seller.id,
        )

        embed = info_embed("PURCHASE", f"{interaction.user.mention}님이 구매 티켓을 열었습니다.")
        await channel.send(content=f"{interaction.user.mention} {seller.mention}", embed=embed)
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
        await self.repos.sellers.upsert(interaction.guild.id, 셀러.id, 셀러.display_name)
        await interaction.response.send_message(
            embed=success_embed("셀러 등록 완료", f"{셀러.mention}"),
            ephemeral=True,
        )
        await self.refresh_purchase_panel(interaction.guild)

    @app_commands.command(name="구매패널", description="현재 채널에 구매 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def purchase_panel(self, interaction: discord.Interaction):
        sellers = await self.repos.sellers.list_active_options(interaction.guild.id)
        if not sellers:
            await interaction.response.send_message(
                embed=error_embed("셀러 없음", "`/셀러등록`으로 셀러를 먼저 등록해주세요."),
                ephemeral=True,
            )
            return

        embed = info_embed("PURCHASE", "원하는 셀러를 선택하면 개인 구매 티켓이 열립니다.")
        message = await interaction.channel.send(embed=embed, view=PurchasePanelView(self, sellers))
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "purchase",
            "purchase_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("구매 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="구매티켓종료", description="현재 구매 티켓을 종료하고 판매 기록을 저장합니다.")
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

        buyer = await fetch_member(interaction.guild, ticket["user_id"])
        seller = await fetch_member(interaction.guild, ticket["seller_id"])
        channel = interaction.channel
        settings = await self.repos.settings.get(interaction.guild.id)

        if buyer:
            await deny_ticket_access(channel, buyer)
        if seller:
            await deny_ticket_access(channel, seller)

        closed_category = interaction.guild.get_channel(settings["categories"].get("purchase_closed") or 0)
        await move_to_category(channel, closed_category if isinstance(closed_category, discord.CategoryChannel) else None)
        await self.repos.tickets.close(
            interaction.guild.id,
            channel.id,
            product_name=상품명,
            amount=금액,
            closed_by=interaction.user.id,
        )

        if 금액 > 0:
            buyer_doc = await self.repos.users.add_spent(interaction.guild.id, ticket["user_id"], 금액)
            await self.repos.users.add_points(interaction.guild.id, ticket["user_id"], 금액 // 1000)
            await self.repos.sellers.add_sale(interaction.guild.id, ticket["seller_id"], 금액)
            if buyer:
                await self.upgrade_user_grade(interaction.guild, buyer, buyer_doc.get("accrued_spent", 0))
            await self.refresh_purchase_panel(interaction.guild)

        log_channel = interaction.guild.get_channel(settings["channels"].get("purchase_log") or 0)
        if log_channel:
            embed = success_embed("PURCHASE LOG")
            embed.add_field(name="구매자", value=buyer.mention if buyer else str(ticket["user_id"]), inline=False)
            embed.add_field(name="판매자", value=seller.mention if seller else str(ticket["seller_id"]), inline=False)
            embed.add_field(name="상품명", value=상품명, inline=True)
            embed.add_field(name="금액", value=f"{금액:,}원", inline=True)
            await log_channel.send(embed=embed)

        await channel.send(embed=success_embed("구매 티켓 종료", f"상품명: {상품명}\n금액: {금액:,}원"))
        await interaction.followup.send(embed=success_embed("구매 티켓 종료 완료"), ephemeral=True)

    @app_commands.command(name="티켓설정", description="본인 셀러 티켓의 생성 가능 여부를 바꿉니다.")
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
        await interaction.followup.send(
            embed=success_embed("티켓 상태 변경", "비활성화" if disabled else "활성화"),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PurchaseCog(bot))
