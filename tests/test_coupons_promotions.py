from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.cogs_coupons import CouponCog, PromotionCreateModal
from database.coupons import CouponStore


class PromotionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = {
            name: AsyncMock()
            for name in (
                "coupons", "user_coupons", "promotion_codes", "invite_attributions",
                "coupon_selections", "coupon_redemptions",
            )
        }
        self.store = CouponStore(self.db)

    async def test_creation_without_invite_marks_promotion_as_public(self):
        await self.store.create_promotion(1, " public ", "Public", None, "", 15, 7)
        update = self.store.promotions.update_one.call_args.args[1]["$set"]
        self.assertIs(update["requires_invite"], False)
        self.assertIsNone(update["invite_code"])
        self.assertEqual(update["invite_url"], "")

    async def test_creation_with_invite_keeps_restriction(self):
        await self.store.create_promotion(1, "VIP", "VIP", "abc", "discord.gg/abc", 10, 7)
        update = self.store.promotions.update_one.call_args.args[1]["$set"]
        self.assertIs(update["requires_invite"], True)
        self.assertEqual(update["invite_code"], "abc")

    async def test_invalid_public_promotion_is_rejected(self):
        for code, discount in [("!!!", 10), ("SALE", 0), ("SALE", 101)]:
            with self.subTest(code=code, discount=discount), self.assertRaises(ValueError):
                await self.store.create_promotion(1, code, "Sale", None, "", discount, 7)
        self.store.promotions.update_one.assert_not_awaited()

    async def test_public_promotion_quotes_discount_without_attribution(self):
        promo = {"code": "PUBLIC", "discount": 15, "requires_invite": False, "invite_code": None}
        self.store.promotions.find_one.return_value = promo
        self.store.selections.find_one.return_value = {"promotion_code": "PUBLIC"}
        self.assertEqual(await self.store.quote(1, 42, "vending", 1000), (850, None, promo))
        self.store.invites.find_one.assert_not_awaited()
        self.store.promotions.find_one.assert_awaited_once_with(
            {"guild_id": 1, "code": "PUBLIC", "active": True}
        )

    async def test_legacy_and_new_restricted_promotions_require_matching_invite(self):
        for fields in ({}, {"requires_invite": True}):
            promo = {"code": "VIP", "invite_code": "abc", **fields}
            self.store.promotions.find_one.return_value = promo
            self.store.invites.find_one.return_value = None
            self.assertIsNone(await self.store.validate_promotion(1, 42, "VIP"))
            self.store.invites.find_one.assert_awaited_with(
                {"guild_id": 1, "user_id": 42, "invite_code": "abc"}
            )
            self.store.invites.find_one.return_value = {"invite_code": "abc"}
            self.assertEqual(await self.store.validate_promotion(1, 42, "VIP"), promo)

    async def test_missing_or_malformed_promotion_is_not_public(self):
        for promo in (None, {"code": "BROKEN"}):
            self.store.promotions.find_one.return_value = promo
            self.assertIsNone(await self.store.validate_promotion(1, 42, "BROKEN"))

    async def test_indexes_migrate_and_allow_multiple_public_codes(self):
        keys = [("guild_id", 1), ("invite_code", 1)]
        self.store.user_coupons.index_information.return_value = {}
        self.store.promotions.index_information.return_value = {
            "guild_id_1_invite_code_1": {"key": keys, "unique": True},
        }
        await self.store.ensure_indexes()
        self.store.promotions.drop_index.assert_awaited_once_with("guild_id_1_invite_code_1")
        self.store.promotions.create_index.assert_any_await(
            keys, unique=True, partialFilterExpression={"invite_code": {"$type": "string"}},
        )
        self.store.promotions.drop_index.reset_mock()
        self.store.promotions.index_information.return_value = {
            "guild_id_1_invite_code_1": {
                "key": keys, "unique": True,
                "partialFilterExpression": {"invite_code": {"$type": "string"}},
            },
        }
        await self.store.ensure_indexes()
        self.store.promotions.drop_index.assert_not_awaited()

    async def test_modal_and_command_make_invite_optional(self):
        self.assertFalse(PromotionCreateModal(object()).invite.required)
        options = {option.name: option for option in CouponCog.promotion_command.parameters}
        self.assertFalse(options["초대링크"].required)
        self.assertTrue(options["할인율"].required)

    async def test_slash_command_uses_shared_creation_with_empty_invite(self):
        cog = SimpleNamespace(create_promotion_from_input=AsyncMock())
        interaction = object()
        await CouponCog.promotion_command.callback(cog, interaction, "PUBLIC", "Sale", 15)
        cog.create_promotion_from_input.assert_awaited_once_with(interaction, "PUBLIC", "Sale", "", 15)


if __name__ == "__main__":
    unittest.main()
