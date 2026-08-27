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


class BrokerageReviewMixin:
    async def create_review(
        self,
        listing_id: str,
        buyer_id: int | None = None,
        *,
        expires_at: datetime | None = None,
    ) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        listing = await self.get_listing(listing_id)
        if listing is None or listing.get("status") != "sold":
            return None
        sold_to = int(listing.get("sold_to", 0) or 0)
        if buyer_id is not None and _require_id(buyer_id, "buyer_id") != sold_to:
            raise ValueError("buyer_id does not match the completed sale")
        if sold_to <= 0:
            return None
        config = await self.get_config(int(listing["guild_id"]))
        sold_at = _safe_datetime(listing.get("sold_at"))
        deadline = (
            _safe_datetime(expires_at)
            if expires_at is not None
            else sold_at + timedelta(days=int(config["review_window_days"]))
        )
        now = _now()
        review_id = f"review:{listing_id}"
        await self.reviews.update_one(
            {"_id": review_id},
            {
                "$setOnInsert": {
                    "_id": review_id,
                    "guild_id": listing["guild_id"],
                    "listing_id": listing_id,
                    "seller_id": listing["seller_id"],
                    "buyer_id": sold_to,
                    "amount": listing["price"],
                    "listing_title": listing["title"],
                    "status": "pending",
                    "review_version": 0,
                    "expires_at": deadline,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        review = await self.reviews.find_one({"_id": review_id})
        await self.listings.update_one(
            {"_id": listing_id},
            {"$set": {"review_id": review_id, "updated_at": _now()}},
        )
        return review

    async def get_review(self, review_id: str) -> dict | None:
        return await self.reviews.find_one(
            {"_id": _clean_text(review_id, "review_id", maximum=200, required=True)}
        )

    async def get_review_for_listing(self, listing_id: str) -> dict | None:
        return await self.reviews.find_one(
            {"listing_id": _clean_text(listing_id, "listing_id", maximum=100, required=True)}
        )

    async def _apply_rating_change(
        self,
        review: Mapping[str, Any],
        *,
        rating_delta: int,
        count_delta: int,
        operation_id: str,
    ) -> dict:
        guild_id = int(review["guild_id"])
        seller_id = int(review["seller_id"])
        await self.ensure_profile(guild_id, seller_id)
        now = _now()
        profile = await self.profiles.find_one_and_update(
            {
                "_id": _profile_key(guild_id, seller_id),
                "rating_operation_ids": {"$ne": operation_id},
            },
            [
                {
                    "$set": {
                        "rating_total": {
                            "$max": [0, {"$add": [{"$ifNull": ["$rating_total", 0]}, int(rating_delta)]}]
                        },
                        "rating_count": {
                            "$max": [0, {"$add": [{"$ifNull": ["$rating_count", 0]}, int(count_delta)]}]
                        },
                        "rating_operation_ids": {
                            "$concatArrays": [
                                {"$ifNull": ["$rating_operation_ids", []]}, [operation_id]
                            ]
                        },
                        "updated_at": now,
                    }
                },
                {
                    "$set": {
                        "rating_average": {
                            "$cond": [
                                {"$gt": ["$rating_count", 0]},
                                {"$round": [{"$divide": ["$rating_total", "$rating_count"]}, 3]},
                                None,
                            ]
                        }
                    }
                },
            ],
            return_document=ReturnDocument.AFTER,
        )
        return profile or await self.ensure_profile(guild_id, seller_id)

    async def submit_review(
        self,
        review_id: str,
        buyer_id: int,
        *,
        rating: int,
        content: str,
    ) -> dict | None:
        review_id = _clean_text(review_id, "review_id", maximum=200, required=True)
        buyer_id = _require_id(buyer_id, "buyer_id")
        rating = int(rating)
        if not 1 <= rating <= 5:
            raise ValueError("rating must be between 1 and 5")
        content = _clean_text(content, "content", maximum=4_000, required=True)
        now = _now()
        review = await self.reviews.find_one_and_update(
            {
                "_id": review_id,
                "buyer_id": buyer_id,
                "status": "pending",
                "expires_at": {"$gte": now},
            },
            {
                "$set": {
                    "status": "submitted",
                    "rating": rating,
                    "content": content,
                    "submitted_at": now,
                    "updated_at": now,
                },
                "$inc": {"review_version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if review is None:
            review = await self.get_review(review_id)
            if (
                review is None
                or review.get("buyer_id") != buyer_id
                or review.get("status") != "submitted"
                or int(review.get("rating", 0)) != rating
                or review.get("content") != content
            ):
                return None

        await self._apply_rating_change(
            review,
            rating_delta=rating,
            count_delta=1,
            operation_id=f"{review_id}:rating:initial",
        )
        config = await self.get_config(int(review["guild_id"]))
        if rating <= int(config["low_rating_problem_threshold"]):
            problem_result = await self.add_problem(
                int(review["guild_id"]),
                int(review["seller_id"]),
                int(review.get("amount", 0)),
                operation_id=f"{review_id}:low-rating",
                kind="low_review",
                listing_id=str(review["listing_id"]),
                deduct_score=False,
                created_by=buyer_id,
                notes=f"{rating}-star review",
            )
            problem_id = problem_result["problem"]["_id"]
            await self.reviews.update_one(
                {"_id": review_id},
                {"$set": {"problem_id": problem_id, "counts_as_problem": True, "updated_at": _now()}},
            )
        else:
            await self.reviews.update_one(
                {"_id": review_id},
                {"$set": {"counts_as_problem": False, "updated_at": _now()}},
            )
        return await self.get_review(review_id)

    async def admin_override_review(
        self,
        review_id: str,
        actor_id: int,
        *,
        rating: int | None = None,
        content: str | None = None,
        counts_as_problem: bool | None = None,
        void: bool = False,
        note: str = "",
    ) -> dict | None:
        review_id = _clean_text(review_id, "review_id", maximum=200, required=True)
        actor_id = _require_id(actor_id, "actor_id")
        note = _clean_text(note, "note", maximum=2_000)
        current = await self.get_review(review_id)
        if current is None or current.get("status") not in {"submitted", "void"}:
            return None
        old_rating = int(current.get("rating", 0) or 0)
        old_version = int(current.get("review_version", 1))
        new_rating = old_rating if rating is None else int(rating)
        if not 1 <= new_rating <= 5:
            raise ValueError("rating must be between 1 and 5")
        new_content = current.get("content", "") if content is None else _clean_text(
            content, "content", maximum=4_000, required=True
        )
        config = await self.get_config(int(current["guild_id"]))
        desired_problem = (
            bool(counts_as_problem)
            if counts_as_problem is not None
            else new_rating <= int(config["low_rating_problem_threshold"])
        )
        now = _now()
        new_version = old_version + 1
        target_status = "void" if void else "submitted"
        updated = await self.reviews.find_one_and_update(
            {"_id": review_id, "review_version": old_version, "status": current["status"]},
            {
                "$set": {
                    "status": target_status,
                    "rating": new_rating,
                    "content": new_content,
                    "review_version": new_version,
                    "override": {
                        "actor_id": actor_id,
                        "note": note,
                        "overridden_at": now,
                        "previous_rating": old_rating,
                        "counts_as_problem": False if void else desired_problem,
                    },
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            return None

        was_counted_in_rating = current.get("status") == "submitted"
        is_counted_in_rating = target_status == "submitted"
        rating_delta = (
            (new_rating if is_counted_in_rating else 0)
            - (old_rating if was_counted_in_rating else 0)
        )
        count_delta = int(is_counted_in_rating) - int(was_counted_in_rating)
        if rating_delta or count_delta:
            await self._apply_rating_change(
                updated,
                rating_delta=rating_delta,
                count_delta=count_delta,
                operation_id=f"{review_id}:rating:v{new_version}",
            )

        problem_id = updated.get("problem_id") or current.get("problem_id")
        problem = (
            await self.problems.find_one({"_id": problem_id}) if problem_id else None
        )
        problem_is_active = bool(
            problem and problem.get("status") in {"open", "upheld"}
        )
        should_count = bool(desired_problem and not void)
        if should_count and not problem_is_active:
            result = await self.add_problem(
                int(updated["guild_id"]),
                int(updated["seller_id"]),
                int(updated.get("amount", 0)),
                operation_id=f"{review_id}:low-rating:override:v{new_version}",
                kind="low_review",
                listing_id=str(updated["listing_id"]),
                deduct_score=False,
                created_by=actor_id,
                notes=f"admin override: {new_rating}-star review",
            )
            problem_id = result["problem"]["_id"]
        elif not should_count and problem_is_active:
            await self.resolve_problem(
                str(problem_id),
                resolved_by=actor_id,
                upheld=False,
                resolution=note or "review administrator override",
            )
        await self.reviews.update_one(
            {"_id": review_id, "review_version": new_version},
            {
                "$set": {
                    "problem_id": problem_id,
                    "counts_as_problem": should_count,
                    "updated_at": _now(),
                }
            },
        )
        return await self.get_review(review_id)

    async def list_pending_reviews(self, *, limit: int = 500) -> list[dict]:
        return await self.reviews.find({"status": "pending"}).sort("expires_at", 1).to_list(
            length=max(1, min(2_000, int(limit)))
        )

    async def expire_reviews(self, now: datetime | None = None, *, limit: int = 500) -> int:
        now = _safe_datetime(now)
        due = await self.reviews.find(
            {"status": "pending", "expires_at": {"$lt": now}}, {"_id": 1}
        ).limit(max(1, min(2_000, int(limit)))).to_list(length=limit)
        if not due:
            return 0
        result = await self.reviews.update_many(
            {"_id": {"$in": [item["_id"] for item in due]}, "status": "pending"},
            {"$set": {"status": "expired", "expired_at": now, "updated_at": now}},
        )
        return int(result.modified_count)
