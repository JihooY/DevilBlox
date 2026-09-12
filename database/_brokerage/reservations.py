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


class BrokerageReservationMixin:
    async def enqueue_buyer(self, listing_id: str, buyer_id: int) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        buyer_id = _require_id(buyer_id, "buyer_id")
        now = _now()
        existing = await self.get_listing(listing_id)
        if existing is None:
            return None
        config = await self.get_config(int(existing["guild_id"]))
        reservation_expires_at = now + timedelta(
            minutes=int(config["reservation_timeout_minutes"])
        )
        listing = await self.listings.find_one_and_update(
            {
                "_id": listing_id,
                "status": {"$in": ["open", "reserved"]},
                "seller_id": {"$ne": buyer_id},
                "buyer_ids": {"$ne": buyer_id},
            },
            [
                {
                    "$set": {
                        "queue_seq": {"$add": [{"$ifNull": ["$queue_seq", 0]}, 1]},
                        "buyer_ids": {
                            "$setUnion": [{"$ifNull": ["$buyer_ids", []]}, [buyer_id]]
                        },
                        "buyer_queue": {
                            "$concatArrays": [
                                {"$ifNull": ["$buyer_queue", []]},
                                [
                                    {
                                        "user_id": buyer_id,
                                        "reservation_number": {
                                            "$add": [{"$ifNull": ["$queue_seq", 0]}, 1]
                                        },
                                        "state": {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$current_buyer_id", None]}, None]},
                                                "active",
                                                "waiting",
                                            ]
                                        },
                                        "enqueued_at": now,
                                        "activated_at": {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$current_buyer_id", None]}, None]},
                                                now,
                                                None,
                                            ]
                                        },
                                    }
                                ],
                            ]
                        },
                        "current_buyer_id": {
                            "$ifNull": ["$current_buyer_id", buyer_id]
                        },
                        "current_reservation_number": {
                            "$ifNull": [
                                "$current_reservation_number",
                                {"$add": [{"$ifNull": ["$queue_seq", 0]}, 1]},
                            ]
                        },
                        "current_reservation_expires_at": {
                            "$cond": [
                                {"$eq": [{"$ifNull": ["$current_buyer_id", None]}, None]},
                                reservation_expires_at,
                                "$current_reservation_expires_at",
                            ]
                        },
                        "status": "reserved",
                        "updated_at": now,
                    }
                }
            ],
            return_document=ReturnDocument.AFTER,
        )
        new = listing is not None
        if listing is None:
            listing = await self.get_listing(listing_id)
        if listing is None:
            return None
        entry = next(
            (item for item in listing.get("buyer_queue", []) if int(item.get("user_id", 0)) == buyer_id),
            None,
        )
        if entry is None:
            return {
                "listing": listing,
                "status": "unavailable",
                "new": False,
                "position": None,
                "active_buyer_id": listing.get("current_buyer_id"),
            }
        waiting = [item for item in listing.get("buyer_queue", []) if item.get("state") in {"active", "waiting"}]
        position = next(
            (index for index, item in enumerate(waiting, 1) if item.get("user_id") == buyer_id), None
        )
        return {
            "listing": listing,
            "status": entry.get("state"),
            "new": new,
            "position": position,
            "reservation_number": entry.get("reservation_number"),
            "active_buyer_id": listing.get("current_buyer_id"),
        }

    async def bind_active_ticket(
        self,
        listing_id: str,
        buyer_id: int,
        channel_id: int,
        message_id: int | None = None,
    ) -> dict | None:
        updates: dict[str, Any] = {
            "active_ticket_channel_id": _require_id(channel_id, "channel_id"),
            "active_ticket_buyer_id": _require_id(buyer_id, "buyer_id"),
            "active_ticket_bound_at": _now(),
            "updated_at": _now(),
        }
        if message_id is not None:
            updates["active_ticket_message_id"] = _require_id(message_id, "message_id")
        return await self.listings.find_one_and_update(
            {
                "_id": _clean_text(listing_id, "listing_id", maximum=100, required=True),
                "status": "reserved",
                "current_buyer_id": int(buyer_id),
                "$or": [
                    {"active_ticket_channel_id": {"$exists": False}},
                    {"active_ticket_channel_id": None},
                    {"active_ticket_channel_id": int(channel_id)},
                ],
            },
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )

    async def release_active_ticket_binding(
        self,
        listing_id: str,
        buyer_id: int,
        channel_id: int,
    ) -> bool:
        """Roll back a ticket binding after the surrounding ticket setup fails."""

        result = await self.listings.update_one(
            {
                "_id": _clean_text(listing_id, "listing_id", maximum=100, required=True),
                "status": "reserved",
                "current_buyer_id": _require_id(buyer_id, "buyer_id"),
                "active_ticket_channel_id": _require_id(channel_id, "channel_id"),
            },
            {
                "$set": {
                    "active_ticket_channel_id": None,
                    "active_ticket_message_id": None,
                    "active_ticket_buyer_id": None,
                    "active_ticket_bound_at": None,
                    "updated_at": _now(),
                }
            },
        )
        return bool(result.modified_count)

    async def list_recoverable_reservations(
        self,
        guild_id: int,
        now: datetime | None = None,
        *,
        limit: int = 100,
    ) -> list[dict]:
        """Return expired or unbound reservations, oldest first."""

        now = _safe_datetime(now)
        return await self.listings.find(
            {
                "guild_id": _require_id(guild_id, "guild_id"),
                "status": "reserved",
                "$or": [
                    {"active_ticket_channel_id": None},
                    {"active_ticket_channel_id": {"$exists": False}},
                    {"current_reservation_expires_at": {"$lte": now}},
                    {"current_reservation_expires_at": {"$exists": False}},
                ],
            }
        ).sort("updated_at", 1).limit(max(1, min(1_000, int(limit)))).to_list(
            length=max(1, min(1_000, int(limit)))
        )

    async def ensure_reservation_deadline(
        self,
        listing_id: str,
        buyer_id: int,
        *,
        now: datetime | None = None,
    ) -> dict | None:
        """Backfill a deadline for reservations created before timeouts existed."""

        now = _safe_datetime(now)
        listing = await self.get_listing(listing_id)
        if listing is None:
            return None
        config = await self.get_config(int(listing["guild_id"]))
        deadline = now + timedelta(minutes=int(config["reservation_timeout_minutes"]))
        updated = await self.listings.find_one_and_update(
            {
                "_id": _clean_text(listing_id, "listing_id", maximum=100, required=True),
                "status": "reserved",
                "current_buyer_id": _require_id(buyer_id, "buyer_id"),
                "$or": [
                    {"current_reservation_expires_at": None},
                    {"current_reservation_expires_at": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "current_reservation_expires_at": deadline,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return updated or await self.get_listing(listing_id)

    async def list_active_ticket_listings(self, *, limit: int = 500) -> list[dict]:
        return await self.listings.find(
            {
                "status": "reserved",
                "active_ticket_channel_id": {"$ne": None},
            }
        ).sort("active_ticket_bound_at", 1).to_list(length=max(1, min(2_000, int(limit))))

    async def complete_sale(
        self,
        listing_id: str,
        buyer_id: int,
        actor_id: int,
    ) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        buyer_id = _require_id(buyer_id, "buyer_id")
        actor_id = _require_id(actor_id, "actor_id")
        existing = await self.get_listing(listing_id)
        if existing is None:
            return None
        config = await self.get_config(int(existing["guild_id"]))
        now = _now()
        due_at = now + timedelta(days=int(config["settlement_delay_days"]))
        settlement_points = _points_for_price(int(existing["price"]), config)
        listing = await self.listings.find_one_and_update(
            {
                "_id": listing_id,
                "status": "reserved",
                "current_buyer_id": buyer_id,
            },
            [
                {
                    "$set": {
                        "buyer_queue": {
                            "$map": {
                                "input": {"$ifNull": ["$buyer_queue", []]},
                                "as": "entry",
                                "in": {
                                    "$cond": [
                                        {"$eq": ["$$entry.user_id", buyer_id]},
                                        {
                                            "$mergeObjects": [
                                                "$$entry",
                                                {
                                                    "state": "completed",
                                                    "completed_at": now,
                                                    "completed_by": actor_id,
                                                },
                                            ]
                                        },
                                        "$$entry",
                                    ]
                                },
                            }
                        },
                        "ticket_history": {
                            "$cond": [
                                {"$ne": [{"$ifNull": ["$active_ticket_channel_id", None]}, None]},
                                {
                                    "$concatArrays": [
                                        {"$ifNull": ["$ticket_history", []]},
                                        [
                                            {
                                                "buyer_id": buyer_id,
                                                "channel_id": "$active_ticket_channel_id",
                                                "message_id": "$active_ticket_message_id",
                                                "closed_reason": "completed",
                                                "closed_at": now,
                                            }
                                        ],
                                    ]
                                },
                                {"$ifNull": ["$ticket_history", []]},
                            ]
                        },
                        "status": "sold",
                        "sold_to": buyer_id,
                        "sold_by": actor_id,
                        "sold_at": now,
                        "settlement": {
                            "status": "pending",
                            "due_at": due_at,
                            "points": settlement_points,
                            "operation_id": f"listing:{listing_id}:settlement",
                            "created_at": now,
                        },
                        "active_ticket_channel_id": None,
                        "active_ticket_message_id": None,
                        "active_ticket_buyer_id": None,
                        "current_reservation_expires_at": None,
                        "updated_at": now,
                    }
                }
            ],
            return_document=ReturnDocument.AFTER,
        )
        already_completed = False
        if listing is None:
            listing = await self.get_listing(listing_id)
            if listing is None or listing.get("status") != "sold" or listing.get("sold_to") != buyer_id:
                return {
                    "listing": listing,
                    "status": "unavailable",
                    "already_completed": False,
                    "review": None,
                }
            already_completed = True
        review = await self.create_review(listing_id, buyer_id=buyer_id)
        return {
            "listing": listing,
            "status": "sold",
            "already_completed": already_completed,
            "review": review,
        }

    async def cancel_reservation_and_promote(
        self,
        listing_id: str,
        buyer_id: int,
        actor_id: int,
        *,
        reason: str = "",
    ) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        buyer_id = _require_id(buyer_id, "buyer_id")
        actor_id = _require_id(actor_id, "actor_id")
        reason = _clean_text(reason, "reason", maximum=1_000)
        now = _now()
        current = await self.get_listing(listing_id)
        if current is None:
            return None
        config = await self.get_config(int(current["guild_id"]))
        next_reservation_expires_at = now + timedelta(
            minutes=int(config["reservation_timeout_minutes"])
        )
        listing = await self.listings.find_one_and_update(
            {
                "_id": listing_id,
                "status": "reserved",
                "current_buyer_id": buyer_id,
            },
            [
                {
                    "$set": {
                        "_next_waiting": {
                            "$arrayElemAt": [
                                {
                                    "$filter": {
                                        "input": {"$ifNull": ["$buyer_queue", []]},
                                        "as": "entry",
                                        "cond": {"$eq": ["$$entry.state", "waiting"]},
                                    }
                                },
                                0,
                            ]
                        }
                    }
                },
                {
                    "$set": {
                        "_next_buyer_id": {"$ifNull": ["$_next_waiting.user_id", None]},
                        "_next_reservation_number": {
                            "$ifNull": ["$_next_waiting.reservation_number", None]
                        },
                    }
                },
                {
                    "$set": {
                        "buyer_queue": {
                            "$map": {
                                "input": {"$ifNull": ["$buyer_queue", []]},
                                "as": "entry",
                                "in": {
                                    "$cond": [
                                        {"$eq": ["$$entry.user_id", buyer_id]},
                                        {
                                            "$mergeObjects": [
                                                "$$entry",
                                                {
                                                    "state": "cancelled",
                                                    "cancelled_at": now,
                                                    "cancelled_by": actor_id,
                                                    "cancel_reason": reason,
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$and": [
                                                        {"$ne": ["$_next_buyer_id", None]},
                                                        {"$eq": ["$$entry.user_id", "$_next_buyer_id"]},
                                                    ]
                                                },
                                                {
                                                    "$mergeObjects": [
                                                        "$$entry",
                                                        {"state": "active", "activated_at": now},
                                                    ]
                                                },
                                                "$$entry",
                                            ]
                                        },
                                    ]
                                },
                            }
                        },
                        "ticket_history": {
                            "$cond": [
                                {"$ne": [{"$ifNull": ["$active_ticket_channel_id", None]}, None]},
                                {
                                    "$concatArrays": [
                                        {"$ifNull": ["$ticket_history", []]},
                                        [
                                            {
                                                "buyer_id": buyer_id,
                                                "channel_id": "$active_ticket_channel_id",
                                                "message_id": "$active_ticket_message_id",
                                                "closed_reason": "cancelled",
                                                "closed_at": now,
                                            }
                                        ],
                                    ]
                                },
                                {"$ifNull": ["$ticket_history", []]},
                            ]
                        },
                        "current_buyer_id": "$_next_buyer_id",
                        "current_reservation_number": "$_next_reservation_number",
                        "current_reservation_expires_at": {
                            "$cond": [
                                {"$ne": ["$_next_buyer_id", None]},
                                next_reservation_expires_at,
                                None,
                            ]
                        },
                        "status": {
                            "$cond": [{"$ne": ["$_next_buyer_id", None]}, "reserved", "open"]
                        },
                        "active_ticket_channel_id": None,
                        "active_ticket_message_id": None,
                        "active_ticket_buyer_id": None,
                        "updated_at": now,
                    }
                },
                {"$set": {"_next_waiting": "$$REMOVE", "_next_buyer_id": "$$REMOVE", "_next_reservation_number": "$$REMOVE"}},
            ],
            return_document=ReturnDocument.AFTER,
        )
        if listing is None:
            current = await self.get_listing(listing_id)
            return (
                {
                    "listing": current,
                    "status": "not_active",
                    "cancelled_buyer_id": buyer_id,
                    "next_buyer_id": current.get("current_buyer_id") if current else None,
                    "next_reservation_number": current.get("current_reservation_number") if current else None,
                }
                if current
                else None
            )
        return {
            "listing": listing,
            "status": "promoted" if listing.get("current_buyer_id") else "open",
            "cancelled_buyer_id": buyer_id,
            "next_buyer_id": listing.get("current_buyer_id"),
            "next_reservation_number": listing.get("current_reservation_number"),
        }
