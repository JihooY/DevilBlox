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


class BrokerageSettlementMixin:
    @staticmethod
    def _settlement_score_operation_ids(listing: Mapping[str, Any]) -> tuple[str, str]:
        settlement = listing.get("settlement") or {}
        base = str(
            settlement.get("operation_id")
            or f"listing:{listing['_id']}:settlement"
        )
        return (
            str(settlement.get("seller_score_operation_id") or f"{base}:seller"),
            str(settlement.get("buyer_score_operation_id") or f"{base}:buyer"),
        )

    async def reverse_settlement_awards(
        self,
        listing: Mapping[str, Any],
        *,
        problem_id: str,
        actor_id: int | None,
    ) -> list[dict]:
        """Compensate any settlement awards that raced with a reported problem."""

        guild_id = int(listing["guild_id"])
        seller_operation, buyer_operation = self._settlement_score_operation_ids(listing)
        results: list[dict] = []
        problem_token = hashlib.sha256(problem_id.encode("utf-8")).hexdigest()[:16]
        for role, user_id, original_operation in (
            ("seller", int(listing["seller_id"]), seller_operation),
            ("buyer", int(listing.get("sold_to", 0) or 0), buyer_operation),
        ):
            if user_id <= 0:
                continue
            original = await self.score_ledger.find_one(
                {"_id": _ledger_key(guild_id, user_id, original_operation)}
            )
            awarded = int(
                (original or {}).get(
                    "applied_delta", (original or {}).get("delta", 0)
                )
            )
            if awarded <= 0:
                continue
            result = await self.adjust_profile(
                guild_id,
                user_id,
                -awarded,
                operation_id=(
                    f"settlement-reversal:{listing['_id']}:{role}:{problem_token}"
                ),
                reason="settlement_problem_reversal",
                actor_id=actor_id,
                metadata={
                    "listing_id": str(listing["_id"]),
                    "problem_id": problem_id,
                    "original_operation_id": original_operation,
                },
            )
            results.append(result)
        return results

    async def restore_settlement_awards(
        self,
        listing: Mapping[str, Any],
        *,
        problem_id: str,
        actor_id: int | None,
    ) -> list[dict]:
        """Undo race compensation when the last listing problem is overturned."""

        guild_id = int(listing["guild_id"])
        problem_token = hashlib.sha256(problem_id.encode("utf-8")).hexdigest()[:16]
        results: list[dict] = []
        for role, user_id in (
            ("seller", int(listing["seller_id"])),
            ("buyer", int(listing.get("sold_to", 0) or 0)),
        ):
            if user_id <= 0:
                continue
            reversal_operation = (
                f"settlement-reversal:{listing['_id']}:{role}:{problem_token}"
            )
            reversal = await self.score_ledger.find_one(
                {"_id": _ledger_key(guild_id, user_id, reversal_operation)}
            )
            reversed_amount = abs(
                int(
                    (reversal or {}).get(
                        "applied_delta", (reversal or {}).get("delta", 0)
                    )
                )
            )
            if reversed_amount <= 0:
                continue
            result = await self.adjust_profile(
                guild_id,
                user_id,
                reversed_amount,
                operation_id=(
                    f"settlement-restoration:{listing['_id']}:{role}:{problem_token}"
                ),
                reason="settlement_problem_overturned",
                actor_id=actor_id,
                metadata={
                    "listing_id": str(listing["_id"]),
                    "problem_id": problem_id,
                    "reversal_operation_id": reversal_operation,
                },
            )
            results.append(result)
        return results

    async def list_due_settlements(
        self,
        now: datetime | None = None,
        *,
        limit: int = 100,
    ) -> list[dict]:
        now = _safe_datetime(now)
        return await self.listings.find(
            {
                "status": "sold",
                "settlement.status": {"$in": ["pending", "processing"]},
                "settlement.due_at": {"$lte": now},
            }
        ).sort("settlement.due_at", 1).to_list(length=max(1, min(1_000, int(limit))))

    async def _record_successful_trade(
        self,
        guild_id: int,
        user_id: int,
        operation_id: str,
    ) -> dict:
        await self.ensure_profile(guild_id, user_id)
        profile = await self.profiles.find_one_and_update(
            {
                "_id": _profile_key(guild_id, user_id),
                "successful_trade_operation_ids": {"$ne": operation_id},
            },
            {
                "$inc": {"successful_trade_count": 1},
                "$addToSet": {"successful_trade_operation_ids": operation_id},
                "$set": {"updated_at": _now()},
            },
            return_document=ReturnDocument.AFTER,
        )
        return profile or await self.ensure_profile(guild_id, user_id)

    async def settle_success(
        self,
        listing_id: str,
        *,
        actor_id: int | None = None,
        operation_id: str | None = None,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict | None:
        listing_id = _clean_text(listing_id, "listing_id", maximum=100, required=True)
        now = _safe_datetime(now)
        listing = await self.get_listing(listing_id)
        if listing is None or listing.get("status") != "sold":
            return None
        settlement = listing.get("settlement") or {}
        if settlement.get("status") == "success":
            return {
                "listing": listing,
                "score": None,
                "seller_score": None,
                "buyer_score": None,
                "already_settled": True,
            }
        if settlement.get("status") not in {"pending", "processing"}:
            return {
                "listing": listing,
                "score": None,
                "seller_score": None,
                "buyer_score": None,
                "already_settled": False,
            }
        due_at = _safe_datetime(settlement.get("due_at"))
        if due_at > now and not force:
            return {
                "listing": listing,
                "score": None,
                "seller_score": None,
                "buyer_score": None,
                "already_settled": False,
                "not_due": True,
            }

        if settlement.get("status") == "pending":
            claim_filter: dict[str, Any] = {
                "_id": listing_id,
                "status": "sold",
                "settlement.status": "pending",
            }
            if not force:
                claim_filter["settlement.due_at"] = {"$lte": now}
            claimed = await self.listings.find_one_and_update(
                claim_filter,
                {
                    "$set": {
                        "settlement.status": "processing",
                        "settlement.processing_at": now,
                        "settlement.processing_by": (
                            int(actor_id) if actor_id is not None else None
                        ),
                        "updated_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if claimed is None:
                current = await self.get_listing(listing_id)
                current_status = (current or {}).get("settlement", {}).get("status")
                if current_status == "success":
                    return {
                        "listing": current,
                        "score": None,
                        "seller_score": None,
                        "buyer_score": None,
                        "already_settled": True,
                    }
                if current_status != "processing":
                    return {
                        "listing": current,
                        "score": None,
                        "seller_score": None,
                        "buyer_score": None,
                        "already_settled": False,
                    }
                listing = current
            else:
                listing = claimed
            settlement = listing.get("settlement") or {}

        config = await self.get_config(int(listing["guild_id"]))
        points = int(settlement.get("points") or _points_for_price(int(listing["price"]), config))
        base_operation_id = _clean_text(
            operation_id or settlement.get("operation_id") or f"listing:{listing_id}:settlement",
            "operation_id",
            maximum=190,
            required=True,
        )
        seller_score = await self.adjust_profile(
            int(listing["guild_id"]),
            int(listing["seller_id"]),
            points,
            operation_id=f"{base_operation_id}:seller",
            reason="transaction_settled",
            actor_id=actor_id,
            metadata={
                "listing_id": listing_id,
                "buyer_id": listing.get("sold_to"),
                "amount": int(listing["price"]),
            },
        )
        buyer_score = await self.adjust_profile(
            int(listing["guild_id"]),
            int(listing["sold_to"]),
            points,
            operation_id=f"{base_operation_id}:buyer",
            reason="transaction_settled",
            actor_id=actor_id,
            metadata={
                "listing_id": listing_id,
                "seller_id": listing.get("seller_id"),
                "amount": int(listing["price"]),
            },
        )
        seller_profile = await self._record_successful_trade(
            int(listing["guild_id"]),
            int(listing["seller_id"]),
            f"{base_operation_id}:seller",
        )
        buyer_profile = await self._record_successful_trade(
            int(listing["guild_id"]),
            int(listing["sold_to"]),
            f"{base_operation_id}:buyer",
        )
        seller_score["profile"] = seller_profile
        buyer_score["profile"] = buyer_profile
        updated = await self.listings.find_one_and_update(
            {
                "_id": listing_id,
                "status": "sold",
                "settlement.status": "processing",
            },
            {
                "$set": {
                    "settlement.status": "success",
                    "settlement.settled_at": now,
                    "settlement.points": points,
                    "settlement.seller_score_operation_id": f"{base_operation_id}:seller",
                    "settlement.buyer_score_operation_id": f"{base_operation_id}:buyer",
                    "settlement.settled_by": int(actor_id) if actor_id is not None else None,
                    "settlement.processing_at": None,
                    "settlement.processing_by": None,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            updated = await self.get_listing(listing_id)
            if (updated or {}).get("settlement", {}).get("status") == "problem":
                problem_id = str(
                    (updated.get("settlement") or {}).get("problem_id")
                    or f"listing:{listing_id}:problem"
                )
                await self.reverse_settlement_awards(
                    updated,
                    problem_id=problem_id,
                    actor_id=actor_id,
                )
                updated = await self.get_listing(listing_id)
                return {
                    "listing": updated,
                    "score": None,
                    "seller_score": None,
                    "buyer_score": None,
                    "already_settled": False,
                    "blocked_by_problem": True,
                }
            if (updated or {}).get("settlement", {}).get("status") == "success":
                return {
                    "listing": updated,
                    "score": None,
                    "seller_score": None,
                    "buyer_score": None,
                    "already_settled": True,
                }
        return {
            "listing": updated,
            "score": seller_score,
            "seller_score": seller_score,
            "buyer_score": buyer_score,
            "already_settled": False,
        }

    async def mark_settlement_problem(
        self,
        listing_id: str,
        *,
        operation_id: str,
        kind: str,
        actor_id: int,
        notes: str = "",
        deduct_score: bool = True,
    ) -> dict | None:
        listing = await self.get_listing(listing_id)
        if listing is None:
            return None
        result = await self.add_problem(
            int(listing["guild_id"]),
            int(listing["seller_id"]),
            int(listing["price"]),
            operation_id=operation_id,
            kind=kind,
            listing_id=listing_id,
            deduct_score=deduct_score,
            created_by=actor_id,
            notes=notes,
        )
        return {"listing": await self.get_listing(listing_id), **result}
