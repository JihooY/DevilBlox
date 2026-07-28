from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.cogs_middleman import (
    MiddlemanDraftCompleteView,
    MiddlemanDraftView,
    MiddlemanInfoView,
    MiddlemanPanelView,
    _panel_edit_kwargs as middleman_panel_edit_kwargs,
)
from cogs.cogs_purchase import (
    PurchasePanelView,
    PurchaseSellerRatingView,
    _panel_edit_kwargs as purchase_panel_edit_kwargs,
)
from utils.embeds import BRAND_LOGO_FILENAME


def member(user_id: int, name: str = "user"):
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


class MiddlemanComponentLayoutTests(unittest.TestCase):
    def test_persistent_panel_keeps_custom_ids_inside_container(self) -> None:
        view = MiddlemanPanelView(SimpleNamespace())

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertTrue(view.is_persistent())
        self.assertEqual(
            dispatchable_ids(view),
            {"devilblox:mm:start", "devilblox:mm:info"},
        )
        self.assertIn("MIDDLEMAN SERVICE", layout_text(view))

    def test_info_selection_is_a_branded_layout(self) -> None:
        middlemen = [{"user_id": 10, "user_name": "중개자"}]
        view = MiddlemanInfoView(SimpleNamespace(), middlemen)

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(dispatchable_ids(view), {"devilblox:mm:info_select"})
        self.assertIn("MIDDLEMAN INFO", layout_text(view))
        self.assertTrue(any(isinstance(item, discord.ui.Thumbnail) for item in view.walk_children()))

    def test_draft_updates_content_and_enables_open_button(self) -> None:
        view = MiddlemanDraftView(SimpleNamespace(), member(1, "신청자"))
        open_button = next(
            item
            for item in view.walk_children()
            if isinstance(item, discord.ui.Button) and item.label == "중개 티켓 열기"
        )

        self.assertTrue(open_button.disabled)
        view.set_participants(member(2, "상대방"), member(3, "중개자"))

        self.assertFalse(open_button.disabled)
        self.assertEqual((view.counterparty_id, view.middleman_id), (2, 3))
        self.assertIn("<@2>", layout_text(view))
        self.assertIn("<@3>", layout_text(view))

    def test_completed_draft_is_read_only_components_v2(self) -> None:
        view = MiddlemanDraftCompleteView("<#987>")

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(dispatchable_ids(view), set())
        self.assertIn("<#987>", layout_text(view))


class PurchaseComponentLayoutTests(unittest.TestCase):
    def test_persistent_panel_keeps_select_and_rating_ids(self) -> None:
        sellers = [
            {
                "user_id": 20,
                "user_name": "셀러",
                "ticket_disabled": False,
                "accrued_sell_money": 50_000,
                "accrued_sell_count": 4,
            }
        ]
        view = PurchasePanelView(SimpleNamespace(), sellers)

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertTrue(view.is_persistent())
        self.assertEqual(
            dispatchable_ids(view),
            {"devilblox:purchase:select", "devilblox:purchase:rating"},
        )
        self.assertIn("등록 셀러 `1`명", layout_text(view))

    def test_rating_selection_is_a_branded_layout(self) -> None:
        sellers = [{"user_id": 20, "user_name": "셀러"}]
        view = PurchaseSellerRatingView(SimpleNamespace(), sellers)

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(dispatchable_ids(view), {"devilblox:purchase:rating_select"})
        self.assertIn("셀러 평점", layout_text(view))
        self.assertTrue(any(isinstance(item, discord.ui.Thumbnail) for item in view.walk_children()))


class InteractiveComponentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_middleman_start_sends_layout_without_embed(self) -> None:
        original = SimpleNamespace()
        interaction = SimpleNamespace(
            user=member(1, "신청자"),
            response=SimpleNamespace(send_message=AsyncMock()),
            original_response=AsyncMock(return_value=original),
        )
        view = MiddlemanPanelView(SimpleNamespace())

        with patch("cogs.cogs_middleman.branded_files", return_value=[]):
            await view.start(interaction)

        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertNotIn("embed", kwargs)
        self.assertIsInstance(kwargs["view"], MiddlemanDraftView)
        self.assertTrue(kwargs["ephemeral"])
        self.assertIs(kwargs["view"].message, original)

    async def test_purchase_rating_button_sends_layout_without_embed(self) -> None:
        sellers = [{"user_id": 20, "user_name": "셀러"}]
        cog = SimpleNamespace(
            repos=SimpleNamespace(
                sellers=SimpleNamespace(list_active_options=AsyncMock(return_value=sellers))
            )
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=7),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        view = PurchasePanelView(cog, sellers)

        with patch("cogs.cogs_purchase.branded_files", return_value=[]):
            await view.seller_rating(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertNotIn("embed", kwargs)
        self.assertIsInstance(kwargs["view"], PurchaseSellerRatingView)
        self.assertTrue(kwargs["ephemeral"])

    async def test_panel_edit_payloads_clear_legacy_embeds_and_keep_logo(self) -> None:
        logo = SimpleNamespace(filename=BRAND_LOGO_FILENAME)
        stale_gif = SimpleNamespace(filename="legacy-panel.gif")
        message = SimpleNamespace(attachments=[logo, stale_gif])

        for kwargs in (
            middleman_panel_edit_kwargs(message, MiddlemanPanelView(SimpleNamespace()), None),
            purchase_panel_edit_kwargs(message, PurchasePanelView(SimpleNamespace(), []), None),
        ):
            self.assertIsNone(kwargs["content"])
            self.assertEqual(kwargs["embeds"], [])
            self.assertIsInstance(kwargs["view"], discord.ui.LayoutView)
            self.assertEqual(kwargs["attachments"], [logo])
            self.assertEqual(
                kwargs["allowed_mentions"].to_dict(),
                discord.AllowedMentions.none().to_dict(),
            )


if __name__ == "__main__":
    unittest.main()
