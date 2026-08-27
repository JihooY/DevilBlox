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


class BrokerageVerificationMixin:
    async def start_verification(
        self,
        interaction: discord.Interaction,
        kind: Literal["email", "phone"],
        destination: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not os.getenv("BROKERAGE_VERIFICATION_PEPPER", "").strip():
            await interaction.followup.send(
                embed=error_embed(
                    "인증 설정 필요",
                    "서버에 `BROKERAGE_VERIFICATION_PEPPER`가 설정되지 않았습니다.",
                ),
                ephemeral=True,
            )
            return
        profile = await self.repos.brokerage.ensure_profile(
            interaction.guild.id, interaction.user.id
        )
        if kind in set(profile.get("verified_kinds") or []):
            await interaction.followup.send(
                embed=error_embed("이미 인증됨", "이 인증 방식의 점수는 이미 지급되었습니다."),
                ephemeral=True,
            )
            return
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            if kind == "email":
                normalized = normalize_email(destination)
            else:
                normalized = normalize_phone(destination)
        except VerificationValidationError:
            await interaction.followup.send(
                embed=error_embed(
                    "인증 대상 오류",
                    "올바른 이메일 또는 국가번호를 포함한 E.164 전화번호를 입력해주세요.",
                ),
                ephemeral=True,
            )
            return
        try:
            challenge = await self.repos.brokerage.create_verification_challenge(
                interaction.guild.id,
                interaction.user.id,
                kind,
                normalized,
                code,
                ttl_minutes=10,
            )
        except ValueError as exc:
            if "cooldown" in str(exc).casefold():
                message = "인증 코드는 60초에 한 번만 발급할 수 있습니다. 잠시 후 다시 시도해주세요."
            else:
                message = "인증 요청을 만들지 못했습니다. 입력값을 확인해주세요."
            await interaction.followup.send(
                embed=error_embed("인증 요청 제한", message),
                ephemeral=True,
            )
            return
        try:
            if kind == "email":
                masked = await send_email_code(normalized, code)
            else:
                masked = await send_phone_code(normalized, code)
        except VerificationConfigurationError:
            await self.repos.brokerage.cancel_verification_challenge(
                str(challenge["_id"]),
                interaction.guild.id,
                interaction.user.id,
                reason="provider_not_configured",
            )
            await interaction.followup.send(
                embed=error_embed("인증 발송 미설정", "관리자가 이메일/SMS 발송 환경 설정을 완료해야 합니다."),
                ephemeral=True,
            )
            return
        except VerificationDeliveryError:
            await self.repos.brokerage.cancel_verification_challenge(
                str(challenge["_id"]),
                interaction.guild.id,
                interaction.user.id,
                reason="delivery_failed",
            )
            await interaction.followup.send(
                embed=error_embed("인증 코드 발송 실패", "잠시 후 다시 시도해주세요."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            **_layout_send_kwargs(
                BrokerageVerificationCodeView(self, kind, masked, str(challenge["_id"]))
            ),
            ephemeral=True,
        )

    async def complete_verification(
        self,
        interaction: discord.Interaction,
        kind: Literal["email", "phone"],
        challenge_id: str,
        code: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not code.isascii() or not code.isdigit() or len(code) != 6:
            await interaction.followup.send(
                embed=error_embed("코드 오류", "6자리 숫자 인증 코드를 입력해주세요."),
                ephemeral=True,
            )
            return
        result = await self.repos.brokerage.consume_verification_challenge(
            challenge_id,
            interaction.guild.id,
            interaction.user.id,
            code,
        )
        if not result:
            await interaction.followup.send(
                embed=error_embed("인증 만료", "인증 요청이 없거나 만료되었습니다. 다시 발급해주세요."),
                ephemeral=True,
            )
            return
        if result.get("already_consumed"):
            profile = await self.repos.brokerage.get_profile(
                interaction.guild.id, interaction.user.id
            )
            await interaction.followup.send(
                embed=success_embed(
                    "이미 인증 완료",
                    f"이 인증 방식은 이미 반영되었습니다. 현재 신용도 {_int((profile or {}).get('trust_score'))}점",
                ),
                ephemeral=True,
            )
            return
        if result.get("target_in_use"):
            await interaction.followup.send(
                embed=error_embed(
                    "이미 사용된 인증 정보",
                    "이 이메일 또는 전화번호는 다른 계정에서 이미 인증되었습니다.",
                ),
                ephemeral=True,
            )
            return
        if not result.get("valid"):
            challenge = result.get("challenge") or {}
            remaining = max(
                0,
                _int(challenge.get("max_attempts"), 5) - _int(challenge.get("attempts")),
            )
            await interaction.followup.send(
                embed=error_embed("코드 불일치", f"인증 코드가 다릅니다. 남은 시도: {remaining}회"),
                ephemeral=True,
            )
            return
        profile = await self.repos.brokerage.get_profile(
            interaction.guild.id, interaction.user.id
        )
        verified_kind = str((result.get("challenge") or {}).get("kind") or kind)
        bonus_result = result.get("bonus") or {}
        bonus = _int(
            bonus_result.get("delta"),
            10 if verified_kind == "email" else 20,
        )
        await interaction.followup.send(
            embed=success_embed(
                "본인 인증 완료",
                f"신용도 +{bonus}점 · 현재 신용도 {_int((profile or {}).get('trust_score'))}점",
            ),
            ephemeral=True,
        )
