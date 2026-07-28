from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord

from database.vending import normalize_product_id
from utils.embeds import BRAND_LOGO_URL, error_embed
from utils.gifs import gif_media_url

if TYPE_CHECKING:
    from cogs.cogs_vending_archive import VendingArchiveCog


COLOR_VENDING = 0x5865F2
COLOR_ARCHIVE = 0x2ECC71
SELECT_OPTION_LIMIT = 25
COMPONENT_TEXT_LIMIT = 3_800


def chunked(items: list[dict], size: int = SELECT_OPTION_LIMIT) -> list[list[dict]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def bounded_component_text(lines: list[str]) -> str:
    text = "\n".join(lines)
    if len(text) <= COMPONENT_TEXT_LIMIT:
        return text
    suffix = "\n-# 일부 내용은 Discord 길이 제한으로 생략되었습니다."
    return text[: COMPONENT_TEXT_LIMIT - len(suffix)].rstrip() + suffix


def add_panel_gif(container: discord.ui.Container, filename: str | None, description: str):
    media_url = gif_media_url(filename)
    if not media_url:
        return
    container.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media_url, description=description)
        )
    )


def add_brand_section(container: discord.ui.Container, content: str):
    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(content),
            accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
        )
    )


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

    def __init__(self, cog: VendingArchiveCog):
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

    def __init__(self, cog: VendingArchiveCog, admin_message_id: int):
        super().__init__()
        self.cog = cog
        self.admin_message_id = admin_message_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_reject_charge(interaction, self.admin_message_id, str(self.reason.value))


class ProductPurchaseModal(discord.ui.Modal, title="상품 구매"):
    def __init__(self, cog: VendingArchiveCog, product_id: str = ""):
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

    def __init__(self, cog: VendingArchiveCog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_archive_search(interaction, str(self.youtube_url.value))


class ChargeAdminView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: VendingArchiveCog,
        charge: dict | None = None,
        *,
        image_url: str | None = None,
        title: str = "충전 요청",
        public: bool = False,
        show_actions: bool = True,
    ):
        super().__init__(timeout=None)
        self.cog = cog

        charge = charge or {}
        charge_status = charge.get("status")
        status = {
            "pending": "대기 중",
            "processing": "처리 중",
            "approved": "수락됨",
            "rejected": "거절됨",
        }.get(charge_status, str(charge_status or "대기 중"))
        color = (
            COLOR_ARCHIVE
            if charge_status == "approved"
            else 0xE5484D
            if charge_status == "rejected"
            else COLOR_VENDING
        )
        user_id = charge.get("user_id")
        user_text = f"<@{user_id}> (`{user_id}`)" if user_id else "-"
        processed_by = charge.get("processed_by")
        if processed_by:
            manager_label = "승인 관리자" if charge_status == "approved" else "거절 관리자"
            manager_text = f"<@{processed_by}> (`{processed_by}`)"
        else:
            manager_label = "처리 관리자"
            manager_text = "처리 대기 중"
        process_text = (
            "관리자가 수락해야 잔액이 충전됩니다."
            if charge_status in {None, "pending"}
            else "처리 완료된 요청입니다."
        )
        lines = [
            f"## {title}",
            f"### 상태\n**{status}**",
            f"### 유저\n{user_text}",
            "### 입금 정보",
            f"**입금자명** · {charge.get('depositor_name') or '-'}",
            f"**금액** · `{int(charge.get('amount', 0) or 0):,}원`",
            f"**처리 방식** · {process_text}",
            f"**{manager_label}** · {manager_text}",
        ]
        if charge.get("proof_filename"):
            lines.append(f"**증빙 파일** · `{charge['proof_filename']}`")
        if charge.get("reject_reason"):
            lines.extend(("", f"### 거절 사유\n{charge['reject_reason']}"))
        if public:
            lines.extend(("", "-# 요청 접수 화면입니다. 충전은 관리자 승인 후 반영됩니다."))

        container = discord.ui.Container(accent_color=color)
        add_brand_section(container, bounded_component_text(lines))
        resolved_image_url = (
            image_url
            or charge.get("admin_proof_url")
            or charge.get("request_proof_url")
            or charge.get("log_proof_url")
        )
        if resolved_image_url:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        resolved_image_url,
                        description=charge.get("proof_filename") or "입금 증빙 이미지",
                    )
                )
            )

        if show_actions and charge_status in {None, "pending"}:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            approve_button = discord.ui.Button(
                label="수락",
                style=discord.ButtonStyle.success,
                custom_id="devilblox:vending:charge:approve",
            )
            approve_button.callback = self.approve
            reject_button = discord.ui.Button(
                label="거절",
                style=discord.ButtonStyle.danger,
                custom_id="devilblox:vending:charge:reject",
            )
            reject_button.callback = self.reject
            container.add_item(discord.ui.ActionRow(approve_button, reject_button))
        self.add_item(container)

    async def approve(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button | None = None,
    ):
        await self.cog.handle_approve_charge(interaction)

    async def reject(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button | None = None,
    ):
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
    def __init__(self, cog: VendingArchiveCog, *, stats: dict | None = None, gif_name: str | None = None):
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
    def __init__(self, cog: VendingArchiveCog, *, gif_name: str | None = None):
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


