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


class BrokerageCommandMixin:
    @app_commands.command(name="거래중개패널", description="현재 채널에 거래중개 통합 패널을 생성합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def brokerage_panel(self, interaction: discord.Interaction) -> None:
        if not await self.is_admin(interaction):
            await interaction.response.send_message(
                embed=error_embed("권한 없음", "관리자만 통합 패널을 설치할 수 있습니다."),
                ephemeral=True,
            )
            return
        view = BrokeragePanelView(self)
        message = await interaction.channel.send(**_layout_send_kwargs(view))
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "brokerage_panel",
            "brokerage_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(
            embed=success_embed("거래중개 통합 패널 생성 완료", message.jump_url),
            ephemeral=True,
        )

    @app_commands.command(name="거래중개관리패널", description="현재 채널에 거래중개 관리 패널을 생성합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def brokerage_admin_panel(self, interaction: discord.Interaction) -> None:
        if not await self.is_admin(interaction):
            await interaction.response.send_message(
                embed=error_embed("권한 없음", "관리자만 관리 패널을 설치할 수 있습니다."),
                ephemeral=True,
            )
            return
        view = BrokerageAdminPanelView(self)
        message = await interaction.channel.send(**_layout_send_kwargs(view))
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "brokerage_admin",
            "brokerage_admin_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(
            embed=success_embed("거래중개 관리 패널 생성 완료", message.jump_url),
            ephemeral=True,
        )

    @app_commands.command(name="신용도조정", description="거래 신용도와 문제 횟수를 관리자가 조정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def credit_adjust_command(
        self,
        interaction: discord.Interaction,
        유저: discord.Member,
        점수증감: app_commands.Range[int, -200, 200] = 0,
        문제증감: app_commands.Range[int, -100, 100] = 0,
        사유: str = "관리자 조정",
    ) -> None:
        await self.admin_adjust_profile(
            interaction,
            user_id=유저.id,
            score_delta=int(점수증감),
            problem_delta=int(문제증감),
            reason=사유,
        )

    @app_commands.command(name="거래삭제", description="등록된 거래를 관리자가 삭제합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def delete_listing_command(
        self,
        interaction: discord.Interaction,
        거래id: str,
        사유: str = "관리자 삭제",
    ) -> None:
        await self.admin_delete_listing(interaction, 거래id.strip(), 사유)

    @app_commands.command(name="거래문제처리", description="환불·회수 등 거래 문제를 기록하고 신용도를 차감합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(
        유형=[
            app_commands.Choice(name="환불", value="refund"),
            app_commands.Choice(name="회수/차지백", value="chargeback"),
            app_commands.Choice(name="사기/미전달", value="fraud"),
            app_commands.Choice(name="기타", value="other"),
        ]
    )
    async def problem_command(
        self,
        interaction: discord.Interaction,
        거래id: str,
        유형: app_commands.Choice[str],
        사유: str,
        대상: discord.Member | None = None,
        점수차감: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self.is_admin(interaction):
            await interaction.followup.send(
                embed=error_embed("권한 없음", "관리자만 문제를 처리할 수 있습니다."),
                ephemeral=True,
            )
            return
        listing = await self.repos.brokerage.get_listing(거래id.strip())
        if (
            listing is None
            or interaction.guild is None
            or _int(listing.get("guild_id")) != interaction.guild.id
        ):
            await interaction.followup.send(
                embed=error_embed("거래 없음", "거래 ID를 확인해주세요."), ephemeral=True
            )
            return
        target_id = 대상.id if 대상 else _int(listing.get("seller_id"))
        operation_id = f"admin-issue:{거래id}:{secrets.token_urlsafe(8)}"
        if target_id == listing.get("seller_id"):
            result = await self.repos.brokerage.mark_settlement_problem(
                str(listing["_id"]),
                operation_id=operation_id,
                kind=유형.value,
                actor_id=interaction.user.id,
                notes=사유,
                deduct_score=점수차감,
            )
        else:
            result = await self.repos.brokerage.add_problem(
                interaction.guild.id,
                target_id,
                _int(listing.get("price")),
                operation_id=operation_id,
                kind=유형.value,
                listing_id=str(listing["_id"]),
                deduct_score=점수차감,
                created_by=interaction.user.id,
                notes=사유,
            )
        if not result:
            await interaction.followup.send(
                embed=error_embed("처리 실패", "문제 기록을 저장하지 못했습니다."), ephemeral=True
            )
            return
        profile = result.get("profile") or await self.repos.brokerage.get_profile(
            interaction.guild.id, target_id
        )
        problem = result.get("problem") or {}
        score_result = result.get("score") or {}
        delta = _int(score_result.get("delta")) if isinstance(score_result, dict) else 0
        await interaction.followup.send(
            embed=success_embed(
                "거래 문제 처리 완료",
                f"대상: <@{target_id}> · 문제 {_int((profile or {}).get('problem_count'))}회 · 패널티 {_int((profile or {}).get('penalty_level'))}단계 · 점수 {delta:+d}\n문제 ID: `{problem.get('_id', '-')}`",
            ),
            ephemeral=True,
        )
        await self.send_log(
            interaction.guild,
            "거래 문제 처리",
            f"거래 ID: `{거래id}`\n문제 ID: `{problem.get('_id', '-')}`\n대상: <@{target_id}>\n유형: {유형.name}\n점수 차감: {점수차감}\n사유: {_truncate(사유, 1_000)}",
        )

    @app_commands.command(name="거래문제해결", description="거래 문제 기록을 유지하거나 오판으로 취소합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def resolve_problem_command(
        self,
        interaction: discord.Interaction,
        문제id: str,
        문제유지: bool,
        사유: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self.is_admin(interaction):
            await interaction.followup.send(
                embed=error_embed("권한 없음", "관리자만 문제 기록을 해결할 수 있습니다."),
                ephemeral=True,
            )
            return
        problem = await self.repos.brokerage.get_problem(문제id.strip())
        if (
            problem is None
            or interaction.guild is None
            or _int(problem.get("guild_id")) != interaction.guild.id
        ):
            await interaction.followup.send(
                embed=error_embed("문제 기록 없음", "이 서버의 문제 ID를 확인해주세요."),
                ephemeral=True,
            )
            return
        try:
            result = await self.repos.brokerage.resolve_problem(
                문제id.strip(),
                resolved_by=interaction.user.id,
                upheld=문제유지,
                resolution=사유,
            )
        except ValueError as exc:
            await interaction.followup.send(
                embed=error_embed("처리 불가", str(exc)), ephemeral=True
            )
            return
        if result is None:
            await interaction.followup.send(
                embed=error_embed("문제 기록 없음", "문제 ID를 확인해주세요."), ephemeral=True
            )
            return
        profile = result.get("profile") or {}
        await interaction.followup.send(
            embed=success_embed(
                "문제 기록 해결 완료",
                f"결과: {'유지' if 문제유지 else '오판 취소'} · 현재 문제 {_int(profile.get('problem_count'))}회 · 신용도 {_int(profile.get('trust_score'))}점",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="거래후기수정", description="별점 테러 등 거래 후기를 관리자가 정정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def review_override_command(
        self,
        interaction: discord.Interaction,
        후기id: str,
        평점: app_commands.Range[int, 1, 5],
        문제반영: bool,
        사유: str,
        내용: str | None = None,
        무효화: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self.is_admin(interaction):
            await interaction.followup.send(
                embed=error_embed("권한 없음", "관리자만 후기를 정정할 수 있습니다."),
                ephemeral=True,
            )
            return
        current = await self.repos.brokerage.get_review(후기id.strip())
        if (
            current is None
            or interaction.guild is None
            or _int(current.get("guild_id")) != interaction.guild.id
        ):
            await interaction.followup.send(
                embed=error_embed("후기 수정 실패", "이 서버의 후기 ID를 확인해주세요."),
                ephemeral=True,
            )
            return
        review = await self.repos.brokerage.admin_override_review(
            후기id.strip(),
            interaction.user.id,
            rating=int(평점),
            content=내용,
            counts_as_problem=문제반영,
            void=무효화,
            note=사유,
        )
        if review is None:
            await interaction.followup.send(
                embed=error_embed("후기 수정 실패", "제출된 후기 ID를 확인해주세요."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                "후기 수정 완료",
                f"평점: {review.get('rating')}/5 · 문제 반영: {bool(review.get('counts_as_problem'))} · 상태: {review.get('status')}",
            ),
            ephemeral=True,
        )
        await self.send_log(
            interaction.guild,
            "관리자 후기 정정",
            f"후기 ID: `{후기id}`\n관리자: {interaction.user.mention}\n평점: {평점}/5\n문제 반영: {문제반영}\n사유: {_truncate(사유, 1_000)}",
        )

    @app_commands.command(name="거래중개설정", description="가격 점수와 좋아요 기준을 빠르게 변경합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def config_command(
        self,
        interaction: discord.Interaction,
        점수당금액: app_commands.Range[int, 1, 1_000_000_000],
        좋아요기준: app_commands.Range[int, 1, 1_000_000] = 10,
        등록최소신용도: app_commands.Range[int, -100, 100] = -100,
        예약제한분: app_commands.Range[int, 5, 1440] | None = None,
    ) -> None:
        current = await self.repos.brokerage.get_config(interaction.guild.id)
        values = {
            "price_point_unit": int(점수당금액),
            "like_score_every": int(좋아요기준),
            "minimum_listing_score": int(등록최소신용도),
            "bump_intervals": current.get("bump_intervals"),
            "like_bump_reduction_minutes": current.get("like_bump_reduction_minutes", 10),
            "reservation_timeout_minutes": (
                int(예약제한분)
                if 예약제한분 is not None
                else current.get("reservation_timeout_minutes", 30)
            ),
        }
        await self.update_config(interaction, values)

    @app_commands.command(name="거래인증코드", description="발급받은 이메일/전화번호 인증 코드를 입력합니다.")
    @app_commands.guild_only()
    @app_commands.choices(
        종류=[
            app_commands.Choice(name="이메일", value="email"),
            app_commands.Choice(name="전화번호", value="phone"),
        ]
    )
    async def verification_code_command(
        self,
        interaction: discord.Interaction,
        종류: app_commands.Choice[str],
        인증요청id: str,
        코드: str,
    ) -> None:
        await self.complete_verification(
            interaction,
            종류.value,
            인증요청id.strip(),
            코드.strip(),
        )
