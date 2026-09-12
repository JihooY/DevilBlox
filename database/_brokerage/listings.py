from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .core import (
    DEFAULT_CONFIG,
    _clamp_score,
    _clean_text,
    _ledger_key,
    _now,
    _penalized_delta,
    _points_for_price,
    _posting_minutes,
    _preference_key,
    _problem_key,
    _profile_key,
    _require_id,
    _safe_datetime,
)


class BrokerageListingMixin:
    async def get_notification_preferences(self, guild_id: int, user_id: int) -> dict:
        guild_id = _require_id(guild_id, "guild_id")
        user_id = _require_id(user_id, "user_id")
        now = _now()
        doc = await self.notification_preferences.find_one_and_update(
            {"_id": _preference_key(guild_id, user_id)},
            {
                "$setOnInsert": {
                    "_id": _preference_key(guild_id, user_id),
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "enabled": False,
                    "min_seller_score": -100,
                    "no_duplicates": True,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            raise RuntimeError("notification preferences could not be loaded")
        return doc

    async def set_notification_preferences(
        self,
        guild_id: int,
        user_id: int,
        *,
        enabled: bool | None = None,
        min_seller_score: int | None = None,
        no_duplicates: bool | None = None,
    ) -> dict:
        await self.get_notification_preferences(guild_id, user_id)
        updates: dict[str, Any] = {"updated_at": _now()}
        if enabled is not None:
            updates["enabled"] = bool(enabled)
        if min_seller_score is not None:
            score = int(min_seller_score)
            if not -100 <= score <= 100:
                raise ValueError("min_seller_score must be between -100 and 100")
            updates["min_seller_score"] = score
        if no_duplicates is not None:
            updates["no_duplicates"] = bool(no_duplicates)
        return await self.notification_preferences.find_one_and_update(
            {"_id": _preference_key(int(guild_id), int(user_id))},
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )

    async def list_notification_preferences(self, guild_id: int, *, enabled_only: bool = True) -> list[dict]:
        query: dict[str, Any] = {"guild_id": _require_id(guild_id, "guild_id")}
        if enabled_only:
            query["enabled"] = True
        return await self.notification_preferences.find(query).sort("user_id", 1).to_list(length=None)

    async def mark_notified_users(self, listing_id: str, user_ids: Iterable[int]) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        normalized = list(dict.fromkeys(_require_id(value, "user_id") for value in user_ids))
        if not normalized:
            listing = await self.get_listing(listing_id)
            return {"listing": listing, "new_user_ids": []} if listing else None
        before = await self.listings.find_one({"_id": listing_id}, {"notified_user_ids": 1})
        if before is None:
            return None
        existing = set(before.get("notified_user_ids", []))
        new_user_ids = [user_id for user_id in normalized if user_id not in existing]
        listing = await self.listings.find_one_and_update(
            {"_id": listing_id, "notified_user_ids": {"$not": {"$all": normalized}}},
            {
                "$addToSet": {"notified_user_ids": {"$each": normalized}},
                "$set": {"updated_at": _now()},
            },
            return_document=ReturnDocument.AFTER,
        )
        if listing is None:
            listing = await self.get_listing(listing_id)
            new_user_ids = []
        return {"listing": listing, "new_user_ids": new_user_ids}

    async def create_listing(
        self,
        guild_id: int,
        seller_id: int,
        title: str,
        description: str,
        price: int,
        quantity: int = 1,
        contact: str = "",
        *,
        category: str = "",
        metadata: Mapping[str, Any] | None = None,
        listing_id: str | None = None,
    ) -> dict:
        guild_id = _require_id(guild_id, "guild_id")
        seller_id = _require_id(seller_id, "seller_id")
        title = _clean_text(title, "title", maximum=100, required=True)
        description = _clean_text(description, "description", maximum=4_000, required=True)
        contact = _clean_text(contact, "contact", maximum=500)
        category = _clean_text(category, "category", maximum=100)
        price = int(price)
        quantity = int(quantity)
        if price <= 0:
            raise ValueError("price must be greater than zero")
        if not 1 <= quantity <= 1_000_000:
            raise ValueError("quantity must be between 1 and 1000000")
        listing_id = (
            _clean_text(listing_id, "listing_id", maximum=100, required=True)
            if listing_id is not None
            else secrets.token_urlsafe(10)
        )
        seller = await self.ensure_profile(guild_id, seller_id)
        config = await self.get_config(guild_id)
        if int(seller.get("trust_score", 0)) < int(config.get("minimum_listing_score", -100)):
            raise ValueError("seller trust score is below the guild's listing minimum")
        interval = _posting_minutes(int(seller.get("trust_score", 0)), config)
        now = _now()
        doc = {
            "_id": listing_id,
            "guild_id": guild_id,
            "seller_id": seller_id,
            "title": title,
            "description": description,
            "price": price,
            "quantity": quantity,
            "contact": contact,
            "category": category,
            "metadata": deepcopy(dict(metadata or {})),
            "status": "open",
            "seller_score_snapshot": int(seller.get("trust_score", 0)),
            "seller_penalty_level_snapshot": int(seller.get("penalty_level", 0)),
            "bump_interval_minutes": interval,
            "next_bump_at": now + timedelta(minutes=interval),
            "queue_seq": 0,
            "buyer_ids": [],
            "buyer_queue": [],
            "current_buyer_id": None,
            "current_reservation_number": None,
            "liked_user_ids": [],
            "like_count": 0,
            "like_score_granted": 0,
            "reporter_ids": [],
            "report_count": 0,
            "notified_user_ids": [],
            "ticket_history": [],
            "created_at": now,
            "updated_at": now,
        }
        await self.listings.insert_one(doc)
        return doc

    async def get_listing(self, listing_id: str) -> dict | None:
        return await self.listings.find_one(
            {"_id": _clean_text(listing_id, "listing_id", maximum=100, required=True)}
        )

    async def get_listing_by_message(self, guild_id: int, channel_id: int, message_id: int) -> dict | None:
        return await self.listings.find_one(
            {
                "guild_id": _require_id(guild_id, "guild_id"),
                "channel_id": _require_id(channel_id, "channel_id"),
                "message_id": _require_id(message_id, "message_id"),
            }
        )

    async def list_open_listings(self, guild_id: int, *, limit: int = 100) -> list[dict]:
        limit = max(1, min(1_000, int(limit)))
        return await self.listings.find(
            {
                "guild_id": _require_id(guild_id, "guild_id"),
                "status": {"$in": ["open", "reserved"]},
            }
        ).sort("created_at", -1).to_list(length=limit)

    async def list_seller_listings(
        self,
        guild_id: int,
        seller_id: int,
        *,
        include_closed: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        query: dict[str, Any] = {
            "guild_id": _require_id(guild_id, "guild_id"),
            "seller_id": _require_id(seller_id, "seller_id"),
        }
        if not include_closed:
            query["status"] = {"$in": ["open", "reserved"]}
        return await self.listings.find(query).sort("created_at", -1).to_list(
            length=max(1, min(1_000, int(limit)))
        )

    async def refresh_listing_intervals(
        self,
        guild_id: int,
        *,
        seller_id: int | None = None,
        seller_score: int | None = None,
        config: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> int:
        """Recalculate active listing schedules after policy or score changes."""

        guild_id = _require_id(guild_id, "guild_id")
        if seller_id is not None:
            seller_id = _require_id(seller_id, "seller_id")
        now = _safe_datetime(now)
        effective_config = dict(config or await self.get_config(guild_id))
        query: dict[str, Any] = {
            "guild_id": guild_id,
            "status": {"$in": ["open", "reserved"]},
        }
        if seller_id is not None:
            query["seller_id"] = seller_id

        score_cache: dict[int, int] = {}
        if seller_id is not None and seller_score is not None:
            score_cache[seller_id] = _clamp_score(seller_score)
        changed = 0
        async for listing in self.listings.find(query):
            listing_seller_id = int(listing["seller_id"])
            score = score_cache.get(listing_seller_id)
            if score is None:
                profile = await self.ensure_profile(guild_id, listing_seller_id)
                score = int(profile.get("trust_score", 0))
                score_cache[listing_seller_id] = score
            interval = _posting_minutes(
                score,
                effective_config,
                like_count=int(listing.get("like_count", 0)),
            )
            last_posted_at = _safe_datetime(
                listing.get("last_posted_at"),
                default=listing.get("created_at") or now,
            )
            next_bump_at = max(now, last_posted_at + timedelta(minutes=interval))
            result = await self.listings.update_one(
                {
                    "_id": listing["_id"],
                    "status": {"$in": ["open", "reserved"]},
                },
                {
                    "$set": {
                        "bump_interval_minutes": interval,
                        "next_bump_at": next_bump_at,
                        "updated_at": now,
                    }
                },
            )
            changed += int(result.modified_count)
        return changed

    async def delete_listing(
        self,
        listing_id: str,
        *,
        deleted_by: int,
        reason: str = "",
    ) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        now = _now()
        return await self.listings.find_one_and_update(
            {"_id": listing_id, "status": {"$nin": ["deleted", "sold"]}},
            {
                "$set": {
                    "status": "deleted",
                    "deleted_by": _require_id(deleted_by, "deleted_by"),
                    "delete_reason": _clean_text(reason, "reason", maximum=1_000),
                    "deleted_at": now,
                    "updated_at": now,
                    "active_ticket_channel_id": None,
                    "active_ticket_message_id": None,
                    "current_reservation_expires_at": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def bind_listing_message(
        self,
        listing_id: str,
        channel_id: int,
        message_id: int,
        next_bump_at: datetime | None = None,
        *,
        bump_interval_minutes: int | None = None,
    ) -> dict | None:
        now = _now()
        updates: dict[str, Any] = {
            "channel_id": _require_id(channel_id, "channel_id"),
            "message_id": _require_id(message_id, "message_id"),
            "last_posted_at": now,
            "updated_at": now,
        }
        if next_bump_at is not None:
            updates["next_bump_at"] = _safe_datetime(next_bump_at)
        if bump_interval_minutes is not None:
            interval = int(bump_interval_minutes)
            if not 1 <= interval <= 24 * 60:
                raise ValueError("bump_interval_minutes must be between 1 and 1440")
            updates["bump_interval_minutes"] = interval
        return await self.listings.find_one_and_update(
            {
                "_id": _clean_text(listing_id, "listing_id", maximum=100, required=True),
                "status": {"$in": ["open", "reserved"]},
            },
            {"$set": updates, "$inc": {"post_count": 1}},
            return_document=ReturnDocument.AFTER,
        )

    async def claim_due_bumps(
        self,
        now: datetime | None = None,
        *,
        limit: int = 25,
        claim_seconds: int = 120,
    ) -> list[dict]:
        now = _safe_datetime(now)
        limit = max(1, min(100, int(limit)))
        claim_seconds = max(10, min(3_600, int(claim_seconds)))
        candidates = await self.listings.find(
            {
                "status": {"$in": ["open", "reserved"]},
                "next_bump_at": {"$lte": now},
                "$or": [
                    {"bump_claim_until": {"$exists": False}},
                    {"bump_claim_until": None},
                    {"bump_claim_until": {"$lte": now}},
                ],
            }
        ).sort("next_bump_at", 1).limit(limit).to_list(length=limit)
        claimed: list[dict] = []
        for candidate in candidates:
            interval = max(1, int(candidate.get("bump_interval_minutes", 60)))
            claim_token = secrets.token_urlsafe(10)
            doc = await self.listings.find_one_and_update(
                {
                    "_id": candidate["_id"],
                    "status": {"$in": ["open", "reserved"]},
                    "next_bump_at": candidate.get("next_bump_at"),
                    "$or": [
                        {"bump_claim_until": {"$exists": False}},
                        {"bump_claim_until": None},
                        {"bump_claim_until": {"$lte": now}},
                    ],
                },
                {
                    "$set": {
                        "bump_claim_token": claim_token,
                        "bump_claim_until": now + timedelta(seconds=claim_seconds),
                        "last_bump_claimed_at": now,
                        "next_bump_at": now + timedelta(minutes=interval),
                        "updated_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if doc is not None:
                claimed.append(doc)
        return claimed

    async def claim_due_listings(
        self,
        now: datetime | None = None,
        limit: int = 25,
    ) -> list[dict]:
        """Compatibility name used by scheduler code for due listing bumps."""

        return await self.claim_due_bumps(now, limit=limit)

    async def release_bump_claim(
        self,
        listing_id: str,
        claim_token: str,
        *,
        retry_at: datetime | None = None,
    ) -> bool:
        updates: dict[str, Any] = {
            "bump_claim_until": None,
            "bump_claim_token": None,
            "updated_at": _now(),
        }
        if retry_at is not None:
            updates["next_bump_at"] = _safe_datetime(retry_at)
        result = await self.listings.update_one(
            {
                "_id": _clean_text(listing_id, "listing_id", maximum=100, required=True),
                "bump_claim_token": _clean_text(claim_token, "claim_token", maximum=100, required=True),
            },
            {"$set": updates},
        )
        return bool(result.modified_count)

    async def add_like(self, listing_id: str, user_id: int) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        user_id = _require_id(user_id, "user_id")
        now = _now()
        listing = await self.listings.find_one_and_update(
            {
                "_id": listing_id,
                "status": {"$in": ["open", "reserved"]},
                "seller_id": {"$ne": user_id},
                "liked_user_ids": {"$ne": user_id},
            },
            {
                "$addToSet": {"liked_user_ids": user_id},
                "$inc": {"like_count": 1},
                "$set": {"updated_at": now},
            },
            return_document=ReturnDocument.AFTER,
        )
        new = listing is not None
        if listing is None:
            listing = await self.get_listing(listing_id)
        if listing is None:
            return None
        count = int(listing.get("like_count", 0))
        config = await self.get_config(int(listing["guild_id"]))
        every = int(config["like_score_every"])
        bonus_each = int(config["like_score_bonus"])
        max_bonus = int(config["like_score_max_bonus"])
        earned_target = min(max_bonus, (count // every) * bonus_each) if bonus_each else 0
        previously_granted = int(listing.get("like_score_granted", 0))
        grant = 0
        if earned_target > previously_granted:
            for credit_number in range(previously_granted + 1, earned_target + 1):
                like_operation_id = f"listing:{listing_id}:like-credit:{credit_number}"
                result = await self.adjust_profile(
                    int(listing["guild_id"]),
                    int(listing["seller_id"]),
                    1,
                    operation_id=like_operation_id,
                    reason="listing_like_milestone",
                    metadata={
                        "listing_id": listing_id,
                        "credit_number": credit_number,
                        "like_count": count,
                    },
                )
                await self.profiles.update_one(
                    {
                        "_id": _profile_key(
                            int(listing["guild_id"]), int(listing["seller_id"])
                        ),
                        "like_score_operation_ids": {"$ne": like_operation_id},
                    },
                    {
                        "$inc": {"like_score_total": 1},
                        "$addToSet": {"like_score_operation_ids": like_operation_id},
                        "$set": {"updated_at": _now()},
                    },
                )
                if result.get("new_operation"):
                    grant += 1
            await self.listings.update_one(
                {"_id": listing_id, "like_score_granted": {"$lt": earned_target}},
                {"$max": {"like_score_granted": earned_target}, "$set": {"updated_at": _now()}},
            )

        seller = await self.get_profile(int(listing["guild_id"]), int(listing["seller_id"]))
        interval = _posting_minutes(
            int((seller or {}).get("trust_score", listing.get("seller_score_snapshot", 0))),
            config,
            like_count=count,
        )
        await self.listings.update_one(
            {"_id": listing_id, "status": {"$in": ["open", "reserved"]}},
            {
                "$set": {"bump_interval_minutes": interval, "updated_at": _now()},
                "$min": {"next_bump_at": now + timedelta(minutes=interval)},
            },
        )
        listing = await self.get_listing(listing_id) or listing
        return {"listing": listing, "new": new, "count": count, "grant": grant}

    async def add_report(
        self,
        listing_id: str,
        reporter_id: int,
        reason: str,
        *,
        details: str = "",
        attachments: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        reporter_id = _require_id(reporter_id, "reporter_id")
        reason = _clean_text(reason, "reason", maximum=100, required=True)
        details = _clean_text(details, "details", maximum=4_000)
        safe_attachments: list[dict[str, Any]] = []
        for item in attachments or []:
            safe_attachments.append(
                {
                    "url": _clean_text(item.get("url"), "attachment url", maximum=2_000),
                    "filename": _clean_text(item.get("filename"), "attachment filename", maximum=255),
                    "content_type": _clean_text(item.get("content_type"), "content type", maximum=100),
                    "size": max(0, int(item.get("size", 0) or 0)),
                }
            )
        now = _now()
        listing = await self.listings.find_one_and_update(
            {
                "_id": listing_id,
                "status": {"$nin": ["deleted"]},
                "reporter_ids": {"$ne": reporter_id},
            },
            {
                "$addToSet": {"reporter_ids": reporter_id},
                "$inc": {"report_count": 1},
                "$set": {"updated_at": now},
            },
            return_document=ReturnDocument.AFTER,
        )
        new = listing is not None
        if listing is None:
            listing = await self.get_listing(listing_id)
        if listing is None:
            return None
        report_id = f"{listing_id}:{reporter_id}"
        await self.reports.update_one(
            {"_id": report_id},
            {
                "$setOnInsert": {
                    "_id": report_id,
                    "guild_id": listing["guild_id"],
                    "listing_id": listing_id,
                    "seller_id": listing["seller_id"],
                    "reporter_id": reporter_id,
                    "reason": reason,
                    "details": details,
                    "attachments": safe_attachments,
                    "status": "open",
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        report = await self.reports.find_one({"_id": report_id})
        return {"listing": listing, "report": report, "new": new, "count": listing.get("report_count", 0)}

    async def resolve_report(
        self,
        report_id: str,
        actor_id: int,
        *,
        action: str = "dismissed",
        note: str = "",
    ) -> dict | None:
        action = _clean_text(action, "action", maximum=50, required=True)
        if action not in {"dismissed", "actioned"}:
            raise ValueError("action must be 'dismissed' or 'actioned'")
        now = _now()
        return await self.reports.find_one_and_update(
            {
                "_id": _clean_text(report_id, "report_id", maximum=250, required=True),
                "status": "open",
            },
            {
                "$set": {
                    "status": action,
                    "resolution_note": _clean_text(note, "note", maximum=2_000),
                    "resolved_by": _require_id(actor_id, "actor_id"),
                    "resolved_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def list_pending_reports(self, *, limit: int = 500) -> list[dict]:
        return await self.reports.find({"status": "open"}).sort("created_at", 1).to_list(
            length=max(1, min(2_000, int(limit)))
        )
