from __future__ import annotations

import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.stock import normalize_stock_id
from utils.embeds import (
    BRAND_LOGO_FILENAME,
    BRAND_LOGO_URL,
    COLOR_INFO,
    branded_files,
    error_embed,
    success_embed,
)
from utils.gifs import (
    SUCCESS_GIFS,
    random_embed_gif_kwargs,
)
from utils.panels import save_panel_location
from utils.roles import has_role

log = logging.getLogger(__name__)


def stock_panel_attachments(message: discord.Message) -> list[discord.Attachment | discord.File]:
    logo_attachments = [attachment for attachment in message.attachments if attachment.filename == BRAND_LOGO_FILENAME]
    return logo_attachments or branded_files()


def stock_panel_send_kwargs(view: discord.ui.LayoutView) -> dict:
    kwargs = {
        "view": view,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    files = branded_files()
    if files:
        kwargs["files"] = files
    return kwargs


def stock_panel_edit_kwargs(message: discord.Message, view: discord.ui.LayoutView) -> dict:
    # Components V2 cannot coexist with legacy content or embeds. Clearing both
    # also migrates already-saved stock panels in place on their next refresh.
    return {
        "content": None,
        "embeds": [],
        "view": view,
        "attachments": stock_panel_attachments(message),
        "allowed_mentions": discord.AllowedMentions.none(),
    }


def parse_quantity(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value)
    if not digits:
        return None
    return int(digits)


def parse_signed_quantity(value: str) -> int | None:
    normalized = re.sub(r"[\s,]", "", value.strip())
    if not normalized:
        return None
    if not re.fullmatch(r"[+-]?\d+", normalized):
        return None
    amount = int(normalized)
    return amount if amount != 0 else None


class StockRegisterModal(discord.ui.Modal, title="재고 상품 등록"):
    item_id = discord.ui.TextInput(label="상품 ID", placeholder="예: devil_pack_01", max_length=64)
    name = discord.ui.TextInput(label="상품명", placeholder="현황 패널에 표시할 이름", max_length=100)
    quantity = discord.ui.TextInput(label="초기 수량", placeholder="예: 10", default="0", max_length=12)

    def __init__(self, cog: "StockCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_register_submit(
            interaction,
            item_id=str(self.item_id.value),
            name=str(self.name.value),
            quantity_text=str(self.quantity.value),
        )


class StockAdjustModal(discord.ui.Modal, title="재고 수량 지정"):
    amount = discord.ui.TextInput(label="변경 수량", placeholder="예: +10 또는 -5", max_length=12)

    def __init__(self, cog: "StockCog", item_id: str):
        super().__init__()
        self.cog = cog
        self.item_id = item_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_adjust_submit(
            interaction,
            item_id=self.item_id,
            amount_text=str(self.amount.value),
        )


class StockItemSelect(discord.ui.Select):
    def __init__(self, cog: "StockCog", items: list[dict], selected_item_id: str | None):
        self.cog = cog
        options = []
        selected_lower = normalize_stock_id(selected_item_id or "")
        for item in items[:25]:
            item_id = item.get("item_id") or item.get("item_id_lower")
            item_lower = item.get("item_id_lower") or normalize_stock_id(item_id)
            options.append(
                discord.SelectOption(
                    label=str(item.get("name") or item_id)[:100],
                    value=item_lower,
                    description=f"재고 {int(item.get('quantity', 0) or 0)}개 · ID: {item_id}"[:100],
                    default=item_lower == selected_lower,
                )
            )

        if not options:
            options.append(discord.SelectOption(label="등록된 상품이 없습니다.", value="none"))

        super().__init__(
            placeholder="재고를 조정할 상품을 선택하세요.",
            custom_id="devilblox:stock:control:select",
            min_values=1,
            max_values=1,
            options=options,
            disabled=not items,
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.defer(ephemeral=True)
            return
        await self.cog.handle_control_select(interaction, self.values[0])


def stock_condition_text(items: list[dict], *, max_length: int = 3_700) -> str:
    if not items:
        return "### 상품별 재고\n등록된 재고 상품이 없습니다."

    lines = ["### 상품별 재고"]
    for index, item in enumerate(items):
        item_id = str(item.get("item_id") or item.get("item_id_lower") or "-")
        name = str(item.get("name") or item_id)
        quantity = int(item.get("quantity", 0) or 0)
        item_line = f"- **{name}** (`{item_id}`) · `{quantity:,}개`"
        hidden_count = len(items) - index
        overflow_line = f"-# 외 `{hidden_count}`개 상품은 길이 제한으로 생략되었습니다."
        if len("\n".join((*lines, item_line, overflow_line))) > max_length:
            lines.append(overflow_line)
            break
        lines.append(item_line)
    return "\n".join(lines)


def add_stock_brand_section(container: discord.ui.Container, content: str):
    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(content),
            accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
        )
    )


