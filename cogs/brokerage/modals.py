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


class BrokerageListingModal(discord.ui.Modal):
    def __init__(self, cog: "BrokerageCog"):
        super().__init__(title="거래 등록")
        self.cog = cog
        self.title_input = discord.ui.TextInput(
            label="상품명",
            placeholder="판매할 상품 이름",
            min_length=2,
            max_length=100,
        )
        self.description_input = discord.ui.TextInput(
            label="상품 설명",
            style=discord.TextStyle.paragraph,
            placeholder="구성, 상태, 전달 방식 등을 적어주세요.",
            min_length=2,
            max_length=1_200,
        )
        self.price_input = discord.ui.TextInput(
            label="가격 (원)",
            placeholder="예: 3000",
            min_length=1,
            max_length=12,
        )
        self.quantity_input = discord.ui.TextInput(
            label="수량",
            placeholder="예: 1",
            default="1",
            min_length=1,
            max_length=5,
        )
        self.notes_input = discord.ui.TextInput(
            label="거래 안내 / 주의사항",
            style=discord.TextStyle.paragraph,
            placeholder="티켓에서 구매자에게 안내할 내용을 적어주세요.",
            required=False,
            max_length=500,
        )
        for item in (
            self.title_input,
            self.description_input,
            self.price_input,
            self.quantity_input,
            self.notes_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            price = int(str(self.price_input.value).replace(",", "").strip())
            quantity = int(str(self.quantity_input.value).replace(",", "").strip())
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed("입력 오류", "가격과 수량은 숫자로 입력해주세요."),
                ephemeral=True,
            )
            return
        await self.cog.create_listing(
            interaction,
            title=str(self.title_input.value),
            description=str(self.description_input.value),
            price=price,
            quantity=quantity,
            notes=str(self.notes_input.value or ""),
        )

class BrokerageReportModal(discord.ui.Modal):
    def __init__(self, cog: "BrokerageCog", listing_id: str):
        super().__init__(title="거래 신고")
        self.cog = cog
        self.listing_id = listing_id
        self.reason = discord.ui.TextInput(
            label="신고 사유",
            style=discord.TextStyle.paragraph,
            placeholder="관리자가 확인할 수 있도록 구체적으로 적어주세요.",
            min_length=5,
            max_length=1_000,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.report_listing(interaction, self.listing_id, str(self.reason.value))

class BrokerageNotificationModal(discord.ui.Modal):
    def __init__(self, cog: "BrokerageCog", preferences: dict):
        super().__init__(title="거래 알림 세부 설정")
        self.cog = cog
        self.minimum_score = discord.ui.TextInput(
            label="최소 판매자 신용도 (-100~100)",
            default=str(_int(preferences.get("min_seller_score"), -100)),
            min_length=1,
            max_length=4,
        )
        self.no_duplicates = discord.ui.TextInput(
            label="같은 거래 중복 알림 차단 (예/아니오)",
            default="예" if preferences.get("no_duplicates", True) else "아니오",
            min_length=1,
            max_length=5,
        )
        self.add_item(self.minimum_score)
        self.add_item(self.no_duplicates)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            minimum_score = int(str(self.minimum_score.value).strip())
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed("입력 오류", "최소 신용도는 -100~100 숫자여야 합니다."),
                ephemeral=True,
            )
            return
        duplicate_text = str(self.no_duplicates.value).strip().casefold()
        if duplicate_text in {"예", "네", "yes", "y", "1", "true"}:
            no_duplicates = True
        elif duplicate_text in {"아니오", "아니요", "no", "n", "0", "false"}:
            no_duplicates = False
        else:
            await interaction.response.send_message(
                embed=error_embed("입력 오류", "중복 알림 차단은 예 또는 아니오로 입력해주세요."),
                ephemeral=True,
            )
            return
        await self.cog.update_notification_preferences(
            interaction,
            minimum_score=minimum_score,
            no_duplicates=no_duplicates,
        )

class BrokerageVerificationDestinationModal(discord.ui.Modal):
    def __init__(self, cog: "BrokerageCog", kind: Literal["email", "phone"]):
        super().__init__(title="이메일 인증" if kind == "email" else "전화번호 인증")
        self.cog = cog
        self.kind = kind
        self.destination = discord.ui.TextInput(
            label="이메일 주소" if kind == "email" else "전화번호 (국가번호 포함)",
            placeholder="name@example.com" if kind == "email" else "+821012345678",
            min_length=5,
            max_length=254 if kind == "email" else 24,
        )
        self.add_item(self.destination)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.start_verification(
            interaction,
            self.kind,
            str(self.destination.value),
        )

class BrokerageVerificationCodeModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "BrokerageCog",
        kind: Literal["email", "phone"],
        challenge_id: str,
    ):
        super().__init__(title="인증 코드 확인")
        self.cog = cog
        self.kind = kind
        self.challenge_id = challenge_id
        self.code = discord.ui.TextInput(
            label="6자리 인증 코드",
            placeholder="123456",
            min_length=6,
            max_length=6,
        )
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.complete_verification(
            interaction,
            self.kind,
            self.challenge_id,
            str(self.code.value),
        )

