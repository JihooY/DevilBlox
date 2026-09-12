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


class BrokerageListingMixin:
    async def create_listing(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
        price: int,
        quantity: int,
        notes: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if price <= 0 or price > 1_000_000_000_000:
            await interaction.followup.send(
                embed=error_embed("가격 오류", "가격은 1원 이상 1조원 이하로 입력해주세요."),
                ephemeral=True,
            )
            return
        if not 1 <= quantity <= 1_000_000:
            await interaction.followup.send(
                embed=error_embed("수량 오류", "수량은 1~1,000,000 사이여야 합니다."),
                ephemeral=True,
            )
            return
        guild_settings = await self.repos.settings.get(interaction.guild.id)
        config = await self.repos.brokerage.get_config(interaction.guild.id)
        channel_id = guild_settings["channels"].get("brokerage") or config.get(
            "listing_channel_id"
        )
        channel = interaction.guild.get_channel(channel_id or 0)
        if channel is None or not hasattr(channel, "send"):
            await interaction.followup.send(
                embed=error_embed(
                    "거래 채널 미설정",
                    "관리자가 `/채널설정`에서 `brokerage` 채널을 먼저 설정해야 합니다.",
                ),
                ephemeral=True,
            )
            return
        profile = await self.repos.brokerage.ensure_profile(
            interaction.guild.id, interaction.user.id
        )
        score = _int(profile.get("trust_score"))
        minimum_score = _int(config.get("minimum_listing_score"), -100)
        if score < minimum_score:
            await interaction.followup.send(
                embed=error_embed(
                    "거래 등록 제한",
                    f"현재 신용도 {score}점 · 등록 최소 신용도는 {minimum_score}점입니다.",
                ),
                ephemeral=True,
            )
            return
        active = await self.repos.brokerage.list_seller_listings(
            interaction.guild.id, interaction.user.id, limit=20
        )
        active_limit = 10 if score >= 80 else 7 if score >= 50 else 5 if score >= 0 else 2
        if len(active) >= active_limit:
            await interaction.followup.send(
                embed=error_embed(
                    "등록 한도 초과",
                    f"현재 등급에서는 동시에 {active_limit}개까지 등록할 수 있습니다.",
                ),
                ephemeral=True,
            )
            return
        listing = None
        try:
            listing = await self.repos.brokerage.create_listing(
                interaction.guild.id,
                interaction.user.id,
                title.strip(),
                description.strip(),
                price,
                quantity,
                contact=notes.strip(),
            )
            message, listing = await self.post_listing(interaction.guild, listing)
        except ValueError as exc:
            await interaction.followup.send(
                embed=error_embed("거래 등록 오류", str(exc)), ephemeral=True
            )
            return
        except Exception:
            log.exception("Failed to publish brokerage listing")
            if listing is not None:
                try:
                    await self.repos.brokerage.delete_listing(
                        str(listing["_id"]),
                        deleted_by=interaction.user.id,
                        reason="initial_publication_failed",
                    )
                except Exception:
                    log.exception(
                        "Failed to roll back unpublished brokerage listing: %s",
                        listing.get("_id"),
                    )
            await interaction.followup.send(
                embed=error_embed("거래 등록 실패", "거래 컨테이너를 게시하지 못했습니다."),
                ephemeral=True,
            )
            return
        self.bot.add_view(BrokerageListingView(self, listing, profile, config))
        registered = getattr(self.bot, "_devilblox_brokerage_view_ids", set())
        registered.add(f"listing:{listing['_id']}")
        await interaction.followup.send(
            embed=success_embed(
                "거래 등록 완료",
                f"거래 ID: `{listing['_id']}`\n게시물: {message.jump_url}",
            ),
            ephemeral=True,
        )
        await self.send_log(
            interaction.guild,
            "거래 등록",
            f"판매자: {interaction.user.mention}\n거래 ID: `{listing['_id']}`\n가격: {price:,}원",
        )

    async def notification_members(
        self,
        guild: discord.Guild,
        listing: dict,
        seller_profile: dict,
    ) -> tuple[list[discord.Member], list[int]]:
        settings = await self.repos.settings.get(guild.id)
        role_id = settings["roles"].get("brokerage_alert")
        if not role_id:
            return [], []
        preferences = await self.repos.brokerage.list_notification_preferences(guild.id)
        seller_score = _int(seller_profile.get("trust_score"))
        previously_notified = set(listing.get("notified_user_ids") or [])
        members: list[discord.Member] = []
        mark_once: list[int] = []
        for preference in preferences:
            user_id = _int(preference.get("user_id"))
            if not user_id or user_id == listing.get("seller_id"):
                continue
            if seller_score < _int(preference.get("min_seller_score"), -100):
                continue
            if preference.get("no_duplicates", True) and user_id in previously_notified:
                continue
            member = guild.get_member(user_id)
            if member is None or not has_role(member, role_id):
                continue
            members.append(member)
            if preference.get("no_duplicates", True):
                mark_once.append(user_id)
            if len(members) >= MAX_NOTIFICATION_MENTIONS:
                break
        return members, mark_once

    async def post_listing(
        self,
        guild: discord.Guild,
        listing: dict,
        *,
        replace_previous: bool = False,
    ) -> tuple[discord.Message, dict]:
        settings = await self.repos.settings.get(guild.id)
        config = await self.repos.brokerage.get_config(guild.id)
        channel_id = settings["channels"].get("brokerage") or config.get("listing_channel_id")
        channel = guild.get_channel(channel_id or 0)
        if channel is None or not hasattr(channel, "send"):
            raise RuntimeError("brokerage listing channel is not configured")
        profile = await self.repos.brokerage.ensure_profile(guild.id, int(listing["seller_id"]))
        effective_interval = _listing_interval(
            config,
            _int(profile.get("trust_score")),
            _int(listing.get("like_count")),
        )
        effective_minutes = max(5, int(effective_interval.total_seconds() // 60))
        listing = {**listing, "bump_interval_minutes": effective_minutes}
        notification_members, mark_once = await self.notification_members(
            guild, listing, profile
        )
        mention_content = " ".join(member.mention for member in notification_members)
        mentions = (
            discord.AllowedMentions(
                everyone=False,
                users=notification_members,
                roles=False,
                replied_user=False,
            )
            if notification_members
            else discord.AllowedMentions.none()
        )
        view = BrokerageListingView(
            self,
            listing,
            profile,
            config,
            notification_mentions=mention_content,
        )
        message = await channel.send(
            **_layout_send_kwargs(view, mentions=mentions),
        )
        try:
            next_bump_at = _now() + timedelta(minutes=effective_minutes)
            bound = await self.repos.brokerage.bind_listing_message(
                str(listing["_id"]),
                channel.id,
                message.id,
                next_bump_at,
                bump_interval_minutes=effective_minutes,
            )
            if bound is None:
                raise RuntimeError("listing was closed while it was being posted")
        except Exception:
            try:
                await message.delete()
            except Exception:
                log.warning(
                    "Failed to remove an unbound brokerage post: listing_id=%s message_id=%s",
                    listing.get("_id"),
                    getattr(message, "id", None),
                )
            raise
        if mark_once:
            try:
                marked = await self.repos.brokerage.mark_notified_users(
                    str(listing["_id"]), mark_once
                )
                if marked and marked.get("listing"):
                    bound = marked["listing"]
            except Exception:
                # The post is already visible and safely bound.  Notification
                # bookkeeping failure must not create another public copy.
                log.exception(
                    "Failed to persist brokerage notification recipients: listing_id=%s",
                    listing.get("_id"),
                )
        if replace_previous:
            old_channel_id = listing.get("channel_id")
            old_message_id = listing.get("message_id")
            if old_channel_id and old_message_id and old_message_id != message.id:
                old_channel = guild.get_channel(int(old_channel_id))
                if old_channel and hasattr(old_channel, "get_partial_message"):
                    try:
                        await old_channel.get_partial_message(int(old_message_id)).delete()
                    except (discord.NotFound, discord.Forbidden):
                        pass
                    except discord.HTTPException:
                        log.warning(
                            "Failed to remove old brokerage post: listing_id=%s message_id=%s",
                            listing["_id"],
                            old_message_id,
                        )
        return message, bound

    async def refresh_listing_message(self, listing: dict) -> None:
        channel_id = listing.get("channel_id")
        message_id = listing.get("message_id")
        guild = self.bot.get_guild(_int(listing.get("guild_id")))
        if guild is None or not channel_id or not message_id:
            return
        channel = guild.get_channel(_int(channel_id))
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(_int(message_id))
            profile = await self.repos.brokerage.ensure_profile(
                guild.id, _int(listing.get("seller_id"))
            )
            config = await self.repos.brokerage.get_config(guild.id)
            await message.edit(
                content=None,
                view=BrokerageListingView(self, listing, profile, config),
                attachments=list(message.attachments),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden):
            return
        except discord.HTTPException:
            log.warning("Failed to refresh brokerage listing: %s", listing.get("_id"))

    async def like_listing(self, interaction: discord.Interaction, listing_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        listing = await self.repos.brokerage.get_listing(listing_id)
        if (
            listing is None
            or interaction.guild is None
            or _int(listing.get("guild_id")) != interaction.guild.id
            or listing.get("status") not in LISTING_STATES_OPEN
        ):
            await interaction.followup.send(
                embed=error_embed("거래 종료", "더 이상 좋아요를 누를 수 없는 거래입니다."),
                ephemeral=True,
            )
            return
        if listing.get("seller_id") == interaction.user.id:
            await interaction.followup.send(
                embed=error_embed("좋아요 불가", "자신의 거래에는 좋아요를 누를 수 없습니다."),
                ephemeral=True,
            )
            return
        result = await self.repos.brokerage.add_like(listing_id, interaction.user.id)
        if not result:
            await interaction.followup.send(
                embed=error_embed("처리 실패", "좋아요를 처리하지 못했습니다."), ephemeral=True
            )
            return
        await self.refresh_listing_message(result["listing"])
        if not result.get("new"):
            description = "이미 이 거래에 좋아요를 눌렀습니다."
        elif result.get("grant"):
            description = (
                f"좋아요 {result['count']}개 · 판매자 신용도 +{result['grant']}점이 지급되었습니다."
            )
        else:
            description = f"좋아요 {result['count']}개"
        await interaction.followup.send(
            embed=success_embed("좋아요", description), ephemeral=True
        )

    async def report_listing(
        self,
        interaction: discord.Interaction,
        listing_id: str,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        listing = await self.repos.brokerage.get_listing(listing_id)
        if (
            listing is None
            or interaction.guild is None
            or _int(listing.get("guild_id")) != interaction.guild.id
        ):
            await interaction.followup.send(
                embed=error_embed("거래 없음", "이 서버의 거래를 찾을 수 없습니다."),
                ephemeral=True,
            )
            return
        result = await self.repos.brokerage.add_report(
            listing_id,
            interaction.user.id,
            _truncate(reason, 100),
            details=reason,
        )
        if not result or not result.get("new"):
            await interaction.followup.send(
                embed=error_embed(
                    "신고 처리 불가",
                    "거래를 찾을 수 없거나 이미 신고한 거래입니다.",
                ),
                ephemeral=True,
            )
            return
        settings = await self.repos.settings.get(interaction.guild.id)
        report_channel = interaction.guild.get_channel(
            settings["channels"].get("brokerage_report") or 0
        )
        if report_channel and hasattr(report_channel, "send"):
            report = result.get("report") or {}
            await report_channel.send(
                embed=info_embed(
                    "거래중개 신고",
                    "\n".join(
                        (
                            f"거래 ID: `{listing_id}`",
                            f"판매자: <@{result['listing'].get('seller_id')}>",
                            f"신고자: {interaction.user.mention}",
                            f"사유: {_truncate(reason, 1_500)}",
                            f"신고 ID: `{report.get('_id', '-')}`",
                        )
                    ),
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await interaction.followup.send(
            embed=success_embed("신고 접수 완료", "관리자에게 신고가 전달되었습니다."),
            ephemeral=True,
        )

    async def reserve_listing(
        self,
        interaction: discord.Interaction,
        listing_id: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        listing = await self.repos.brokerage.get_listing(listing_id)
        if (
            listing is None
            or interaction.guild is None
            or _int(listing.get("guild_id")) != interaction.guild.id
            or listing.get("status") not in LISTING_STATES_OPEN
        ):
            await interaction.followup.send(
                embed=error_embed("판매 종료", "이미 판매되었거나 삭제된 거래입니다."),
                ephemeral=True,
            )
            return
        if listing.get("seller_id") == interaction.user.id:
            await interaction.followup.send(
                embed=error_embed("구매 불가", "자신이 등록한 거래는 구매할 수 없습니다."),
                ephemeral=True,
            )
            return
        result = await self.repos.brokerage.enqueue_buyer(listing_id, interaction.user.id)
        if not result or result.get("status") == "unavailable":
            await interaction.followup.send(
                embed=error_embed("예약 불가", "거래가 종료되었거나 예약할 수 없습니다."),
                ephemeral=True,
            )
            return
        listing = result["listing"]
        if result.get("status") == "active":
            if result.get("new"):
                try:
                    channel = await self.open_trade_ticket(
                        interaction.guild, listing, interaction.user.id
                    )
                except (discord.HTTPException, RuntimeError):
                    log.exception("Failed to open brokerage trade ticket: %s", listing_id)
                    await self.repos.brokerage.cancel_reservation_and_promote(
                        listing_id,
                        interaction.user.id,
                        self.bot.user.id,
                        reason="ticket_creation_failed",
                    )
                    await interaction.followup.send(
                        embed=error_embed(
                            "티켓 생성 실패",
                            "예약을 되돌렸습니다. 관리자에게 카테고리와 권한 설정 확인을 요청해주세요.",
                        ),
                        ephemeral=True,
                    )
                    return
                description = (
                    f"예약 번호 `{result.get('reservation_number')}` · 거래 티켓 {channel.mention}"
                )
            else:
                channel_id = listing.get("active_ticket_channel_id")
                channel = interaction.guild.get_channel(_int(channel_id)) if channel_id else None
                description = (
                    f"현재 구매 순번입니다. 거래 티켓: {channel.mention if channel else '생성 대기중'}"
                )
            title = "구매 순번 활성화"
        else:
            title = "예약 완료"
            description = (
                f"예약 번호 `{result.get('reservation_number')}` · 현재 대기 순번 `{result.get('position')}`번"
            )
        await self.refresh_listing_message(listing)
        await interaction.followup.send(
            embed=success_embed(title, description), ephemeral=True
        )