class StockConditionView(discord.ui.LayoutView):
    def __init__(self, items: list[dict], reset_at: int | None = None):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_color=COLOR_INFO)
        add_stock_brand_section(
            container,
            "\n".join(
                (
                    "## STOCK CONDITION",
                    "현재 등록된 상품 재고 수량을 표시합니다.",
                    f"등록 상품 `{len(items)}`개",
                )
            ),
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(stock_condition_text(items)))
        if reset_at:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(
                discord.ui.TextDisplay(f"-# 마지막 갱신 · <t:{reset_at}:F> (<t:{reset_at}:R>)")
            )
        self.add_item(container)


class StockControlView(discord.ui.LayoutView):
    def __init__(self, cog: "StockCog", items: list[dict], selected_item_id: str | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.items = items
        self.selected_item_id = self.resolve_selected_item_id(items, selected_item_id)
        selected = self.selected_item(items, self.selected_item_id)

        container = discord.ui.Container(accent_color=COLOR_INFO)
        add_stock_brand_section(
            container,
            "\n".join(
                (
                    "## STOCK CONTROL",
                    "상품 등록과 재고 수량 조정을 처리합니다.",
                    f"등록 상품 `{len(items)}`개",
                )
            ),
        )

        if selected is None:
            selected_text = "### 선택 상품\n등록된 상품이 없습니다.\n-# 아래 상품 등록 버튼으로 첫 상품을 추가해주세요."
        else:
            item_id = selected.get("item_id") or selected.get("item_id_lower")
            name = selected.get("name") or item_id
            quantity = int(selected.get("quantity", 0) or 0)
            selected_text = (
                f"### 선택 상품\n**{name}**\n"
                f"-# 상품 ID · `{item_id}`\n\n"
                f"### 현재 재고\n`{quantity:,}개`"
            )

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(selected_text))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(StockItemSelect(cog, items, self.selected_item_id)))

        quantity_buttons = []
        for label, delta, style, custom_id in (
            ("-5", -5, discord.ButtonStyle.danger, "devilblox:stock:control:minus5"),
            ("-1", -1, discord.ButtonStyle.secondary, "devilblox:stock:control:minus1"),
            ("+1", 1, discord.ButtonStyle.success, "devilblox:stock:control:plus1"),
            ("+5", 5, discord.ButtonStyle.success, "devilblox:stock:control:plus5"),
        ):
            button = discord.ui.Button(label=label, style=style, custom_id=custom_id, disabled=selected is None)
            button.callback = self.adjust_callback(delta)
            quantity_buttons.append(button)

        adjust_button = discord.ui.Button(
            label="수량 지정",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:stock:control:adjust",
            disabled=selected is None,
        )
        adjust_button.callback = self.adjust_custom
        quantity_buttons.append(adjust_button)
        container.add_item(discord.ui.ActionRow(*quantity_buttons))

        register_button = discord.ui.Button(
            label="상품 등록",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:stock:control:register",
        )
        register_button.callback = self.register_item

        delete_button = discord.ui.Button(
            label="상품 삭제",
            style=discord.ButtonStyle.danger,
            custom_id="devilblox:stock:control:delete",
            disabled=selected is None,
        )
        delete_button.callback = self.delete_item

        refresh_button = discord.ui.Button(
            label="새로고침",
            style=discord.ButtonStyle.secondary,
            custom_id="devilblox:stock:control:refresh",
        )
        refresh_button.callback = self.refresh
        container.add_item(discord.ui.ActionRow(register_button, delete_button, refresh_button))
        self.add_item(container)

    @staticmethod
    def resolve_selected_item_id(items: list[dict], selected_item_id: str | None) -> str | None:
        if not items:
            return None
        selected_lower = normalize_stock_id(selected_item_id or "")
        item_lowers = {item.get("item_id_lower") or normalize_stock_id(item.get("item_id") or "") for item in items}
        if selected_lower in item_lowers:
            return selected_lower
        first = items[0]
        return first.get("item_id_lower") or normalize_stock_id(first.get("item_id") or "")

    @staticmethod
    def selected_item(items: list[dict], selected_item_id: str | None) -> dict | None:
        selected_lower = normalize_stock_id(selected_item_id or "")
        for item in items:
            item_lower = item.get("item_id_lower") or normalize_stock_id(item.get("item_id") or "")
            if item_lower == selected_lower:
                return item
        return None

    def adjust_callback(self, delta: int):
        async def callback(interaction: discord.Interaction):
            await self.cog.handle_adjust(interaction, self.selected_item_id, delta)

        return callback

    async def adjust_custom(self, interaction: discord.Interaction):
        if not await self.cog.staff_allowed(interaction):
            await interaction.response.send_message(
                embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."),
                ephemeral=True,
            )
            return
        if not self.selected_item_id:
            await interaction.response.send_message(
                embed=error_embed("상품 없음", "수량을 조정할 상품을 먼저 선택해주세요."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(StockAdjustModal(self.cog, self.selected_item_id))

    async def register_item(self, interaction: discord.Interaction):
        if not await self.cog.staff_allowed(interaction):
            await interaction.response.send_message(
                embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(StockRegisterModal(self.cog))

    async def delete_item(self, interaction: discord.Interaction):
        await self.cog.handle_delete(interaction, self.selected_item_id)

    async def refresh(self, interaction: discord.Interaction):
        await self.cog.handle_control_select(interaction, self.selected_item_id)


class StockCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.stock_condition_loop.start()
        self.restore_stock_control_loop.start()

    async def cog_unload(self):
        self.stock_condition_loop.cancel()
        self.restore_stock_control_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def staff_allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("seller")) or has_role(
            interaction.user,
            settings["roles"].get("admin"),
        )

    async def build_stock_condition_view(
        self,
        guild: discord.Guild,
        reset_at: int | None = None,
    ) -> StockConditionView:
        items = await self.repos.stock.list_active(guild.id)
        if reset_at is None:
            settings = await self.repos.settings.get(guild.id)
            reset_at = settings["meta"].get("stock_condition_reset_at")
        return StockConditionView(items, reset_at)

    async def build_stock_control_view(
        self,
        guild: discord.Guild,
        selected_item_id: str | None = None,
    ) -> StockControlView:
        items = await self.repos.stock.list_active(guild.id)
        selected_id = StockControlView.resolve_selected_item_id(items, selected_item_id)
        return StockControlView(self, items, selected_id)

    async def refresh_stock_condition_panel(self, guild: discord.Guild, *, rotate_image: bool = False):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("stock_condition")
        message_id = settings["meta"].get("stock_condition_message_id")
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        reset_at = int(time.time())
        try:
            message = await channel.fetch_message(message_id)
            await self.repos.settings.set_value(guild.id, "meta", "stock_condition_reset_at", reset_at)
            view = await self.build_stock_condition_view(guild, reset_at=reset_at)
            await message.edit(**stock_panel_edit_kwargs(message, view))
        except discord.NotFound:
            await self.repos.settings.set_value(guild.id, "meta", "stock_condition_message_id", None)
        except discord.HTTPException:
            return

    async def refresh_stock_control_panel(
        self,
        guild: discord.Guild,
        selected_item_id: str | None = None,
        *,
        rotate_image: bool = False,
    ):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("stock_control")
        message_id = settings["meta"].get("stock_control_message_id")
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            view = await self.build_stock_control_view(guild, selected_item_id)
            await message.edit(**stock_panel_edit_kwargs(message, view))
        except discord.NotFound:
            await self.repos.settings.set_value(guild.id, "meta", "stock_control_message_id", None)
        except discord.HTTPException:
            return

    async def update_control_message(self, interaction: discord.Interaction, selected_item_id: str | None = None):
        view = await self.build_stock_control_view(interaction.guild, selected_item_id)
        if interaction.message is not None:
            await interaction.message.edit(**stock_panel_edit_kwargs(interaction.message, view))
            return
        await self.refresh_stock_control_panel(interaction.guild, selected_item_id)

    async def handle_control_select(self, interaction: discord.Interaction, selected_item_id: str | None):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        await self.update_control_message(interaction, selected_item_id)

    async def handle_adjust(self, interaction: discord.Interaction, item_id: str | None, delta: int):
        await interaction.response.defer(ephemeral=True)
        await self.apply_adjust(interaction, item_id, delta)

    async def handle_adjust_submit(self, interaction: discord.Interaction, *, item_id: str, amount_text: str):
        await interaction.response.defer(ephemeral=True)
        delta = parse_signed_quantity(amount_text)
        if delta is None:
            await interaction.followup.send(embed=error_embed("수량 오류", "`+10`, `-5`처럼 0이 아닌 숫자로 입력해주세요."), ephemeral=True)
            return
        await self.apply_adjust(interaction, item_id, delta)

    async def apply_adjust(self, interaction: discord.Interaction, item_id: str | None, delta: int):
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        if not item_id:
            await interaction.followup.send(embed=error_embed("상품 없음", "먼저 상품을 등록해주세요."), ephemeral=True)
            return

        updated = await self.repos.stock.adjust_quantity(interaction.guild.id, item_id, delta)
        if updated is None:
            await interaction.followup.send(embed=error_embed("재고 부족", "재고는 0개 아래로 내릴 수 없습니다."), ephemeral=True)
            return

        await self.update_control_message(interaction, updated["item_id"])
        await self.refresh_stock_condition_panel(interaction.guild)

    async def handle_delete(self, interaction: discord.Interaction, item_id: str | None):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        if not item_id:
            await interaction.followup.send(embed=error_embed("상품 없음", "삭제할 상품을 먼저 선택해주세요."), ephemeral=True)
            return

        deleted = await self.repos.stock.deactivate(interaction.guild.id, item_id, interaction.user.id)
        if deleted is None:
            await interaction.followup.send(embed=error_embed("상품 없음", "이미 삭제되었거나 찾을 수 없는 상품입니다."), ephemeral=True)
            return

        await self.update_control_message(interaction)
        await self.refresh_stock_condition_panel(interaction.guild)
        await interaction.followup.send(
            embed=success_embed("재고 상품 삭제 완료", f"`{deleted['item_id']}`가 목록에서 제거되었습니다."),
            ephemeral=True,
        )

    async def handle_register_submit(
        self,
        interaction: discord.Interaction,
        *,
        item_id: str,
        name: str,
        quantity_text: str,
    ):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        if not item_id.strip() or len(item_id.strip()) > 64:
            await interaction.followup.send(embed=error_embed("상품 ID 오류", "상품 ID는 1~64자로 입력해주세요."), ephemeral=True)
            return
        quantity = parse_quantity(quantity_text)
        if quantity is None:
            await interaction.followup.send(embed=error_embed("수량 오류", "수량은 0 이상의 숫자로 입력해주세요."), ephemeral=True)
            return

        item = await self.repos.stock.upsert(
            interaction.guild.id,
            item_id,
            name=name,
            quantity=quantity,
            created_by=interaction.user.id,
        )
        await self.refresh_stock_control_panel(interaction.guild, item["item_id"])
        await self.refresh_stock_condition_panel(interaction.guild)
        embed = success_embed("재고 상품 등록 완료", f"`{item['item_id']}`: {int(item.get('quantity', 0) or 0)}개")
        await interaction.followup.send(
            **random_embed_gif_kwargs(embed, SUCCESS_GIFS),
            ephemeral=True,
        )

    @tasks.loop(minutes=1)
    async def stock_condition_loop(self):
        for guild in self.bot.guilds:
            try:
                await self.refresh_stock_condition_panel(guild, rotate_image=True)
            except Exception:
                log.exception("Failed to refresh stock condition panel: guild_id=%s", guild.id)

    @stock_condition_loop.before_loop
    async def before_stock_condition_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def restore_stock_control_loop(self):
        for guild in self.bot.guilds:
            try:
                await self.refresh_stock_control_panel(guild, rotate_image=True)
            except Exception:
                log.exception("Failed to restore stock control panel: guild_id=%s", guild.id)

    @restore_stock_control_loop.before_loop
    async def before_restore_stock_control_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="재고현황패널", description="현재 채널에 재고 현황 메시지를 생성하고 자동 갱신 대상으로 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    async def stock_condition_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reset_at = int(time.time())
        view = await self.build_stock_condition_view(interaction.guild, reset_at=reset_at)
        message = await interaction.channel.send(**stock_panel_send_kwargs(view))
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "stock_condition",
            "stock_condition_message_id",
            interaction.channel.id,
            message.id,
        )
        await self.repos.settings.set_value(interaction.guild.id, "meta", "stock_condition_reset_at", reset_at)
        await interaction.followup.send(embed=success_embed("재고 현황 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="재고컨트롤패널", description="현재 채널에 재고 컨트롤 패널을 생성합니다.")
    @app_commands.default_permissions(send_messages=True)
    async def stock_control_panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self.staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        view = await self.build_stock_control_view(interaction.guild)
        message = await interaction.channel.send(**stock_panel_send_kwargs(view))
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "stock_control",
            "stock_control_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.followup.send(embed=success_embed("재고 컨트롤 패널 생성 완료"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StockCog(bot))
