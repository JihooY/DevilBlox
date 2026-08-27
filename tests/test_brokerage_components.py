from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.cogs_brokerage import (
    BrokerageAdminAdjustmentModal,
    BrokerageAdminPanelView,
    BrokerageConfigModal,
    BrokerageDeleteModal,
    BrokerageListingModal,
    BrokerageListingView,
    BrokerageNotificationModal,
    BrokeragePanelView,
    BrokerageReportModal,
    BrokerageReviewModal,
    BrokerageReviewRequestView,
    BrokerageTicketView,
    BrokerageVerificationView,
    BrokerageVerificationCodeView,
    BrokerageCog,
    _listing_interval,
    _listing_markdown,
    _profile_markdown,
)


def member(user_id: int = 123, name: str = "tester") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        mention=f"<@{user_id}>",
        display_name=name,
    )

def layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )

def dispatchable_ids(view: discord.ui.LayoutView) -> set[str]:
    return {
        item.custom_id
        for item in view.walk_children()
        if item.is_dispatchable()
    }

def button_with_id(view: discord.ui.LayoutView, custom_id: str) -> discord.ui.Button:
    return next(
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button) and item.custom_id == custom_id
    )

def set_input_value(text_input: discord.ui.TextInput, value: str) -> None:
    # discord.py populates this private state from the modal submission payload.
    text_input._value = value

def close_uploaded_files(kwargs: dict) -> None:
    for uploaded in kwargs.get("files", []):
        uploaded.close()


class BrokerageComponentLayoutTests(unittest.TestCase):
    def assert_components_v2_container(self, view: discord.ui.LayoutView) -> None:
        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(len(view.children), 1)
        self.assertIsInstance(view.children[0], discord.ui.Container)
        self.assertTrue(
            any(isinstance(item, discord.ui.Thumbnail) for item in view.walk_children())
        )

    def test_integrated_panel_is_a_persistent_components_v2_container(self) -> None:
        view = BrokeragePanelView(SimpleNamespace())

        self.assert_components_v2_container(view)
        self.assertTrue(view.is_persistent())
        self.assertEqual(
            dispatchable_ids(view),
            {
                "devilblox:broker:profile",
                "devilblox:broker:register",
                "devilblox:broker:notifications",
                "devilblox:broker:verify",
            },
        )
        self.assertIn("거래중개 통합 패널", layout_text(view))

    def test_admin_panel_is_a_persistent_components_v2_container(self) -> None:
        view = BrokerageAdminPanelView(SimpleNamespace())

        self.assert_components_v2_container(view)
        self.assertTrue(view.is_persistent())
        self.assertEqual(
            dispatchable_ids(view),
            {
                "devilblox:broker:admin:delete",
                "devilblox:broker:admin:adjust",
                "devilblox:broker:admin:configure",
                "devilblox:broker:admin:overview",
            },
        )
        self.assertIn("거래중개 관리 패널", layout_text(view))

    def test_open_listing_keeps_listing_scoped_persistent_actions(self) -> None:
        listing = {
            "_id": "listing-42",
            "seller_id": 10,
            "title": "테스트 상품",
            "description": "상품 설명",
            "price": 3_000,
            "quantity": 2,
            "status": "open",
        }
        view = BrokerageListingView(
            SimpleNamespace(),
            listing,
            {"trust_score": 80},
            {},
        )

        self.assert_components_v2_container(view)
        self.assertTrue(view.is_persistent())
        self.assertEqual(
            dispatchable_ids(view),
            {
                "devilblox:broker:buy:listing-42",
                "devilblox:broker:report:listing-42",
                "devilblox:broker:like:listing-42",
            },
        )
        self.assertIn("테스트 상품", layout_text(view))

    def test_closed_listing_is_read_only_components_v2(self) -> None:
        view = BrokerageListingView(
            SimpleNamespace(),
            {
                "_id": "listing-closed",
                "seller_id": 10,
                "title": "판매된 상품",
                "description": "상품 설명",
                "price": 1_000,
                "status": "completed",
            },
        )

        self.assert_components_v2_container(view)
        self.assertEqual(dispatchable_ids(view), set())
        self.assertIn("거래 확정", layout_text(view))

    def test_ticket_and_review_keep_object_scoped_persistent_ids(self) -> None:
        ticket = BrokerageTicketView(SimpleNamespace(), "listing-42", 987)
        review = BrokerageReviewRequestView(
            SimpleNamespace(),
            {
                "_id": "review-24",
                "listing_title": "후기 상품",
                "seller_id": 10,
                "expires_at": datetime(2026, 9, 3, tzinfo=timezone.utc),
            },
        )

        for view in (ticket, review):
            with self.subTest(view=type(view).__name__):
                self.assert_components_v2_container(view)
                self.assertTrue(view.is_persistent())
        self.assertEqual(
            dispatchable_ids(ticket),
            {
                "devilblox:broker:ticket:complete:listing-42:987",
                "devilblox:broker:ticket:next:listing-42:987",
                "devilblox:broker:ticket:report:listing-42:987",
            },
        )
        self.assertEqual(
            dispatchable_ids(review),
            {"devilblox:broker:review:review-24"},
        )
        self.assertIn("거래중개 티켓", layout_text(ticket))
        self.assertIn("후기 상품", layout_text(review))

    def test_mentions_are_rendered_inside_components_v2(self) -> None:
        listing = BrokerageListingView(
            SimpleNamespace(),
            {
                "_id": "listing-mentions",
                "seller_id": 10,
                "title": "알림 상품",
                "description": "설명",
                "price": 1_000,
                "status": "open",
            },
            notification_mentions="<@20> <@30>",
        )
        ticket = BrokerageTicketView(
            SimpleNamespace(),
            "listing-mentions",
            20,
            participant_mentions="<@10> <@20>",
        )

        self.assertIn("<@20> <@30>", layout_text(listing))
        self.assertIn("<@10> <@20>", layout_text(ticket))

