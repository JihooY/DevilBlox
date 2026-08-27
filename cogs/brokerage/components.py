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
from .modals import (
    BrokerageAdminAdjustmentModal,
    BrokerageConfigModal,
    BrokerageDeleteModal,
    BrokerageListingModal,
    BrokerageNotificationModal,
    BrokerageReportModal,
    BrokerageReviewModal,
    BrokerageVerificationCodeModal,
    BrokerageVerificationDestinationModal,
)


class BrokerageListingView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "BrokerageCog",
        listing: dict,
        seller_profile: dict | None = None,
        config: dict | None = None,
        notification_mentions: str = "",
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.listing_id = str(listing.get("_id") or "unknown")
        self.status = str(listing.get("status") or "open")

        container = discord.ui.Container(
            accent_color=COLOR_SUCCESS if self.status in LISTING_STATES_OPEN else COLOR_INFO
        )
        listing_content = _listing_markdown(
            listing, seller_profile or {}, config or {}
        )
        notification_mentions = str(notification_mentions or "").strip()
        if notification_mentions:
            listing_content = f"## 거래 알림\n{notification_mentions}\n\n{listing_content}"
        _add_brand_section(container, listing_content)
        if self.status in LISTING_STATES_OPEN:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            purchase = discord.ui.Button(
                label="구매 / 예약",
                style=discord.ButtonStyle.success,
                custom_id=f"devilblox:broker:buy:{self.listing_id}",
            )
            purchase.callback = self.purchase
            report = discord.ui.Button(
                label="신고",
                style=discord.ButtonStyle.danger,
                custom_id=f"devilblox:broker:report:{self.listing_id}",
            )
            report.callback = self.report
            like = discord.ui.Button(
                label="좋아요",
                style=discord.ButtonStyle.primary,
                custom_id=f"devilblox:broker:like:{self.listing_id}",
            )
            like.callback = self.like
            container.add_item(discord.ui.ActionRow(purchase, report, like))
        self.add_item(container)

    async def purchase(self, interaction: discord.Interaction) -> None:
        await self.cog.reserve_listing(interaction, self.listing_id)

    async def report(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BrokerageReportModal(self.cog, self.listing_id))

    async def like(self, interaction: discord.Interaction) -> None:
        await self.cog.like_listing(interaction, self.listing_id)

