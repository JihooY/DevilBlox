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


try:  # The domain module may be added independently of this store.
    from services.brokerage import (  # type: ignore
        clamp_score as _domain_clamp_score,
        penalty_level as _domain_penalty_level,
        penalized_decrease as _domain_penalized_decrease,
        posting_interval_minutes as _domain_posting_interval_minutes,
        price_points as _domain_price_points,
    )
except (ImportError, AttributeError):
    try:
        from services.brokerage import (  # type: ignore
            apply_problem_penalty as _canonical_problem_penalty,
            clamp_credit_score as _canonical_clamp_score,
            penalty_level as _domain_penalty_level,
            posting_interval_for_score as _canonical_posting_interval,
            price_to_base_points as _canonical_price_points,
        )
    except (ImportError, AttributeError):
        _canonical_problem_penalty = None
        _canonical_clamp_score = None
        _canonical_posting_interval = None
        _canonical_price_points = None
        _domain_penalty_level = None

    _domain_clamp_score = _canonical_clamp_score
    _domain_price_points = _canonical_price_points
    _domain_penalized_decrease = _canonical_problem_penalty
    _domain_posting_interval_minutes = _canonical_posting_interval


DEFAULT_CONFIG: dict[str, Any] = {
    "listing_channel_id": None,
    "ticket_category_id": None,
    "helper_role_id": None,
    "notification_role_id": None,
    "minimum_listing_score": -100,
    "price_point_unit": 1_000,
    "min_price_points": 1,
    "max_price_points": 10,
    "settlement_delay_days": 7,
    "review_window_days": 7,
    "email_verification_bonus": 10,
    "phone_verification_bonus": 20,
    "low_rating_problem_threshold": 2,
    "like_score_every": 10,
    "like_score_bonus": 1,
    "like_score_max_bonus": 3,
    "like_fast_bump_threshold": 10,
    "like_fast_bump_percent": 0,
    "like_bump_reduction_minutes": 2,
    "bump_intervals": [
        {"min_score": 80, "minutes": 10},
        {"min_score": 50, "minutes": 20},
        {"min_score": 0, "minutes": 30},
        {"min_score": -100, "minutes": 60},
    ],
}

