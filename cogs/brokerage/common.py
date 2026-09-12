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


log = logging.getLogger(__name__)

MAX_NOTIFICATION_MENTIONS = 40
LISTING_STATES_OPEN = {"open", "reserved"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _discord_time(value, style: str = "R") -> str:
    if not hasattr(value, "timestamp"):
        return "-"
    return f"<t:{int(value.timestamp())}:{style}>"


def _truncate(value: str, limit: int) -> str:
    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _trust_tier(score: int) -> str:
    if score >= 80:
        return "최우수"
    if score >= 50:
        return "우수"
    if score >= 0:
        return "일반"
    if score >= -50:
        return "주의"
    return "거래 제한"


def _penalty_percent(problem_count: int) -> int:
    return penalty_level(max(0, problem_count)) * 10


def _tier_minutes(config: dict, minimum_score: int, default: int) -> int:
    for item in config.get("bump_intervals") or []:
        if isinstance(item, dict) and _int(item.get("min_score"), -101) == minimum_score:
            return max(1, _int(item.get("minutes"), default))
    return default


def _price_policy(config: dict) -> PricePointPolicy:
    return PricePointPolicy(
        unit_price_krw=max(1, _int(config.get("price_point_unit"), 1_000)),
        minimum_points=max(1, _int(config.get("min_price_points"), 1)),
        maximum_points=max(1, _int(config.get("max_price_points"), 10)),
    )


def _posting_policy(config: dict) -> PostingIntervalPolicy:
    tiers = {
        _int(item.get("min_score")): _int(item.get("minutes"))
        for item in config.get("bump_intervals") or []
        if isinstance(item, dict)
    }
    return PostingIntervalPolicy(
        high_score_minimum=80,
        medium_score_minimum=50,
        nonnegative_minimum=0,
        high_interval=timedelta(minutes=max(1, tiers.get(80, 10))),
        medium_interval=timedelta(minutes=max(1, tiers.get(50, 20))),
        nonnegative_interval=timedelta(
            minutes=max(1, tiers.get(0, 30))
        ),
        negative_interval=timedelta(
            minutes=max(1, tiers.get(-100, 60))
        ),
    )


def _listing_interval(config: dict, seller_score: int, like_count: int = 0) -> timedelta:
    base_minutes = max(
        1,
        int(
            posting_interval_for_score(
                seller_score, policy=_posting_policy(config)
            ).total_seconds()
            // 60
        ),
    )
    like_count = max(0, _int(like_count))
    threshold = max(0, _int(config.get("like_fast_bump_threshold"), 10))
    percent = max(0, min(90, _int(config.get("like_fast_bump_percent"), 0)))
    if threshold and like_count >= threshold and percent:
        base_minutes = max(1, (base_minutes * (100 - percent) + 99) // 100)

    every = max(1, _int(config.get("like_score_every"), 10))
    bonus_each = max(0, _int(config.get("like_score_bonus"), 1))
    maximum_bonus = max(0, _int(config.get("like_score_max_bonus"), 3))
    like_credit = min(maximum_bonus, (like_count // every) * bonus_each)
    reduction_minutes = (
        max(0, _int(config.get("like_bump_reduction_minutes"), 2)) * like_credit
    )
    return timedelta(minutes=max(5, base_minutes - reduction_minutes))


def _add_brand_section(container: discord.ui.Container, content: str) -> None:
    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(content),
            accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
        )
    )


def _layout_send_kwargs(view: discord.ui.LayoutView, *, mentions=None) -> dict:
    kwargs: dict = {
        "view": view,
        "allowed_mentions": mentions or discord.AllowedMentions.none(),
    }
    files = branded_files()
    if files:
        kwargs["files"] = files
    return kwargs


def _profile_markdown(member: discord.abc.User, profile: dict, config: dict) -> str:
    score = _int(profile.get("trust_score"))
    problems = max(0, _int(profile.get("problem_count")))
    interval = _listing_interval(config, score)
    verified_kinds = set(profile.get("verified_kinds") or [])
    email_verified = "email" in verified_kinds
    phone_verified = "phone" in verified_kinds
    email_bonus = max(0, _int(config.get("email_verification_bonus"), 10))
    phone_bonus = max(0, _int(config.get("phone_verification_bonus"), 20))
    return "\n".join(
        (
            "## 내 거래중개 정보",
            f"**사용자**  {member.mention} (`{member.id}`)",
            f"**신용도**  `{score}/100` · {_trust_tier(score)}",
            f"**문제 횟수**  `{problems}회`",
            f"**패널티**  `{penalty_level(problems)}단계` · 다음 감점 `+{_penalty_percent(problems)}%`",
            f"**재등록 주기**  `{int(interval.total_seconds() // 60)}분`",
            "",
            f"**이메일 인증**  {f'완료 (+{email_bonus})' if email_verified else '미인증'}",
            f"**전화번호 인증**  {f'완료 (+{phone_bonus})' if phone_verified else '미인증'}",
            f"**정상 거래**  `{_int(profile.get('successful_trade_count'))}회`",
            f"**받은 좋아요 보상**  `+{_int(profile.get('like_score_total'))}점`",
        )
    )


def _listing_markdown(listing: dict, seller_profile: dict, config: dict) -> str:
    score = _int(seller_profile.get("trust_score"))
    problems = max(0, _int(seller_profile.get("problem_count")))
    price = max(0, _int(listing.get("price")))
    queue = list(listing.get("buyer_queue") or listing.get("queue") or [])
    status = str(listing.get("status") or "open")
    status_label = {
        "open": "판매중",
        "reserved": "예약 거래중",
        "settling": "판매 완료 · 보호기간",
        "completed": "거래 확정",
        "sold": "판매 완료",
        "deleted": "관리자 삭제",
    }.get(status, status)
    points = price_to_base_points(max(1, price), policy=_price_policy(config))
    interval = timedelta(
        minutes=max(
            1,
            _int(
                listing.get("bump_interval_minutes"),
                int(_listing_interval(config, score).total_seconds() // 60),
            ),
        )
    )
    return "\n".join(
        (
            f"## {_truncate(listing.get('title') or '거래 상품', 100)}",
            f"{_truncate(listing.get('description') or '-', 1_200)}",
            "",
            f"**가격**  `{price:,}원` · 보호기간 후 `+{points}점` 대상",
            f"**수량**  `{max(1, _int(listing.get('quantity'), 1))}개` (전체 일괄 거래)",
            "-# 표시된 수량 전체를 하나의 거래로 처리합니다.",
            f"**판매자**  <@{listing.get('seller_id')}> · 신용도 `{score}` ({_trust_tier(score)})",
            f"**문제/패널티**  `{problems}회` · `{penalty_level(problems)}단계`",
            f"**상태**  `{status_label}` · 예약 `{len(queue)}명` · 좋아요 `{_int(listing.get('like_count'))}개`",
            f"**재등록 주기**  `{int(interval.total_seconds() // 60)}분`",
            f"**안내**  {_truncate(listing.get('notes') or listing.get('contact') or '티켓에서 안전하게 거래를 진행해주세요.', 500)}",
            "",
            f"-# 거래 ID: {listing.get('_id')} · 등록 {_discord_time(listing.get('created_at'))}",
        )
    )
