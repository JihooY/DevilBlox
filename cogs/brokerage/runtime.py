from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.brokerage import BrokerageStore
from services.brokerage import (
    PostingIntervalPolicy,
    PricePointPolicy,
    penalty_level,
    posting_interval_for_score,
    price_to_base_points,
)
from services.brokerage_verification import (
    VerificationConfigurationError,
    VerificationDeliveryError,
    VerificationValidationError,
    normalize_email,
    normalize_phone,
    send_email_code,
    send_phone_code,
)
from utils.embeds import (
    BRAND_LOGO_URL,
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_SUCCESS,
    branded_files,
    error_embed,
    info_embed,
    success_embed,
)
from utils.panels import save_panel_location
from utils.roles import has_role
from utils.tickets import safe_channel_name

from .common import (
    LISTING_STATES_OPEN,
    MAX_NOTIFICATION_MENTIONS,
    _add_brand_section,
    _discord_time,
    _int,
    _layout_send_kwargs,
    _listing_interval,
    _listing_markdown,
    _now,
    _penalty_percent,
    _profile_markdown,
    _tier_minutes,
    _truncate,
    _trust_tier,
)
from .components import (
    BrokerageAdminPanelView,
    BrokerageListingView,
    BrokerageMessageView,
    BrokerageNotificationView,
    BrokeragePanelView,
    BrokerageReviewRequestView,
    BrokerageTicketView,
    BrokerageVerificationCodeView,
)

log = logging.getLogger(__name__)


class BrokerageRuntimeMixin:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(BrokeragePanelView(self))
        self.bot.add_view(BrokerageAdminPanelView(self))

    @property
    def repos(self):
        return self.bot.repos

    async def cog_load(self) -> None:
        await self.ensure_brokerage_store()
        await self.register_persistent_views()
        self.bump_listings_loop.start()
        self.settle_transactions_loop.start()

    async def cog_unload(self) -> None:
        self.bump_listings_loop.cancel()
        self.settle_transactions_loop.cancel()

    async def ensure_brokerage_store(self) -> None:
        if hasattr(self.repos, "brokerage"):
            return
        db = getattr(self.bot, "db", None)
        if db is None:
            raise RuntimeError("MongoDB is unavailable for brokerage storage")
        self.repos.brokerage = BrokerageStore(
            db,
            verification_pepper=os.getenv("BROKERAGE_VERIFICATION_PEPPER", ""),
        )
        await self.repos.brokerage.ensure_indexes()

    async def register_persistent_views(self) -> None:
        registered = getattr(self.bot, "_devilblox_brokerage_view_ids", None)
        if registered is None:
            registered = set()
            setattr(self.bot, "_devilblox_brokerage_view_ids", registered)
        persistent: dict[str, list[dict]] = {}
        try:
            persistent = await self.repos.brokerage.list_pending_persistent_objects(
                limit=500
            )
            listings = persistent.get("listings", [])
        except Exception:
            log.exception("Failed to load persistent brokerage listings")
            listings = []
        for listing in listings:
            listing_id = str(listing.get("_id") or "")
            if not listing_id or f"listing:{listing_id}" in registered:
                continue
            profile = await self.repos.brokerage.get_profile(
                int(listing["guild_id"]), int(listing["seller_id"])
            )
            config = await self.repos.brokerage.get_config(int(listing["guild_id"]))
            self.bot.add_view(BrokerageListingView(self, listing, profile, config))
            registered.add(f"listing:{listing_id}")
            buyer_id = listing.get("current_buyer_id")
            if buyer_id and listing.get("active_ticket_channel_id"):
                self.bot.add_view(BrokerageTicketView(self, listing_id, int(buyer_id)))
                registered.add(f"ticket:{listing_id}:{buyer_id}")
        try:
            reviews = persistent.get("reviews", [])
        except Exception:
            log.exception("Failed to load persistent brokerage reviews")
            reviews = []
        for review in reviews:
            review_id = str(review.get("_id") or "")
            if not review_id or f"review:{review_id}" in registered:
                continue
            self.bot.add_view(BrokerageReviewRequestView(self, review))
            registered.add(f"review:{review_id}")

    async def is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions and permissions.administrator:
            return True
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("admin"))

    async def is_trade_staff(self, interaction: discord.Interaction, listing: dict) -> bool:
        if (
            interaction.guild is None
            or _int(listing.get("guild_id")) != interaction.guild.id
        ):
            return False
        if interaction.user.id == listing.get("seller_id"):
            return True
        if await self.is_admin(interaction):
            return True
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("brokerage_helper"))

    async def send_profile(self, interaction: discord.Interaction) -> None:
        profile = await self.repos.brokerage.ensure_profile(
            interaction.guild.id, interaction.user.id
        )
        config = await self.repos.brokerage.get_config(interaction.guild.id)
        await interaction.response.send_message(
            **_layout_send_kwargs(
                BrokerageMessageView(_profile_markdown(interaction.user, profile, config))
            ),
            ephemeral=True,
        )

    async def send_notification_settings(self, interaction: discord.Interaction) -> None:
        preferences = await self.repos.brokerage.get_notification_preferences(
            interaction.guild.id, interaction.user.id
        )
        settings = await self.repos.settings.get(interaction.guild.id)
        role_id = settings["roles"].get("brokerage_alert")
        role_enabled = has_role(interaction.user, role_id)
        await interaction.response.send_message(
            **_layout_send_kwargs(
                BrokerageNotificationView(self, preferences, role_enabled)
            ),
            ephemeral=True,
        )

    async def toggle_notification_role(self, interaction: discord.Interaction) -> None:
        settings = await self.repos.settings.get(interaction.guild.id)
        role = interaction.guild.get_role(settings["roles"].get("brokerage_alert") or 0)
        if role is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "알림 역할 미설정",
                    "관리자가 `/역할설정`에서 거래중개 알림 역할을 먼저 설정해야 합니다.",
                ),
                ephemeral=True,
            )
            return
        enabled = not has_role(interaction.user, role.id)
        try:
            if enabled:
                await interaction.user.add_roles(role, reason="DevilBlox brokerage notifications enabled")
            else:
                await interaction.user.remove_roles(role, reason="DevilBlox brokerage notifications disabled")
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("역할 변경 실패", "봇 역할 순서와 역할 관리 권한을 확인해주세요."),
                ephemeral=True,
            )
            return
        await self.repos.brokerage.set_notification_preferences(
            interaction.guild.id,
            interaction.user.id,
            enabled=enabled,
        )
        await interaction.response.send_message(
            embed=success_embed("알림 설정 변경", "알림을 받습니다." if enabled else "알림을 받지 않습니다."),
            ephemeral=True,
        )

    async def update_notification_preferences(
        self,
        interaction: discord.Interaction,
        *,
        minimum_score: int,
        no_duplicates: bool,
    ) -> None:
        if not -100 <= minimum_score <= 100:
            await interaction.response.send_message(
                embed=error_embed("범위 오류", "최소 신용도는 -100~100이어야 합니다."),
                ephemeral=True,
            )
            return
        preferences = await self.repos.brokerage.set_notification_preferences(
            interaction.guild.id,
            interaction.user.id,
            min_seller_score=minimum_score,
            no_duplicates=no_duplicates,
        )
        await interaction.response.send_message(
            embed=success_embed(
                "알림 세부 설정 완료",
                f"최소 신용도: {preferences['min_seller_score']} · "
                f"중복 알림: {'차단' if preferences['no_duplicates'] else '허용'}",
            ),
            ephemeral=True,
        )
