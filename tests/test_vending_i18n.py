from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from cogs.cogs_vending_archive import VendingArchiveCog
from cogs.cogs_verify import VerifyLanguageView
from cogs.vending_views import (
    CategoryMenuView, ChargeRequestModal, DiscountMenuView, DownloadSelectView,
    ProductDetailView, ProductMenuView, PromotionCodeModal, VendingLanguageSelect,
    VendingPanelView,
)
from database.vending import VendingLogStore
from utils.vending_i18n import LANGUAGES, LANGUAGE, OWNER, TRANSLATIONS, tr


def text(view):
    return "\n".join(item.content for item in view.walk_children()
                     if isinstance(item, discord.ui.TextDisplay))


def interaction(user_id=2):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), guild=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


PRODUCT = {
    "guild_id": 1, "product_id": "test-id", "product_id_lower": "test-id",
    "title": "한국어 상품명", "description": "원본 상품 설명 그대로", "price": 12345,
    "product_type": "stock",
}


class VendingTranslationTests(unittest.IsolatedAsyncioTestCase):
    def test_auth_has_only_four_real_languages(self):
        view = VerifyLanguageView(SimpleNamespace(), 2)
        children = list(view.walk_children())
        self.assertFalse(any(isinstance(item, discord.ui.Select) for item in children))
        self.assertEqual([item.label for item in children if isinstance(item, discord.ui.Button)],
                         list(LANGUAGES.values()))

    def test_every_template_has_all_translations_and_preserves_placeholders(self):
        formatter = Formatter()
        root = Path(__file__).resolve().parents[1]
        for filename in ("cogs/vending_views.py", "cogs/cogs_vending_archive.py"):
            tree = ast.parse((root / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        self.assertIn(node.args[0].value, TRANSLATIONS)
        for source, translations in TRANSLATIONS.items():
            source_fields = sorted((field, spec, conversion) for _, field, spec, conversion
                                   in formatter.parse(source) if field is not None)
            self.assertEqual(len(translations), 3)
            for translated in translations:
                self.assertTrue(translated)
                self.assertEqual(source_fields, sorted((field, spec, conversion)
                                 for _, field, spec, conversion in formatter.parse(translated)
                                 if field is not None), source)

    def test_all_customer_screens_capture_language_and_preserve_product_data(self):
        cog = SimpleNamespace(product_thread_mention=lambda _: "<#123>", product_page_url=lambda *_: None)
        category = {"category_id": "cat", "name": "원본 카테고리"}
        for language in LANGUAGES:
            token = LANGUAGE.set(language)
            owner = OWNER.set(2)
            try:
                views = [
                    CategoryMenuView(cog, [category], "buy"),
                    ProductMenuView(cog, category, [PRODUCT], "buy", stock_counts={"test-id": 3}),
                    ProductDetailView(cog, PRODUCT, stock_count=3),
                    DiscountMenuView(cog, "test-id", []),
                    DownloadSelectView(cog, [PRODUCT]),
                    VendingPanelView(cog),
                ]
                self.assertIn(PRODUCT["title"], text(views[2]))
                self.assertIn(PRODUCT["description"], text(views[2]))
                self.assertIn("12,345", text(views[2]))
                self.assertIn(tr("가격: `{0:,}원`").format(12345), text(views[2]))
                for view in views:
                    self.assertEqual(view.language, language)
                    self.assertEqual(view.user_id, 2)
                    for item in view.walk_children():
                        if isinstance(item, discord.ui.Select) and hasattr(item, "language"):
                            self.assertEqual(item.language, language)
                modal = ChargeRequestModal(cog)
                self.assertEqual(modal.title, tr("충전 신청"))
                self.assertEqual(modal.children[0].text, tr("입금자명"))
                self.assertEqual(modal.children[1].text, tr("입금 금액"))
                self.assertEqual(PromotionCodeModal(cog, "test-id").title, tr("프로모션 코드 입력"))
            finally:
                OWNER.reset(owner)
                LANGUAGE.reset(token)

    async def test_persistent_panel_actions_use_saved_language_without_prompt(self):
        for language in LANGUAGES:
            async def category_menu(event, mode):
                await event.followup.send(view=CategoryMenuView(cog, [{"category_id": "cat"}], mode))

            async def download_menu(event):
                await event.followup.send(view=DownloadSelectView(cog, [PRODUCT]))

            cog = SimpleNamespace(
                repos=SimpleNamespace(users=SimpleNamespace(get=AsyncMock(return_value={"default_language": language}))),
                handle_category_menu=category_menu, handle_download_menu=download_menu,
            )
            view = VendingPanelView(cog)
            self.assertTrue(view.is_persistent())
            for action in ("charge", "catalog", "buy", "download"):
                event = interaction()
                await getattr(view, action)(event)
                if action == "charge":
                    result = event.response.send_modal.await_args.args[0]
                else:
                    result = event.followup.send.await_args.kwargs["view"]
                self.assertEqual(result.language, language)
                self.assertEqual(result.user_id, event.user.id)
                event.response.send_message.assert_not_awaited()
                self.assertEqual(LANGUAGE.get(), "ko")
                self.assertIsNone(OWNER.get())

    async def test_legacy_user_without_language_can_top_up_directly_in_korean(self):
        cog = SimpleNamespace(repos=SimpleNamespace(users=SimpleNamespace(get=AsyncMock(return_value=None))))
        event = interaction()
        await VendingPanelView(cog).charge(event)
        modal = event.response.send_modal.await_args.args[0]
        self.assertEqual(modal.language, "ko")
        event.response.send_message.assert_not_awaited()

    async def test_concurrent_language_selections_do_not_mix(self):
        async def category_menu(event, mode):
            await asyncio.sleep(0)
            await event.followup.send(view=CategoryMenuView(cog, [{"category_id": "cat"}], mode))

        cog = SimpleNamespace(handle_category_menu=category_menu)
        ja, en = interaction(2), interaction(3)
        await asyncio.gather(
            VendingLanguageSelect(cog, 2, "buy").open_language(ja, "ja"),
            VendingLanguageSelect(cog, 3, "buy").open_language(en, "en"),
        )
        for event, language, user in ((ja, "ja", 2), (en, "en", 3)):
            view = event.followup.send.await_args.kwargs["view"]
            self.assertEqual(view.language, language)
            self.assertEqual(view.user_id, user)
            child = next(item for item in view.walk_children() if isinstance(item, discord.ui.Select))
            self.assertEqual(child.language, language)
            self.assertEqual(child.user_id, user)
        self.assertEqual(LANGUAGE.get(), "ko")
        self.assertIsNone(OWNER.get())

    async def test_charge_selection_and_later_submit_preserve_language(self):
        cog = SimpleNamespace(handle_charge_submit=AsyncMock())
        event = interaction()
        await VendingLanguageSelect(cog, 2, "charge").open_language(event, "zh")
        modal = event.response.send_modal.await_args.args[0]
        self.assertEqual(modal.language, "zh")
        self.assertEqual(modal.title, tr("충전 신청", "zh"))
        # Even when a new task starts with Korean, the captured modal language wins.
        async def submit(*args, **kwargs):
            self.assertEqual(LANGUAGE.get(), "zh")
        cog.handle_charge_submit.side_effect = submit
        await modal.on_submit(interaction())
        self.assertEqual(LANGUAGE.get(), "ko")

    async def test_localized_purchase_reaches_same_service_and_translates_error(self):
        cog = object.__new__(VendingArchiveCog)
        cog.bot = SimpleNamespace(repos=SimpleNamespace(products=SimpleNamespace(get=AsyncMock(return_value=PRODUCT))))
        cog.commerce = SimpleNamespace(purchase=AsyncMock(return_value=SimpleNamespace(
            product=PRODUCT, status="insufficient_funds", current_cash=100, price=12345,
        )))
        view = ProductDetailView(cog, PRODUCT, stock_count=1, language="en", user_id=2)
        event = interaction()
        await view.buy(event)
        cog.commerce.purchase.assert_awaited_once_with(1, 2, PRODUCT)
        embed = event.followup.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, tr("잔액 부족", "en"))
        self.assertIn("KRW 12,345", embed.description)

    async def test_other_user_cannot_purchase_in_localized_session(self):
        cog = SimpleNamespace(handle_purchase=AsyncMock(), product_thread_mention=lambda _: "", product_page_url=lambda *_: None)
        view = ProductDetailView(cog, PRODUCT, stock_count=1, language="ja", user_id=2)
        event = interaction(3)
        await view.buy(event)
        cog.handle_purchase.assert_not_awaited()
        event.response.send_message.assert_awaited_once_with(tr("본인 세션만 조작할 수 있습니다.", "ja"), ephemeral=True)

    async def test_charge_language_is_saved_for_delayed_dm(self):
        collection = SimpleNamespace(insert_one=AsyncMock(return_value=SimpleNamespace(inserted_id="request")))
        store = object.__new__(VendingLogStore)
        store.charge_logs = collection
        charge = await store.create_charge_request(1, 2, "입금자", 10000, language="zh")
        self.assertEqual(charge["language"], "zh")
        self.assertEqual(collection.insert_one.await_args.args[0]["language"], "zh")

    async def test_charge_result_dm_uses_saved_language_after_restart(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                charge = {"user_id": 2, "amount": 10000, "language": language, "reject_reason": "관리자 원문 사유"}
                cog = object.__new__(VendingArchiveCog)
                cog.bot = SimpleNamespace(repos=SimpleNamespace(vending=SimpleNamespace(
                    reject_charge_request=AsyncMock(return_value=charge),
                )))
                cog.commerce = SimpleNamespace(approve_charge=AsyncMock(return_value=SimpleNamespace(
                    status="approved", charge=charge, newly_completed=True,
                )))
                cog.admin_allowed = AsyncMock(return_value=True)
                cog.edit_charge_messages = AsyncMock()
                cog.send_charge_log = AsyncMock()
                cog.send_user_dm = AsyncMock()
                event = interaction()
                event.message = SimpleNamespace(id=9)
                event.user.mention = "<@2>"
                with patch("cogs.cogs_vending_archive.random_embed_gif_kwargs", side_effect=lambda embed, _: {"embed": embed}):
                    await cog.handle_approve_charge(event)
                    dm = cog.send_user_dm.await_args.args[2]
                    self.assertEqual(dm.title, tr("충전 수락", language))
                    self.assertEqual(dm.description, tr("{0:,}원이 충전되었습니다.\n승인 관리자: {1}", language).format(10000, "<@2>"))
                    await cog.handle_reject_charge(event, 9, "관리자 원문 사유")
                    dm = cog.send_user_dm.await_args.args[2]
                    self.assertEqual(dm.title, tr("충전 거절", language))
                    self.assertIn("관리자 원문 사유", dm.description)
                self.assertEqual(LANGUAGE.get(), "ko")


if __name__ == "__main__":
    unittest.main()
