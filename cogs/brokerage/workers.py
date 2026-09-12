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


class BrokerageWorkerMixin:
    @tasks.loop(minutes=1)
    async def bump_listings_loop(self) -> None:
        try:
            due = await self.repos.brokerage.claim_due_bumps(_now(), limit=50)
        except Exception:
            log.exception("Failed to claim due brokerage listings")
            return
        for listing in due:
            token = str(listing.get("bump_claim_token") or "")
            guild = self.bot.get_guild(_int(listing.get("guild_id")))
            if guild is None:
                if token:
                    await self.repos.brokerage.release_bump_claim(
                        str(listing["_id"]), token, retry_at=_now() + timedelta(minutes=10)
                    )
                continue
            try:
                await self.post_listing(guild, listing, replace_previous=True)
            except Exception:
                log.exception("Failed to bump brokerage listing: %s", listing.get("_id"))
                if token:
                    await self.repos.brokerage.release_bump_claim(
                        str(listing["_id"]), token, retry_at=_now() + timedelta(minutes=5)
                    )
            else:
                if token:
                    await self.repos.brokerage.release_bump_claim(str(listing["_id"]), token)

        # Recover a reservation if the process stopped between queue activation
        # and ticket binding. Conditional storage updates prevent duplicate tickets.
        for guild in self.bot.guilds:
            try:
                listings = await self.repos.brokerage.list_recoverable_reservations(
                    guild.id, _now(), limit=100
                )
            except Exception:
                log.exception("Failed to inspect brokerage reservation recovery: %s", guild.id)
                continue
            recovered = 0
            for listing in listings:
                buyer_id = listing.get("current_buyer_id")
                if (
                    listing.get("status") != "reserved"
                    or not buyer_id
                ):
                    continue
                expires_at = listing.get("current_reservation_expires_at")
                if expires_at is None:
                    try:
                        listing = await self.repos.brokerage.ensure_reservation_deadline(
                            str(listing["_id"]), _int(buyer_id), now=_now()
                        ) or listing
                    except Exception:
                        log.exception(
                            "Failed to backfill brokerage reservation deadline: listing_id=%s",
                            listing.get("_id"),
                        )
                    expires_at = listing.get("current_reservation_expires_at")
                if expires_at is not None and (
                    expires_at.tzinfo is None or expires_at.utcoffset() is None
                ):
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at is not None and expires_at <= _now():
                    old_channel_id = listing.get("active_ticket_channel_id")
                    try:
                        result = await self.repos.brokerage.cancel_reservation_and_promote(
                            str(listing["_id"]),
                            _int(buyer_id),
                            self.bot.user.id,
                            reason="reservation_timeout",
                        )
                        await self.archive_trade_ticket(
                            guild,
                            _int(old_channel_id),
                            buyer_id=_int(buyer_id),
                            seller_id=_int(listing.get("seller_id")),
                            reason="Brokerage reservation timed out",
                        )
                        promoted = (result or {}).get("listing") or {}
                        next_buyer_id = (result or {}).get("next_buyer_id")
                        if next_buyer_id:
                            await self.open_trade_ticket(
                                guild, promoted, _int(next_buyer_id)
                            )
                        if promoted:
                            await self.refresh_listing_message(promoted)
                    except Exception:
                        log.exception(
                            "Failed to expire brokerage reservation: listing_id=%s buyer_id=%s",
                            listing.get("_id"),
                            buyer_id,
                        )
                    continue
                if listing.get("active_ticket_channel_id"):
                    continue
                if guild.get_member(_int(buyer_id)) is None:
                    try:
                        await self.repos.brokerage.cancel_reservation_and_promote(
                            str(listing["_id"]),
                            _int(buyer_id),
                            self.bot.user.id,
                            reason="buyer_left_guild",
                        )
                    except Exception:
                        log.exception(
                            "Failed to skip departed brokerage buyer: listing_id=%s buyer_id=%s",
                            listing.get("_id"),
                            buyer_id,
                        )
                    continue
                try:
                    await self.open_trade_ticket(guild, listing, _int(buyer_id))
                except Exception:
                    log.exception(
                        "Failed to recover brokerage ticket: listing_id=%s buyer_id=%s",
                        listing.get("_id"),
                        buyer_id,
                    )
                recovered += 1
                if recovered >= 5:
                    break

    @bump_listings_loop.before_loop
    async def before_bump_listings_loop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def settle_transactions_loop(self) -> None:
        try:
            due = await self.repos.brokerage.list_due_settlements(_now(), limit=100)
        except Exception:
            log.exception("Failed to load brokerage settlements")
            return
        for listing in due:
            try:
                result = await self.repos.brokerage.settle_success(
                    str(listing["_id"]), actor_id=self.bot.user.id, now=_now()
                )
            except Exception:
                log.exception("Failed to settle brokerage listing: %s", listing.get("_id"))
                continue
            if (
                not result
                or result.get("already_settled")
                or result.get("blocked_by_problem")
            ):
                continue
            settled = result.get("listing") or listing
            guild = self.bot.get_guild(_int(settled.get("guild_id")))
            if guild is None:
                continue
            points = _int((settled.get("settlement") or {}).get("points"))
            for user_id, label in (
                (_int(settled.get("seller_id")), "판매"),
                (_int(settled.get("sold_to")), "구매"),
            ):
                user = guild.get_member(user_id) or self.bot.get_user(user_id)
                if user is None:
                    continue
                try:
                    await user.send(
                        embed=success_embed(
                            "거래 보호기간 종료",
                            f"`{settled.get('title')}` {label} 거래가 문제 없이 확정되어 신용도 +{points}점이 반영되었습니다.",
                        )
                    )
                except discord.HTTPException:
                    pass
            await self.send_log(
                guild,
                "거래 자동 정산",
                f"거래 ID: `{settled.get('_id')}`\n판매자/구매자 신용도: +{points}점",
            )
        try:
            await self.repos.brokerage.expire_reviews(_now(), limit=500)
        except Exception:
            log.exception("Failed to expire brokerage reviews")

    @settle_transactions_loop.before_loop
    async def before_settle_transactions_loop(self) -> None:
        await self.bot.wait_until_ready()