class ArchiveResultView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: VendingArchiveCog,
        product_id: str,
        page_url: str | None,
        *,
        product: dict | None = None,
        canonical_url: str | None = None,
        description: str | None = None,
        image_url: str | None = None,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.product_id = product_id

        product_title = (product or {}).get("title") or product_id
        thread_mention = (
            cog.product_thread_mention(product or {})
            if hasattr(cog, "product_thread_mention")
            else "`미설정`"
        )
        lines = [
            "## 아카이브 검색 결과",
            description or (product or {}).get("description") or "등록된 요약이 없습니다.",
            "",
            "### 연결 상품",
            f"**{product_title}**",
            f"**상품 ID** · `{product_id}`",
            f"**상품 쓰레드** · {thread_mention}",
        ]
        if product:
            lines.append(f"**가격** · `{int(product.get('price', 0) or 0):,}원`")
        if canonical_url:
            lines.extend(("", f"**검색 영상** · [YouTube에서 보기]({canonical_url})"))

        container = discord.ui.Container(accent_color=COLOR_ARCHIVE)
        add_brand_section(container, bounded_component_text(lines))
        if image_url:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(image_url, description=f"{product_title} 아카이브 이미지")
                )
            )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        buy_button = discord.ui.Button(label="구매하기", style=discord.ButtonStyle.success)
        buy_button.callback = self.buy
        buttons = [buy_button]
        if page_url:
            buttons.append(discord.ui.Button(label="상품 페이지", style=discord.ButtonStyle.link, url=page_url))
        container.add_item(discord.ui.ActionRow(*buttons))
        self.add_item(container)

    async def buy(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button | None = None,
    ):
        await interaction.response.send_modal(ProductPurchaseModal(self.cog, self.product_id))


