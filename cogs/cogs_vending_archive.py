from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.vending import normalize_product_id
from utils.embeds import error_embed, info_embed, success_embed
from utils.panels import restore_panel_message, save_panel_location
from utils.roles import has_role


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}


def parse_positive_amount(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value)
    if not digits:
        return None
    amount = int(digits)
    return amount if amount > 0 else None


def parse_discord_id(value: str) -> int | None:
    if not value:
        return None
    match = re.search(r"\d{15,25}", value)
    return int(match.group(0)) if match else None


def is_http_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_youtube_url(raw_url: str) -> tuple[str, str] | None:
    value = raw_url.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"

    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host not in YOUTUBE_HOSTS:
        return None

    video_id = None
    if host.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [None])[0]
    elif parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
        video_id = parsed.path.strip("/").split("/")[1]

    if not video_id:
        return None

    video_id = re.sub(r"[^A-Za-z0-9_-]", "", video_id)
    if not video_id:
        return None

    return video_id, f"https://youtu.be/{video_id}"


class ChargeRequestModal(discord.ui.Modal, title="충전 신청"):
    depositor_name = discord.ui.TextInput(
        label="입금자명",
        placeholder="입금자명을 입력하세요.",
        max_length=50,
    )
    amount = discord.ui.TextInput(
        label="입금 금액",
        placeholder="예: 10000",
        max_length=20,
    )

    def __init__(self, cog: "VendingArchiveCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_charge_submit(
            interaction,
            depositor_name=str(self.depositor_name.value),
            amount_text=str(self.amount.value),
        )


class RejectChargeModal(discord.ui.Modal, title="충전 거절 사유"):
    reason = discord.ui.TextInput(
        label="거절 사유",
        style=discord.TextStyle.paragraph,
        placeholder="유저에게 전달할 사유를 입력하세요.",
        max_length=500,
    )

    def __init__(self, cog: "VendingArchiveCog", admin_message_id: int):
        super().__init__()
        self.cog = cog
        self.admin_message_id = admin_message_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_reject_charge(interaction, self.admin_message_id, str(self.reason.value))


class ProductPurchaseModal(discord.ui.Modal, title="상품 구매"):
    def __init__(self, cog: "VendingArchiveCog", product_id: str = ""):
        super().__init__()
        self.cog = cog
        self.product_id = discord.ui.TextInput(
            label="상품 ID",
            placeholder="구매할 상품 ID를 입력하세요.",
            default=product_id,
            max_length=64,
        )
        self.add_item(self.product_id)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_purchase(interaction, str(self.product_id.value))


