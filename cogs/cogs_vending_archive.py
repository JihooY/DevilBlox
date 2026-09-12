from __future__ import annotations

import io
import re
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.vending_views import (
    ArchivePanelView,
    ArchiveResultView,
    ArchiveSearchModal,
    CategoryMenuView,
    CategorySelect,
    ChargeAdminView,
    ChargeRequestModal,
    COLOR_ARCHIVE,
    COLOR_VENDING,
    DiscountMenuView,
    DownloadSelect,
    DownloadSelectView,
    ProductDetailView,
    ProductMenuView,
    ProductPurchaseModal,
    ProductSelect,
    PromotionCodeModal,
    RejectChargeModal,
    SELECT_OPTION_LIMIT,
    VendingCouponSelect,
    VendingPanelView,
    VendingStockPanelView,
    add_brand_section,
    add_panel_gif,
    chunked,
)
from database.vending import (
    ArchiveStore,
    ProductCategoryStore,
    ProductStore,
    VendingLogStore,
    VendingStockUnitStore,
    normalize_product_id,
)
from services.vending import VendingCommerceService
from services.vending_topup import TopupPendingError
from utils.embeds import (
    BRAND_LOGO_FILENAME,
    branded_files,
    error_embed,
    info_embed,
    success_embed,
)
from utils.gifs import (
    ARCHIVE_PANEL_GIFS,
    DENIED_GIFS,
    SUCCESS_GIFS,
    VENDING_PANEL_GIFS,
    choose_gif,
    gif_delivery_status,
    gif_file,
    is_gif_filename,
    message_media_urls,
    random_embed_gif_kwargs,
    retained_non_gif_attachments,
)
from utils.panels import save_panel_location
from utils.roles import has_role


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
MAX_CHARGE_PROOF_BYTES = 8 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


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


def safe_attachment_filename(attachment: discord.Attachment) -> str:
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", attachment.filename or "deposit-proof.png")
    if "." not in filename:
        filename += ".png"
    return filename[:80]


def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").casefold()
    if content_type.startswith("image/"):
        return True
    filename = (attachment.filename or "").casefold()
    return any(filename.endswith(extension) for extension in IMAGE_EXTENSIONS)


def attachment_image_url(filename: str) -> str:
    return f"attachment://{filename}"


def message_attachment_url(message: discord.Message, filename: str) -> str:
    return next(
        (
            attachment.url
            for attachment in message.attachments
            if attachment.filename == filename
        ),
        "",
    )


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


class VendingArchiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.commerce = VendingCommerceService(bot.repos)
        self.bot.add_view(VendingPanelView(self))
        self.bot.add_view(ArchivePanelView(self))
        self.bot.add_view(ChargeAdminView(self))
        self.bot.add_view(VendingStockPanelView(self, [], {}))

    async def cog_load(self):
        await self.ensure_vending_stores()
        self.restore_panel_loop.start()

    async def cog_unload(self):
        self.restore_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def ensure_vending_stores(self):
        repos = self.repos
        db = getattr(self.bot, "db", None)
        if repos is None or db is None:
            return

        missing_stores = []
        if not hasattr(repos, "product_categories"):
            repos.product_categories = ProductCategoryStore(db)
            missing_stores.append(repos.product_categories)
        if not hasattr(repos, "products"):
            repos.products = ProductStore(db)
            missing_stores.append(repos.products)
        if not hasattr(repos, "archives"):
            repos.archives = ArchiveStore(db)
            missing_stores.append(repos.archives)
        if not hasattr(repos, "vending"):
            repos.vending = VendingLogStore(db)
            missing_stores.append(repos.vending)
        if not hasattr(repos, "vending_stock"):
            repos.vending_stock = VendingStockUnitStore(db)
            missing_stores.append(repos.vending_stock)

        for store in missing_stores:
            await store.ensure_indexes()

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

    async def get_admin_channel(self, guild: discord.Guild, *, forbidden_channel_id: int | None = None):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("vending_admin")
        if channel_id and channel_id in {settings["channels"].get("vending"), forbidden_channel_id}:
            return None
        return guild.get_channel(channel_id or 0)

    async def get_log_channel(self, guild: discord.Guild, *, forbidden_channel_id: int | None = None):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("vending_log")
        if channel_id and channel_id in {settings["channels"].get("vending"), forbidden_channel_id}:
            return None
        return guild.get_channel(channel_id or 0)

    async def get_purchase_log_channel(self, guild: discord.Guild):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("purchase_log")
        if channel_id and channel_id == settings["channels"].get("vending"):
            return None
        return guild.get_channel(channel_id or 0)

    async def get_restock_channel(self, guild: discord.Guild):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("vending_restock")
        return guild.get_channel(channel_id) if channel_id else None

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

    async def build_product_embed(self, guild_id: int, product: dict) -> discord.Embed:
        embed = info_embed(product.get("title") or "상품 정보", product.get("description") or None)
        embed.add_field(name="상품 ID", value=f"`{product.get('product_id')}`", inline=True)
        embed.add_field(name="가격", value=f"{int(product.get('price', 0)):,}원", inline=True)
        embed.add_field(name="상품 페이지", value=self.product_thread_mention(product), inline=False)
        if product.get("product_type") == "stock":
            count = await self.repos.vending_stock.count(guild_id, product["product_id"])
            embed.add_field(name="재고", value=f"{count}개" + (" (품절)" if count <= 0 else ""), inline=True)
        if product.get("topup_enabled"):
            if product.get("topup_kind", "tokens") == "plan":
                delivery = f"{product.get('topup_plan', '').upper()} 플랜 {int(product.get('topup_months', 0))}개월"
            else:
                delivery = f"{int(product.get('topup_tokens', 0)):,} 토큰"
            embed.add_field(name="API 지급", value=delivery, inline=True)
        if await self.commerce.discount_blocked_for(guild_id, product):
            embed.add_field(name="쿠폰/프로모션", value="사용 불가", inline=True)
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

    def build_downloads_embed(self, items: list[tuple[dict | None, dict]]) -> discord.Embed:
        embed = success_embed("다운로드 링크", "선택한 상품의 전달 링크입니다.")
        for product, owned in items[:25]:
            source = product or owned
            product_id = source.get("product_id") or "unknown"
            title = source.get("title") or product_id
            url = (product or {}).get("terabox_url") or owned.get("terabox_url") or "저장된 링크가 없습니다."
            embed.add_field(name=f"{title} (`{product_id}`)", value=url, inline=False)
        return embed

    def build_stock_unit_embed(self, product: dict, unit: dict | None) -> discord.Embed:
        title = product.get("title") or product.get("product_id") or "상품"
        content = (unit or {}).get("content") or "저장된 내용이 없습니다."
        embed = success_embed(title, f"상품 `{product.get('product_id')}`의 재고 내용입니다.")
        embed.add_field(name=title, value=content, inline=False)
        return embed

    async def vending_panel_stats(self, guild_id: int) -> dict:
        categories = await self.repos.product_categories.list_active(guild_id, limit=25)
        products = await self.repos.products.list_active(guild_id, limit=25)
        return {"category_count": len(categories), "product_count": len(products)}

    async def build_vending_panel_view(self, guild_id: int, gif_name: str | None = None) -> VendingPanelView:
        return VendingPanelView(self, stats=await self.vending_panel_stats(guild_id), gif_name=gif_name)

    async def build_tracked_panel_view(
        self,
        guild_id: int,
        channel_key: str | None,
        gif_name: str | None = None,
    ) -> discord.ui.LayoutView | None:
        if channel_key == "vending":
            return await self.build_vending_panel_view(guild_id, gif_name)
        if channel_key == "archive":
            return ArchivePanelView(self, gif_name=gif_name)
        if channel_key == "vending_stock":
            return await self.build_stock_panel_view(guild_id)
        return None

    async def refresh_vending_panel(self, guild: discord.Guild, *, rotate_image: bool = False):
        stats = await self.vending_panel_stats(guild.id)
        await self.restore_v2_panel_message(
            guild,
            "vending",
            "vending_panel_message_id",
            lambda gif_name: VendingPanelView(self, stats=stats, gif_name=gif_name),
            image_attachment_filename=VENDING_PANEL_GIFS,
            rotate_image=rotate_image,
        )

    def build_charge_view(
        self,
        charge: dict,
        *,
        image_url: str | None = None,
        public: bool = False,
        title: str = "충전 요청",
        show_actions: bool = True,
    ) -> ChargeAdminView:
        return ChargeAdminView(
            self,
            charge,
            image_url=image_url,
            public=public,
            title=title,
            show_actions=show_actions,
        )

    async def send_charge_log(self, guild: discord.Guild, charge: dict):
        channel = await self.get_log_channel(guild)
        if not channel:
            return
        status = "성공" if charge.get("success") else "거절"
        embed = info_embed("충전 로그")
        embed.add_field(name="처리 결과", value=status, inline=True)
        embed.add_field(name="요청 유저", value=f"<@{charge['user_id']}>", inline=True)
        if charge.get("processed_by"):
            embed.add_field(name="처리 관리자", value=f"<@{charge['processed_by']}>", inline=True)
        embed.add_field(name="입금자명", value=charge.get("depositor_name") or "-", inline=True)
        embed.add_field(name="충전 금액", value=f"{int(charge.get('amount', 0)):,}원", inline=True)
        if charge.get("reject_reason"):
            embed.add_field(name="거절 사유", value=charge["reject_reason"], inline=False)
        proof_url = charge.get("admin_proof_url") or charge.get("request_proof_url") or charge.get("log_proof_url")
        if proof_url:
            embed.set_image(url=proof_url)
            await channel.send(embed=embed)
            return
        gif_pool = SUCCESS_GIFS if charge.get("success") else DENIED_GIFS
        await channel.send(**random_embed_gif_kwargs(embed, gif_pool))

    async def send_charge_request_log(
        self,
        guild: discord.Guild,
        charge: dict,
        *,
        image_url: str | None = None,
        file: discord.File | None = None,
    ):
        channel = await self.get_log_channel(guild)
        if not channel:
            return None
        kwargs = {
            "view": self.build_charge_view(
                charge,
                image_url=image_url,
                title="충전 요청 로그",
                show_actions=False,
            ),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        files = branded_files(file)
        if files:
            kwargs["files"] = files
        return await channel.send(**kwargs)

    async def send_purchase_log(self, guild: discord.Guild, log_doc: dict):
        buyer_mention = f"<@{log_doc['user_id']}>"
        product_title = log_doc.get("title") or "-"
        price = int(log_doc.get("price", 0))
        time_value = None
        purchased_at = log_doc.get("purchased_at")
        if hasattr(purchased_at, "timestamp"):
            timestamp = int(purchased_at.timestamp())
            time_value = f"<t:{timestamp}:F> (<t:{timestamp}:R>)"

        vending_log_channel = await self.get_log_channel(guild)
        if vending_log_channel:
            embed = info_embed("VENDING PURCHASE LOG")
            embed.add_field(name="구매 유저", value=buyer_mention, inline=True)
            embed.add_field(name="상품 ID", value=f"`{log_doc['product_id']}`", inline=True)
            embed.add_field(name="상품명", value=product_title, inline=False)
            embed.add_field(name="가격", value=f"{price:,}원", inline=True)
            if time_value:
                embed.add_field(name="구매 시간", value=time_value, inline=False)
            await vending_log_channel.send(**random_embed_gif_kwargs(embed, SUCCESS_GIFS))

        purchase_log_channel = await self.get_purchase_log_channel(guild)
        if purchase_log_channel and (not vending_log_channel or purchase_log_channel.id != vending_log_channel.id):
            embed = info_embed("PURCHASE LOG", f"{buyer_mention}님 {product_title} 구매 감사합니다!")
            embed.add_field(name="구매 상품", value=product_title, inline=True)
            if time_value:
                embed.add_field(name="구매 시간", value=time_value, inline=False)
            await purchase_log_channel.send(**random_embed_gif_kwargs(embed, SUCCESS_GIFS))

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

    async def edit_charge_message(
        self,
        guild: discord.Guild,
        charge: dict,
        *,
        channel_id: int | None,
        message_id: int | None,
        image_url: str | None = None,
        public: bool = False,
        title: str = "충전 요청",
        show_actions: bool = True,
    ):
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            update = {
                "content": None,
                "embeds": [],
                "view": self.build_charge_view(
                    charge,
                    image_url=image_url,
                    public=public,
                    title=title,
                    show_actions=show_actions,
                ),
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if not any(
                attachment.filename == BRAND_LOGO_FILENAME
                for attachment in message.attachments
            ):
                logo_files = branded_files()
                if logo_files:
                    update["attachments"] = [*logo_files, *message.attachments]
            await message.edit(**update)
        except discord.HTTPException:
            return

    async def delete_charge_message(self, guild: discord.Guild, channel_id: int | None, message_id: int | None):
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.HTTPException:
            return

    async def edit_charge_messages(self, guild: discord.Guild, charge: dict):
        admin_target = (charge.get("admin_channel_id"), charge.get("admin_message_id"))
        log_target = (charge.get("log_channel_id"), charge.get("log_message_id"))
        request_target = (charge.get("request_channel_id"), charge.get("request_message_id"))

        if request_target[0] and request_target[1] and request_target not in {admin_target, log_target}:
            await self.delete_charge_message(guild, request_target[0], request_target[1])

        seen: set[tuple[int, int]] = set()
        targets = [
            (
                charge.get("admin_channel_id"),
                charge.get("admin_message_id"),
                charge.get("admin_proof_url"),
                False,
                "충전 요청",
                True,
            ),
            (
                charge.get("log_channel_id"),
                charge.get("log_message_id"),
                charge.get("log_proof_url"),
                False,
                "충전 요청 로그",
                False,
            ),
        ]
        for channel_id, message_id, image_url, public, title, show_actions in targets:
            if not channel_id or not message_id:
                continue
            key = (int(channel_id), int(message_id))
            if key in seen:
                continue
            seen.add(key)
            await self.edit_charge_message(
                guild,
                charge,
                channel_id=int(channel_id),
                message_id=int(message_id),
                image_url=image_url,
                public=public,
                title=title,
                show_actions=show_actions,
            )

    async def handle_charge_submit(
        self,
        interaction: discord.Interaction,
        *,
        depositor_name: str,
        amount_text: str,
        proof: discord.Attachment | None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        amount = parse_positive_amount(amount_text)
        if amount is None:
            await interaction.followup.send(embed=error_embed("금액 오류", "1원 이상의 숫자로 입력해주세요."), ephemeral=True)
            return
        if proof is None:
            await interaction.followup.send(embed=error_embed("입금 사진 없음", "입금 확인 사진을 1장 업로드해주세요."), ephemeral=True)
            return
        if not is_image_attachment(proof):
            await interaction.followup.send(embed=error_embed("파일 오류", "입금 사진은 이미지 파일만 업로드할 수 있습니다."), ephemeral=True)
            return
        if proof.size > MAX_CHARGE_PROOF_BYTES:
            await interaction.followup.send(
                embed=error_embed("파일 용량 오류", "입금 사진은 8MB 이하로 업로드해주세요."),
                ephemeral=True,
            )
            return
        admin_channel = await self.get_admin_channel(interaction.guild, forbidden_channel_id=interaction.channel_id)
        if admin_channel is None:
            await interaction.followup.send(
                embed=error_embed(
                    "관리자 채널 미설정",
                    "`/채널설정`으로 자판기 관리자 채널을 패널 채널과 다른 채널로 설정해주세요.",
                ),
                ephemeral=True,
            )
            return

        proof_filename = safe_attachment_filename(proof)
        try:
            proof_bytes = await proof.read()
        except discord.HTTPException:
            await interaction.followup.send(embed=error_embed("파일 처리 실패", "입금 사진을 읽지 못했습니다. 다시 시도해주세요."), ephemeral=True)
            return

        charge = await self.repos.vending.create_charge_request(
            interaction.guild.id,
            interaction.user.id,
            depositor_name,
            amount,
            proof_filename=proof_filename,
            proof_content_type=proof.content_type or "",
            proof_size=proof.size,
        )

        admin_file = discord.File(io.BytesIO(proof_bytes), filename=proof_filename)
        admin_files = branded_files(admin_file)
        admin_message = await admin_channel.send(
            view=self.build_charge_view(
                charge,
                image_url=attachment_image_url(proof_filename),
            ),
            files=admin_files,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        admin_proof_url = message_attachment_url(admin_message, proof_filename)

        request_channel = admin_channel
        request_message = admin_message
        request_proof_url = admin_proof_url

        log_channel = await self.get_log_channel(interaction.guild, forbidden_channel_id=interaction.channel_id)
        log_message = None
        log_proof_url = ""
        if log_channel is not None and log_channel.id != admin_channel.id:
            log_file = discord.File(io.BytesIO(proof_bytes), filename=proof_filename)
            try:
                log_message = await self.send_charge_request_log(
                    interaction.guild,
                    charge,
                    image_url=attachment_image_url(proof_filename),
                    file=log_file,
                )
                log_proof_url = message_attachment_url(log_message, proof_filename) if log_message else ""
            except discord.HTTPException:
                log_message = None
                log_proof_url = ""

        charge = await self.repos.vending.attach_charge_message(
            charge["_id"],
            admin_channel.id,
            admin_message.id,
            request_channel_id=request_channel.id if request_channel is not None else None,
            request_message_id=request_message.id if request_message is not None else None,
            log_channel_id=log_channel.id if log_channel is not None and log_message is not None else None,
            log_message_id=log_message.id if log_message is not None else None,
            admin_proof_url=admin_proof_url,
            request_proof_url=request_proof_url,
            log_proof_url=log_proof_url,
        )

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

        result = await self.commerce.approve_charge(
            interaction.guild.id,
            interaction.message.id,
            interaction.user.id,
        )
        if result.status != "approved" or result.charge is None:
            await interaction.followup.send(embed=error_embed("이미 처리됨", "이미 처리된 충전 요청입니다."), ephemeral=True)
            return

        charge = result.charge
        if not result.newly_completed:
            await self.edit_charge_messages(interaction.guild, charge)
            await interaction.followup.send(
                embed=success_embed("충전 처리 완료", "이미 안전하게 승인된 요청입니다."),
                ephemeral=True,
            )
            return
        await self.edit_charge_messages(interaction.guild, charge)
        await self.send_charge_log(interaction.guild, charge)
        await self.send_user_dm(
            interaction.guild,
            charge["user_id"],
            success_embed("충전 수락", f"{int(charge['amount']):,}원이 충전되었습니다.\n승인 관리자: {interaction.user.mention}"),
        )
        embed = success_embed("충전 수락 완료")
        await interaction.followup.send(**random_embed_gif_kwargs(embed, SUCCESS_GIFS), ephemeral=True)

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

        await self.edit_charge_messages(interaction.guild, charge)
        await self.send_charge_log(interaction.guild, charge)
        await self.send_user_dm(
            interaction.guild,
            charge["user_id"],
            error_embed("충전 거절", f"사유: {charge.get('reject_reason') or '사유 없음'}\n거절 관리자: {interaction.user.mention}"),
        )
        embed = success_embed("충전 거절 완료")
        await interaction.followup.send(**random_embed_gif_kwargs(embed, DENIED_GIFS), ephemeral=True)

    async def handle_category_menu(self, interaction: discord.Interaction, mode: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        categories = await self.repos.product_categories.list_active(interaction.guild.id)
        if not categories:
            await interaction.followup.send(
                embed=error_embed("카테고리 없음", "`/상품카테고리등록`으로 카테고리를 먼저 등록해주세요."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(view=CategoryMenuView(self, categories, mode), files=branded_files(), ephemeral=True)

    async def handle_category_selected(self, interaction: discord.Interaction, category_id_lower: str, mode: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        category = await self.repos.product_categories.get(interaction.guild.id, category_id_lower)
        if category is None:
            await interaction.followup.send(embed=error_embed("카테고리 없음", "선택한 카테고리를 찾을 수 없습니다."), ephemeral=True)
            return
        products = await self.repos.products.list_by_category(interaction.guild.id, category["category_id"])
        if not products:
            await interaction.followup.send(
                embed=error_embed("상품 없음", "이 카테고리에 등록된 판매 상품이 없습니다."),
                ephemeral=True,
            )
            return
        stock_counts = await self.stock_counts_for(interaction.guild.id, products)
        await interaction.followup.send(
            view=ProductMenuView(self, category, products, mode, stock_counts=stock_counts),
            files=branded_files(),
            ephemeral=True,
        )

    async def stock_counts_for(self, guild_id: int, products: list[dict]) -> dict[str, int]:
        product_ids_lower = [
            str(product.get("product_id_lower") or normalize_product_id(product["product_id"]))
            for product in products
            if product.get("product_type") == "stock"
        ]
        return await self.repos.vending_stock.count_for_products(guild_id, product_ids_lower)

    async def handle_product_selected(self, interaction: discord.Interaction, product_id_lower: str, mode: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        product = await self.repos.products.get(interaction.guild.id, product_id_lower)
        if product is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "선택한 상품을 찾을 수 없습니다."), ephemeral=True)
            return
        owned = await self.repos.vending.owns_product(interaction.guild.id, interaction.user.id, product["product_id"])
        if mode == "catalog":
            await interaction.followup.send(embed=await self.build_product_embed(interaction.guild.id, product), ephemeral=True)
            return
        discount_blocked = await self.commerce.discount_blocked_for(interaction.guild.id, product)
        if discount_blocked:
            discounted_price, coupon, promotion = int(product.get("price", 0)), None, None
        else:
            discounted_price, coupon, promotion = await self.repos.coupons.quote(
                interaction.guild.id, interaction.user.id,
                f"vending:{normalize_product_id(product['product_id'])}", int(product.get("price", 0)),
            )
        stock_count = None
        if product.get("product_type") == "stock":
            stock_count = await self.repos.vending_stock.count(interaction.guild.id, product["product_id"])
        await interaction.followup.send(
            view=ProductDetailView(self, product, owned=owned, discounted_price=discounted_price,
                                   applied=coupon or promotion, stock_count=stock_count,
                                   discount_blocked=discount_blocked),
            files=branded_files(),
            ephemeral=True,
        )

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

        try:
            result = await self.commerce.purchase(
                interaction.guild.id,
                interaction.user.id,
                product,
            )
        except TopupPendingError:
            await interaction.followup.send(
                embed=error_embed("지급 확인 대기", "결제한 주문의 지급 완료를 확인하지 못했습니다. 같은 상품의 구매 버튼을 다시 누르면 기존 주문번호로 재시도합니다. 추가 결제되지 않습니다."),
                ephemeral=True,
            )
            return
        product = result.product
        if result.status == "already_owned":
            await interaction.followup.send(embed=self.build_download_embed(product), ephemeral=True)
            return
        if result.status == "insufficient_funds":
            await interaction.followup.send(
                embed=error_embed(
                    "잔액 부족",
                    f"현재 잔액은 {result.current_cash:,}원이고, 상품 가격은 {result.price:,}원입니다.",
                ),
                ephemeral=True,
            )
            return
        if result.status == "out_of_stock":
            await interaction.followup.send(
                embed=error_embed("품절", "현재 재고가 없습니다. 입고 후 다시 시도해주세요."),
                ephemeral=True,
            )
            return

        log_doc = result.log
        spent = result.spent
        if log_doc is None or spent is None:
            raise RuntimeError("completed purchase is missing its persisted result")
        price = result.price
        original_price = result.original_price
        applied_code = result.applied_code

        if result.newly_completed and applied_code:
            coupon_cog = self.bot.get_cog("CouponCog")
            if coupon_cog is not None:
                is_promotion = result.discount_kind == "promotion"
                await coupon_cog.send_coupon_log(
                    interaction.guild,
                    "PROMOTION USED" if is_promotion else "VENDING COUPON USED",
                    f"{interaction.user.mention}님이 {'프로모션 코드' if is_promotion else '일반 쿠폰'}를 사용했습니다.",
                    사용자=f"{interaction.user.mention} (`{interaction.user.id}`)",
                    코드=f"`{applied_code}`",
                    상품=f"{product.get('title') or product_id} (`{product_id}`)",
                    원래_가격=f"{original_price:,}원",
                    할인_금액=f"{original_price - price:,}원",
                    결제_가격=f"{price:,}원",
                )

        purchase_cog = self.bot.get_cog("PurchaseCog")
        if result.newly_completed and purchase_cog is not None and isinstance(interaction.user, discord.Member):
            await purchase_cog.upgrade_user_grade(
                interaction.guild,
                interaction.user,
                spent["user"].get("accrued_spent", 0),
            )

        if result.newly_completed:
            await self.send_purchase_log(interaction.guild, log_doc)
        reviews_cog = self.bot.get_cog("ReviewsCog")
        if result.newly_completed and reviews_cog is not None:
            category = None
            if product.get("category_id"):
                category = await self.repos.product_categories.get(interaction.guild.id, product["category_id"])
            await reviews_cog.request_review(
                guild=interaction.guild,
                buyer_id=interaction.user.id,
                seller_id=product.get("seller_id"),
                product_id=product.get("product_id", ""),
                product_title=product.get("title") or product.get("product_id") or "상품",
                category_id=product.get("category_id", ""),
                category_name=(category or {}).get("name", ""),
                source="vending",
                purchased_at=log_doc.get("purchased_at"),
                amount=price,
            )
        if product.get("topup_enabled"):
            if product.get("topup_kind", "tokens") == "plan":
                delivery = f"{product['topup_plan'].upper()} 플랜 {int(product['topup_months'])}개월"
            else:
                delivery = f"{int(product['topup_tokens']):,} 토큰"
            await interaction.followup.send(
                embed=success_embed("API 지급 완료", f"{delivery} 지급을 완료했습니다.\n주문번호: `{result.operation_id}`"),
                ephemeral=True,
            )
            return
        if product.get("product_type") == "stock":
            await interaction.followup.send(
                embed=await self.deliver_stock_unit(interaction, product, result.stock_unit),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=self.build_download_embed(product), ephemeral=True)

    async def deliver_stock_unit(
        self,
        interaction: discord.Interaction,
        product: dict,
        unit: dict | None,
    ) -> discord.Embed:
        """DM the buyer their stock unit; fall back to the ephemeral reply if DMs are closed.

        The unit is already popped from inventory by this point (that's the
        atomic step that prevents two buyers sharing one unit), so a DM
        failure must not lose the content -- the caller still needs it.
        """
        title = product.get("title") or product.get("product_id") or "상품"
        try:
            await interaction.user.send(embed=self.build_stock_unit_embed(product, unit))
        except discord.HTTPException:
            embed = self.build_stock_unit_embed(product, unit)
            embed.description = f"DM을 보낼 수 없어 아래에 바로 표시합니다. (DM 허용 설정을 확인해주세요)\n{embed.description or ''}"
            return embed
        return success_embed("구매 완료", f"**{title}** 재고를 DM으로 전송했습니다.")

    async def stock_products(self, guild_id: int) -> list[dict]:
        products = await self.repos.products.list_active(guild_id, limit=None)
        return [product for product in products if product.get("product_type") == "stock"]

    async def build_stock_panel_view(
        self,
        guild_id: int,
        selected_product_id: str | None = None,
    ) -> VendingStockPanelView:
        products = await self.stock_products(guild_id)
        stock_counts = await self.stock_counts_for(guild_id, products)
        return VendingStockPanelView(self, products, stock_counts, selected_product_id)

    async def refresh_stock_panel(self, guild: discord.Guild, selected_product_id: str | None = None):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("vending_stock")
        message_id = settings["meta"].get("vending_stock_panel_message_id")
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            view = await self.build_stock_panel_view(guild.id, selected_product_id)
            update = {"content": None, "embeds": [], "view": view}
            if not any(attachment.filename == BRAND_LOGO_FILENAME for attachment in message.attachments):
                logo_files = branded_files()
                if logo_files:
                    update["attachments"] = [*logo_files, *message.attachments]
            await message.edit(**update)
        except discord.NotFound:
            await self.repos.settings.set_value(guild.id, "meta", "vending_stock_panel_message_id", None)
        except discord.HTTPException:
            return

    async def update_stock_panel_message(self, interaction: discord.Interaction, selected_product_id: str | None):
        view = await self.build_stock_panel_view(interaction.guild.id, selected_product_id)
        if interaction.message is not None:
            update = {"content": None, "embeds": [], "view": view}
            if not any(
                attachment.filename == BRAND_LOGO_FILENAME for attachment in interaction.message.attachments
            ):
                logo_files = branded_files()
                if logo_files:
                    update["attachments"] = [*logo_files, *interaction.message.attachments]
            await interaction.message.edit(**update)
            return
        await self.refresh_stock_panel(interaction.guild, selected_product_id)

    async def handle_stock_panel_select(self, interaction: discord.Interaction, product_id_lower: str | None):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        await self.update_stock_panel_message(interaction, product_id_lower)

    async def handle_stock_add_submit(self, interaction: discord.Interaction, product_id_lower: str, contents_text: str):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        product = await self.repos.products.get(interaction.guild.id, product_id_lower)
        if product is None or product.get("product_type") != "stock":
            await interaction.followup.send(embed=error_embed("상품 없음", "재고형 상품을 찾을 수 없습니다."), ephemeral=True)
            return
        lines = [line.strip() for line in contents_text.splitlines() if line.strip()]
        if not lines:
            await interaction.followup.send(embed=error_embed("입력 오류", "추가할 재고 내용을 한 줄에 하나씩 입력해주세요."), ephemeral=True)
            return

        units = await self.repos.vending_stock.add_many(
            interaction.guild.id,
            product["product_id"],
            lines,
            created_by=interaction.user.id,
        )
        await self.update_stock_panel_message(interaction, product_id_lower)
        await interaction.followup.send(
            embed=success_embed("재고 추가 완료", f"`{product['product_id']}`에 {len(units)}개를 추가했습니다."),
            ephemeral=True,
        )

    async def handle_stock_clear(self, interaction: discord.Interaction, product_id_lower: str | None):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        if not product_id_lower:
            await interaction.followup.send(embed=error_embed("상품 없음", "재고를 삭제할 상품을 먼저 선택해주세요."), ephemeral=True)
            return
        product = await self.repos.products.get(interaction.guild.id, product_id_lower)
        if product is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "재고형 상품을 찾을 수 없습니다."), ephemeral=True)
            return
        deleted = await self.repos.vending_stock.clear(interaction.guild.id, product["product_id"])
        await self.update_stock_panel_message(interaction, product_id_lower)
        await interaction.followup.send(
            embed=success_embed("재고 전체 삭제 완료", f"`{product['product_id']}`에서 {deleted}개를 삭제했습니다."),
            ephemeral=True,
        )

    async def handle_discount_menu(self, interaction: discord.Interaction, product_id: str):
        coupon_cog = self.bot.get_cog("CouponCog")
        if coupon_cog is None:
            await interaction.response.send_message(embed=error_embed("쿠폰 오류", "쿠폰 시스템이 로드되지 않았습니다."), ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is not None and await self.commerce.discount_blocked_for(interaction.guild.id, product):
            await interaction.followup.send(embed=error_embed("사용 불가", "이 상품은 쿠폰/프로모션을 사용할 수 없습니다."), ephemeral=True)
            return
        items = await self.repos.coupons.list_for_user(interaction.guild.id, interaction.user.id, "general")
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        await interaction.followup.send(view=DiscountMenuView(self, product_id, items), ephemeral=True)

    async def send_replacement_product_detail(self, interaction: discord.Interaction, product_id: str):
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is None:
            return
        discount_blocked = await self.commerce.discount_blocked_for(interaction.guild.id, product)
        if discount_blocked:
            price, coupon, promotion = int(product.get("price", 0)), None, None
        else:
            price, coupon, promotion = await self.repos.coupons.quote(
                interaction.guild.id, interaction.user.id,
                f"vending:{normalize_product_id(product_id)}", int(product.get("price", 0)),
            )
        owned = await self.repos.vending.owns_product(interaction.guild.id, interaction.user.id, product_id)
        stock_count = None
        if product.get("product_type") == "stock":
            stock_count = await self.repos.vending_stock.count(interaction.guild.id, product["product_id"])
        await interaction.followup.send(
            view=ProductDetailView(self, product, owned=owned, discounted_price=price, applied=coupon or promotion,
                                   stock_count=stock_count, discount_blocked=discount_blocked),
            files=branded_files(),
            ephemeral=True,
        )

    async def handle_vending_coupon(self, interaction: discord.Interaction, product_id: str, code: str | None):
        await interaction.response.defer(ephemeral=True)
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is not None and await self.commerce.discount_blocked_for(interaction.guild.id, product):
            await interaction.followup.send(embed=error_embed("사용 불가", "이 상품은 쿠폰/프로모션을 사용할 수 없습니다."), ephemeral=True)
            return
        await self.repos.coupons.select(
            interaction.guild.id, interaction.user.id, f"vending:{normalize_product_id(product_id)}", code
        )
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        await self.send_replacement_product_detail(interaction, product_id)

    async def handle_promotion_code(self, interaction: discord.Interaction, product_id: str, code: str):
        await interaction.response.defer(ephemeral=True)
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is not None and await self.commerce.discount_blocked_for(interaction.guild.id, product):
            await interaction.followup.send(embed=error_embed("사용 불가", "이 상품은 쿠폰/프로모션을 사용할 수 없습니다."), ephemeral=True)
            return
        promo = await self.repos.coupons.validate_promotion(interaction.guild.id, interaction.user.id, code)
        if promo is None:
            await interaction.followup.send(embed=error_embed("사용 불가", "해당 프로모션 전용 초대 링크로 가입한 계정만 사용할 수 있습니다."), ephemeral=True); return
        await self.repos.coupons.select_promotion(interaction.guild.id, interaction.user.id,
                                                  f"vending:{normalize_product_id(product_id)}", code)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        await self.send_replacement_product_detail(interaction, product_id)

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
            view=DownloadSelectView(self, owned),
            files=branded_files(),
            ephemeral=True,
        )

    async def handle_download_selected(self, interaction: discord.Interaction, product_ids_lower: list[str]):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        owned_products = await self.repos.vending.user_products.find(
            {
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "product_id_lower": {"$in": product_ids_lower},
                "status": "purchased",
                "topup_enabled": {"$ne": True},
            }
        ).to_list(length=25)
        owned_by_id = {owned["product_id_lower"]: owned for owned in owned_products}
        items = []
        for product_id_lower in product_ids_lower:
            owned = owned_by_id.get(product_id_lower)
            if owned is None:
                continue
            product = await self.repos.products.get(interaction.guild.id, owned["product_id"], include_inactive=True)
            items.append((product, owned))

        if not items:
            await interaction.followup.send(embed=error_embed("권한 없음", "구매한 상품만 다운로드할 수 있습니다."), ephemeral=True)
            return
        await interaction.followup.send(embed=self.build_downloads_embed(items), ephemeral=True)

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
        view = ArchiveResultView(
            self,
            archive["product_id"],
            self.product_page_url(interaction.guild.id, product),
            product=product,
            canonical_url=canonical_url,
            description=description,
            image_url=f"https://i.ytimg.com/vi/{video_key}/hqdefault.jpg",
        )
        kwargs = {
            "view": view,
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        files = branded_files()
        if files:
            kwargs["files"] = files
        await interaction.followup.send(**kwargs)

    @tasks.loop(minutes=1)
    async def restore_panel_loop(self):
        for guild in self.bot.guilds:
            vending_stats = await self.vending_panel_stats(guild.id)
            await self.restore_v2_panel_message(
                guild,
                "vending",
                "vending_panel_message_id",
                lambda gif_name: VendingPanelView(self, stats=vending_stats, gif_name=gif_name),
                image_attachment_filename=VENDING_PANEL_GIFS,
                rotate_image=True,
            )
            await self.restore_v2_panel_message(
                guild,
                "archive",
                "archive_panel_message_id",
                lambda gif_name: ArchivePanelView(self, gif_name=gif_name),
                image_attachment_filename=ARCHIVE_PANEL_GIFS,
                rotate_image=True,
            )
            await self.refresh_stock_panel(guild)

    @restore_panel_loop.before_loop
    async def before_restore_panel_loop(self):
        await self.bot.wait_until_ready()

    async def restore_v2_panel_message(
        self,
        guild: discord.Guild,
        channel_key: str,
        meta_key: str,
        view: discord.ui.LayoutView | Callable[[str | None], discord.ui.LayoutView],
        *,
        image_attachment_filename=None,
        rotate_image: bool = False,
    ) -> bool:
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get(channel_key)
        message_id = settings["meta"].get(meta_key)
        if not channel_id or not message_id:
            return False
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return False
        try:
            message = await channel.fetch_message(message_id)
            image_filename = choose_gif(
                image_attachment_filename,
                message.attachments,
                force_new=rotate_image,
                existing_urls=message_media_urls(message),
            )
            panel_view = view(image_filename) if callable(view) else view
            update = {"content": None, "embeds": [], "view": panel_view}
            local_mode = gif_delivery_status().effective_mode == "local"
            needs_logo = not any(attachment.filename == BRAND_LOGO_FILENAME for attachment in message.attachments)
            needs_image = local_mode and bool(image_filename) and not any(
                attachment.filename == image_filename for attachment in message.attachments
            )
            if needs_image:
                file = gif_file(image_filename) if image_filename else None
                retained = retained_non_gif_attachments(message)
                if needs_logo:
                    attachments = [*branded_files(file), *retained]
                else:
                    attachments = [*retained, *([file] if file is not None else [])]
                if attachments:
                    update["attachments"] = attachments
            elif needs_logo:
                update["attachments"] = [*branded_files(), *message.attachments]
            elif not local_mode and any(
                is_gif_filename(attachment.filename) for attachment in message.attachments
            ):
                update["attachments"] = retained_non_gif_attachments(message)
            await message.edit(**update)
        except discord.NotFound:
            await self.repos.settings.set_value(guild.id, "meta", meta_key, None)
            return False
        except discord.HTTPException:
            return False
        return True

    @app_commands.command(name="자판기패널", description="현재 채널에 자판기 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def vending_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        image_filename = choose_gif(VENDING_PANEL_GIFS)
        file = gif_file(image_filename)
        kwargs = {"view": await self.build_vending_panel_view(interaction.guild.id, image_filename)}
        files = branded_files(file)
        if files:
            kwargs["files"] = files
        message = await interaction.channel.send(**kwargs)
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "vending",
            "vending_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        embed = success_embed("자판기 패널 생성 완료")
        await interaction.followup.send(**random_embed_gif_kwargs(embed, SUCCESS_GIFS), ephemeral=True)

    @app_commands.command(name="아카이브패널", description="현재 채널에 아카이브 검색 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def archive_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        image_filename = choose_gif(ARCHIVE_PANEL_GIFS)
        file = gif_file(image_filename)
        kwargs = {"view": ArchivePanelView(self, gif_name=image_filename)}
        files = branded_files(file)
        if files:
            kwargs["files"] = files
        message = await interaction.channel.send(**kwargs)
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "archive",
            "archive_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        embed = success_embed("아카이브 패널 생성 완료")
        await interaction.followup.send(**random_embed_gif_kwargs(embed, SUCCESS_GIFS), ephemeral=True)

    @app_commands.command(name="아카이브검색", description="유튜브 URL로 아카이브를 검색합니다.")
    @app_commands.default_permissions(send_messages=True)
    async def archive_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ArchiveSearchModal(self))

    @app_commands.command(name="상품카테고리등록", description="자판기 상품 카테고리를 등록하거나 수정합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(
        category_id="상품 등록 때 사용할 카테고리 ID",
        name="카테고리 표시 이름",
        description="카테고리 설명",
        emoji="셀렉트 메뉴에 표시할 이모지",
        sort_order="낮을수록 먼저 표시됩니다.",
    )
    async def register_product_category(
        self,
        interaction: discord.Interaction,
        category_id: str,
        name: str,
        description: str = "",
        emoji: str = "",
        sort_order: int = 0,
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        if not category_id.strip() or len(category_id.strip()) > 64:
            await interaction.followup.send(embed=error_embed("카테고리 ID 오류", "카테고리 ID는 1~64자로 입력해주세요."), ephemeral=True)
            return
        category = await self.repos.product_categories.upsert(
            interaction.guild.id,
            category_id,
            name=name,
            description=description,
            emoji=emoji,
            sort_order=sort_order,
            created_by=interaction.user.id,
        )
        await self.refresh_vending_panel(interaction.guild)
        await interaction.followup.send(
            embed=success_embed("상품 카테고리 등록 완료", f"{category.get('emoji') or ''} {category['name']} (`{category['category_id']}`)"),
            ephemeral=True,
        )

    @app_commands.command(name="상품카테고리삭제", description="자판기 상품 카테고리를 비활성화합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(category_id="비활성화할 카테고리 ID")
    async def delete_product_category(self, interaction: discord.Interaction, category_id: str):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        category = await self.repos.product_categories.get(interaction.guild.id, category_id, include_inactive=True)
        if category is None:
            await interaction.followup.send(embed=error_embed("카테고리 없음", "해당 카테고리 ID를 찾을 수 없습니다."), ephemeral=True)
            return
        deleted = await self.repos.product_categories.deactivate(interaction.guild.id, category_id, interaction.user.id)
        if not deleted:
            await interaction.followup.send(embed=error_embed("처리 실패", "이미 비활성화된 카테고리입니다."), ephemeral=True)
            return
        await self.refresh_vending_panel(interaction.guild)
        await interaction.followup.send(embed=success_embed("상품 카테고리 삭제 완료", f"`{category['category_id']}`"), ephemeral=True)

    @app_commands.command(name="카테고리할인차단", description="카테고리 전체에서 쿠폰/프로모션 사용을 막거나 허용합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(category_id="대상 카테고리 ID", 차단="체크하면 쿠폰/프로모션 사용 금지, 비우면 다시 허용")
    async def block_category_discount(self, interaction: discord.Interaction, category_id: str, 차단: bool = True):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        category = await self.repos.product_categories.get(interaction.guild.id, category_id, include_inactive=True)
        if category is None:
            await interaction.followup.send(embed=error_embed("카테고리 없음", "해당 카테고리 ID를 찾을 수 없습니다."), ephemeral=True)
            return
        updated = await self.repos.product_categories.set_discount_blocked(
            interaction.guild.id, category_id, 차단, interaction.user.id
        )
        if updated is None:
            await interaction.followup.send(embed=error_embed("처리 실패", "비활성화된 카테고리는 변경할 수 없습니다."), ephemeral=True)
            return
        state = "차단" if 차단 else "허용"
        await interaction.followup.send(
            embed=success_embed("카테고리 할인 설정 변경", f"`{updated['category_id']}` 쿠폰/프로모션 {state}"),
            ephemeral=True,
        )

    @app_commands.command(name="상품카테고리목록", description="등록된 자판기 상품 카테고리를 확인합니다.")
    @app_commands.default_permissions(send_messages=True)
    async def list_product_categories(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        categories = await self.repos.product_categories.list_active(interaction.guild.id)
        if not categories:
            await interaction.followup.send(embed=error_embed("카테고리 없음", "`/상품카테고리등록`으로 먼저 등록해주세요."), ephemeral=True)
            return
        lines = [
            f"{category.get('emoji') or ''} `{category['category_id']}` · {category['name']}"
            + (f" · {category['description']}" if category.get("description") else "")
            + (" · 쿠폰/프로모션 사용 불가" if category.get("discount_blocked") else "")
            for category in categories
        ]
        await interaction.followup.send(embed=info_embed("상품 카테고리 목록", "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="상품등록", description="자판기 상품을 등록하거나 수정합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.choices(
        plan=[
            app_commands.Choice(name="Plus", value="plus"),
            app_commands.Choice(name="Pro", value="pro"),
        ],
        months=[
            app_commands.Choice(name="1개월", value=1),
            app_commands.Choice(name="2개월", value=2),
            app_commands.Choice(name="3개월", value=3),
            app_commands.Choice(name="6개월", value=6),
        ],
    )
    @app_commands.describe(
        product_id="자판기에서 사용할 상품 ID",
        price="상품 가격",
        title="상품명",
        category_id="상품을 넣을 카테고리 ID",
        재고형="체크하면 재고 소진형 상품(재고 패널에서 재고 관리 필요), 비워두면 상시 판매 상품",
        terabox_url="구매자에게 지급할 테라박스 링크. 상시 판매 상품은 필수, 재고형 상품은 비워두세요.",
        description="상품 설명 요약",
        thread_id="상품 설명 쓰레드 ID 또는 멘션",
        page_url="상품 설명 페이지 URL",
        seller="상품 셀러",
        api_on="토큰/플랜 지급 API ON/OFF (상시 판매 상품 전용)",
        tokens="토큰 상품일 때 충전할 토큰 수",
        plan="플랜 상품일 때 plus 또는 pro",
        months="플랜 적용 개월: 1, 2, 3, 6",
    )
    async def register_product(
        self,
        interaction: discord.Interaction,
        product_id: str,
        price: int,
        title: str,
        category_id: str,
        재고형: bool = False,
        terabox_url: str = "",
        description: str = "",
        thread_id: str = "",
        page_url: str = "",
        seller: discord.Member | None = None,
        api_on: bool = False,
        tokens: int = 0,
        plan: str = "",
        months: int = 0,
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
        product_type = "stock" if 재고형 else "standing"
        plan = plan.strip().casefold()
        if api_on and 재고형:
            await interaction.followup.send(embed=error_embed("API 설정 오류", "API 상품은 재고형을 끄고 등록해주세요."), ephemeral=True)
            return
        if api_on and plan and (plan not in {"plus", "pro"} or months not in {1, 2, 3, 6}):
            await interaction.followup.send(embed=error_embed("플랜 설정 오류", "plan은 plus/pro, months는 1/2/3/6 중 하나로 입력해주세요."), ephemeral=True)
            return
        if api_on and not plan and tokens <= 0:
            await interaction.followup.send(embed=error_embed("토큰 설정 오류", "토큰 상품은 tokens를 1 이상 입력해주세요."), ephemeral=True)
            return
        if product_type == "standing" and not api_on and not is_http_url(terabox_url):
            await interaction.followup.send(
                embed=error_embed("링크 오류", "상시 판매 상품은 테라박스 링크(http 또는 https URL)가 필요합니다."),
                ephemeral=True,
            )
            return
        if terabox_url and not is_http_url(terabox_url):
            await interaction.followup.send(embed=error_embed("링크 오류", "테라박스 링크는 http 또는 https URL이어야 합니다."), ephemeral=True)
            return
        if page_url and not is_http_url(page_url):
            await interaction.followup.send(embed=error_embed("페이지 URL 오류", "상품 페이지 URL은 http 또는 https URL이어야 합니다."), ephemeral=True)
            return
        category = await self.repos.product_categories.get(interaction.guild.id, category_id)
        if category is None:
            await interaction.followup.send(
                embed=error_embed("카테고리 없음", "먼저 `/상품카테고리등록`으로 카테고리를 등록해주세요."),
                ephemeral=True,
            )
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
            category_id=category["category_id"],
            thread_id=parse_discord_id(thread_id),
            page_url=page_url,
            product_type=product_type,
            topup_enabled=api_on,
            topup_tokens=tokens,
            topup_plan=plan,
            topup_months=months,
            created_by=interaction.user.id,
        )
        await self.refresh_vending_panel(interaction.guild)
        type_label = "재고형" if product_type == "stock" else "상시 판매"
        note = "\n-# 재고 패널에서 재고를 추가해야 구매할 수 있습니다." if product_type == "stock" else ""
        if api_on and plan:
            api_label = f"ON · {plan.upper()} 플랜 {months}개월"
        elif api_on:
            api_label = f"ON · {tokens} 토큰"
        else:
            api_label = "OFF"
        note += f"\nAPI 지급: {api_label}"
        await interaction.followup.send(
            embed=success_embed("상품 등록 완료", f"`{product['product_id']}` -> {category['name']} ({type_label}){note}"),
            ephemeral=True,
        )

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
        await self.refresh_vending_panel(interaction.guild)
        await interaction.followup.send(embed=success_embed("상품 삭제 완료", f"`{product['product_id']}`"), ephemeral=True)

    @app_commands.command(name="상품할인차단", description="특정 상품에서 쿠폰/프로모션 사용을 막거나 허용합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(product_id="대상 상품 ID", 차단="체크하면 쿠폰/프로모션 사용 금지, 비우면 다시 허용")
    async def block_product_discount(self, interaction: discord.Interaction, product_id: str, 차단: bool = True):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        product = await self.repos.products.get(interaction.guild.id, product_id, include_inactive=True)
        if product is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "해당 상품 ID를 찾을 수 없습니다."), ephemeral=True)
            return
        if not await self.admin_allowed(interaction) and product.get("seller_id") != interaction.user.id:
            await interaction.followup.send(embed=error_embed("권한 없음", "본인 상품만 변경할 수 있습니다."), ephemeral=True)
            return
        updated = await self.repos.products.set_discount_blocked(
            interaction.guild.id, product_id, 차단, interaction.user.id
        )
        if updated is None:
            await interaction.followup.send(embed=error_embed("처리 실패", "비활성화된 상품은 변경할 수 없습니다."), ephemeral=True)
            return
        state = "차단" if 차단 else "허용"
        await interaction.followup.send(
            embed=success_embed("상품 할인 설정 변경", f"`{updated['product_id']}` 쿠폰/프로모션 {state}"),
            ephemeral=True,
        )

    @app_commands.command(name="상품조회", description="상품 ID로 자판기 상품 정보를 조회합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(product_id="조회할 상품 ID")
    async def product_info(self, interaction: discord.Interaction, product_id: str):
        await interaction.response.defer(ephemeral=True)
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "해당 상품 ID를 찾을 수 없습니다."), ephemeral=True)
            return
        await interaction.followup.send(embed=await self.build_product_embed(interaction.guild.id, product), ephemeral=True)

    @app_commands.command(name="상품목록", description="카테고리별 자판기 상품 목록을 확인합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(category_id="조회할 카테고리 ID. 비우면 카테고리 선택 메뉴가 뜹니다.")
    async def product_list(self, interaction: discord.Interaction, category_id: str = ""):
        await interaction.response.defer(ephemeral=True)
        if not category_id:
            categories = await self.repos.product_categories.list_active(interaction.guild.id)
            if not categories:
                await interaction.followup.send(embed=error_embed("카테고리 없음", "`/상품카테고리등록`으로 먼저 등록해주세요."), ephemeral=True)
                return
            await interaction.followup.send(
                view=CategoryMenuView(self, categories, "catalog"),
                files=branded_files(),
                ephemeral=True,
            )
            return

        category = await self.repos.product_categories.get(interaction.guild.id, category_id)
        if category is None:
            await interaction.followup.send(embed=error_embed("카테고리 없음", "해당 카테고리 ID를 찾을 수 없습니다."), ephemeral=True)
            return
        products = await self.repos.products.list_by_category(interaction.guild.id, category["category_id"])
        if not products:
            await interaction.followup.send(embed=error_embed("상품 없음", "이 카테고리에 등록된 판매 상품이 없습니다."), ephemeral=True)
            return
        await interaction.followup.send(
            view=ProductMenuView(self, category, products, "catalog"),
            files=branded_files(),
            ephemeral=True,
        )

    @app_commands.command(name="잔액조회", description="자판기 충전 잔액을 확인합니다.")
    @app_commands.default_permissions(send_messages=True)
    async def cash_balance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = await self.repos.users.ensure_user(interaction.guild.id, interaction.user.id)
        await interaction.followup.send(
            embed=info_embed("자판기 잔액", f"현재 잔액은 `{int(user.get('cash', 0)):,}원`입니다."),
            ephemeral=True,
        )

    @app_commands.command(name="잔액지급", description="관리자가 유저 자판기 잔액을 수동으로 지급합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="잔액을 지급할 유저", amount="지급 금액", reason="지급 사유")
    async def grant_cash(self, interaction: discord.Interaction, user: discord.Member, amount: int, reason: str = "관리자 수동 지급"):
        await interaction.response.defer(ephemeral=True)
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return
        if amount <= 0:
            await interaction.followup.send(embed=error_embed("금액 오류", "1원 이상 입력해주세요."), ephemeral=True)
            return
        user_doc = await self.repos.users.add_cash(interaction.guild.id, user.id, amount)
        log_channel = await self.get_log_channel(interaction.guild)
        if log_channel:
            embed = info_embed("수동 잔액 지급")
            embed.add_field(name="관리자", value=interaction.user.mention, inline=True)
            embed.add_field(name="대상", value=user.mention, inline=True)
            embed.add_field(name="금액", value=f"{amount:,}원", inline=True)
            embed.add_field(name="사유", value=reason, inline=False)
            await log_channel.send(embed=embed)
        await interaction.followup.send(
            embed=success_embed("잔액 지급 완료", f"{user.mention}: {int(user_doc.get('cash', 0)):,}원"),
            ephemeral=True,
        )

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

    @app_commands.command(name="자판기재고패널", description="현재 채널에 자판기 재고 관리 패널을 생성합니다.")
    @app_commands.default_permissions(send_messages=True)
    async def vending_stock_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        view = await self.build_stock_panel_view(interaction.guild.id)
        kwargs = {"view": view}
        files = branded_files()
        if files:
            kwargs["files"] = files
        message = await interaction.channel.send(**kwargs)
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "vending_stock",
            "vending_stock_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.followup.send(embed=success_embed("재고 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="입고공지", description="재고형 상품의 입고 공지를 입고 채널에 올립니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(product_id="입고 공지를 올릴 재고형 상품 ID", message="공지에 추가할 안내 메시지")
    async def announce_restock(self, interaction: discord.Interaction, product_id: str, message: str = ""):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is None or product.get("product_type") != "stock":
            await interaction.followup.send(embed=error_embed("상품 없음", "재고형 상품만 입고 공지를 올릴 수 있습니다."), ephemeral=True)
            return
        channel = await self.get_restock_channel(interaction.guild)
        if channel is None:
            await interaction.followup.send(
                embed=error_embed("입고 채널 미설정", "`/채널설정`으로 자판기 입고 알림 채널을 먼저 설정해주세요."),
                ephemeral=True,
            )
            return

        count = await self.repos.vending_stock.count(interaction.guild.id, product["product_id"])
        settings = await self.repos.settings.get(interaction.guild.id)
        role_id = settings["roles"].get("alarm_stock")

        embed = success_embed(f"입고 안내 · {product.get('title') or product['product_id']}")
        embed.add_field(name="상품 ID", value=f"`{product['product_id']}`", inline=True)
        embed.add_field(name="가격", value=f"{int(product.get('price', 0)):,}원", inline=True)
        embed.add_field(name="현재 재고", value=f"{count}개", inline=True)
        if message:
            embed.add_field(name="안내", value=message, inline=False)

        send_kwargs = random_embed_gif_kwargs(embed, SUCCESS_GIFS)
        if role_id:
            send_kwargs["content"] = f"<@&{role_id}>"
            send_kwargs["allowed_mentions"] = discord.AllowedMentions(roles=[discord.Object(id=role_id)])
        await channel.send(**send_kwargs)
        await interaction.followup.send(
            embed=success_embed("입고 공지 완료", f"{channel.mention}에 공지를 올렸습니다."),
            ephemeral=True,
        )

    async def autocomplete_categories(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        current_lower = current.casefold()
        categories = await self.repos.product_categories.list_active(interaction.guild.id)
        choices = []
        for category in categories:
            label = f"{category.get('emoji') or ''} {category['name']} ({category['category_id']})".strip()
            haystack = f"{category['category_id']} {category['name']}".casefold()
            if current_lower and current_lower not in haystack:
                continue
            choices.append(app_commands.Choice(name=label[:100], value=category["category_id"]))
        return choices[:25]

    async def autocomplete_products(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        current_lower = current.casefold()
        products = await self.repos.products.list_active(interaction.guild.id)
        choices = []
        for product in products:
            label = f"{product.get('title') or product['product_id']} ({product['product_id']})"
            haystack = f"{product['product_id']} {product.get('title', '')}".casefold()
            if current_lower and current_lower not in haystack:
                continue
            choices.append(app_commands.Choice(name=label[:100], value=product["product_id"]))
        return choices[:25]

    @delete_product_category.autocomplete("category_id")
    @product_list.autocomplete("category_id")
    @register_product.autocomplete("category_id")
    @block_category_discount.autocomplete("category_id")
    async def category_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.autocomplete_categories(interaction, current)

    @delete_product.autocomplete("product_id")
    @product_info.autocomplete("product_id")
    @add_archive.autocomplete("product_id")
    @block_product_discount.autocomplete("product_id")
    async def product_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.autocomplete_products(interaction, current)

    @announce_restock.autocomplete("product_id")
    async def stock_product_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        current_lower = current.casefold()
        products = await self.stock_products(interaction.guild.id)
        choices = []
        for product in products:
            label = f"{product.get('title') or product['product_id']} ({product['product_id']})"
            haystack = f"{product['product_id']} {product.get('title', '')}".casefold()
            if current_lower and current_lower not in haystack:
                continue
            choices.append(app_commands.Choice(name=label[:100], value=product["product_id"]))
        return choices[:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(VendingArchiveCog(bot))