class CategorySelect(discord.ui.Select):
    def __init__(
        self,
        cog: VendingArchiveCog,
        categories: list[dict],
        mode: str,
        *,
        page_index: int = 0,
        page_count: int = 1,
    ):
        self.cog = cog
        self.mode = mode
        options = []
        for category in categories[:SELECT_OPTION_LIMIT]:
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

        placeholder = "카테고리를 선택하세요."
        if page_count > 1:
            placeholder = f"카테고리 선택 ({page_index + 1}/{page_count})"
        super().__init__(
            placeholder=placeholder,
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
    def __init__(self, cog: VendingArchiveCog, categories: list[dict], mode: str):
        super().__init__(timeout=180)
        title = "상품 구매" if mode == "buy" else "상품 목록"
        container = discord.ui.Container(accent_color=COLOR_VENDING)
        add_brand_section(container, f"## {title}\n카테고리를 선택하면 해당 카테고리의 상품이 표시됩니다.")
        container.add_item(discord.ui.Separator())
        category_chunks = chunked(categories)
        for page_index, category_chunk in enumerate(category_chunks):
            container.add_item(
                discord.ui.ActionRow(
                    CategorySelect(
                        cog,
                        category_chunk,
                        mode,
                        page_index=page_index,
                        page_count=len(category_chunks),
                    )
                )
            )
        self.add_item(container)


class ProductSelect(discord.ui.Select):
    def __init__(
        self,
        cog: VendingArchiveCog,
        products: list[dict],
        mode: str,
        *,
        page_index: int = 0,
        page_count: int = 1,
    ):
        self.cog = cog
        self.mode = mode
        options = []
        for product in products[:SELECT_OPTION_LIMIT]:
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

        placeholder = "상품을 선택하세요."
        if page_count > 1:
            placeholder = f"상품 선택 ({page_index + 1}/{page_count})"
        super().__init__(
            placeholder=placeholder,
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
    def __init__(self, cog: VendingArchiveCog, category: dict | None, products: list[dict], mode: str):
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
        product_chunks = chunked(products)
        for page_index, product_chunk in enumerate(product_chunks):
            container.add_item(
                discord.ui.ActionRow(
                    ProductSelect(
                        cog,
                        product_chunk,
                        mode,
                        page_index=page_index,
                        page_count=len(product_chunks),
                    )
                )
            )
        self.add_item(container)


class ProductDetailView(discord.ui.LayoutView):
    def __init__(self, cog: VendingArchiveCog, product: dict, *, owned: bool = False,
                 discounted_price: int | None = None, applied: dict | None = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.product_id = product["product_id"]
        lines = [
            f"## {product.get('title') or product['product_id']}",
            product.get("description") or "등록된 상품 설명이 없습니다.",
            "",
            f"상품 ID: `{product['product_id']}`",
            f"가격: `{int(product.get('price', 0)):,}원`",
            f"상품 페이지: {cog.product_thread_mention(product)}",
        ]
        original_price = int(product.get("price", 0))
        if applied and discounted_price is not None and discounted_price < original_price:
            discount_amount = original_price - discounted_price
            value = f"{applied['discount']}%" if applied.get("discount_type", "percent") == "percent" else f"{int(applied['discount']):,}원"
            lines.extend([
                "",
                f"적용 코드: **{applied.get('name') or applied['code']}** (`{applied['code']}` · {value})",
                f"할인 금액: **-{discount_amount:,}원**",
                f"적용 후 가격: **{discounted_price:,}원**",
            ])
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
        discount_button = discord.ui.Button(label="쿠폰 / 프로모션", style=discord.ButtonStyle.primary)
        discount_button.callback = self.discount
        detail_buttons = [buy_button, discount_button]
        page_url = cog.product_page_url(int(product["guild_id"]), product)
        if page_url:
            detail_buttons.append(discord.ui.Button(label="상품 페이지", style=discord.ButtonStyle.link, url=page_url))
        container.add_item(discord.ui.ActionRow(*detail_buttons))
        self.add_item(container)

    async def buy(self, interaction: discord.Interaction):
        await self.cog.handle_purchase(interaction, self.product_id)

    async def discount(self, interaction: discord.Interaction):
        await self.cog.handle_discount_menu(interaction, self.product_id)


class PromotionCodeModal(discord.ui.Modal, title="프로모션 코드 입력"):
    code = discord.ui.TextInput(label="프로모션 코드", max_length=40)

    def __init__(self, cog: VendingArchiveCog, product_id: str):
        super().__init__()
        self.cog = cog
        self.product_id = product_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_promotion_code(interaction, self.product_id, str(self.code))


class VendingCouponSelect(discord.ui.Select):
    def __init__(self, cog: VendingArchiveCog, product_id: str, items: list[dict]):
        self.cog = cog
        self.product_id = product_id
        options = [discord.SelectOption(label="쿠폰 사용 안 함", value="none")]
        for owned in items[:24]:
            coupon = owned["coupon"]
            options.append(discord.SelectOption(
                label=f"{coupon['name']} ({owned['quantity']}장)", value=coupon["code"],
                description=f"{coupon['discount']}% 할인 · {coupon['code']}"[:100],
            ))
        super().__init__(placeholder="보유 쿠폰을 선택하세요", options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_vending_coupon(
            interaction, self.product_id, None if self.values[0] == "none" else self.values[0]
        )


class DiscountMenuView(discord.ui.LayoutView):
    def __init__(self, cog: VendingArchiveCog, product_id: str, items: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.product_id = product_id
        box = discord.ui.Container(accent_color=COLOR_VENDING)
        box.add_item(discord.ui.TextDisplay("## 쿠폰 / 프로모션\n보유 쿠폰을 선택하거나 프로모션 코드를 입력하세요."))
        box.add_item(discord.ui.Separator())
        if items:
            box.add_item(discord.ui.ActionRow(VendingCouponSelect(cog, product_id, items)))
        promotion = discord.ui.Button(label="프로모션 코드 입력", style=discord.ButtonStyle.primary)
        promotion.callback = self.promotion
        box.add_item(discord.ui.ActionRow(promotion))
        self.add_item(box)

    async def promotion(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PromotionCodeModal(self.cog, self.product_id))


class DownloadSelect(discord.ui.Select):
    def __init__(
        self,
        cog: VendingArchiveCog,
        owned_products: list[dict],
        *,
        page_index: int = 0,
        page_count: int = 1,
    ):
        self.cog = cog
        options = []
        for owned in owned_products[:SELECT_OPTION_LIMIT]:
            label = owned.get("title") or owned.get("product_id") or "상품"
            product_id = owned.get("product_id") or owned.get("product_id_lower")
            options.append(
                discord.SelectOption(
                    label=str(label)[:100],
                    value=str(owned.get("product_id_lower") or normalize_product_id(product_id)),
                    description=f"ID: {product_id}"[:100],
                )
            )
        placeholder = "다운로드할 상품을 모두 선택하세요."
        if page_count > 1:
            placeholder = f"다운로드 상품 선택 ({page_index + 1}/{page_count})"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=max(1, min(len(options), 25)),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_download_selected(interaction, list(self.values))


class DownloadSelectView(discord.ui.LayoutView):
    def __init__(self, cog: VendingArchiveCog, owned_products: list[dict]):
        super().__init__(timeout=180)
        container = discord.ui.Container(accent_color=COLOR_VENDING)
        add_brand_section(container, "## 다운로드\n링크를 다시 받을 상품을 하나 이상 선택하세요.")
        container.add_item(discord.ui.Separator())
        owned_chunks = chunked(owned_products)
        for page_index, owned_chunk in enumerate(owned_chunks):
            container.add_item(
                discord.ui.ActionRow(
                    DownloadSelect(
                        cog,
                        owned_chunk,
                        page_index=page_index,
                        page_count=len(owned_chunks),
                    )
                )
            )
        self.add_item(container)


__all__ = [
    "ArchivePanelView",
    "ArchiveResultView",
    "ArchiveSearchModal",
    "CategoryMenuView",
    "CategorySelect",
    "ChargeAdminView",
    "ChargeRequestModal",
    "COLOR_ARCHIVE",
    "COLOR_VENDING",
    "DiscountMenuView",
    "DownloadSelect",
    "DownloadSelectView",
    "ProductDetailView",
    "ProductMenuView",
    "ProductPurchaseModal",
    "ProductSelect",
    "PromotionCodeModal",
    "RejectChargeModal",
    "SELECT_OPTION_LIMIT",
    "VendingCouponSelect",
    "VendingPanelView",
    "add_brand_section",
    "add_panel_gif",
    "chunked",
]