_CONFIG_ID_FIELDS = {
    "listing_channel_id",
    "ticket_category_id",
    "helper_role_id",
    "notification_role_id",
}
_CONFIG_INT_RANGES: dict[str, tuple[int, int]] = {
    "minimum_listing_score": (-100, 100),
    "price_point_unit": (1, 1_000_000_000),
    "min_price_points": (1, 100),
    "max_price_points": (1, 100),
    "settlement_delay_days": (1, 90),
    "review_window_days": (1, 90),
    "email_verification_bonus": (0, 100),
    "phone_verification_bonus": (0, 100),
    "low_rating_problem_threshold": (1, 5),
    "like_score_every": (1, 1_000_000),
    "like_score_bonus": (0, 100),
    "like_score_max_bonus": (0, 100),
    "like_fast_bump_threshold": (0, 1_000_000),
    "like_fast_bump_percent": (0, 90),
    "like_bump_reduction_minutes": (0, 60),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_id(value: int, name: str) -> int:
    value = int(value)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _clean_text(value: Any, name: str, *, maximum: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} is required")
    if len(text) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return text


def _clamp_score(value: int | float) -> int:
    if _domain_clamp_score is not None:
        try:
            return int(_domain_clamp_score(value))
        except TypeError:
            pass
    return max(-100, min(100, int(value)))


def _problem_penalty_level(problem_count: int) -> int:
    count = max(0, int(problem_count))
    if _domain_penalty_level is not None:
        try:
            return max(0, int(_domain_penalty_level(count)))
        except TypeError:
            pass
    return max(0, count - 3)


def _penalized_delta(delta: int, problem_count: int) -> int:
    delta = int(delta)
    if delta >= 0:
        return delta
    if _domain_penalized_decrease is not None:
        try:
            if getattr(_domain_penalized_decrease, "__name__", "") == "apply_problem_penalty":
                return int(_domain_penalized_decrease(delta, int(problem_count)))
            magnitude = int(_domain_penalized_decrease(abs(delta), int(problem_count)))
            return -abs(magnitude)
        except (TypeError, ValueError):
            pass
    level = _problem_penalty_level(problem_count)
    return -int(math.ceil(abs(delta) * (100 + level * 10) / 100))


def _points_for_price(amount: int, config: Mapping[str, Any]) -> int:
    amount = int(amount)
    if amount < 0:
        raise ValueError("amount must be zero or greater")
    unit = int(config.get("price_point_unit", 1_000))
    minimum = int(config.get("min_price_points", 1))
    maximum = int(config.get("max_price_points", 10))
    if unit <= 0 or minimum < 0 or maximum < minimum:
        raise ValueError("invalid price point configuration")

    # Domain defaults are used when they match the guild policy. Custom policies
    # remain supported without coupling the database layer to policy dataclasses.
    if amount == 0:
        return minimum
    if _domain_price_points is not None:
        try:
            return max(
                minimum,
                min(
                    maximum,
                    int(
                        _domain_price_points(
                            amount,
                            unit,
                            minimum_points=minimum,
                            maximum_points=maximum,
                        )
                    ),
                ),
            )
        except TypeError:
            if (unit, minimum, maximum) == (1_000, 1, 10):
                try:
                    return max(minimum, min(maximum, int(_domain_price_points(amount))))
                except TypeError:
                    pass
    points = int(math.ceil(amount / unit)) if amount else minimum
    return max(minimum, min(maximum, points))


def _posting_minutes(score: int, config: Mapping[str, Any], *, like_count: int = 0) -> int:
    tiers = config.get("bump_intervals") or DEFAULT_CONFIG["bump_intervals"]
    default_tiers = DEFAULT_CONFIG["bump_intervals"]
    minutes: int | None = None
    if tiers == default_tiers and _domain_posting_interval_minutes is not None:
        try:
            result = _domain_posting_interval_minutes(int(score))
            minutes = int(result.total_seconds() // 60) if isinstance(result, timedelta) else int(result)
        except TypeError:
            pass
    if minutes is None:
        ordered = sorted(tiers, key=lambda item: int(item["min_score"]), reverse=True)
        minutes = int(ordered[-1]["minutes"])
        for tier in ordered:
            if int(score) >= int(tier["min_score"]):
                minutes = int(tier["minutes"])
                break

    threshold = int(config.get("like_fast_bump_threshold", 10))
    percent = int(config.get("like_fast_bump_percent", 20))
    if threshold > 0 and int(like_count) >= threshold:
        if percent > 0:
            minutes = max(1, int(math.ceil(minutes * (100 - percent) / 100)))
    every = max(1, int(config.get("like_score_every", 10)))
    bonus_each = max(0, int(config.get("like_score_bonus", 1)))
    maximum_bonus = max(0, int(config.get("like_score_max_bonus", 3)))
    like_credit = min(maximum_bonus, (max(0, int(like_count)) // every) * bonus_each)
    minutes = max(
        5,
        minutes - int(config.get("like_bump_reduction_minutes", 2)) * like_credit,
    )
    return max(1, minutes)


def _profile_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def _preference_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def _ledger_key(guild_id: int, user_id: int, operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"{guild_id}:{user_id}:{digest}"


def _problem_key(guild_id: int, user_id: int, operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"{guild_id}:{user_id}:{digest}"


def _safe_datetime(value: datetime | None, *, default: datetime | None = None) -> datetime:
    value = value or default or _now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class BrokerageCore:
    """Mongo-backed marketplace state shared by repository mixins."""

    def __init__(self, db, *, verification_pepper: str = ""):
        self.configs = db["brokerage_configs"]
        self.profiles = db["brokerage_profiles"]
        self.score_ledger = db["brokerage_score_ledger"]
        self.problems = db["brokerage_problems"]
        self.verifications = db["brokerage_verifications"]
        self.notification_preferences = db["brokerage_notification_preferences"]
        self.listings = db["brokerage_listings"]
        self.reports = db["brokerage_reports"]
        self.reviews = db["brokerage_reviews"]
        self._verification_pepper = verification_pepper.encode("utf-8")

    async def ensure_indexes(self) -> None:
        await self.profiles.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
        await self.profiles.create_index([("guild_id", 1), ("trust_score", -1)])
        await self.score_ledger.create_index(
            [("guild_id", 1), ("user_id", 1), ("operation_id", 1)], unique=True
        )
        await self.score_ledger.create_index([("guild_id", 1), ("user_id", 1), ("created_at", -1)])
        await self.problems.create_index(
            [("guild_id", 1), ("user_id", 1), ("operation_id", 1)], unique=True
        )
        await self.problems.create_index([("guild_id", 1), ("user_id", 1), ("status", 1)])
        await self.problems.create_index([("listing_id", 1), ("created_at", -1)])
        await self.verifications.create_index(
            [("guild_id", 1), ("user_id", 1), ("kind", 1), ("status", 1)]
        )
        await self.verifications.create_index(
            [("guild_id", 1), ("user_id", 1), ("kind", 1)],
            name="brokerage_pending_verification_unique",
            unique=True,
            partialFilterExpression={"status": "pending"},
        )
        await self.verifications.create_index(
            [("guild_id", 1), ("kind", 1), ("target_hash", 1)],
            name="brokerage_verified_target_unique",
            unique=True,
            partialFilterExpression={"status": "consumed"},
        )
        await self.verifications.create_index([("expires_at", 1)])
        await self.notification_preferences.create_index(
            [("guild_id", 1), ("user_id", 1)], unique=True
        )
        await self.notification_preferences.create_index([("guild_id", 1), ("enabled", 1)])
        await self.listings.create_index([("guild_id", 1), ("status", 1), ("next_bump_at", 1)])
        await self.listings.create_index([("guild_id", 1), ("seller_id", 1), ("created_at", -1)])
        await self.listings.create_index(
            [("guild_id", 1), ("channel_id", 1), ("message_id", 1)],
            unique=True,
            partialFilterExpression={
                "channel_id": {"$type": "number"},
                "message_id": {"$type": "number"},
            },
        )
        await self.listings.create_index([("status", 1), ("settlement.due_at", 1)])
        await self.listings.create_index([("active_ticket_channel_id", 1)], sparse=True)
        await self.reports.create_index([("listing_id", 1), ("reporter_id", 1)], unique=True)
        await self.reports.create_index([("guild_id", 1), ("status", 1), ("created_at", -1)])
        await self.reviews.create_index([("listing_id", 1)], unique=True)
        await self.reviews.create_index([("guild_id", 1), ("seller_id", 1), ("status", 1)])
        await self.reviews.create_index([("status", 1), ("expires_at", 1)])

    async def get_config(self, guild_id: int) -> dict:
        guild_id = _require_id(guild_id, "guild_id")
        now = _now()
        await self.configs.update_one(
            {"_id": guild_id},
            {
                "$setOnInsert": {
                    "_id": guild_id,
                    "guild_id": guild_id,
                    **deepcopy(DEFAULT_CONFIG),
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        doc = await self.configs.find_one({"_id": guild_id})
        if doc is None:
            raise RuntimeError("brokerage configuration could not be loaded")
        merged = {**deepcopy(DEFAULT_CONFIG), **doc}
        return merged

    async def update_config(
        self,
        guild_id: int,
        settings: Mapping[str, Any] | None = None,
        *,
        updated_by: int | None = None,
        **changes: Any,
    ) -> dict:
        guild_id = _require_id(guild_id, "guild_id")
        supplied = dict(settings or {})
        supplied.update(changes)
        if not supplied:
            return await self.get_config(guild_id)
        unknown = set(supplied) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError(f"unknown brokerage settings: {', '.join(sorted(unknown))}")

        clean: dict[str, Any] = {}
        for name, value in supplied.items():
            if name in _CONFIG_ID_FIELDS:
                if value is None:
                    clean[name] = None
                else:
                    clean[name] = _require_id(value, name)
            elif name in _CONFIG_INT_RANGES:
                lower, upper = _CONFIG_INT_RANGES[name]
                value = int(value)
                if value < lower or value > upper:
                    raise ValueError(f"{name} must be between {lower} and {upper}")
                clean[name] = value
            elif name == "bump_intervals":
                if not isinstance(value, (list, tuple)) or not value:
                    raise ValueError("bump_intervals must be a non-empty list")
                tiers: list[dict[str, int]] = []
                seen: set[int] = set()
                for raw in value:
                    if not isinstance(raw, Mapping):
                        raise ValueError("each bump interval must be an object")
                    min_score = int(raw.get("min_score", -101))
                    minutes = int(raw.get("minutes", 0))
                    if not -100 <= min_score <= 100 or not 1 <= minutes <= 24 * 60:
                        raise ValueError("invalid bump interval")
                    if min_score in seen:
                        raise ValueError("bump interval score thresholds must be unique")
                    seen.add(min_score)
                    tiers.append({"min_score": min_score, "minutes": minutes})
                if min(tier["min_score"] for tier in tiers) > -100:
                    raise ValueError("bump_intervals must include a fallback at -100")
                clean[name] = sorted(tiers, key=lambda item: item["min_score"], reverse=True)

        prospective = {**(await self.get_config(guild_id)), **clean}
        if int(prospective["min_price_points"]) > int(prospective["max_price_points"]):
            raise ValueError("min_price_points cannot exceed max_price_points")
        now = _now()
        clean["updated_at"] = now
        if updated_by is not None:
            clean["updated_by"] = _require_id(updated_by, "updated_by")
        await self.configs.update_one({"_id": guild_id}, {"$set": clean})
        updated_config = await self.get_config(guild_id)
        await self.refresh_listing_intervals(guild_id, config=updated_config)
        return updated_config

    async def list_pending_persistent_objects(self, *, limit: int = 500) -> dict[str, list[dict]]:
        capped = max(1, min(2_000, int(limit)))
        listings = await self.listings.find(
            {"status": {"$in": ["open", "reserved"]}}
        ).sort("created_at", 1).to_list(length=capped)
        settlements = await self.listings.find(
            {"status": "sold", "settlement.status": "pending"}
        ).sort("settlement.due_at", 1).to_list(length=capped)
        reviews = await self.list_pending_reviews(limit=capped)
        problems = await self.list_pending_problems(limit=capped)
        reports = await self.list_pending_reports(limit=capped)
        verifications = await self.list_pending_verifications(limit=capped)
        return {
            "listings": listings,
            "settlements": settlements,
            "reviews": reviews,
            "problems": problems,
            "reports": reports,
            "verifications": verifications,
        }