class BrokeragePanelView(discord.ui.LayoutView):
    def __init__(self, cog: "BrokerageCog"):
        super().__init__(timeout=None)
        self.cog = cog
        container = discord.ui.Container(accent_color=COLOR_INFO)
        _add_brand_section(
            container,
            "## 거래중개 통합 패널\n신용도 확인, 거래 등록, 알림 설정과 본인 인증을 한 곳에서 관리합니다.",
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        profile = discord.ui.Button(
            label="내 신용도",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:broker:profile",
        )
        profile.callback = self.profile
        register = discord.ui.Button(
            label="거래 등록",
            style=discord.ButtonStyle.success,
            custom_id="devilblox:broker:register",
        )
        register.callback = self.register
        notifications = discord.ui.Button(
            label="알림 설정",
            style=discord.ButtonStyle.secondary,
            custom_id="devilblox:broker:notifications",
        )
        notifications.callback = self.notifications
        verify = discord.ui.Button(
            label="본인 인증",
            style=discord.ButtonStyle.secondary,
            custom_id="devilblox:broker:verify",
        )
        verify.callback = self.verify
        container.add_item(discord.ui.ActionRow(profile, register, notifications, verify))
        self.add_item(container)

    async def profile(self, interaction: discord.Interaction) -> None:
        await self.cog.send_profile(interaction)

    async def register(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BrokerageListingModal(self.cog))

    async def notifications(self, interaction: discord.Interaction) -> None:
        await self.cog.send_notification_settings(interaction)

    async def verify(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=BrokerageVerificationView(self.cog),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

class BrokerageAdminPanelView(discord.ui.LayoutView):
    def __init__(self, cog: "BrokerageCog"):
        super().__init__(timeout=None)
        self.cog = cog
        container = discord.ui.Container(accent_color=COLOR_ERROR)
        _add_brand_section(
            container,
            "## 거래중개 관리 패널\n거래 삭제, 신용도·문제 조정, 운영 설정과 현황 조회를 처리합니다.",
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        delete = discord.ui.Button(
            label="거래 삭제",
            style=discord.ButtonStyle.danger,
            custom_id="devilblox:broker:admin:delete",
        )
        delete.callback = self.delete
        adjust = discord.ui.Button(
            label="신용도 / 문제 조정",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:broker:admin:adjust",
        )
        adjust.callback = self.adjust
        configure = discord.ui.Button(
            label="운영 설정",
            style=discord.ButtonStyle.secondary,
            custom_id="devilblox:broker:admin:configure",
        )
        configure.callback = self.configure
        overview = discord.ui.Button(
            label="현황",
            style=discord.ButtonStyle.secondary,
            custom_id="devilblox:broker:admin:overview",
        )
        overview.callback = self.overview
        container.add_item(discord.ui.ActionRow(delete, adjust, configure, overview))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.is_admin(interaction):
            return True
        await interaction.response.send_message(
            embed=error_embed("권한 없음", "거래중개 관리자만 사용할 수 있습니다."),
            ephemeral=True,
        )
        return False

    async def delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BrokerageDeleteModal(self.cog))

    async def adjust(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BrokerageAdminAdjustmentModal(self.cog))

    async def configure(self, interaction: discord.Interaction) -> None:
        config = await self.cog.repos.brokerage.get_config(interaction.guild.id)
        await interaction.response.send_modal(BrokerageConfigModal(self.cog, config))

    async def overview(self, interaction: discord.Interaction) -> None:
        await self.cog.send_admin_overview(interaction)

class BrokerageNotificationView(discord.ui.LayoutView):
    def __init__(self, cog: "BrokerageCog", preferences: dict, role_enabled: bool):
        super().__init__(timeout=300)
        self.cog = cog
        self.preferences = preferences
        content = "\n".join(
            (
                "## 거래 등록 알림 설정",
                f"**알림 역할**  {'선택됨' if role_enabled else '선택 안 함'}",
                f"**최소 판매자 신용도**  `{_int(preferences.get('min_seller_score'), -100)}`",
                f"**같은 거래 중복 알림**  {'받지 않음' if preferences.get('no_duplicates', True) else '받음'}",
            )
        )
        container = discord.ui.Container(accent_color=COLOR_INFO)
        _add_brand_section(container, content)
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        role = discord.ui.Button(label="알림 역할 토글", style=discord.ButtonStyle.primary)
        role.callback = self.toggle_role
        configure = discord.ui.Button(label="세부 설정", style=discord.ButtonStyle.secondary)
        configure.callback = self.configure
        container.add_item(discord.ui.ActionRow(role, configure))
        self.add_item(container)

    async def toggle_role(self, interaction: discord.Interaction) -> None:
        await self.cog.toggle_notification_role(interaction)

    async def configure(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            BrokerageNotificationModal(self.cog, self.preferences)
        )

class BrokerageVerificationView(discord.ui.LayoutView):
    def __init__(self, cog: "BrokerageCog"):
        super().__init__(timeout=300)
        self.cog = cog
        container = discord.ui.Container(accent_color=COLOR_INFO)
        _add_brand_section(
            container,
            "## 본인 인증\n이메일 인증은 +10점, 전화번호 인증은 +20점입니다. 각 방식은 계정당 한 번만 지급됩니다.",
        )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        email = discord.ui.Button(label="이메일 인증", style=discord.ButtonStyle.primary)
        email.callback = self.email
        phone = discord.ui.Button(label="전화번호 인증", style=discord.ButtonStyle.success)
        phone.callback = self.phone
        container.add_item(discord.ui.ActionRow(email, phone))
        self.add_item(container)

    async def email(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            BrokerageVerificationDestinationModal(self.cog, "email")
        )

    async def phone(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            BrokerageVerificationDestinationModal(self.cog, "phone")
        )

class BrokerageVerificationCodeView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "BrokerageCog",
        kind: Literal["email", "phone"],
        masked: str,
        challenge_id: str,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.kind = kind
        self.challenge_id = challenge_id
        container = discord.ui.Container(accent_color=COLOR_SUCCESS)
        _add_brand_section(
            container,
            f"## 인증 코드 전송 완료\n`{masked}`로 보낸 6자리 코드를 10분 안에 입력해주세요.",
        )
        button = discord.ui.Button(label="인증 코드 입력", style=discord.ButtonStyle.success)
        button.callback = self.enter_code
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(button))
        self.add_item(container)

    async def enter_code(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            BrokerageVerificationCodeModal(self.cog, self.kind, self.challenge_id)
        )

class BrokerageReviewRequestView(discord.ui.LayoutView):
    def __init__(self, cog: "BrokerageCog", review: dict):
        super().__init__(timeout=None)
        self.cog = cog
        self.review_id = str(review.get("_id") or "unknown")
        container = discord.ui.Container(accent_color=COLOR_SUCCESS)
        _add_brand_section(
            container,
            "\n".join(
                (
                    "## 거래 후기 작성",
                    f"**상품**  {_truncate(review.get('listing_title') or '거래 상품', 100)}",
                    f"**판매자**  <@{review.get('seller_id')}>",
                    f"**작성 기한**  {_discord_time(review.get('expires_at'), 'F')}",
                    "평점 2점 이하는 문제 1회로 반영되며, 별점 테러는 관리자가 정정할 수 있습니다.",
                    f"-# 후기 ID: {self.review_id}",
                )
            ),
        )
        button = discord.ui.Button(
            label="후기 작성",
            style=discord.ButtonStyle.primary,
            custom_id=f"devilblox:broker:review:{self.review_id}",
        )
        button.callback = self.write_review
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(button))
        self.add_item(container)

    async def write_review(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BrokerageReviewModal(self.cog, self.review_id))

