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


class BrokerageTradeMixin:
    async def open_trade_ticket(
        self,
        guild: discord.Guild,
        listing: dict,
        buyer_id: int,
    ) -> discord.TextChannel:
        if _int(listing.get("guild_id")) != guild.id:
            raise RuntimeError("listing does not belong to this guild")
        seller = guild.get_member(_int(listing.get("seller_id")))
        buyer = guild.get_member(buyer_id)
        if seller is None or buyer is None:
            raise RuntimeError("seller or buyer is no longer in the guild")
        settings = await self.repos.settings.get(guild.id)
        category = guild.get_channel(settings["categories"].get("brokerage_ticket") or 0)
        helper_role = guild.get_role(settings["roles"].get("brokerage_helper") or 0)
        admin_role = guild.get_role(settings["roles"].get("admin") or 0)
        participant_overwrite = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
            use_application_commands=True,
        )
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            seller: participant_overwrite,
            buyer: participant_overwrite,
        }
        if helper_role is not None:
            overwrites[helper_role] = participant_overwrite
        if admin_role is not None:
            overwrites[admin_role] = participant_overwrite
        channel = await guild.create_text_channel(
            name=safe_channel_name(
                "거래",
                str(listing.get("title") or "상품"),
                buyer.display_name,
            ),
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason=f"DevilBlox brokerage ticket {listing.get('_id')}",
        )
        view = BrokerageTicketView(
            self,
            str(listing["_id"]),
            buyer_id,
            participant_mentions=f"{seller.mention} {buyer.mention}",
        )
        try:
            message = await channel.send(
                **_layout_send_kwargs(
                    view,
                    mentions=discord.AllowedMentions(
                        everyone=False,
                        users=[seller, buyer],
                        roles=False,
                        replied_user=False,
                    ),
                ),
            )
            bound = await self.repos.brokerage.bind_active_ticket(
                str(listing["_id"]), buyer_id, channel.id, message.id
            )
            if bound is None:
                raise RuntimeError("reservation changed during ticket creation")
            await self.repos.tickets.create(
                guild.id,
                "brokerage",
                buyer_id,
                channel.id,
                seller_id=seller.id,
                listing_id=str(listing["_id"]),
                reservation_number=listing.get("current_reservation_number"),
            )
        except Exception:
            try:
                await channel.delete(reason="Brokerage ticket creation did not complete")
            except discord.HTTPException:
                pass
            raise
        self.bot.add_view(view)
        registered = getattr(self.bot, "_devilblox_brokerage_view_ids", set())
        registered.add(f"ticket:{listing['_id']}:{buyer_id}")
        try:
            await buyer.send(
                embed=success_embed(
                    "구매 순번 도착",
                    f"`{listing.get('title')}` 거래 티켓이 열렸습니다: {channel.mention}",
                )
            )
        except discord.HTTPException:
            pass
        return channel

    async def archive_trade_ticket(
        self,
        guild: discord.Guild,
        channel_id: int | None,
        *,
        buyer_id: int | None,
        seller_id: int | None,
        reason: str,
    ) -> None:
        if not channel_id:
            return
        channel = guild.get_channel(_int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        for user_id in (buyer_id, seller_id):
            member = guild.get_member(_int(user_id)) if user_id else None
            if member is None:
                continue
            try:
                await channel.set_permissions(
                    member,
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                    reason=reason,
                )
            except discord.HTTPException:
                pass
        settings = await self.repos.settings.get(guild.id)
        closed = guild.get_channel(settings["categories"].get("brokerage_closed") or 0)
        if isinstance(closed, discord.CategoryChannel):
            try:
                await channel.edit(category=closed, reason=reason)
            except discord.HTTPException:
                pass
        ticket = await self.repos.tickets.get_by_channel(guild.id, channel.id, "brokerage")
        if ticket and ticket.get("status") == "open":
            await self.repos.tickets.close(
                guild.id,
                channel.id,
                close_reason=reason,
                closed_by=self.bot.user.id,
            )

    async def complete_sale(
        self,
        interaction: discord.Interaction,
        listing_id: str,
        buyer_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        listing = await self.repos.brokerage.get_listing(listing_id)
        if listing is None:
            await interaction.followup.send(
                embed=error_embed("거래 없음", "거래를 찾을 수 없습니다."), ephemeral=True
            )
            return
        if not await self.is_trade_staff(interaction, listing):
            await interaction.followup.send(
                embed=error_embed("권한 없음", "판매자, 거래 도우미 또는 관리자만 완료할 수 있습니다."),
                ephemeral=True,
            )
            return
        old_channel_id = listing.get("active_ticket_channel_id")
        waiting_ids = [
            _int(item.get("user_id"))
            for item in listing.get("buyer_queue") or []
            if item.get("state") == "waiting"
        ]
        result = await self.repos.brokerage.complete_sale(
            listing_id, buyer_id, interaction.user.id
        )
        if not result or result.get("status") != "sold":
            await interaction.followup.send(
                embed=error_embed("완료 불가", "현재 활성 구매자가 아니거나 이미 처리된 거래입니다."),
                ephemeral=True,
            )
            return
        if result.get("already_completed"):
            await interaction.followup.send(
                embed=success_embed("이미 완료된 거래", "이 거래는 이미 판매 완료 처리되었습니다."),
                ephemeral=True,
            )
            return
        listing = result["listing"]
        await self.refresh_listing_message(listing)
        await self.archive_trade_ticket(
            interaction.guild,
            _int(old_channel_id),
            buyer_id=buyer_id,
            seller_id=_int(listing.get("seller_id")),
            reason="Brokerage trade completed",
        )
        review = result.get("review")
        buyer = interaction.guild.get_member(buyer_id) or self.bot.get_user(buyer_id)
        if review and buyer:
            review_view = BrokerageReviewRequestView(self, review)
            try:
                await buyer.send(**_layout_send_kwargs(review_view))
            except discord.HTTPException:
                pass
            self.bot.add_view(review_view)
            registered = getattr(self.bot, "_devilblox_brokerage_view_ids", set())
            registered.add(f"review:{review['_id']}")
        for waiting_id in waiting_ids:
            user = interaction.guild.get_member(waiting_id) or self.bot.get_user(waiting_id)
            if user is None:
                continue
            try:
                await user.send(
                    embed=info_embed(
                        "거래 판매 완료",
                        f"예약하신 `{listing.get('title')}` 상품이 앞 순번에서 판매되었습니다.",
                    )
                )
            except discord.HTTPException:
                pass
        due_at = (listing.get("settlement") or {}).get("due_at")
        await interaction.followup.send(
            embed=success_embed(
                "거래 완료 처리",
                f"환불·회수 문제가 없으면 {_discord_time(due_at, 'F')}에 신용도가 자동 반영됩니다.",
            ),
            ephemeral=True,
        )
        await self.send_log(
            interaction.guild,
            "거래 완료",
            f"거래 ID: `{listing_id}`\n판매자: <@{listing.get('seller_id')}>\n구매자: <@{buyer_id}>\n금액: {_int(listing.get('price')):,}원",
        )

    async def cancel_and_promote(
        self,
        interaction: discord.Interaction,
        listing_id: str,
        buyer_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        listing = await self.repos.brokerage.get_listing(listing_id)
        if listing is None:
            await interaction.followup.send(
                embed=error_embed("거래 없음", "거래를 찾을 수 없습니다."), ephemeral=True
            )
            return
        if interaction.guild is None or _int(listing.get("guild_id")) != interaction.guild.id:
            await interaction.followup.send(
                embed=error_embed("거래 없음", "이 서버의 거래를 찾을 수 없습니다."),
                ephemeral=True,
            )
            return
        allowed = interaction.user.id == buyer_id or await self.is_trade_staff(
            interaction, listing
        )
        if not allowed:
            await interaction.followup.send(
                embed=error_embed("권한 없음", "현재 구매자, 판매자 또는 거래 도우미만 처리할 수 있습니다."),
                ephemeral=True,
            )
            return
        old_channel_id = listing.get("active_ticket_channel_id")
        result = await self.repos.brokerage.cancel_reservation_and_promote(
            listing_id,
            buyer_id,
            interaction.user.id,
            reason="trade_cancelled",
        )
        if not result or result.get("status") == "not_active":
            await interaction.followup.send(
                embed=error_embed("처리 불가", "현재 활성 구매 순번이 아닙니다."), ephemeral=True
            )
            return
        listing = result["listing"]
        await self.archive_trade_ticket(
            interaction.guild,
            _int(old_channel_id),
            buyer_id=buyer_id,
            seller_id=_int(listing.get("seller_id")),
            reason="Brokerage trade cancelled",
        )
        next_buyer_id = result.get("next_buyer_id")
        channel = None
        if next_buyer_id:
            try:
                channel = await self.open_trade_ticket(
                    interaction.guild, listing, _int(next_buyer_id)
                )
            except (discord.HTTPException, RuntimeError):
                log.exception(
                    "Failed to open promoted brokerage ticket: listing_id=%s buyer_id=%s",
                    listing_id,
                    next_buyer_id,
                )
        await self.refresh_listing_message(listing)
        if next_buyer_id:
            description = (
                f"다음 예약자 <@{next_buyer_id}>에게 넘어갔습니다."
                + (f" 티켓: {channel.mention}" if channel else " 티켓 생성은 재시도 대기중입니다.")
            )
        else:
            description = "대기자가 없어 다시 구매 가능한 상태가 되었습니다."
        await interaction.followup.send(
            embed=success_embed("예약 순번 처리 완료", description), ephemeral=True
        )

    async def submit_review(
        self,
        interaction: discord.Interaction,
        review_id: str,
        *,
        rating: int,
        content: str,
    ) -> None:
        private = interaction.guild is not None
        await interaction.response.defer(ephemeral=private)
        if not 1 <= rating <= 5:
            await interaction.followup.send(
                embed=error_embed("평점 오류", "평점은 1~5 사이 숫자여야 합니다."),
                ephemeral=private,
            )
            return
        content = content.strip()
        if len(content) < 2:
            await interaction.followup.send(
                embed=error_embed("후기 오류", "후기는 2자 이상 입력해주세요."),
                ephemeral=private,
            )
            return
        review = await self.repos.brokerage.submit_review(
            review_id,
            interaction.user.id,
            rating=rating,
            content=content,
        )
        if review is None:
            await interaction.followup.send(
                embed=error_embed(
                    "후기 작성 불가",
                    "본인의 후기 요청이 아니거나 1주일 작성 기한이 지났습니다.",
                ),
                ephemeral=private,
            )
            return
        problem_notice = (
            " 평점 2점 이하로 판매자의 문제 횟수에 1회 반영되었습니다."
            if review.get("counts_as_problem")
            else ""
        )
        await interaction.followup.send(
            embed=success_embed("후기 등록 완료", f"소중한 후기 감사합니다.{problem_notice}"),
            ephemeral=private,
        )
        guild = self.bot.get_guild(_int(review.get("guild_id")))
        if guild:
            await self.send_log(
                guild,
                "거래 후기",
                f"후기 ID: `{review_id}`\n평점: {rating}/5\n판매자: <@{review.get('seller_id')}>\n구매자: <@{review.get('buyer_id')}>\n내용: {_truncate(content, 1_000)}",
            )
