from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from services.brokerage import (
    MAX_CREDIT_SCORE,
    MIN_CREDIT_SCORE,
    REVIEW_WINDOW,
    SETTLEMENT_COOLING_PERIOD,
    LikeCreditPolicy,
    PenaltyPolicy,
    PostingIntervalPolicy,
    PricePointPolicy,
    apply_credit_change,
    apply_problem_penalty,
    calculate_like_credit_grant,
    calculate_transaction_adjustment,
    clamp_credit_score,
    clamp_score,
    is_review_window_open,
    is_settlement_ready,
    like_credit_for_count,
    penalty_level,
    penalized_decrease,
    posting_interval_for_score,
    posting_interval_minutes,
    price_points,
    price_to_base_points,
    review_counts_as_problem,
    review_deadline,
    settlement_available_at,
    transaction_score_change,
    unique_like_count,
)


class PriceAndCreditRuleTests(unittest.TestCase):
    def test_default_price_mapping_uses_ceiling_and_clamps_to_one_through_ten(self) -> None:
        cases = {
            1: 1,
            999: 1,
            1_000: 1,
            1_001: 2,
            3_000: 3,
            9_001: 10,
            10_000: 10,
            500_000: 10,
        }
        for amount, expected in cases.items():
            with self.subTest(amount=amount):
                self.assertEqual(price_to_base_points(amount), expected)

    def test_price_unit_and_point_bounds_are_configurable(self) -> None:
        policy = PricePointPolicy(unit_price_krw=2_500, minimum_points=2, maximum_points=4)

        self.assertEqual(price_to_base_points(1, policy=policy), 2)
        self.assertEqual(price_to_base_points(5_001, policy=policy), 3)
        self.assertEqual(price_to_base_points(50_000, policy=policy), 4)

    def test_nonpositive_amount_and_invalid_policy_are_rejected(self) -> None:
        for amount in (0, -1):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                price_to_base_points(amount)
        with self.assertRaises(TypeError):
            price_to_base_points(True)
        with self.assertRaises(ValueError):
            PricePointPolicy(unit_price_krw=0)
        with self.assertRaises(ValueError):
            PricePointPolicy(minimum_points=5, maximum_points=4)

    def test_credit_score_is_clamped_at_both_boundaries(self) -> None:
        self.assertEqual(clamp_credit_score(-999), MIN_CREDIT_SCORE)
        self.assertEqual(clamp_credit_score(999), MAX_CREDIT_SCORE)
        self.assertEqual(apply_credit_change(98, 10), MAX_CREDIT_SCORE)
        self.assertEqual(apply_credit_change(-98, -10), MIN_CREDIT_SCORE)
        self.assertEqual(apply_credit_change(10, -3), 7)
        self.assertEqual(clamp_score(999), MAX_CREDIT_SCORE)

    def test_primitive_price_compatibility_helper_accepts_a_custom_unit(self) -> None:
        self.assertEqual(price_points(3_001, 2_000), 2)