class BrokerageTicketView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "BrokerageCog",
        listing_id: str,
        buyer_id: int,
        participant_mentions: str = "",
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.listing_id = listing_id
        self.buyer_id = buyer_id
        participant_mentions = str(participant_mentions or "").strip()
        container = discord.ui.Container(accent_color=COLOR_INFO)
        _add_brand_section(
            container,
            "\n".join(
                (
                    "## 거래중개 티켓",
                    participant_mentions,
                    "거래가 끝나면 판매자 또는 도우미가 완료 처리합니다.",
                    "거래가 무산되면 다음 예약자에게 자동으로 넘어갑니다.",
                    f"-# 거래 ID: {listing_id}",
                )
            ),
        )
        complete = discord.ui.Button(
            label="거래 완료",
            style=discord.ButtonStyle.success,
            custom_id=f"devilblox:broker:ticket:complete:{listing_id}:{buyer_id}",
        )
        complete.callback = self.complete
        next_buyer = discord.ui.Button(
            label="거래 무산 / 다음 순번",
            style=discord.ButtonStyle.danger,
            custom_id=f"devilblox:broker:ticket:next:{listing_id}:{buyer_id}",
        )
        next_buyer.callback = self.next_buyer
        issue = discord.ui.Button(
            label="문제 신고",
            style=discord.ButtonStyle.secondary,
            custom_id=f"devilblox:broker:ticket:report:{listing_id}:{buyer_id}",
        )
        issue.callback = self.issue
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(complete, next_buyer, issue))
        self.add_item(container)

    async def complete(self, interaction: discord.Interaction) -> None:
        await self.cog.complete_sale(interaction, self.listing_id, self.buyer_id)

    async def next_buyer(self, interaction: discord.Interaction) -> None:
        await self.cog.cancel_and_promote(interaction, self.listing_id, self.buyer_id)

    async def issue(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BrokerageReportModal(self.cog, self.listing_id))

class BrokerageMessageView(discord.ui.LayoutView):
    def __init__(self, content: str, *, color: int = COLOR_INFO):
        super().__init__(timeout=300)
        container = discord.ui.Container(accent_color=color)
        _add_brand_section(container, content)
        self.add_item(container)