class BrokerageDeleteModal(discord.ui.Modal):
    def __init__(self, cog: "BrokerageCog"):
        super().__init__(title="등록 거래 삭제")
        self.cog = cog
        self.listing_id = discord.ui.TextInput(label="거래 ID", min_length=4, max_length=40)
        self.reason = discord.ui.TextInput(
            label="삭제 사유",
            style=discord.TextStyle.paragraph,
            min_length=2,
            max_length=500,
        )
        self.add_item(self.listing_id)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.admin_delete_listing(
            interaction,
            str(self.listing_id.value).strip(),
            str(self.reason.value),
        )

class BrokerageAdminAdjustmentModal(discord.ui.Modal):
    def __init__(self, cog: "BrokerageCog"):
        super().__init__(title="신용도 / 문제 조정")
        self.cog = cog
        self.user_id = discord.ui.TextInput(label="사용자 Discord ID", min_length=5, max_length=24)
        self.score_delta = discord.ui.TextInput(
            label="신용도 증감 (-200~200)", default="0", min_length=1, max_length=4
        )
        self.problem_delta = discord.ui.TextInput(
            label="문제 횟수 증감", default="0", min_length=1, max_length=4
        )
        self.reason = discord.ui.TextInput(
            label="조정 사유",
            style=discord.TextStyle.paragraph,
            min_length=2,
            max_length=500,
        )
        for item in (self.user_id, self.score_delta, self.problem_delta, self.reason):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            user_id = int(str(self.user_id.value).strip())
            score_delta = int(str(self.score_delta.value).strip())
            problem_delta = int(str(self.problem_delta.value).strip())
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed("입력 오류", "사용자 ID와 조정값은 숫자여야 합니다."),
                ephemeral=True,
            )
            return
        await self.cog.admin_adjust_profile(
            interaction,
            user_id=user_id,
            score_delta=score_delta,
            problem_delta=problem_delta,
            reason=str(self.reason.value),
        )

class BrokerageConfigModal(discord.ui.Modal):
    def __init__(self, cog: "BrokerageCog", config: dict):
        super().__init__(title="거래중개 운영 설정")
        self.cog = cog
        self.price_unit = discord.ui.TextInput(
            label="신용도 1점당 가격 (원)",
            default=str(_int(config.get("price_point_unit"), 1_000)),
            min_length=1,
            max_length=12,
        )
        self.likes_per_credit = discord.ui.TextInput(
            label="신용도 +1에 필요한 좋아요",
            default=str(_int(config.get("like_score_every"), 10)),
            min_length=1,
            max_length=5,
        )
        self.minimum_listing_score = discord.ui.TextInput(
            label="거래 등록 최소 신용도",
            default=str(_int(config.get("minimum_listing_score"), -100)),
            min_length=1,
            max_length=4,
        )
        self.intervals = discord.ui.TextInput(
            label="재등록 주기: 최우수,우수,일반,주의 (분)",
            default=",".join(
                str(_tier_minutes(config, score, default))
                for score, default in (
                    (80, 10),
                    (50, 20),
                    (0, 30),
                    (-100, 60),
                )
            ),
            min_length=7,
            max_length=30,
        )
        self.like_bump_reduction = discord.ui.TextInput(
            label="좋아요 보상 1점당 재등록 단축 (분)",
            default=str(_int(config.get("like_bump_reduction_minutes"), 2)),
            min_length=1,
            max_length=4,
        )
        for item in (
            self.price_unit,
            self.likes_per_credit,
            self.minimum_listing_score,
            self.intervals,
            self.like_bump_reduction,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            interval_values = [int(item.strip()) for item in str(self.intervals.value).split(",")]
            if len(interval_values) != 4:
                raise ValueError
            values = {
                "price_point_unit": int(str(self.price_unit.value).replace(",", "").strip()),
                "like_score_every": int(str(self.likes_per_credit.value).strip()),
                "minimum_listing_score": int(str(self.minimum_listing_score.value).strip()),
                "bump_intervals": [
                    {"min_score": score, "minutes": minutes}
                    for score, minutes in zip((80, 50, 0, -100), interval_values)
                ],
                "like_bump_reduction_minutes": int(str(self.like_bump_reduction.value).strip()),
            }
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed("입력 오류", "모든 설정은 숫자이며 주기는 쉼표로 구분한 숫자 4개여야 합니다."),
                ephemeral=True,
            )
            return
        await self.cog.update_config(interaction, values)

class BrokerageReviewModal(discord.ui.Modal):
    def __init__(self, cog: "BrokerageCog", review_id: str):
        super().__init__(title="거래 후기 작성")
        self.cog = cog
        self.review_id = review_id
        self.rating = discord.ui.TextInput(
            label="평점 (1~5)", placeholder="5", min_length=1, max_length=1
        )
        self.content = discord.ui.TextInput(
            label="후기 내용",
            style=discord.TextStyle.paragraph,
            min_length=2,
            max_length=1_000,
        )
        self.add_item(self.rating)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            rating = int(str(self.rating.value).strip())
        except ValueError:
            rating = 0
        await self.cog.submit_review(
            interaction,
            self.review_id,
            rating=rating,
            content=str(self.content.value),
        )