class ProblemPenaltyRuleTests(unittest.TestCase):
    def test_penalty_starts_with_fourth_problem_and_grows_one_level_each_time(self) -> None:
        self.assertEqual([penalty_level(count) for count in range(7)], [0, 0, 0, 0, 1, 2, 3])

    def test_negative_changes_are_rounded_away_from_zero_deterministically(self) -> None:
        self.assertEqual(apply_problem_penalty(-3, 3), -3)
        self.assertEqual(apply_problem_penalty(-3, 4), -4)
        self.assertEqual(apply_problem_penalty(-3, 5), -4)
        self.assertEqual(apply_problem_penalty(-10, 4), -11)
        self.assertEqual(apply_problem_penalty(-10, 5), -12)

    def test_penalty_never_changes_positive_or_zero_awards(self) -> None:
        self.assertEqual(apply_problem_penalty(3, 100), 3)
        self.assertEqual(apply_problem_penalty(0, 100), 0)

    def test_penalty_policy_can_be_changed_without_float_rounding(self) -> None:
        policy = PenaltyPolicy(unpenalized_problem_count=1, percent_per_level=25)

        self.assertEqual(policy.level_for(2), 1)
        self.assertEqual(policy.apply(-3, 2), -4)
        self.assertEqual(policy.apply(-3, 3), -5)

    def test_transaction_change_combines_price_direction_and_penalty(self) -> None:
        self.assertEqual(transaction_score_change(3_000, problematic=False, problem_count=99), 3)
        self.assertEqual(transaction_score_change(3_000, problematic=True, problem_count=4), -4)

        result = calculate_transaction_adjustment(
            99,
            3_000,
            problematic=False,
            problem_count=20,
        )
        self.assertEqual(result.base_points, 3)
        self.assertEqual(result.penalty_level, 0)
        self.assertEqual(result.score_change, 3)
        self.assertEqual(result.score_before, 99)
        self.assertEqual(result.score_after, 100)

        problem = calculate_transaction_adjustment(
            -99,
            3_000,
            problematic=True,
            problem_count=4,
        )
        self.assertEqual(problem.penalty_level, 1)
        self.assertEqual(problem.score_change, -4)
        self.assertEqual(problem.score_after, -100)

        normalized = calculate_transaction_adjustment(
            150,
            3_000,
            problematic=True,
            problem_count=3,
        )
        self.assertEqual(normalized.score_before, 100)
        self.assertEqual(normalized.score_after, 97)

    def test_positive_deduction_compatibility_helper_has_clear_sign_semantics(self) -> None:
        self.assertEqual(penalized_decrease(3, 4), 4)
        self.assertEqual(penalized_decrease(10, 5), 12)
        with self.assertRaises(ValueError):
            penalized_decrease(-3, 4)

    def test_invalid_problem_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            penalty_level(-1)
        with self.assertRaises(TypeError):
            transaction_score_change(1_000, problematic=1)  # type: ignore[arg-type]


class ReviewAndCoolingPeriodTests(unittest.TestCase):
    def setUp(self) -> None:
        self.completed_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def test_review_ratings_of_two_or_less_count_as_one_problem(self) -> None:
        self.assertTrue(review_counts_as_problem(1))
        self.assertTrue(review_counts_as_problem(2))
        self.assertFalse(review_counts_as_problem(3))
        self.assertFalse(review_counts_as_problem(5))

    def test_admin_can_override_abusive_or_misclassified_review(self) -> None:
        self.assertFalse(review_counts_as_problem(1, admin_override=False))
        self.assertTrue(review_counts_as_problem(5, admin_override=True))

    def test_rating_must_be_on_five_point_scale(self) -> None:
        for rating in (0, 6):
            with self.subTest(rating=rating), self.assertRaises(ValueError):
                review_counts_as_problem(rating)

    def test_review_and_settlement_periods_are_exactly_seven_days(self) -> None:
        self.assertEqual(REVIEW_WINDOW, timedelta(days=7))
        self.assertEqual(SETTLEMENT_COOLING_PERIOD, timedelta(days=7))
        self.assertEqual(review_deadline(self.completed_at), self.completed_at + timedelta(days=7))
        self.assertEqual(
            settlement_available_at(self.completed_at),
            self.completed_at + timedelta(days=7),
        )

    def test_review_window_is_inclusive_but_not_open_before_completion(self) -> None:
        deadline = self.completed_at + timedelta(days=7)

        self.assertFalse(
            is_review_window_open(self.completed_at, now=self.completed_at - timedelta(microseconds=1))
        )
        self.assertTrue(is_review_window_open(self.completed_at, now=self.completed_at))
        self.assertTrue(is_review_window_open(self.completed_at, now=deadline))
        self.assertFalse(
            is_review_window_open(self.completed_at, now=deadline + timedelta(microseconds=1))
        )

    def test_settlement_becomes_ready_at_the_exact_deadline(self) -> None:
        deadline = self.completed_at + timedelta(days=7)

        self.assertFalse(
            is_settlement_ready(self.completed_at, now=deadline - timedelta(microseconds=1))
        )
        self.assertTrue(is_settlement_ready(self.completed_at, now=deadline))

    def test_timezone_aware_values_in_different_offsets_compare_correctly(self) -> None:
        singapore = timezone(timedelta(hours=8))
        completion = self.completed_at.astimezone(singapore)
        exact_deadline_utc = self.completed_at + timedelta(days=7)

        self.assertTrue(is_review_window_open(completion, now=exact_deadline_utc))
        self.assertTrue(is_settlement_ready(completion, now=exact_deadline_utc))

    def test_naive_datetimes_are_rejected(self) -> None:
        naive = datetime(2026, 8, 1, 12, 0)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            review_deadline(naive)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            is_review_window_open(self.completed_at, now=naive)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            is_settlement_ready(naive, now=self.completed_at)


