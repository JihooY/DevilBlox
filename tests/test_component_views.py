from __future__ import annotations

import unittest
from datetime import datetime, timezone

import discord

from cogs.cogs_lottery import LotteryTicketRevealView
from cogs.cogs_reviews import ReviewRequestView


class ComponentViewMigrationTests(unittest.TestCase):
    def test_lottery_ticket_is_one_interactive_container(self) -> None:
        event = {"_id": "lottery-1", "title": "여름 추첨", "guild_id": 1}
        entry = {"shapes": ["○", "△", "□"], "is_winner": False}

        view = LotteryTicketRevealView(object(), event, entry, user_id=7)

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(len(view.children), 1)
        container = view.children[0]
        self.assertIsInstance(container, discord.ui.Container)
        self.assertTrue(any(isinstance(item, discord.ui.MediaGallery) for item in container.children))
        self.assertTrue(any(isinstance(item, discord.ui.ActionRow) for item in container.children))

    def test_review_request_keeps_persistent_button_inside_container(self) -> None:
        review = {
            "product_title": "테스트 상품",
            "seller_id": 123,
            "category_id": "video",
            "category_name": "영상",
            "purchased_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
        }

        view = ReviewRequestView(object(), "review-1", review=review)

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertTrue(view.is_persistent())
        custom_ids = {
            item.custom_id
            for item in view.walk_children()
            if isinstance(item, discord.ui.Button)
        }
        self.assertEqual(custom_ids, {"devilblox:review:write:review-1"})
        text = "\n".join(
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertIn("테스트 상품", text)
        self.assertIn("후기 ID: review-1", text)


if __name__ == "__main__":
    unittest.main()
