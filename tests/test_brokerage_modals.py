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


class BrokerageModalParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_modal_parses_grouped_numbers_and_delegates(self) -> None:
        cog = SimpleNamespace(create_listing=AsyncMock())
        modal = BrokerageListingModal(cog)
        set_input_value(modal.title_input, "  상품명  ")
        set_input_value(modal.description_input, "상품 설명")
        set_input_value(modal.price_input, "1,234,567")
        set_input_value(modal.quantity_input, "2,000")
        set_input_value(modal.notes_input, "티켓 안내")
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

        await modal.on_submit(interaction)

        cog.create_listing.assert_awaited_once_with(
            interaction,
            title="  상품명  ",
            description="상품 설명",
            price=1_234_567,
            quantity=2_000,
            notes="티켓 안내",
        )
        interaction.response.send_message.assert_not_awaited()

    async def test_listing_modal_rejects_non_numeric_price_before_delegation(self) -> None:
        cog = SimpleNamespace(create_listing=AsyncMock())
        modal = BrokerageListingModal(cog)
        set_input_value(modal.price_input, "삼천원")
        set_input_value(modal.quantity_input, "1")
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

        await modal.on_submit(interaction)

        cog.create_listing.assert_not_awaited()
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertEqual(kwargs["embed"].title, "입력 오류")
        self.assertTrue(kwargs["ephemeral"])

    async def test_notification_modal_parses_korean_boolean_values(self) -> None:
        for raw_value, expected in (("예", True), ("아니요", False)):
            with self.subTest(raw_value=raw_value):
                cog = SimpleNamespace(update_notification_preferences=AsyncMock())
                modal = BrokerageNotificationModal(cog, {})
                set_input_value(modal.minimum_score, " -50 ")
                set_input_value(modal.no_duplicates, raw_value)
                interaction = SimpleNamespace(
                    response=SimpleNamespace(send_message=AsyncMock())
                )

                await modal.on_submit(interaction)

                cog.update_notification_preferences.assert_awaited_once_with(
                    interaction,
                    minimum_score=-50,
                    no_duplicates=expected,
                )

    async def test_admin_adjustment_modal_parses_signed_deltas(self) -> None:
        cog = SimpleNamespace(admin_adjust_profile=AsyncMock())
        modal = BrokerageAdminAdjustmentModal(cog)
        set_input_value(modal.user_id, " 123456789 ")
        set_input_value(modal.score_delta, "-20")
        set_input_value(modal.problem_delta, "+2")
        set_input_value(modal.reason, "별점 테러 정정")
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

        await modal.on_submit(interaction)

        cog.admin_adjust_profile.assert_awaited_once_with(
            interaction,
            user_id=123_456_789,
            score_delta=-20,
            problem_delta=2,
            reason="별점 테러 정정",
        )

    async def test_config_modal_parses_all_operating_values(self) -> None:
        cog = SimpleNamespace(update_config=AsyncMock())
        modal = BrokerageConfigModal(cog, {})
        set_input_value(modal.price_unit, "2,500")
        set_input_value(modal.likes_per_credit, "12")
        set_input_value(modal.minimum_listing_score, "-25")
        set_input_value(modal.intervals, "5, 15, 30, 60")
        set_input_value(modal.like_bump_reduction, "3")
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

        await modal.on_submit(interaction)

        cog.update_config.assert_awaited_once_with(
            interaction,
            {
                "price_point_unit": 2_500,
                "like_score_every": 12,
                "minimum_listing_score": -25,
                "bump_intervals": [
                    {"min_score": 80, "minutes": 5},
                    {"min_score": 50, "minutes": 15},
                    {"min_score": 0, "minutes": 30},
                    {"min_score": -100, "minutes": 60},
                ],
                "like_bump_reduction_minutes": 3,
            },
        )

    async def test_review_modal_parses_rating_and_delegates(self) -> None:
        cog = SimpleNamespace(submit_review=AsyncMock())
        modal = BrokerageReviewModal(cog, "review-24")
        set_input_value(modal.rating, " 2 ")
        set_input_value(modal.content, "거래 후기")
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

        await modal.on_submit(interaction)

        cog.submit_review.assert_awaited_once_with(
            interaction,
            "review-24",
            rating=2,
            content="거래 후기",
        )


if __name__ == "__main__":
    unittest.main()