class ArchiveSearchModal(discord.ui.Modal, title="아카이브 검색"):
    youtube_url = discord.ui.TextInput(
        label="유튜브 URL",
        placeholder="영상 우클릭 후 복사한 링크를 붙여넣으세요.",
        max_length=300,
    )

    def __init__(self, cog: "VendingArchiveCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_archive_search(interaction, str(self.youtube_url.value))


class ChargeAdminView(discord.ui.View):
    def __init__(self, cog: "VendingArchiveCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="수락",
        style=discord.ButtonStyle.success,
        custom_id="devilblox:vending:charge:approve",
    )
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_approve_charge(interaction)

    @discord.ui.button(
        label="거절",
        style=discord.ButtonStyle.danger,
        custom_id="devilblox:vending:charge:reject",
    )
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self.cog.admin_allowed(interaction):
            await interaction.response.send_message(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return
        if interaction.message is None:
            await interaction.response.send_message(embed=error_embed("처리 실패", "요청 메시지를 찾을 수 없습니다."), ephemeral=True)
            return
        await interaction.response.send_modal(RejectChargeModal(self.cog, interaction.message.id))


class VendingPanelView(discord.ui.View):
    def __init__(self, cog: "VendingArchiveCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="충전", style=discord.ButtonStyle.success, custom_id="devilblox:vending:charge")
    async def charge(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ChargeRequestModal(self.cog))

    @discord.ui.button(label="구매", style=discord.ButtonStyle.primary, custom_id="devilblox:vending:buy")
    async def buy(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ProductPurchaseModal(self.cog))

    @discord.ui.button(label="다운로드", style=discord.ButtonStyle.secondary, custom_id="devilblox:vending:download")
    async def download(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_download_menu(interaction)


class ArchivePanelView(discord.ui.View):
    def __init__(self, cog: "VendingArchiveCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="검색", style=discord.ButtonStyle.primary, custom_id="devilblox:archive:search")
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ArchiveSearchModal(self.cog))


class ArchiveResultView(discord.ui.View):
    def __init__(self, cog: "VendingArchiveCog", product_id: str, page_url: str | None):
        super().__init__(timeout=180)
        self.cog = cog
        self.product_id = product_id
        if page_url:
            self.add_item(discord.ui.Button(label="상품 페이지", style=discord.ButtonStyle.link, url=page_url))

    @discord.ui.button(label="구매하기", style=discord.ButtonStyle.success)
    async def buy(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ProductPurchaseModal(self.cog, self.product_id))


class DownloadSelect(discord.ui.Select):
    def __init__(self, cog: "VendingArchiveCog", owned_products: list[dict]):
        self.cog = cog
        options = []
        for owned in owned_products[:25]:
            label = owned.get("title") or owned.get("product_id") or "상품"
            product_id = owned.get("product_id") or owned.get("product_id_lower")
            options.append(
                discord.SelectOption(
                    label=str(label)[:100],
                    value=str(owned.get("product_id_lower") or normalize_product_id(product_id)),
                    description=f"ID: {product_id}"[:100],
                )
            )
        super().__init__(
            placeholder="다운로드할 상품을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_download_selected(interaction, self.values[0])


class DownloadSelectView(discord.ui.View):
    def __init__(self, cog: "VendingArchiveCog", owned_products: list[dict]):
        super().__init__(timeout=180)
        self.add_item(DownloadSelect(cog, owned_products))


class VendingArchiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(VendingPanelView(self))
        self.bot.add_view(ArchivePanelView(self))
        self.bot.add_view(ChargeAdminView(self))

    async def cog_load(self):
        self.restore_panel_loop.start()

    async def cog_unload(self):
        self.restore_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def admin_allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("admin"))

    async def staff_allowed(self, interaction: discord.Interaction) -> bool:
        if await self.admin_allowed(interaction):
            return True
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("seller"))

    async def get_admin_channel(self, guild: discord.Guild):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("vending_admin") or settings["channels"].get("purchase_log")
        return guild.get_channel(channel_id or 0)

    async def get_log_channel(self, guild: discord.Guild):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("vending_log") or settings["channels"].get("purchase_log")
        return guild.get_channel(channel_id or 0)

    def product_page_url(self, guild_id: int, product: dict | None) -> str | None:
        if not product:
            return None
        page_url = product.get("page_url") or ""
        if is_http_url(page_url):
            return page_url
        thread_id = product.get("thread_id")
        if thread_id:
            return f"https://discord.com/channels/{guild_id}/{thread_id}"
        return None

    def product_thread_mention(self, product: dict | None) -> str:
        if not product or not product.get("thread_id"):
            return "`미설정`"
        return f"<#{product['thread_id']}>"

    def build_product_embed(self, product: dict) -> discord.Embed:
        embed = info_embed(product.get("title") or "상품 정보", product.get("description") or None)
        embed.add_field(name="상품 ID", value=f"`{product.get('product_id')}`", inline=True)
        embed.add_field(name="가격", value=f"{int(product.get('price', 0)):,}원", inline=True)
        embed.add_field(name="상품 페이지", value=self.product_thread_mention(product), inline=False)
        seller_id = product.get("seller_id")
        if seller_id:
            embed.add_field(name="셀러", value=f"<@{seller_id}>", inline=True)
        return embed

    def build_download_embed(self, product: dict | None, owned: dict | None = None) -> discord.Embed:
        title = (product or owned or {}).get("title") or "상품 다운로드"
        product_id = (product or owned or {}).get("product_id") or "unknown"
        url = (product or {}).get("terabox_url") or (owned or {}).get("terabox_url") or ""
        embed = success_embed("다운로드 링크", f"상품 `{product_id}`의 전달 링크입니다.")
        embed.add_field(name=title, value=url or "저장된 링크가 없습니다.", inline=False)
        return embed

    def build_charge_embed(self, charge: dict, status_label: str | None = None) -> discord.Embed:
        status = status_label or {
            "pending": "대기 중",
            "processing": "처리 중",
            "approved": "수락됨",
            "rejected": "거절됨",
        }.get(charge.get("status"), str(charge.get("status")))
        color = 0x2ECC71 if charge.get("status") == "approved" else 0xE5484D if charge.get("status") == "rejected" else 0x5865F2
        embed = discord.Embed(title="충전 요청", color=color)
        embed.add_field(name="상태", value=status, inline=True)
        embed.add_field(name="유저", value=f"<@{charge['user_id']}> (`{charge['user_id']}`)", inline=False)
        embed.add_field(name="입금자명", value=charge.get("depositor_name") or "-", inline=True)
        embed.add_field(name="금액", value=f"{int(charge.get('amount', 0)):,}원", inline=True)
        if charge.get("reject_reason"):
            embed.add_field(name="거절 사유", value=charge["reject_reason"], inline=False)
        return embed

    async def send_charge_log(self, guild: discord.Guild, charge: dict):
        channel = await self.get_log_channel(guild)
        if not channel:
            return
        status = "성공" if charge.get("success") else "거절"
        embed = info_embed("충전 로그")
        embed.add_field(name="처리 결과", value=status, inline=True)
        embed.add_field(name="요청 유저", value=f"<@{charge['user_id']}>", inline=True)
        embed.add_field(name="입금자명", value=charge.get("depositor_name") or "-", inline=True)
        embed.add_field(name="충전 금액", value=f"{int(charge.get('amount', 0)):,}원", inline=True)
        if charge.get("reject_reason"):
            embed.add_field(name="거절 사유", value=charge["reject_reason"], inline=False)
        await channel.send(embed=embed)

    async def send_purchase_log(self, guild: discord.Guild, log_doc: dict):
        channel = await self.get_log_channel(guild)
        if not channel:
            return
        embed = info_embed("구매 로그")
        embed.add_field(name="구매 유저", value=f"<@{log_doc['user_id']}>", inline=True)
        embed.add_field(name="상품 ID", value=f"`{log_doc['product_id']}`", inline=True)
        embed.add_field(name="상품명", value=log_doc.get("title") or "-", inline=False)
        embed.add_field(name="구매 전 금액", value=f"{log_doc['before_cash']:,}원", inline=True)
        embed.add_field(name="구매 후 잔액", value=f"{log_doc['after_cash']:,}원", inline=True)
        embed.add_field(name="가격", value=f"{log_doc['price']:,}원", inline=True)
        await channel.send(embed=embed)

    async def send_user_dm(self, guild: discord.Guild, user_id: int, embed: discord.Embed):
        user = guild.get_member(user_id) or self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return
        try:
            await user.send(embed=embed)
        except discord.HTTPException:
            return

    async def edit_charge_message(self, guild: discord.Guild, charge: dict):
        channel = guild.get_channel(charge.get("admin_channel_id") or 0)
        if not channel or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(charge["admin_message_id"])
            await message.edit(embed=self.build_charge_embed(charge), view=None)
        except discord.HTTPException:
            return

    async def handle_charge_submit(self, interaction: discord.Interaction, *, depositor_name: str, amount_text: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        amount = parse_positive_amount(amount_text)
        if amount is None:
            await interaction.followup.send(embed=error_embed("금액 오류", "1원 이상의 숫자로 입력해주세요."), ephemeral=True)
            return
        admin_channel = await self.get_admin_channel(interaction.guild)
        if admin_channel is None:
            await interaction.followup.send(
                embed=error_embed("관리자 채널 미설정", "`/채널설정`으로 자판기 관리자 채널을 먼저 설정해주세요."),
                ephemeral=True,
            )
            return

        charge = await self.repos.vending.create_charge_request(
            interaction.guild.id,
            interaction.user.id,
            depositor_name,
            amount,
        )
        message = await admin_channel.send(embed=self.build_charge_embed(charge), view=ChargeAdminView(self))
        await self.repos.vending.attach_charge_message(charge["_id"], admin_channel.id, message.id)

        await interaction.followup.send(
            embed=success_embed("충전 신청 완료", "관리자가 입금을 확인하면 DM으로 결과를 알려드립니다."),
            ephemeral=True,
        )

    async def handle_approve_charge(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild or interaction.message is None:
            await interaction.followup.send(embed=error_embed("처리 실패", "요청 메시지를 찾을 수 없습니다."), ephemeral=True)
            return
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return

        charge = await self.repos.vending.claim_charge_request(
            interaction.guild.id,
            interaction.message.id,
            interaction.user.id,
        )
        if charge is None:
            await interaction.followup.send(embed=error_embed("이미 처리됨", "이미 처리된 충전 요청입니다."), ephemeral=True)
            return

        await self.repos.users.add_cash(interaction.guild.id, charge["user_id"], int(charge["amount"]))
        charge = await self.repos.vending.approve_charge_request(charge["_id"], interaction.user.id)
        await self.edit_charge_message(interaction.guild, charge)
        await self.send_charge_log(interaction.guild, charge)
        await self.send_user_dm(
            interaction.guild,
            charge["user_id"],
            success_embed("충전 수락", f"{int(charge['amount']):,}원이 충전되었습니다."),
        )
        await interaction.followup.send(embed=success_embed("충전 수락 완료"), ephemeral=True)

    async def handle_reject_charge(self, interaction: discord.Interaction, admin_message_id: int, reason: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return

        charge = await self.repos.vending.reject_charge_request(
            interaction.guild.id,
            admin_message_id,
            interaction.user.id,
            reason,
        )
        if charge is None:
            await interaction.followup.send(embed=error_embed("이미 처리됨", "이미 처리된 충전 요청입니다."), ephemeral=True)
            return

        await self.edit_charge_message(interaction.guild, charge)
        await self.send_charge_log(interaction.guild, charge)
        await self.send_user_dm(
            interaction.guild,
            charge["user_id"],
            error_embed("충전 거절", f"사유: {charge.get('reject_reason') or '사유 없음'}"),
        )
        await interaction.followup.send(embed=success_embed("충전 거절 완료"), ephemeral=True)

    async def handle_purchase(self, interaction: discord.Interaction, product_id: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        product_id = product_id.strip()
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "해당 상품 ID를 찾을 수 없습니다."), ephemeral=True)
            return

        if await self.repos.vending.owns_product(interaction.guild.id, interaction.user.id, product_id):
            await interaction.followup.send(embed=self.build_download_embed(product), ephemeral=True)
            return

        reserved = await self.repos.vending.reserve_product(interaction.guild.id, interaction.user.id, product)
        if not reserved:
            if await self.repos.vending.owns_product(interaction.guild.id, interaction.user.id, product_id):
                await interaction.followup.send(embed=self.build_download_embed(product), ephemeral=True)
            else:
                await interaction.followup.send(
                    embed=error_embed("처리 중", "이미 구매 처리가 진행 중인 상품입니다. 잠시 후 다시 시도해주세요."),
                    ephemeral=True,
                )
            return

        price = int(product.get("price", 0))
        spent = await self.repos.users.spend_cash(interaction.guild.id, interaction.user.id, price)
        if spent is None:
            await self.repos.vending.release_product_reservation(interaction.guild.id, interaction.user.id, product_id)
            user = await self.repos.users.ensure_user(interaction.guild.id, interaction.user.id)
            await interaction.followup.send(
                embed=error_embed(
                    "잔액 부족",
                    f"현재 잔액은 {int(user.get('cash', 0)):,}원이고, 상품 가격은 {price:,}원입니다.",
                ),
                ephemeral=True,
            )
            return

        log_doc = await self.repos.vending.record_purchase(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            product=product,
            before_cash=spent["before_cash"],
            after_cash=spent["after_cash"],
        )
        if product.get("seller_id"):
            await self.repos.sellers.add_sale(interaction.guild.id, product["seller_id"], price)

        purchase_cog = self.bot.get_cog("PurchaseCog")
        if purchase_cog is not None and isinstance(interaction.user, discord.Member):
            await purchase_cog.upgrade_user_grade(
                interaction.guild,
                interaction.user,
                spent["user"].get("accrued_spent", 0),
            )

        await self.send_purchase_log(interaction.guild, log_doc)
        await interaction.followup.send(embed=self.build_download_embed(product), ephemeral=True)

    async def handle_download_menu(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        owned = await self.repos.vending.list_owned_products(interaction.guild.id, interaction.user.id)
        if not owned:
            await interaction.followup.send(embed=error_embed("구매 내역 없음", "아직 구매한 상품이 없습니다."), ephemeral=True)
            return
        await interaction.followup.send(
            embed=info_embed("다운로드", "링크를 다시 받을 상품을 선택하세요."),
            view=DownloadSelectView(self, owned),
            ephemeral=True,
        )

    async def handle_download_selected(self, interaction: discord.Interaction, product_id_lower: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        owned = await self.repos.vending.user_products.find_one(
            {
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "product_id_lower": product_id_lower,
                "status": "purchased",
            }
        )
        if owned is None:
            await interaction.followup.send(embed=error_embed("권한 없음", "구매한 상품만 다운로드할 수 있습니다."), ephemeral=True)
            return
        product = await self.repos.products.get(interaction.guild.id, owned["product_id"], include_inactive=True)
        await interaction.followup.send(embed=self.build_download_embed(product, owned), ephemeral=True)

    async def handle_archive_search(self, interaction: discord.Interaction, youtube_url: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        normalized = normalize_youtube_url(youtube_url)
        if normalized is None:
            await interaction.followup.send(embed=error_embed("URL 오류", "유튜브 영상 URL을 입력해주세요."), ephemeral=True)
            return

        video_key, canonical_url = normalized
        archive = await self.repos.archives.find(interaction.guild.id, video_key)
        if archive is None:
            await interaction.followup.send(
                embed=error_embed("아카이브 없음", "해당 영상은 아카이브에 존재하지 않습니다."),
                ephemeral=True,
            )
            return

        product = await self.repos.products.get(interaction.guild.id, archive["product_id"], include_inactive=True)
        description = archive.get("summary") or (product or {}).get("description") or "등록된 요약이 없습니다."
        embed = info_embed("아카이브 검색 결과", description)
        embed.add_field(name="검색 URL", value=canonical_url, inline=False)
        embed.add_field(name="상품 ID", value=f"`{archive['product_id']}`", inline=True)
        embed.add_field(name="상품 쓰레드", value=self.product_thread_mention(product), inline=True)
        if product:
            embed.add_field(name="가격", value=f"{int(product.get('price', 0)):,}원", inline=True)

        await interaction.followup.send(
            embed=embed,
            view=ArchiveResultView(self, archive["product_id"], self.product_page_url(interaction.guild.id, product)),
            ephemeral=True,
        )

    @tasks.loop(seconds=1, count=1)
    async def restore_panel_loop(self):
        for guild in self.bot.guilds:
            await restore_panel_message(
                self.repos,
                guild,
                "vending",
                "vending_panel_message_id",
                embed=self.vending_panel_embed(),
                view=VendingPanelView(self),
            )
            await restore_panel_message(
                self.repos,
                guild,
                "archive",
                "archive_panel_message_id",
                embed=info_embed("ARCHIVE", "유튜브 영상 URL로 사용된 상품을 검색할 수 있습니다."),
                view=ArchivePanelView(self),
            )

    @restore_panel_loop.before_loop
    async def before_restore_panel_loop(self):
        await self.bot.wait_until_ready()

    def vending_panel_embed(self) -> discord.Embed:
        bank_account = os.getenv("VENDING_BANK_ACCOUNT", "").strip()
        description = "충전, 상품 구매, 구매한 상품 다운로드를 이용할 수 있습니다."
        if bank_account:
            description += f"\n입금 계좌: `{bank_account}`"
        return info_embed("VENDING MACHINE", description)

    @app_commands.command(name="자판기패널", description="현재 채널에 자판기 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def vending_panel(self, interaction: discord.Interaction):
        message = await interaction.channel.send(embed=self.vending_panel_embed(), view=VendingPanelView(self))
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "vending",
            "vending_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("자판기 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="아카이브패널", description="현재 채널에 아카이브 검색 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def archive_panel(self, interaction: discord.Interaction):
        message = await interaction.channel.send(
            embed=info_embed("ARCHIVE", "유튜브 영상 URL로 사용된 상품을 검색할 수 있습니다."),
            view=ArchivePanelView(self),
        )
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "archive",
            "archive_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("아카이브 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="아카이브검색", description="유튜브 URL로 아카이브를 검색합니다.")
    @app_commands.default_permissions(send_messages=True)
    async def archive_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ArchiveSearchModal(self))

    @app_commands.command(name="상품등록", description="자판기 상품을 등록하거나 수정합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(
        product_id="자판기에서 사용할 상품 ID",
        price="상품 가격",
        terabox_url="구매자에게 지급할 테라박스 링크",
        title="상품명",
        description="상품 설명 요약",
        thread_id="상품 설명 쓰레드 ID 또는 멘션",
        page_url="상품 설명 페이지 URL",
        seller="상품 셀러",
    )
    async def register_product(
        self,
        interaction: discord.Interaction,
        product_id: str,
        price: int,
        terabox_url: str,
        title: str,
        description: str = "",
        thread_id: str = "",
        page_url: str = "",
        seller: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        if not product_id.strip() or len(product_id.strip()) > 64:
            await interaction.followup.send(embed=error_embed("상품 ID 오류", "상품 ID는 1~64자로 입력해주세요."), ephemeral=True)
            return
        if price < 0:
            await interaction.followup.send(embed=error_embed("가격 오류", "가격은 0원 이상이어야 합니다."), ephemeral=True)
            return
        if not is_http_url(terabox_url):
            await interaction.followup.send(embed=error_embed("링크 오류", "테라박스 링크는 http 또는 https URL이어야 합니다."), ephemeral=True)
            return
        if page_url and not is_http_url(page_url):
            await interaction.followup.send(embed=error_embed("페이지 URL 오류", "상품 페이지 URL은 http 또는 https URL이어야 합니다."), ephemeral=True)
            return

        is_admin = await self.admin_allowed(interaction)
        if seller is not None and not is_admin and seller.id != interaction.user.id:
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러는 본인 상품만 등록할 수 있습니다."), ephemeral=True)
            return

        seller_member = seller
        if seller_member is None and not is_admin and isinstance(interaction.user, discord.Member):
            seller_member = interaction.user
        seller_id = seller_member.id if seller_member else None
        if seller_member:
            await self.repos.sellers.upsert(interaction.guild.id, seller_member.id, seller_member.display_name)

        product = await self.repos.products.upsert(
            interaction.guild.id,
            product_id,
            title=title,
            price=price,
            terabox_url=terabox_url,
            description=description,
            seller_id=seller_id,
            thread_id=parse_discord_id(thread_id),
            page_url=page_url,
            created_by=interaction.user.id,
        )
        await interaction.followup.send(embed=success_embed("상품 등록 완료", f"`{product['product_id']}`"), ephemeral=True)

    @app_commands.command(name="상품삭제", description="자판기 상품을 비활성화합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(product_id="비활성화할 상품 ID")
    async def delete_product(self, interaction: discord.Interaction, product_id: str):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        product = await self.repos.products.get(interaction.guild.id, product_id, include_inactive=True)
        if product is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "해당 상품 ID를 찾을 수 없습니다."), ephemeral=True)
            return
        if not await self.admin_allowed(interaction) and product.get("seller_id") != interaction.user.id:
            await interaction.followup.send(embed=error_embed("권한 없음", "본인 상품만 삭제할 수 있습니다."), ephemeral=True)
            return
        deleted = await self.repos.products.deactivate(interaction.guild.id, product_id, interaction.user.id)
        if not deleted:
            await interaction.followup.send(embed=error_embed("처리 실패", "이미 비활성화된 상품입니다."), ephemeral=True)
            return
        await interaction.followup.send(embed=success_embed("상품 삭제 완료", f"`{product['product_id']}`"), ephemeral=True)

    @app_commands.command(name="상품조회", description="상품 ID로 자판기 상품 정보를 조회합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(product_id="조회할 상품 ID")
    async def product_info(self, interaction: discord.Interaction, product_id: str):
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is None:
            await interaction.response.send_message(embed=error_embed("상품 없음", "해당 상품 ID를 찾을 수 없습니다."), ephemeral=True)
            return
        await interaction.response.send_message(embed=self.build_product_embed(product), ephemeral=True)

    @app_commands.command(name="아카이브추가", description="유튜브 URL과 상품 ID를 아카이브에 연결합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(
        url="유튜브 영상 URL",
        product_id="연결할 상품 ID",
        summary="검색 결과에 보여줄 간단한 요약",
    )
    async def add_archive(self, interaction: discord.Interaction, url: str, product_id: str, summary: str = ""):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        normalized = normalize_youtube_url(url)
        if normalized is None:
            await interaction.followup.send(embed=error_embed("URL 오류", "유튜브 영상 URL을 입력해주세요."), ephemeral=True)
            return
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "먼저 `/상품등록`으로 상품을 등록해주세요."), ephemeral=True)
            return
        if not await self.admin_allowed(interaction) and product.get("seller_id") != interaction.user.id:
            await interaction.followup.send(embed=error_embed("권한 없음", "본인 상품만 아카이브에 연결할 수 있습니다."), ephemeral=True)
            return

        video_key, canonical_url = normalized
        archive = await self.repos.archives.upsert(
            interaction.guild.id,
            youtube_url=canonical_url,
            video_key=video_key,
            product_id=product["product_id"],
            summary=summary,
            created_by=interaction.user.id,
        )
        await interaction.followup.send(
            embed=success_embed("아카이브 등록 완료", f"{archive['youtube_url']} -> `{archive['product_id']}`"),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(VendingArchiveCog(bot))
