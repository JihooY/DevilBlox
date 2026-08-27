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


class BrokerageAdminMixin:
    async def admin_delete_listing(
        self,
        interaction: discord.Interaction,
        listing_id: str,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self.is_admin(interaction):
            await interaction.followup.send(
                embed=error_embed("권한 없음", "관리자만 거래를 삭제할 수 있습니다."),
                ephemeral=True,
            )
            return
        before = await self.repos.brokerage.get_listing(listing_id)
        if (
            before is None
            or interaction.guild is None
            or _int(before.get("guild_id")) != interaction.guild.id
        ):
            await interaction.followup.send(
                embed=error_embed("삭제 불가", "이 서버의 거래가 없거나 이미 종료되었습니다."),
                ephemeral=True,
            )
            return
        listing = await self.repos.brokerage.delete_listing(
            listing_id,
            deleted_by=interaction.user.id,
            reason=reason,
        )
        if listing is None:
            await interaction.followup.send(
                embed=error_embed("삭제 불가", "거래가 없거나 이미 종료되었습니다."),
                ephemeral=True,
            )
            return
        await self.refresh_listing_message(listing)
        await self.archive_trade_ticket(
            interaction.guild,
            _int(before.get("active_ticket_channel_id")),
            buyer_id=_int(before.get("current_buyer_id")),
            seller_id=_int(before.get("seller_id")),
            reason="Brokerage listing deleted by administrator",
        )
        affected = {
            _int(item.get("user_id"))
            for item in before.get("buyer_queue") or []
            if item.get("state") in {"active", "waiting"}
        }
        for user_id in affected:
            user = interaction.guild.get_member(user_id) or self.bot.get_user(user_id)
            if user is None:
                continue
            try:
                await user.send(
                    embed=info_embed(
                        "거래 삭제 안내",
                        f"예약하신 `{before.get('title')}` 거래가 관리자에 의해 삭제되었습니다.\n사유: {_truncate(reason, 500)}",
                    )
                )
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            embed=success_embed("거래 삭제 완료", f"거래 ID: `{listing_id}`"),
            ephemeral=True,
        )
        await self.send_log(
            interaction.guild,
            "관리자 거래 삭제",
            f"거래 ID: `{listing_id}`\n관리자: {interaction.user.mention}\n사유: {_truncate(reason, 1_000)}",
        )

    async def admin_adjust_profile(
        self,
        interaction: discord.Interaction,
        *,
        user_id: int,
        score_delta: int,
        problem_delta: int,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self.is_admin(interaction):
            await interaction.followup.send(
                embed=error_embed("권한 없음", "관리자만 조정할 수 있습니다."), ephemeral=True
            )
            return
        if not -200 <= score_delta <= 200 or not -100 <= problem_delta <= 100:
            await interaction.followup.send(
                embed=error_embed("범위 오류", "신용도는 ±200, 문제 횟수는 ±100 범위에서 조정해주세요."),
                ephemeral=True,
            )
            return
        if score_delta == 0 and problem_delta == 0:
            await interaction.followup.send(
                embed=error_embed("조정 없음", "신용도 또는 문제 횟수 변경값을 입력해주세요."),
                ephemeral=True,
            )
            return
        operation = secrets.token_urlsafe(10)
        if score_delta:
            await self.repos.brokerage.adjust_profile(
                interaction.guild.id,
                user_id,
                score_delta,
                operation_id=f"admin:{operation}:score",
                reason=_truncate(reason, 100),
                actor_id=interaction.user.id,
            )
        if problem_delta:
            await self.repos.brokerage.admin_adjust_problem_count(
                interaction.guild.id,
                user_id,
                problem_delta,
                f"admin:{operation}:problem",
                interaction.user.id,
                reason,
            )
        profile = await self.repos.brokerage.ensure_profile(interaction.guild.id, user_id)
        await interaction.followup.send(
            embed=success_embed(
                "거래 신용 정보 조정 완료",
                f"<@{user_id}> · 신용도 `{profile.get('trust_score')}` · 문제 `{profile.get('problem_count')}회` · 패널티 `{profile.get('penalty_level')}단계`",
            ),
            ephemeral=True,
        )
        await self.send_log(
            interaction.guild,
            "관리자 신용 정보 조정",
            f"대상: <@{user_id}>\n신용도 증감: {score_delta:+d}\n문제 증감: {problem_delta:+d}\n관리자: {interaction.user.mention}\n사유: {_truncate(reason, 1_000)}",
        )

    async def update_config(
        self,
        interaction: discord.Interaction,
        values: dict,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self.is_admin(interaction):
            await interaction.followup.send(
                embed=error_embed("권한 없음", "관리자만 운영 설정을 바꿀 수 있습니다."),
                ephemeral=True,
            )
            return
        intervals = values.get("bump_intervals") or []
        if (
            _int(values.get("price_point_unit")) <= 0
            or _int(values.get("like_score_every")) <= 0
            or not -100 <= _int(values.get("minimum_listing_score"), -101) <= 100
            or any(_int(item.get("minutes")) <= 0 for item in intervals)
        ):
            await interaction.followup.send(
                embed=error_embed("설정 범위 오류", "가격/좋아요/주기는 양수이고 최소 신용도는 -100~100이어야 합니다."),
                ephemeral=True,
            )
            return
        try:
            config = await self.repos.brokerage.update_config(
                interaction.guild.id,
                values,
                updated_by=interaction.user.id,
            )
        except ValueError as exc:
            await interaction.followup.send(
                embed=error_embed("설정 오류", str(exc)), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                "거래중개 설정 완료",
                f"1점당 {_int(config.get('price_point_unit')):,}원 · 좋아요 {_int(config.get('like_score_every'))}개당 +{_int(config.get('like_score_bonus'), 1)}점 · 등록 최소 {_int(config.get('minimum_listing_score'))}점",
            ),
            ephemeral=True,
        )

    async def send_admin_overview(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self.is_admin(interaction):
            await interaction.followup.send(
                embed=error_embed("권한 없음", "관리자만 현황을 볼 수 있습니다."), ephemeral=True
            )
            return
        listings = await self.repos.brokerage.list_open_listings(
            interaction.guild.id, limit=1_000
        )
        reports = [
            item
            for item in await self.repos.brokerage.list_pending_reports(limit=1_000)
            if item.get("guild_id") == interaction.guild.id
        ]
        problems = [
            item
            for item in await self.repos.brokerage.list_pending_problems(limit=1_000)
            if item.get("guild_id") == interaction.guild.id
        ]
        settlements = [
            item
            for item in await self.repos.brokerage.list_due_settlements(limit=1_000)
            if item.get("guild_id") == interaction.guild.id
        ]
        await interaction.followup.send(
            **_layout_send_kwargs(
                BrokerageMessageView(
                    "\n".join(
                        (
                            "## 거래중개 운영 현황",
                            f"**진행중 거래**  `{len(listings)}개`",
                            f"**미처리 신고**  `{len(reports)}건`",
                            f"**유효 문제 기록**  `{len(problems)}건`",
                            f"**정산 대기/도래**  `{len(settlements)}건`",
                        )
                    ),
                    color=COLOR_ERROR if reports else COLOR_INFO,
                )
            ),
            ephemeral=True,
        )

    async def send_log(self, guild: discord.Guild, title: str, description: str) -> None:
        settings = await self.repos.settings.get(guild.id)
        channel = guild.get_channel(settings["channels"].get("brokerage_log") or 0)
        if channel is None or not hasattr(channel, "send"):
            return
        try:
            await channel.send(
                embed=info_embed(title, description),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("Failed to send brokerage log: guild_id=%s title=%s", guild.id, title)
