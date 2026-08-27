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


class BrokerageProfileMixin:
    async def ensure_profile(self, guild_id: int, user_id: int) -> dict:
        guild_id = _require_id(guild_id, "guild_id")
        user_id = _require_id(user_id, "user_id")
        now = _now()
        profile = await self.profiles.find_one_and_update(
            {"_id": _profile_key(guild_id, user_id)},
            {
                "$setOnInsert": {
                    "_id": _profile_key(guild_id, user_id),
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "trust_score": 0,
                    "problem_count": 0,
                    "penalty_level": 0,
                    "rating_total": 0,
                    "rating_count": 0,
                    "rating_average": None,
                    "like_score_total": 0,
                    "like_score_operation_ids": [],
                    "successful_trade_count": 0,
                    "successful_trade_operation_ids": [],
                    "verified_kinds": [],
                    "score_operation_ids": [],
                    "problem_operation_ids": [],
                    "problem_count_operation_ids": [],
                    "resolved_problem_ids": [],
                    "rating_operation_ids": [],
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if profile is None:
            raise RuntimeError("brokerage profile could not be created")
        return profile

    async def get_profile(self, guild_id: int, user_id: int, *, create: bool = True) -> dict | None:
        if create:
            return await self.ensure_profile(guild_id, user_id)
        return await self.profiles.find_one(
            {"_id": _profile_key(_require_id(guild_id, "guild_id"), _require_id(user_id, "user_id"))}
        )

    async def list_score_ledger(self, guild_id: int, user_id: int, *, limit: int = 100) -> list[dict]:
        limit = max(1, min(1_000, int(limit)))
        return await self.score_ledger.find(
            {
                "guild_id": _require_id(guild_id, "guild_id"),
                "user_id": _require_id(user_id, "user_id"),
            }
        ).sort("created_at", -1).to_list(length=limit)

    async def adjust_profile(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        *,
        operation_id: str,
        reason: str,
        apply_penalty: bool = False,
        actor_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict:
        guild_id = _require_id(guild_id, "guild_id")
        user_id = _require_id(user_id, "user_id")
        operation_id = _clean_text(operation_id, "operation_id", maximum=200, required=True)
        reason = _clean_text(reason, "reason", maximum=100, required=True)
        requested_delta = int(delta)
        if abs(requested_delta) > 1_000:
            raise ValueError("score delta is unreasonably large")
        profile = await self.ensure_profile(guild_id, user_id)
        effective_delta = (
            _penalized_delta(requested_delta, int(profile.get("problem_count", 0)))
            if apply_penalty and requested_delta < 0
            else requested_delta
        )
        ledger_id = _ledger_key(guild_id, user_id, operation_id)
        now = _now()
        ledger = {
            "_id": ledger_id,
            "guild_id": guild_id,
            "user_id": user_id,
            "operation_id": operation_id,
            "reason": reason,
            "requested_delta": requested_delta,
            "delta": effective_delta,
            "penalty_applied": bool(apply_penalty and requested_delta < 0),
            "penalty_level": int(profile.get("penalty_level", 0)),
            "problem_count": int(profile.get("problem_count", 0)),
            "actor_id": int(actor_id) if actor_id is not None else None,
            "metadata": deepcopy(dict(metadata or {})),
            "created_at": now,
        }
        inserted = False
        try:
            await self.score_ledger.insert_one(ledger)
            inserted = True
        except DuplicateKeyError:
            ledger = await self.score_ledger.find_one({"_id": ledger_id})
            if ledger is None:
                raise
            immutable_signature = (
                ledger.get("guild_id"),
                ledger.get("user_id"),
                ledger.get("operation_id"),
                ledger.get("reason"),
                int(ledger.get("requested_delta", 0)),
            )
            if immutable_signature != (guild_id, user_id, operation_id, reason, requested_delta):
                raise ValueError("operation_id is already used for a different score adjustment")
            effective_delta = int(ledger.get("delta", 0))

        updated = await self.profiles.find_one_and_update(
            {
                "_id": _profile_key(guild_id, user_id),
                "score_operation_ids": {"$ne": operation_id},
            },
            [
                {
                    "$set": {
                        "trust_score": {
                            "$max": [
                                -100,
                                {"$min": [100, {"$add": [{"$ifNull": ["$trust_score", 0]}, effective_delta]}]},
                            ]
                        },
                        "score_operation_ids": {
                            "$concatArrays": [
                                {"$ifNull": ["$score_operation_ids", []]},
                                [operation_id],
                            ]
                        },
                        "updated_at": now,
                    }
                }
            ],
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            updated = await self.get_profile(guild_id, user_id)
        if updated is None:
            raise RuntimeError("brokerage score adjustment could not be applied")
        await self.refresh_listing_intervals(
            guild_id,
            seller_id=user_id,
            seller_score=int(updated.get("trust_score", 0)),
        )
        return {
            "profile": updated,
            "ledger": ledger,
            "applied": bool(updated and operation_id in updated.get("score_operation_ids", [])),
            "new_operation": inserted,
            "delta": effective_delta,
        }

    async def add_problem(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        operation_id: str,
        kind: str = "transaction_issue",
        listing_id: str | None = None,
        deduct_score: bool = True,
        created_by: int | None = None,
        notes: str = "",
    ) -> dict:
        guild_id = _require_id(guild_id, "guild_id")
        user_id = _require_id(user_id, "user_id")
        amount = int(amount)
        if amount < 0:
            raise ValueError("amount must be zero or greater")
        if deduct_score and amount == 0:
            raise ValueError("amount must be greater than zero when deduct_score is enabled")
        operation_id = _clean_text(operation_id, "operation_id", maximum=200, required=True)
        kind = _clean_text(kind, "kind", maximum=50, required=True)
        listing_id = _clean_text(listing_id, "listing_id", maximum=100) or None
        notes = _clean_text(notes, "notes", maximum=2_000)
        config = await self.get_config(guild_id)
        base_points = _points_for_price(amount, config) if deduct_score else 0
        problem_id = _problem_key(guild_id, user_id, operation_id)
        now = _now()
        problem = {
            "_id": problem_id,
            "guild_id": guild_id,
            "user_id": user_id,
            "operation_id": operation_id,
            "kind": kind,
            "listing_id": listing_id,
            "amount": amount,
            "deduct_score": bool(deduct_score),
            "base_points": base_points,
            "status": "open",
            "created_by": int(created_by) if created_by is not None else None,
            "notes": notes,
            "created_at": now,
        }
        try:
            await self.problems.insert_one(problem)
        except DuplicateKeyError:
            problem = await self.problems.find_one({"_id": problem_id})
            if problem is None:
                raise
            if (
                problem.get("guild_id") != guild_id
                or problem.get("user_id") != user_id
                or problem.get("operation_id") != operation_id
                or problem.get("kind") != kind
                or int(problem.get("amount", 0)) != amount
                or bool(problem.get("deduct_score", True)) != bool(deduct_score)
            ):
                raise ValueError("operation_id is already used for a different problem")

        base_points = int(problem.get("base_points", base_points))

        profile = await self.ensure_profile(guild_id, user_id)
        if operation_id not in profile.get("problem_operation_ids", []):
            profile = await self.profiles.find_one_and_update(
                {
                    "_id": _profile_key(guild_id, user_id),
                    "problem_operation_ids": {"$ne": operation_id},
                },
                [
                    {
                        "$set": {
                            "problem_count": {"$add": [{"$ifNull": ["$problem_count", 0]}, 1]},
                            "problem_operation_ids": {
                                "$concatArrays": [
                                    {"$ifNull": ["$problem_operation_ids", []]}, [operation_id]
                                ]
                            },
                            "updated_at": now,
                        }
                    },
                    {
                        "$set": {
                            "penalty_level": {
                                "$max": [0, {"$subtract": ["$problem_count", 3]}]
                            }
                        }
                    },
                ],
                return_document=ReturnDocument.AFTER,
            ) or await self.get_profile(guild_id, user_id)

        score_result = None
        if deduct_score:
            score_result = await self.adjust_profile(
                guild_id,
                user_id,
                -base_points,
                operation_id=f"problem:{problem_id}:deduction",
                reason=f"problem:{kind}",
                apply_penalty=True,
                actor_id=created_by,
                metadata={"problem_id": problem_id, "listing_id": listing_id, "amount": amount},
            )
        if listing_id:
            await self.listings.update_one(
                {
                    "_id": listing_id,
                    "settlement.status": "pending",
                },
                {
                    "$set": {
                        "settlement.status": "problem",
                        "settlement.problem_id": problem_id,
                        "updated_at": now,
                    }
                },
            )
        fresh = await self.problems.find_one({"_id": problem_id}) or problem
        return {"problem": fresh, "profile": profile, "score": score_result}

    async def get_problem(self, problem_id: str) -> dict | None:
        return await self.problems.find_one(
            {
                "_id": _clean_text(
                    problem_id, "problem_id", maximum=200, required=True
                )
            }
        )

    async def resolve_problem(
        self,
        problem_id: str,
        *,
        resolved_by: int,
        upheld: bool = False,
        resolution: str = "",
    ) -> dict | None:
        problem_id = _clean_text(problem_id, "problem_id", maximum=200, required=True)
        resolved_by = _require_id(resolved_by, "resolved_by")
        resolution = _clean_text(resolution, "resolution", maximum=2_000)
        now = _now()
        status = "upheld" if upheld else "overturned"
        resolvable_statuses = ["open"] if upheld else ["open", "upheld"]
        problem = await self.problems.find_one_and_update(
            {"_id": problem_id, "status": {"$in": resolvable_statuses}},
            {
                "$set": {
                    "status": status,
                    "resolution": resolution,
                    "resolved_by": resolved_by,
                    "resolved_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if problem is None:
            problem = await self.problems.find_one({"_id": problem_id})
        if problem is None:
            return None
        if problem.get("status") != status:
            raise ValueError("problem has already been resolved with a different outcome")

        guild_id = int(problem["guild_id"])
        user_id = int(problem["user_id"])
        score_result = None
        if not upheld:
            profile = await self.ensure_profile(guild_id, user_id)
            if problem_id not in profile.get("resolved_problem_ids", []):
                profile = await self.profiles.find_one_and_update(
                    {
                        "_id": _profile_key(guild_id, user_id),
                        "resolved_problem_ids": {"$ne": problem_id},
                    },
                    [
                        {
                            "$set": {
                                "problem_count": {
                                    "$max": [0, {"$subtract": [{"$ifNull": ["$problem_count", 0]}, 1]}]
                                },
                                "resolved_problem_ids": {
                                    "$concatArrays": [
                                        {"$ifNull": ["$resolved_problem_ids", []]}, [problem_id]
                                    ]
                                },
                                "updated_at": now,
                            }
                        },
                        {
                            "$set": {
                                "penalty_level": {
                                    "$max": [0, {"$subtract": ["$problem_count", 3]}]
                                }
                            }
                        },
                    ],
                    return_document=ReturnDocument.AFTER,
                ) or await self.get_profile(guild_id, user_id)
            if problem.get("deduct_score"):
                ledger = await self.score_ledger.find_one(
                    {"_id": _ledger_key(guild_id, user_id, f"problem:{problem_id}:deduction")}
                )
                restore = abs(int((ledger or {}).get("delta", problem.get("base_points", 0))))
                score_result = await self.adjust_profile(
                    guild_id,
                    user_id,
                    restore,
                    operation_id=f"problem:{problem_id}:reversal",
                    reason="problem_overturned",
                    actor_id=resolved_by,
                    metadata={"problem_id": problem_id},
                )
        else:
            profile = await self.get_profile(guild_id, user_id)
        listing_id = problem.get("listing_id")
        if not upheld and listing_id:
            remaining = await self.problems.count_documents(
                {
                    "listing_id": listing_id,
                    "status": {"$in": ["open", "upheld"]},
                }
            )
            if remaining == 0:
                await self.listings.update_one(
                    {
                        "_id": listing_id,
                        "settlement.status": "problem",
                    },
                    {
                        "$set": {"settlement.status": "pending", "updated_at": _now()},
                        "$unset": {"settlement.problem_id": ""},
                    },
                )
        return {"problem": problem, "profile": profile, "score": score_result}

    async def admin_adjust_problem_count(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        operation_id: str,
        actor_id: int,
        reason: str,
    ) -> dict:
        """Apply an audited, idempotent manual correction to a problem count."""

        guild_id = _require_id(guild_id, "guild_id")
        user_id = _require_id(user_id, "user_id")
        actor_id = _require_id(actor_id, "actor_id")
        delta = int(delta)
        if delta == 0 or abs(delta) > 1_000:
            raise ValueError("delta must be a non-zero integer between -1000 and 1000")
        operation_id = _clean_text(operation_id, "operation_id", maximum=200, required=True)
        reason = _clean_text(reason, "reason", maximum=2_000, required=True)
        count_operation_id = f"admin-problem-count:{operation_id}"
        audit_id = _problem_key(guild_id, user_id, count_operation_id)
        now = _now()
        audit = {
            "_id": audit_id,
            "guild_id": guild_id,
            "user_id": user_id,
            "operation_id": count_operation_id,
            "kind": "admin_problem_count_adjustment",
            "problem_count_delta": delta,
            "deduct_score": False,
            "status": "applied",
            "created_by": actor_id,
            "notes": reason,
            "created_at": now,
            "applied_at": now,
        }
        try:
            await self.problems.insert_one(audit)
        except DuplicateKeyError:
            audit = await self.problems.find_one({"_id": audit_id})
            if audit is None:
                raise
            if (
                audit.get("operation_id") != count_operation_id
                or int(audit.get("problem_count_delta", 0)) != delta
                or audit.get("notes") != reason
            ):
                raise ValueError("operation_id is already used for another problem-count adjustment")

        await self.ensure_profile(guild_id, user_id)
        profile = await self.profiles.find_one_and_update(
            {
                "_id": _profile_key(guild_id, user_id),
                "problem_count_operation_ids": {"$ne": count_operation_id},
            },
            [
                {
                    "$set": {
                        "problem_count": {
                            "$max": [0, {"$add": [{"$ifNull": ["$problem_count", 0]}, delta]}]
                        },
                        "problem_count_operation_ids": {
                            "$concatArrays": [
                                {"$ifNull": ["$problem_count_operation_ids", []]},
                                [count_operation_id],
                            ]
                        },
                        "updated_at": now,
                    }
                },
                {
                    "$set": {
                        "penalty_level": {"$max": [0, {"$subtract": ["$problem_count", 3]}]}
                    }
                },
            ],
            return_document=ReturnDocument.AFTER,
        )
        if profile is None:
            profile = await self.ensure_profile(guild_id, user_id)
        return {"profile": profile, "audit": audit, "applied": count_operation_id in profile.get("problem_count_operation_ids", [])}

    async def list_pending_problems(self, *, limit: int = 500) -> list[dict]:
        return await self.problems.find({"status": "open"}).sort("created_at", 1).to_list(
            length=max(1, min(2_000, int(limit)))
        )