class PostingIntervalRuleTests(unittest.TestCase):
    def test_default_score_tiers_include_every_boundary(self) -> None:
        cases = {
            100: 10,
            80: 10,
            79: 20,
            50: 20,
            49: 30,
            0: 30,
            -1: 60,
            -100: 60,
        }
        for score, minutes in cases.items():
            with self.subTest(score=score):
                self.assertEqual(posting_interval_for_score(score), timedelta(minutes=minutes))
                self.assertEqual(posting_interval_minutes(score), minutes)

    def test_posting_policy_is_configurable(self) -> None:
        policy = PostingIntervalPolicy(
            high_score_minimum=90,
            medium_score_minimum=60,
            nonnegative_minimum=10,
            high_interval=timedelta(minutes=5),
            medium_interval=timedelta(minutes=15),
            nonnegative_interval=timedelta(minutes=25),
            negative_interval=timedelta(minutes=45),
        )

        self.assertEqual(posting_interval_for_score(90, policy=policy), timedelta(minutes=5))
        self.assertEqual(posting_interval_for_score(60, policy=policy), timedelta(minutes=15))
        self.assertEqual(posting_interval_for_score(10, policy=policy), timedelta(minutes=25))
        self.assertEqual(posting_interval_for_score(9, policy=policy), timedelta(minutes=45))

    def test_posting_policy_rejects_ambiguous_thresholds_and_nonpositive_intervals(self) -> None:
        with self.assertRaises(ValueError):
            PostingIntervalPolicy(high_score_minimum=50, medium_score_minimum=50)
        with self.assertRaises(ValueError):
            PostingIntervalPolicy(high_interval=timedelta(0))


class LikeCreditRuleTests(unittest.TestCase):
    def test_only_unique_likes_count_toward_default_ten_like_milestones(self) -> None:
        liker_ids = [1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

        self.assertEqual(unique_like_count(liker_ids), 10)
        grant = calculate_like_credit_grant(liker_ids)
        self.assertEqual(grant.unique_like_count, 10)
        self.assertEqual(grant.total_earned_credit, 1)
        self.assertEqual(grant.new_credit, 1)

    def test_default_policy_awards_each_ten_likes_and_caps_each_listing(self) -> None:
        self.assertEqual(like_credit_for_count(9), 0)
        self.assertEqual(like_credit_for_count(10), 1)
        self.assertEqual(like_credit_for_count(29), 2)
        self.assertEqual(like_credit_for_count(30), 3)
        self.assertEqual(like_credit_for_count(10_000), 3)

    def test_configurable_milestone_cap_and_prior_grants_make_award_idempotent(self) -> None:
        policy = LikeCreditPolicy(likes_per_credit=3, max_credit_per_listing=2)
        likers = [1, 2, 3, 4, 5, 6, 7, 7]

        first = calculate_like_credit_grant(likers, policy=policy)
        retry = calculate_like_credit_grant(
            likers,
            previously_granted_credit=first.total_earned_credit,
            policy=policy,
        )
        self.assertEqual(first.total_earned_credit, 2)
        self.assertEqual(first.new_credit, 2)
        self.assertEqual(retry.new_credit, 0)

    def test_partial_prior_grant_only_returns_the_difference(self) -> None:
        policy = LikeCreditPolicy(likes_per_credit=2, max_credit_per_listing=4)
        result = calculate_like_credit_grant(
            range(7),
            previously_granted_credit=1,
            policy=policy,
        )

        self.assertEqual(result.total_earned_credit, 3)
        self.assertEqual(result.new_credit, 2)

    def test_like_policy_rejects_invalid_configuration_and_counts(self) -> None:
        with self.assertRaises(ValueError):
            LikeCreditPolicy(likes_per_credit=0)
        with self.assertRaises(ValueError):
            LikeCreditPolicy(max_credit_per_listing=-1)
        with self.assertRaises(ValueError):
            like_credit_for_count(-1)
        with self.assertRaises(ValueError):
            calculate_like_credit_grant([], previously_granted_credit=-1)


if __name__ == "__main__":
    unittest.main()
