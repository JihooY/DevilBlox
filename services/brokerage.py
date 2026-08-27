"""Pure domain rules for the brokerage marketplace.

This module intentionally contains no Discord or persistence code.  Callers can
therefore use the same rules from interaction handlers, scheduled settlement
jobs, and administrative commands without duplicating business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Hashable, Iterable


MIN_CREDIT_SCORE = -100
MAX_CREDIT_SCORE = 100

DEFAULT_PRICE_UNIT_KRW = 1_000
MIN_TRANSACTION_POINTS = 1
MAX_TRANSACTION_POINTS = 10

PENALTY_FREE_PROBLEM_COUNT = 3
PENALTY_PERCENT_PER_LEVEL = 10

REVIEW_WINDOW = timedelta(days=7)
SETTLEMENT_COOLING_PERIOD = timedelta(days=7)

LOW_REVIEW_PROBLEM_MAX_RATING = 2
MIN_REVIEW_RATING = 1
MAX_REVIEW_RATING = 5

DEFAULT_LIKES_PER_CREDIT = 10
# A finite default prevents a single popular listing from increasing a user's
# score without bound.  Guild settings can replace this policy when needed.
DEFAULT_MAX_LIKE_CREDIT_PER_LISTING = 3


def _require_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


@dataclass(frozen=True, slots=True)
class PricePointPolicy:
    """Configurable mapping from a KRW transaction price to score points."""

    unit_price_krw: int = DEFAULT_PRICE_UNIT_KRW
    minimum_points: int = MIN_TRANSACTION_POINTS
    maximum_points: int = MAX_TRANSACTION_POINTS

    def __post_init__(self) -> None:
        _require_int(self.unit_price_krw, "unit_price_krw")
        _require_int(self.minimum_points, "minimum_points")
        _require_int(self.maximum_points, "maximum_points")
        if self.unit_price_krw <= 0:
            raise ValueError("unit_price_krw must be greater than zero")
        if self.minimum_points <= 0:
            raise ValueError("minimum_points must be greater than zero")
        if self.maximum_points < self.minimum_points:
            raise ValueError("maximum_points must be at least minimum_points")

    def points_for(self, amount_krw: int) -> int:
        """Return ``ceil(amount / unit)`` constrained to the policy range."""

        amount_krw = _require_int(amount_krw, "amount_krw")
        if amount_krw <= 0:
            raise ValueError("amount_krw must be greater than zero")
        raw_points = _ceil_div(amount_krw, self.unit_price_krw)
        return min(self.maximum_points, max(self.minimum_points, raw_points))


DEFAULT_PRICE_POINT_POLICY = PricePointPolicy()


@dataclass(frozen=True, slots=True)
class PenaltyPolicy:
    """Escalation applied to score losses after repeated problems.

    The first ``unpenalized_problem_count`` problems do not add a multiplier.
    Each subsequent problem adds ``percent_per_level`` percent to losses only.
    """

    unpenalized_problem_count: int = PENALTY_FREE_PROBLEM_COUNT
    percent_per_level: int = PENALTY_PERCENT_PER_LEVEL

    def __post_init__(self) -> None:
        _require_int(self.unpenalized_problem_count, "unpenalized_problem_count")
        _require_int(self.percent_per_level, "percent_per_level")
        if self.unpenalized_problem_count < 0:
            raise ValueError("unpenalized_problem_count cannot be negative")
        if self.percent_per_level < 0:
            raise ValueError("percent_per_level cannot be negative")

    def level_for(self, problem_count: int) -> int:
        problem_count = _require_int(problem_count, "problem_count")
        if problem_count < 0:
            raise ValueError("problem_count cannot be negative")
        return max(0, problem_count - self.unpenalized_problem_count)

    def apply(self, score_change: int, problem_count: int) -> int:
        """Increase only a negative change, rounding its magnitude upward.

        Integer arithmetic keeps the result stable across platforms.  For
        example, a -3 change at level 1 becomes ``ceil(3 * 1.10) == -4``.
        """

        score_change = _require_int(score_change, "score_change")
        level = self.level_for(problem_count)
        if score_change >= 0 or level == 0:
            return score_change

        multiplier_percent = 100 + (level * self.percent_per_level)
        penalized_magnitude = _ceil_div(abs(score_change) * multiplier_percent, 100)
        return -penalized_magnitude


DEFAULT_PENALTY_POLICY = PenaltyPolicy()


@dataclass(frozen=True, slots=True)
class TransactionCreditAdjustment:
    """Fully calculated credit outcome for one completed transaction."""

    amount_krw: int
    problematic: bool
    problem_count: int
    base_points: int
    penalty_level: int
    score_change: int
    score_before: int
    score_after: int


def price_to_base_points(
    amount_krw: int,
    *,
    policy: PricePointPolicy = DEFAULT_PRICE_POINT_POLICY,
) -> int:
    """Map a positive transaction amount to configurable points (default 1..10)."""

    if not isinstance(policy, PricePointPolicy):
        raise TypeError("policy must be a PricePointPolicy")
    return policy.points_for(amount_krw)


def penalty_level(
    problem_count: int,
    *,
    policy: PenaltyPolicy = DEFAULT_PENALTY_POLICY,
) -> int:
    """Return the current escalation level (counts 0..3 => 0, 4 => 1)."""

    if not isinstance(policy, PenaltyPolicy):
        raise TypeError("policy must be a PenaltyPolicy")
    return policy.level_for(problem_count)


def apply_problem_penalty(
    score_change: int,
    problem_count: int,
    *,
    policy: PenaltyPolicy = DEFAULT_PENALTY_POLICY,
) -> int:
    """Apply repeated-problem escalation to a negative score change."""

    if not isinstance(policy, PenaltyPolicy):
        raise TypeError("policy must be a PenaltyPolicy")
    return policy.apply(score_change, problem_count)


def clamp_credit_score(score: int) -> int:
    """Constrain a credit score to the system-wide -100..100 range."""

    score = _require_int(score, "score")
    return min(MAX_CREDIT_SCORE, max(MIN_CREDIT_SCORE, score))


def apply_credit_change(score: int, score_change: int) -> int:
    """Apply an integer change and clamp the resulting credit score."""

    score = _require_int(score, "score")
    score_change = _require_int(score_change, "score_change")
    return clamp_credit_score(score + score_change)


def transaction_score_change(
    amount_krw: int,
    *,
    problematic: bool,
    problem_count: int = 0,
    price_policy: PricePointPolicy = DEFAULT_PRICE_POINT_POLICY,
    penalty_policy: PenaltyPolicy = DEFAULT_PENALTY_POLICY,
) -> int:
    """Return the signed score change for a successful or problematic trade.

    ``problem_count`` is the total count after recording the current problem.
    It is ignored for successful transactions because penalties amplify losses
    only; they never reduce or amplify positive awards.
    """

    if not isinstance(problematic, bool):
        raise TypeError("problematic must be a boolean")
    base_points = price_to_base_points(amount_krw, policy=price_policy)
    if not problematic:
        return base_points
    return apply_problem_penalty(-base_points, problem_count, policy=penalty_policy)


def calculate_transaction_adjustment(
    score: int,
    amount_krw: int,
    *,
    problematic: bool,
    problem_count: int = 0,
    price_policy: PricePointPolicy = DEFAULT_PRICE_POINT_POLICY,
    penalty_policy: PenaltyPolicy = DEFAULT_PENALTY_POLICY,
) -> TransactionCreditAdjustment:
    """Calculate an auditable transaction adjustment without mutating state."""

    score = _require_int(score, "score")
    normalized_score = clamp_credit_score(score)
    base_points = price_to_base_points(amount_krw, policy=price_policy)
    change = transaction_score_change(
        amount_krw,
        problematic=problematic,
        problem_count=problem_count,
        price_policy=price_policy,
        penalty_policy=penalty_policy,
    )
    level = penalty_level(problem_count, policy=penalty_policy) if problematic else 0
    return TransactionCreditAdjustment(
        amount_krw=amount_krw,
        problematic=problematic,
        problem_count=problem_count,
        base_points=base_points,
        penalty_level=level,
        score_change=change,
        score_before=normalized_score,
        score_after=apply_credit_change(normalized_score, change),
    )


def review_counts_as_problem(
    rating: int,
    *,
    admin_override: bool | None = None,
) -> bool:
    """Return whether a review adds one problem to the seller's history.

    Ratings of one or two count by default.  ``admin_override`` deliberately
    exposes the moderation escape hatch needed to undo rating abuse: ``False``
    suppresses a low-rating problem and ``True`` marks any rating as a problem.
    """

    rating = _require_int(rating, "rating")
    if not MIN_REVIEW_RATING <= rating <= MAX_REVIEW_RATING:
        raise ValueError(f"rating must be between {MIN_REVIEW_RATING} and {MAX_REVIEW_RATING}")
    if admin_override is not None and not isinstance(admin_override, bool):
        raise TypeError("admin_override must be a boolean or None")
    if admin_override is not None:
        return admin_override
    return rating <= LOW_REVIEW_PROBLEM_MAX_RATING


def review_deadline(completed_at: datetime) -> datetime:
    """Return the inclusive deadline for the buyer to submit a review."""

    return _require_aware(completed_at, "completed_at") + REVIEW_WINDOW


def settlement_available_at(completed_at: datetime) -> datetime:
    """Return when a clean transaction can receive its positive score award."""

    return _require_aware(completed_at, "completed_at") + SETTLEMENT_COOLING_PERIOD


def is_review_window_open(completed_at: datetime, *, now: datetime | None = None) -> bool:
    """Whether ``now`` is between completion and the inclusive 7-day deadline."""

    completed_at = _require_aware(completed_at, "completed_at")
    current = datetime.now(timezone.utc) if now is None else _require_aware(now, "now")
    return completed_at <= current <= review_deadline(completed_at)


def is_settlement_ready(completed_at: datetime, *, now: datetime | None = None) -> bool:
    """Whether the 7-day issue/refund cooling period has elapsed."""

    completed_at = _require_aware(completed_at, "completed_at")
    current = datetime.now(timezone.utc) if now is None else _require_aware(now, "now")
    return current >= settlement_available_at(completed_at)


@dataclass(frozen=True, slots=True)
class PostingIntervalPolicy:
    """Credit-score tiers controlling how often a listing may be reposted."""

    high_score_minimum: int = 80
    medium_score_minimum: int = 50
    nonnegative_minimum: int = 0
    high_interval: timedelta = timedelta(minutes=10)
    medium_interval: timedelta = timedelta(minutes=20)
    nonnegative_interval: timedelta = timedelta(minutes=30)
    negative_interval: timedelta = timedelta(minutes=60)

    def __post_init__(self) -> None:
        high = _require_int(self.high_score_minimum, "high_score_minimum")
        medium = _require_int(self.medium_score_minimum, "medium_score_minimum")
        nonnegative = _require_int(self.nonnegative_minimum, "nonnegative_minimum")
        if not high > medium > nonnegative:
            raise ValueError("posting score thresholds must be strictly descending")
        for name in (
            "high_interval",
            "medium_interval",
            "nonnegative_interval",
            "negative_interval",
        ):
            value = getattr(self, name)
            if not isinstance(value, timedelta):
                raise TypeError(f"{name} must be a timedelta")
            if value <= timedelta(0):
                raise ValueError(f"{name} must be greater than zero")

    def interval_for(self, score: int) -> timedelta:
        score = _require_int(score, "score")
        score = clamp_credit_score(score)
        if score >= self.high_score_minimum:
            return self.high_interval
        if score >= self.medium_score_minimum:
            return self.medium_interval
        if score >= self.nonnegative_minimum:
            return self.nonnegative_interval
        return self.negative_interval


DEFAULT_POSTING_INTERVAL_POLICY = PostingIntervalPolicy()


def posting_interval_for_score(
    score: int,
    *,
    policy: PostingIntervalPolicy = DEFAULT_POSTING_INTERVAL_POLICY,
) -> timedelta:
    """Return the listing repost interval for a credit score."""

    if not isinstance(policy, PostingIntervalPolicy):
        raise TypeError("policy must be a PostingIntervalPolicy")
    return policy.interval_for(score)


# Small compatibility helpers used by persistence/services that store primitive
# integer settings.  The canonical APIs above remain preferable when a caller
# already has a policy object.
def clamp_score(score: int) -> int:
    """Compatibility name for :func:`clamp_credit_score`."""

    return clamp_credit_score(score)


def price_points(
    amount_krw: int,
    unit_price: int = DEFAULT_PRICE_UNIT_KRW,
    *,
    minimum_points: int = MIN_TRANSACTION_POINTS,
    maximum_points: int = MAX_TRANSACTION_POINTS,
) -> int:
    """Return price points using primitive, settings-friendly arguments."""

    return price_to_base_points(
        amount_krw,
        policy=PricePointPolicy(
            unit_price_krw=unit_price,
            minimum_points=minimum_points,
            maximum_points=maximum_points,
        ),
    )


def penalized_decrease(
    base_points: int,
    problem_count: int,
    *,
    unpenalized_problem_count: int = PENALTY_FREE_PROBLEM_COUNT,
    percent_per_level: int = PENALTY_PERCENT_PER_LEVEL,
) -> int:
    """Return a positive score-deduction magnitude after escalation.

    For example, ``penalized_decrease(3, 4) == 4``.  Use
    :func:`apply_problem_penalty` when working with signed score changes.
    """

    base_points = _require_int(base_points, "base_points")
    if base_points < 0:
        raise ValueError("base_points cannot be negative")
    policy = PenaltyPolicy(
        unpenalized_problem_count=unpenalized_problem_count,
        percent_per_level=percent_per_level,
    )
    return abs(policy.apply(-base_points, problem_count))


def posting_interval_minutes(
    score: int,
    *,
    policy: PostingIntervalPolicy = DEFAULT_POSTING_INTERVAL_POLICY,
) -> int:
    """Return the score tier's posting interval as whole minutes."""

    interval = posting_interval_for_score(score, policy=policy)
    seconds = interval.total_seconds()
    if seconds % 60:
        raise ValueError("posting interval must contain a whole number of minutes")
    return int(seconds // 60)


@dataclass(frozen=True, slots=True)
class LikeCreditPolicy:
    """Milestone and per-listing cap for unique-like score awards."""

    likes_per_credit: int = DEFAULT_LIKES_PER_CREDIT
    max_credit_per_listing: int = DEFAULT_MAX_LIKE_CREDIT_PER_LISTING

    def __post_init__(self) -> None:
        _require_int(self.likes_per_credit, "likes_per_credit")
        _require_int(self.max_credit_per_listing, "max_credit_per_listing")
        if self.likes_per_credit <= 0:
            raise ValueError("likes_per_credit must be greater than zero")
        if self.max_credit_per_listing < 0:
            raise ValueError("max_credit_per_listing cannot be negative")

    def total_credit_for_count(self, unique_like_count: int) -> int:
        unique_like_count = _require_int(unique_like_count, "unique_like_count")
        if unique_like_count < 0:
            raise ValueError("unique_like_count cannot be negative")
        earned = unique_like_count // self.likes_per_credit
        return min(self.max_credit_per_listing, earned)


DEFAULT_LIKE_CREDIT_POLICY = LikeCreditPolicy()


@dataclass(frozen=True, slots=True)
class LikeCreditGrant:
    """Idempotency-friendly result for awarding listing-like milestones."""

    unique_like_count: int
    total_earned_credit: int
    previously_granted_credit: int
    new_credit: int


def like_credit_for_count(
    unique_like_count: int,
    *,
    policy: LikeCreditPolicy = DEFAULT_LIKE_CREDIT_POLICY,
) -> int:
    """Return cumulative credit earned for a unique-like count."""

    if not isinstance(policy, LikeCreditPolicy):
        raise TypeError("policy must be a LikeCreditPolicy")
    return policy.total_credit_for_count(unique_like_count)


def unique_like_count(liker_ids: Iterable[Hashable]) -> int:
    """Count distinct likers so repeated clicks cannot advance milestones."""

    try:
        return len(set(liker_ids))
    except TypeError as exc:
        raise TypeError("liker_ids must be an iterable of hashable IDs") from exc


def calculate_like_credit_grant(
    liker_ids: Iterable[Hashable],
    *,
    previously_granted_credit: int = 0,
    policy: LikeCreditPolicy = DEFAULT_LIKE_CREDIT_POLICY,
) -> LikeCreditGrant:
    """Calculate only the not-yet-awarded credit for a listing.

    Persisting ``total_earned_credit`` (or incrementing the stored grant count
    atomically by ``new_credit``) makes retries safe and enforces the cap.
    """

    previously_granted_credit = _require_int(
        previously_granted_credit,
        "previously_granted_credit",
    )
    if previously_granted_credit < 0:
        raise ValueError("previously_granted_credit cannot be negative")
    if not isinstance(policy, LikeCreditPolicy):
        raise TypeError("policy must be a LikeCreditPolicy")

    count = unique_like_count(liker_ids)
    total_earned = policy.total_credit_for_count(count)
    return LikeCreditGrant(
        unique_like_count=count,
        total_earned_credit=total_earned,
        previously_granted_credit=previously_granted_credit,
        new_credit=max(0, total_earned - previously_granted_credit),
    )


__all__ = [
    "DEFAULT_LIKES_PER_CREDIT",
    "DEFAULT_LIKE_CREDIT_POLICY",
    "DEFAULT_MAX_LIKE_CREDIT_PER_LISTING",
    "DEFAULT_PENALTY_POLICY",
    "DEFAULT_POSTING_INTERVAL_POLICY",
    "DEFAULT_PRICE_POINT_POLICY",
    "DEFAULT_PRICE_UNIT_KRW",
    "LikeCreditGrant",
    "LikeCreditPolicy",
    "MAX_CREDIT_SCORE",
    "MAX_TRANSACTION_POINTS",
    "MIN_CREDIT_SCORE",
    "MIN_TRANSACTION_POINTS",
    "PenaltyPolicy",
    "PostingIntervalPolicy",
    "PricePointPolicy",
    "REVIEW_WINDOW",
    "SETTLEMENT_COOLING_PERIOD",
    "TransactionCreditAdjustment",
    "apply_credit_change",
    "apply_problem_penalty",
    "calculate_like_credit_grant",
    "calculate_transaction_adjustment",
    "clamp_credit_score",
    "clamp_score",
    "is_review_window_open",
    "is_settlement_ready",
    "like_credit_for_count",
    "penalty_level",
    "penalized_decrease",
    "posting_interval_for_score",
    "posting_interval_minutes",
    "price_points",
    "price_to_base_points",
    "review_counts_as_problem",
    "review_deadline",
    "settlement_available_at",
    "transaction_score_change",
    "unique_like_count",
]
