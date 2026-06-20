from __future__ import annotations

import io
import os
import re
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.vending import (
    ArchiveStore,
    ProductCategoryStore,
    ProductStore,
    VendingLogStore,
    normalize_product_id,
)
from utils.embeds import (
    BRAND_LOGO_FILENAME,
    BRAND_LOGO_URL,
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
    gif_file,
    random_embed_gif_kwargs,
)
from utils.panels import save_panel_location
from utils.roles import has_role


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
COLOR_VENDING = 0x5865F2
COLOR_ARCHIVE = 0x2ECC71
MAX_CHARGE_PROOF_BYTES = 8 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def add_panel_gif(container: discord.ui.Container, filename: str | None, description: str):
    if not filename:
        return
    container.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(f"attachment://{filename}", description=description)
        )
    )


def add_brand_section(container: discord.ui.Container, content: str):
    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(content),
            accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
        )
    )


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
        self.proof_upload = discord.ui.FileUpload(required=True, min_values=1, max_values=1)
        self.add_item(
            discord.ui.Label(
                text="입금 사진",
                description="입금 확인용 이미지 1장을 업로드하세요.",
                component=self.proof_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_charge_submit(
            interaction,
            depositor_name=str(self.depositor_name.value),
            amount_text=str(self.amount.value),
            proof=self.proof_upload.values[0] if self.proof_upload.values else None,
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
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return
        if interaction.message is None:
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(embed=error_embed("처리 실패", "요청 메시지를 찾을 수 없습니다."), ephemeral=True)
            return
        await interaction.response.send_modal(RejectChargeModal(self.cog, interaction.message.id))


class VendingPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "VendingArchiveCog", *, stats: dict | None = None, gif_name: str | None = None):
        super().__init__(timeout=None)
        self.cog = cog

        bank_account = os.getenv("VENDING_BANK_ACCOUNT", "").strip()
        lines = [
            "## VENDING MACHINE",
            "충전, 카테고리별 상품 구매, 구매한 상품 다운로드를 이용할 수 있습니다.",
        ]
        if bank_account:
            lines.append(f"입금 계좌: `{bank_account}`")
        if stats:
            lines.append(
                f"등록 카테고리 `{stats.get('category_count', 0)}`개 · 판매 상품 `{stats.get('product_count', 0)}`개"
            )

        container = discord.ui.Container(accent_color=COLOR_VENDING)
        add_brand_section(container, "\n".join(lines))
        add_panel_gif(container, gif_name, "DevilBlox vending panel")
        container.add_item(discord.ui.Separator())

        charge_button = discord.ui.Button(
            label="충전",
            style=discord.ButtonStyle.success,
            custom_id="devilblox:vending:charge",
        )
        charge_button.callback = self.charge

        catalog_button = discord.ui.Button(
            label="상품목록",
            style=discord.ButtonStyle.secondary,
            custom_id="devilblox:vending:catalog",
        )
        catalog_button.callback = self.catalog

        buy_button = discord.ui.Button(
            label="구매",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:vending:buy",
        )
        buy_button.callback = self.buy

        download_button = discord.ui.Button(
            label="다운로드",
            style=discord.ButtonStyle.secondary,
            custom_id="devilblox:vending:download",
        )
        download_button.callback = self.download
        container.add_item(discord.ui.ActionRow(charge_button, catalog_button, buy_button, download_button))

        self.add_item(container)

    async def charge(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ChargeRequestModal(self.cog))

    async def catalog(self, interaction: discord.Interaction):
        await self.cog.handle_category_menu(interaction, mode="catalog")

    async def buy(self, interaction: discord.Interaction):
        await self.cog.handle_category_menu(interaction, mode="buy")

    async def download(self, interaction: discord.Interaction):
        await self.cog.handle_download_menu(interaction)


class ArchivePanelView(discord.ui.LayoutView):
    def __init__(self, cog: "VendingArchiveCog", *, gif_name: str | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        container = discord.ui.Container(accent_color=COLOR_ARCHIVE)
        add_brand_section(container, "## ARCHIVE\n유튜브 영상 URL로 영상에 사용된 상품을 검색할 수 있습니다.")
        add_panel_gif(container, gif_name, "DevilBlox archive panel")
        container.add_item(discord.ui.Separator())
        search_button = discord.ui.Button(
            label="검색",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:archive:search",
        )
        search_button.callback = self.search
        container.add_item(discord.ui.ActionRow(search_button))
        self.add_item(container)

    async def search(self, interaction: discord.Interaction):
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


class CategorySelect(discord.ui.Select):
    def __init__(self, cog: "VendingArchiveCog", categories: list[dict], mode: str):
        self.cog = cog
        self.mode = mode
        options = []
        for category in categories[:25]:
            label = category.get("name") or category.get("category_id") or "카테고리"
            option = discord.SelectOption(
                label=str(label)[:100],
                value=str(category.get("category_id_lower") or normalize_product_id(category["category_id"])),
                description=(category.get("description") or f"ID: {category['category_id']}")[:100],
            )
            if category.get("emoji"):
                option.emoji = category["emoji"]
            options.append(option)

        if not options:
            options.append(discord.SelectOption(label="등록된 카테고리가 없습니다.", value="none"))

        super().__init__(
            placeholder="카테고리를 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not categories,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                embed=error_embed("카테고리 없음", "`/상품카테고리등록`으로 카테고리를 먼저 등록해주세요."),
                ephemeral=True,
            )
            return
        await self.cog.handle_category_selected(interaction, self.values[0], self.mode)


class CategoryMenuView(discord.ui.LayoutView):
    def __init__(self, cog: "VendingArchiveCog", categories: list[dict], mode: str):
        super().__init__(timeout=180)
        title = "상품 구매" if mode == "buy" else "상품 목록"
        container = discord.ui.Container(accent_color=COLOR_VENDING)
        add_brand_section(container, f"## {title}\n카테고리를 선택하면 해당 카테고리의 상품이 표시됩니다.")
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(CategorySelect(cog, categories, mode)))
        self.add_item(container)


class ProductSelect(discord.ui.Select):
    def __init__(self, cog: "VendingArchiveCog", products: list[dict], mode: str):
        self.cog = cog
        self.mode = mode
        options = []
        for product in products[:25]:
            product_id = product.get("product_id") or product.get("product_id_lower")
            price = int(product.get("price", 0))
            description = f"{price:,}원 · ID: {product_id}"
            options.append(
                discord.SelectOption(
                    label=str(product.get("title") or product_id)[:100],
                    value=str(product.get("product_id_lower") or normalize_product_id(product_id)),
                    description=description[:100],
                )
            )
        if not options:
            options.append(discord.SelectOption(label="등록된 상품이 없습니다.", value="none"))

        super().__init__(
            placeholder="상품을 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not products,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(embed=error_embed("상품 없음", "이 카테고리에 상품이 없습니다."), ephemeral=True)
            return
        await self.cog.handle_product_selected(interaction, self.values[0], self.mode)


class ProductMenuView(discord.ui.LayoutView):
    def __init__(self, cog: "VendingArchiveCog", category: dict | None, products: list[dict], mode: str):
        super().__init__(timeout=180)
        category_name = (category or {}).get("name") or "카테고리"
        lines = [f"## {category_name}", "상품을 선택하면 상세 정보와 구매 버튼이 표시됩니다."]
        if products:
            lines.append("")
            lines.extend(
                f"- `{product['product_id']}` · {product.get('title') or product['product_id']} · {int(product.get('price', 0)):,}원"
                for product in products[:10]
            )
            if len(products) > 10:
                lines.append(f"- 외 {len(products) - 10}개")

        container = discord.ui.Container(accent_color=COLOR_VENDING)
        add_brand_section(container, "\n".join(lines))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(ProductSelect(cog, products, mode)))
        self.add_item(container)


class ProductDetailView(discord.ui.LayoutView):
    def __init__(self, cog: "VendingArchiveCog", product: dict, *, owned: bool = False):
        super().__init__(timeout=180)
        self.cog = cog
        self.product_id = product["product_id"]
        self.seller_id = product.get("seller_id")
        lines = [
            f"## {product.get('title') or product['product_id']}",
            product.get("description") or "등록된 상품 설명이 없습니다.",
            "",
            f"상품 ID: `{product['product_id']}`",
            f"가격: `{int(product.get('price', 0)):,}원`",
            f"상품 페이지: {cog.product_thread_mention(product)}",
        ]
        if owned:
            lines.append("이미 구매한 상품입니다. 다운로드 버튼으로 링크를 다시 받을 수 있습니다.")

        container = discord.ui.Container(accent_color=COLOR_VENDING)
        add_brand_section(container, "\n".join(lines))
        container.add_item(discord.ui.Separator())
        buy_button = discord.ui.Button(
            label="구매하기" if not owned else "다운로드",
            style=discord.ButtonStyle.success if not owned else discord.ButtonStyle.secondary,
        )
        buy_button.callback = self.buy
        detail_buttons = [buy_button]
        if self.seller_id:
            rating_button = discord.ui.Button(label="셀러 평점", style=discord.ButtonStyle.primary)
            rating_button.callback = self.seller_rating
            detail_buttons.append(rating_button)
        page_url = cog.product_page_url(int(product["guild_id"]), product)
        if page_url:
            detail_buttons.append(discord.ui.Button(label="상품 페이지", style=discord.ButtonStyle.link, url=page_url))
        container.add_item(discord.ui.ActionRow(*detail_buttons))
        self.add_item(container)

    async def buy(self, interaction: discord.Interaction):
        await self.cog.handle_purchase(interaction, self.product_id)

    async def seller_rating(self, interaction: discord.Interaction):
        if not self.seller_id:
            await interaction.response.send_message(embed=error_embed("셀러 없음", "이 상품에는 셀러가 등록되어 있지 않습니다."), ephemeral=True)
            return
        await self.cog.handle_seller_rating(interaction, int(self.seller_id))


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
            placeholder="다운로드할 상품을 모두 선택하세요.",
            min_values=1,
            max_values=max(1, min(len(options), 25)),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_download_selected(interaction, list(self.values))


class DownloadSelectView(discord.ui.LayoutView):
    def __init__(self, cog: "VendingArchiveCog", owned_products: list[dict]):
        super().__init__(timeout=180)
        container = discord.ui.Container(accent_color=COLOR_VENDING)
        add_brand_section(container, "## 다운로드\n링크를 다시 받을 상품을 하나 이상 선택하세요.")
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(DownloadSelect(cog, owned_products)))
        self.add_item(container)


class VendingArchiveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(VendingPanelView(self))
        self.bot.add_view(ArchivePanelView(self))
        self.bot.add_view(ChargeAdminView(self))

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

    def build_downloads_embed(self, items: list[tuple[dict | None, dict]]) -> discord.Embed:
        embed = success_embed("다운로드 링크", "선택한 상품의 전달 링크입니다.")
        for product, owned in items[:25]:
            source = product or owned
            product_id = source.get("product_id") or "unknown"
            title = source.get("title") or product_id
            url = (product or {}).get("terabox_url") or owned.get("terabox_url") or "저장된 링크가 없습니다."
            embed.add_field(name=f"{title} (`{product_id}`)", value=url, inline=False)
        return embed

    async def vending_panel_stats(self, guild_id: int) -> dict:
        categories = await self.repos.product_categories.list_active(guild_id, limit=25)
        products = await self.repos.products.list_active(guild_id, limit=25)
        return {"category_count": len(categories), "product_count": len(products)}

    async def build_vending_panel_view(self, guild_id: int, gif_name: str | None = None) -> VendingPanelView:
        return VendingPanelView(self, stats=await self.vending_panel_stats(guild_id), gif_name=gif_name)

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

    def build_charge_embed(
        self,
        charge: dict,
        status_label: str | None = None,
        *,
        image_url: str | None = None,
        public: bool = False,
    ) -> discord.Embed:
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
        embed.add_field(
            name="처리 방식",
            value="관리자가 수락해야 잔액이 충전됩니다." if charge.get("status") == "pending" else "처리 완료된 요청입니다.",
            inline=False,
        )
        if charge.get("processed_by"):
            label = "승인 관리자" if charge.get("status") == "approved" else "거절 관리자"
            embed.add_field(name=label, value=f"<@{charge['processed_by']}> (`{charge['processed_by']}`)", inline=False)
        if charge.get("proof_filename"):
            embed.add_field(name="입금 사진", value=charge["proof_filename"], inline=True)
        if charge.get("reject_reason"):
            embed.add_field(name="거절 사유", value=charge["reject_reason"], inline=False)
        resolved_image_url = (
            image_url
            or charge.get("admin_proof_url")
            or charge.get("request_proof_url")
            or charge.get("log_proof_url")
        )
        if resolved_image_url:
            embed.set_image(url=resolved_image_url)
        if public:
            embed.set_footer(text="요청 접수 화면입니다. 충전은 관리자 승인 후 반영됩니다.")
        return embed

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
        embed = self.build_charge_embed(charge, image_url=image_url)
        embed.title = "충전 요청 로그"
        if file is not None:
            return await channel.send(embed=embed, file=file)
        return await channel.send(embed=embed)

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
        view: discord.ui.View | None = None,
    ):
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=self.build_charge_embed(charge, image_url=image_url, public=public), view=view)
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
                None,
            ),
            (
                charge.get("log_channel_id"),
                charge.get("log_message_id"),
                charge.get("log_proof_url"),
                False,
                None,
            ),
        ]
        for channel_id, message_id, image_url, public, view in targets:
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
                view=view,
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
        admin_message = await admin_channel.send(
            embed=self.build_charge_embed(charge, image_url=attachment_image_url(proof_filename)),
            view=ChargeAdminView(self),
            file=admin_file,
        )
        admin_proof_url = admin_message.attachments[0].url if admin_message.attachments else ""

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
                log_proof_url = log_message.attachments[0].url if log_message and log_message.attachments else ""
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
        await interaction.followup.send(
            view=ProductMenuView(self, category, products, mode),
            files=branded_files(),
            ephemeral=True,
        )

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
            await interaction.followup.send(embed=self.build_product_embed(product), ephemeral=True)
            return
        await interaction.followup.send(
            view=ProductDetailView(self, product, owned=owned),
            files=branded_files(),
            ephemeral=True,
        )

    async def handle_seller_rating(self, interaction: discord.Interaction, seller_id: int):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return

        reviews_cog = self.bot.get_cog("ReviewsCog")
        if reviews_cog is None:
            await interaction.followup.send(embed=error_embed("후기 시스템 없음", "후기 시스템이 아직 로드되지 않았습니다."), ephemeral=True)
            return

        rating_doc = await self.repos.reviews.get_seller_rating(interaction.guild.id, seller_id)
        recent_reviews = await self.repos.reviews.list_by_seller(interaction.guild.id, seller_id, limit=5)
        await interaction.followup.send(
            embed=reviews_cog.build_seller_rating_embed(
                seller_id=seller_id,
                rating_doc=rating_doc,
                recent_reviews=recent_reviews,
            ),
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
        reviews_cog = self.bot.get_cog("ReviewsCog")
        if reviews_cog is not None:
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
            image_filename = choose_gif(image_attachment_filename, message.attachments, force_new=rotate_image)
            panel_view = view(image_filename) if callable(view) else view
            update = {"content": None, "embeds": [], "view": panel_view}
            needs_logo = not any(attachment.filename == BRAND_LOGO_FILENAME for attachment in message.attachments)
            needs_image = bool(image_filename) and not any(
                attachment.filename == image_filename for attachment in message.attachments
            )
            if needs_logo or needs_image:
                file = gif_file(image_filename) if image_filename else None
                attachments = branded_files(file)
                if attachments:
                    update["attachments"] = attachments
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
            for category in categories
        ]
        await interaction.followup.send(embed=info_embed("상품 카테고리 목록", "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="상품등록", description="자판기 상품을 등록하거나 수정합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(
        product_id="자판기에서 사용할 상품 ID",
        price="상품 가격",
        terabox_url="구매자에게 지급할 테라박스 링크",
        title="상품명",
        category_id="상품을 넣을 카테고리 ID",
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
        category_id: str,
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
            created_by=interaction.user.id,
        )
        await self.refresh_vending_panel(interaction.guild)
        await interaction.followup.send(
            embed=success_embed("상품 등록 완료", f"`{product['product_id']}` -> {category['name']}"),
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

    @app_commands.command(name="상품조회", description="상품 ID로 자판기 상품 정보를 조회합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(product_id="조회할 상품 ID")
    async def product_info(self, interaction: discord.Interaction, product_id: str):
        await interaction.response.defer(ephemeral=True)
        product = await self.repos.products.get(interaction.guild.id, product_id)
        if product is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "해당 상품 ID를 찾을 수 없습니다."), ephemeral=True)
            return
        await interaction.followup.send(embed=self.build_product_embed(product), ephemeral=True)

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
    async def category_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.autocomplete_categories(interaction, current)

    @delete_product.autocomplete("product_id")
    @product_info.autocomplete("product_id")
    @add_archive.autocomplete("product_id")
    async def product_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.autocomplete_products(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(VendingArchiveCog(bot))
