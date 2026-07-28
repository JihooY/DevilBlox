from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from cogs.cogs_stock import (
    StockConditionView,
    StockControlView,
    stock_panel_edit_kwargs,
)
from utils.embeds import BRAND_LOGO_FILENAME


def stock_item(item_id: str, name: str, quantity: int) -> dict:
    return {
        "item_id": item_id,
        "item_id_lower": item_id.casefold(),
        "name": name,
        "quantity": quantity,
    }


class StockComponentLayoutTests(unittest.TestCase):
    def test_condition_layout_stays_within_components_v2_text_limit(self) -> None:
        items = [
            stock_item(f"product_{index:03d}_" + "x" * 48, "상품 " + "가" * 90, index)
            for index in range(100)
        ]

        view = StockConditionView(items, reset_at=1_753_000_000)
        text = "\n".join(
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertLessEqual(view.content_length(), 4_000)
        self.assertIn("STOCK CONDITION", text)
        self.assertIn("길이 제한으로 생략", text)
        self.assertIn("<t:1753000000:F>", text)

    def test_control_layout_keeps_persistent_component_ids_and_selection(self) -> None:
        cog = SimpleNamespace()
        items = [
            stock_item("GhostPepperSeed", "고스트 페퍼 씨앗", 7),
            stock_item("BlueSeed", "블루 씨앗", 3),
        ]

        view = StockControlView(cog, items, "BLUESEED")
        dispatchable = [item for item in view.walk_children() if item.is_dispatchable()]
        custom_ids = {item.custom_id for item in dispatchable}
        text = "\n".join(
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertIsNone(view.timeout)
        self.assertEqual(
            custom_ids,
            {
                "devilblox:stock:control:select",
                "devilblox:stock:control:minus5",
                "devilblox:stock:control:minus1",
                "devilblox:stock:control:plus1",
                "devilblox:stock:control:plus5",
                "devilblox:stock:control:adjust",
                "devilblox:stock:control:register",
                "devilblox:stock:control:delete",
                "devilblox:stock:control:refresh",
            },
        )
        self.assertTrue(all(item.custom_id for item in dispatchable))
        self.assertEqual(view.selected_item_id, "blueseed")
        self.assertIn("블루 씨앗", text)
        self.assertIn("`3개`", text)

        select = next(item for item in dispatchable if isinstance(item, discord.ui.Select))
        selected_options = [option.value for option in select.options if option.default]
        self.assertEqual(selected_options, ["blueseed"])

    def test_empty_control_layout_only_enables_safe_actions(self) -> None:
        view = StockControlView(SimpleNamespace(), [])
        dispatchable = {item.custom_id: item for item in view.walk_children() if item.is_dispatchable()}

        self.assertTrue(dispatchable["devilblox:stock:control:select"].disabled)
        for custom_id in (
            "devilblox:stock:control:minus5",
            "devilblox:stock:control:minus1",
            "devilblox:stock:control:plus1",
            "devilblox:stock:control:plus5",
            "devilblox:stock:control:adjust",
            "devilblox:stock:control:delete",
        ):
            self.assertTrue(dispatchable[custom_id].disabled)
        self.assertFalse(dispatchable["devilblox:stock:control:register"].disabled)
        self.assertFalse(dispatchable["devilblox:stock:control:refresh"].disabled)

    def test_edit_payload_migrates_legacy_panel_and_retains_logo(self) -> None:
        logo = SimpleNamespace(filename=BRAND_LOGO_FILENAME)
        stale_gif = SimpleNamespace(filename="old-stock.gif")
        message = SimpleNamespace(attachments=[logo, stale_gif])
        view = StockConditionView([], reset_at=None)

        kwargs = stock_panel_edit_kwargs(message, view)

        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"], [])
        self.assertIs(kwargs["view"], view)
        self.assertEqual(kwargs["attachments"], [logo])
        self.assertEqual(kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())


class StockComponentCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_quantity_button_delegates_with_selected_item(self) -> None:
        cog = SimpleNamespace(handle_adjust=AsyncMock())
        view = StockControlView(cog, [stock_item("GhostPepperSeed", "고스트 페퍼 씨앗", 7)])
        plus_one = next(
            item
            for item in view.walk_children()
            if getattr(item, "custom_id", None) == "devilblox:stock:control:plus1"
        )
        interaction = SimpleNamespace()

        await plus_one.callback(interaction)

        cog.handle_adjust.assert_awaited_once_with(interaction, "ghostpepperseed", 1)


if __name__ == "__main__":
    unittest.main()