class BrokerageMarkdownBoundaryTests(unittest.TestCase):
    def test_like_benefits_use_the_same_dynamic_bump_policy(self) -> None:
        self.assertEqual(_listing_interval({}, 80, 0).total_seconds() // 60, 10)
        self.assertEqual(_listing_interval({}, 50, 10).total_seconds() // 60, 18)
        self.assertEqual(_listing_interval({}, 80, 30).total_seconds() // 60, 5)
        self.assertEqual(
            _listing_interval(
                {
                    "like_fast_bump_threshold": 5,
                    "like_fast_bump_percent": 25,
                    "like_score_every": 10,
                },
                0,
                5,
            ).total_seconds()
            // 60,
            23,
        )

    def test_profile_displays_every_score_tier_and_posting_boundary(self) -> None:
        cases = (
            (100, "최우수", 10),
            (80, "최우수", 10),
            (79, "우수", 20),
            (50, "우수", 20),
            (49, "일반", 30),
            (0, "일반", 30),
            (-1, "주의", 60),
            (-50, "주의", 60),
            (-51, "거래 제한", 60),
            (-100, "거래 제한", 60),
        )

        for score, tier, minutes in cases:
            with self.subTest(score=score):
                rendered = _profile_markdown(
                    member(),
                    {"trust_score": score, "problem_count": 0},
                    {},
                )
                self.assertIn(f"`{score}/100` · {tier}", rendered)
                self.assertIn(f"**재등록 주기**  `{minutes}분`", rendered)

    def test_profile_displays_penalty_and_verification_boundaries(self) -> None:
        before_penalty = _profile_markdown(
            member(),
            {"trust_score": 0, "problem_count": 3},
            {},
        )
        first_penalty = _profile_markdown(
            member(),
            {
                "trust_score": 0,
                "problem_count": 4,
                "verified_kinds": ["email", "phone"],
                "successful_trade_count": 7,
                "like_score_total": 2,
            },
            {},
        )

        self.assertIn("`0단계` · 다음 감점 `+0%`", before_penalty)
        self.assertIn("`1단계` · 다음 감점 `+10%`", first_penalty)
        self.assertIn("이메일 인증**  완료 (+10)", first_penalty)
        self.assertIn("전화번호 인증**  완료 (+20)", first_penalty)
        self.assertIn("정상 거래**  `7회`", first_penalty)
        self.assertIn("받은 좋아요 보상**  `+2점`", first_penalty)

    def test_listing_displays_price_point_boundaries(self) -> None:
        cases = (
            (1, 1),
            (1_000, 1),
            (1_001, 2),
            (9_000, 9),
            (9_001, 10),
            (1_000_000, 10),
        )
        base = {
            "_id": "listing-boundary",
            "seller_id": 123,
            "title": "가격 경계 상품",
            "description": "상품 설명",
            "quantity": 1,
            "status": "open",
        }

        for price, points in cases:
            with self.subTest(price=price):
                rendered = _listing_markdown(
                    {**base, "price": price},
                    {"trust_score": 0, "problem_count": 0},
                    {},
                )
                self.assertIn(
                    f"**가격**  `{price:,}원` · 보호기간 후 `+{points}점` 대상",
                    rendered,
                )

    def test_listing_displays_state_counts_tier_and_text_limits(self) -> None:
        exact_title = "가" * 100
        long_description = "나" * 1_201
        rendered = _listing_markdown(
            {
                "_id": "listing-display",
                "seller_id": 321,
                "title": exact_title,
                "description": long_description,
                "price": 3_000,
                "quantity": 0,
                "status": "reserved",
                "buyer_queue": [{"user_id": 1}, {"user_id": 2}],
                "like_count": 12,
                "bump_interval_minutes": 5,
                "notes": "티켓 안내",
            },
            {"trust_score": -51, "problem_count": 5},
            {},
        )
        lines = rendered.splitlines()

        self.assertEqual(lines[0], f"## {exact_title}")
        self.assertEqual(len(lines[1]), 1_200)
        self.assertTrue(lines[1].endswith("…"))
        self.assertIn("**수량**  `1개`", rendered)
        self.assertIn("신용도 `-51` (거래 제한)", rendered)
        self.assertIn("`5회` · `2단계`", rendered)
        self.assertIn("`예약 거래중` · 예약 `2명` · 좋아요 `12개`", rendered)
        self.assertIn("**재등록 주기**  `5분`", rendered)


if __name__ == "__main__":
    unittest.main()
