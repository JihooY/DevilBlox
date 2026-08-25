from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.cogs_vending_archive import (
    VendingArchiveCog,
    message_attachment_url,
)
from cogs.vending_views import (
    ArchiveResultView,
    ChargeAdminView,
    ProductDetailView,
    ProductPurchaseModal,
    ProductSelect,
    VendingPanelView,
    VendingStockPanelView,
)


def view_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )


def view_buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button]:
    return [
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button)
    ]


class VendingComponentLayoutTests(unittest.TestCase):
    def test_vending_panel_still_builds_after_view_extraction(self) -> None:
        view = VendingPanelView(
            SimpleNamespace(),
            stats={"category_count": 2, "product_count": 25},
        )

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(len(view.children), 1)
        self.assertIsInstance(view.children[0], discord.ui.Container)
        text = view_text(view)
        self.assertIn("VENDING MACHINE", text)
        self.assertIn("### 🇰🇷 한국어", text)
        self.assertIn("판매 상품 `25`개", text)
        self.assertIn("### 🇺🇸 English", text)
        self.assertIn("Products for sale `25`", text)
        self.assertIn("### 🇯🇵 日本語", text)
        self.assertIn("販売商品 `25`件", text)
        self.assertEqual(
            sum(isinstance(item, discord.ui.Separator) for item in view.children[0].children),
            4,
        )
        buttons = view_buttons(view)
        self.assertEqual(len(buttons), 4)

    def test_product_select_shows_stock_count_for_stock_products_only(self) -> None:
        products = [
            {
                "product_id": "standing-a",
                "product_id_lower": "standing-a",
                "title": "상시 상품",
                "price": 1_000,
                "product_type": "standing",
            },
            {
                "product_id": "stock-a",
                "product_id_lower": "stock-a",
                "title": "재고 상품",
                "price": 2_000,
                "product_type": "stock",
            },
        ]

        select = ProductSelect(SimpleNamespace(), products, "buy", stock_counts={"stock-a": 3})

        standing_option = next(option for option in select.options if option.value == "standing-a")
        stock_option = next(option for option in select.options if option.value == "stock-a")
        self.assertNotIn("재고", standing_option.description)
        self.assertIn("재고 3개", stock_option.description)

    def test_product_detail_marks_stock_product_sold_out(self) -> None:
        cog = SimpleNamespace(
            product_thread_mention=lambda product: "`미설정`",
            product_page_url=lambda guild_id, product: None,
        )
        product = {
            "guild_id": 1,
            "product_id": "stock-a",
            "title": "재고 상품",
            "price": 2_000,
            "product_type": "stock",
        }

        view = ProductDetailView(cog, product, stock_count=0)

        buy_button = next(button for button in view_buttons(view) if button.label in {"구매하기", "품절"})
        self.assertEqual(buy_button.label, "품절")
        self.assertTrue(buy_button.disabled)
        self.assertIn("품절", view_text(view))

    def test_stock_panel_lists_products_with_counts_and_management_buttons(self) -> None:
        products = [
            {"product_id": "stock-a", "product_id_lower": "stock-a", "title": "재고 상품"},
        ]

        view = VendingStockPanelView(SimpleNamespace(), products, {"stock-a": 4})

        self.assertIn("재고형 상품 `1`개", view_text(view))
        self.assertIn("`4개`", view_text(view))
        button_labels = {button.label for button in view_buttons(view)}
        self.assertEqual(button_labels, {"재고 추가", "전체 삭제", "새로고침"})

    def test_pending_charge_is_one_persistent_interactive_container(self) -> None:
        charge = {
            "status": "pending",
            "user_id": 123456789012345,
            "depositor_name": "테스트 입금자",
            "amount": 20_000,
            "proof_filename": "proof.png",
        }

        view = ChargeAdminView(
            SimpleNamespace(),
            charge,
            image_url="attachment://proof.png",
        )

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertTrue(view.is_persistent())
        self.assertEqual(len(view.children), 1)
        self.assertIsInstance(view.children[0], discord.ui.Container)
        self.assertTrue(any(isinstance(item, discord.ui.Thumbnail) for item in view.walk_children()))
        self.assertTrue(any(isinstance(item, discord.ui.MediaGallery) for item in view.walk_children()))
        self.assertEqual(
            {button.custom_id for button in view_buttons(view)},
            {
                "devilblox:vending:charge:approve",
                "devilblox:vending:charge:reject",
            },
        )
        text = view_text(view)
        self.assertIn("충전 요청", text)
        self.assertIn("대기 중", text)
        self.assertIn("<@123456789012345>", text)
        self.assertIn("테스트 입금자", text)
        self.assertIn("20,000원", text)
        self.assertIn("관리자가 수락해야", text)
        self.assertIn("처리 대기 중", text)

    def test_completed_charge_keeps_details_without_actions(self) -> None:
        charge = {
            "status": "approved",
            "user_id": 123456789012345,
            "depositor_name": "테스트 입금자",
            "amount": 20_000,
            "processed_by": 987654321098765,
            "proof_filename": "proof.png",
        }

        view = ChargeAdminView(view_cog := SimpleNamespace(), charge, image_url="https://cdn.example/proof.png")

        self.assertIs(view.cog, view_cog)
        self.assertEqual(view_buttons(view), [])
        text = view_text(view)
        self.assertIn("수락됨", text)
        self.assertIn("승인 관리자", text)
        self.assertIn("<@987654321098765>", text)
        self.assertIn("처리 완료된 요청", text)

    def test_archive_result_combines_product_media_and_actions(self) -> None:
        cog = SimpleNamespace(product_thread_mention=lambda product: "<#456>")
        product = {
            "product_id": "GhostPepperSeed",
            "title": "고스트 페퍼 씨앗",
            "price": 15_000,
            "thread_id": 456,
        }

        view = ArchiveResultView(
            cog,
            "GhostPepperSeed",
            "https://example.com/products/ghost-pepper",
            product=product,
            canonical_url="https://youtu.be/video123",
            description="영상에 사용된 상품입니다.",
            image_url="https://i.ytimg.com/vi/video123/hqdefault.jpg",
        )

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(len(view.children), 1)
        self.assertIsInstance(view.children[0], discord.ui.Container)
        self.assertTrue(any(isinstance(item, discord.ui.MediaGallery) for item in view.walk_children()))
        buttons = view_buttons(view)
        self.assertEqual({button.label for button in buttons}, {"구매하기", "상품 페이지"})
        page_button = next(button for button in buttons if button.label == "상품 페이지")
        self.assertEqual(page_button.url, "https://example.com/products/ghost-pepper")
        text = view_text(view)
        self.assertIn("아카이브 검색 결과", text)
        self.assertIn("고스트 페퍼 씨앗", text)
        self.assertIn("GhostPepperSeed", text)
        self.assertIn("15,000원", text)
        self.assertIn("https://youtu.be/video123", text)

    def test_attachment_lookup_uses_filename_instead_of_attachment_order(self) -> None:
        message = SimpleNamespace(
            attachments=[
                SimpleNamespace(filename="devilblox_icon.png", url="logo-url"),
                SimpleNamespace(filename="proof.png", url="proof-url"),
            ]
        )

        self.assertEqual(message_attachment_url(message, "proof.png"), "proof-url")

class VendingComponentCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_buy_button_preserves_modal_callback(self) -> None:
        cog = SimpleNamespace(product_thread_mention=lambda product: "`미설정`")
        view = ArchiveResultView(cog, "product-1", None)
        buy_button = next(button for button in view_buttons(view) if button.label == "구매하기")
        interaction = SimpleNamespace(response=SimpleNamespace(send_modal=AsyncMock()))

        await buy_button.callback(interaction)

        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, ProductPurchaseModal)
        self.assertEqual(str(modal.product_id.default), "product-1")

    async def test_stock_purchase_delivers_unit_by_dm(self) -> None:
        cog = object.__new__(VendingArchiveCog)
        product = {"product_id": "stock-a", "title": "재고 상품", "product_type": "stock"}
        unit = {"content": "id:pw"}
        sent_dm = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(send=sent_dm))

        embed = await cog.deliver_stock_unit(interaction, product, unit)

        sent_dm.assert_awaited_once()
        dm_embed = sent_dm.await_args.kwargs["embed"]
        self.assertIn("id:pw", dm_embed.fields[0].value)
        self.assertIn("재고 상품", embed.description)

    async def test_stock_purchase_falls_back_to_ephemeral_reply_when_dm_fails(self) -> None:
        cog = object.__new__(VendingArchiveCog)
        product = {"product_id": "stock-a", "title": "재고 상품", "product_type": "stock"}
        unit = {"content": "id:pw"}
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                send=AsyncMock(
                    side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "blocked")
                )
            )
        )

        embed = await cog.deliver_stock_unit(interaction, product, unit)

        self.assertIn("DM을 보낼 수 없어", embed.description)
        self.assertIn("id:pw", embed.fields[0].value)

    async def test_stock_add_submit_requires_a_stock_type_product(self) -> None:
        repos = SimpleNamespace(products=SimpleNamespace(get=AsyncMock(return_value={"product_id": "standing-a", "product_type": "standing"})))
        cog = object.__new__(VendingArchiveCog)
        cog.bot = SimpleNamespace(repos=repos)
        cog.staff_allowed = AsyncMock(return_value=True)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog.handle_stock_add_submit(interaction, "standing-a", "id:pw")

        kwargs = interaction.followup.send.await_args.kwargs
        self.assertIn("상품 없음", kwargs["embed"].title)

    async def test_stock_add_submit_splits_lines_and_refreshes_panel(self) -> None:
        product = {"product_id": "stock-a", "product_type": "stock"}
        added_units = [{"content": "a"}, {"content": "b"}]
        repos = SimpleNamespace(
            products=SimpleNamespace(get=AsyncMock(return_value=product)),
            vending_stock=SimpleNamespace(add_many=AsyncMock(return_value=added_units)),
        )
        cog = object.__new__(VendingArchiveCog)
        cog.bot = SimpleNamespace(repos=repos)
        cog.staff_allowed = AsyncMock(return_value=True)
        cog.update_stock_panel_message = AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog.handle_stock_add_submit(interaction, "stock-a", "a\n\nb\n")

        repos.vending_stock.add_many.assert_awaited_once_with(1, "stock-a", ["a", "b"], created_by=99)
        cog.update_stock_panel_message.assert_awaited_once_with(interaction, "stock-a")
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertIn("2개", kwargs["embed"].description)

    async def test_charge_edit_migrates_embed_and_retains_existing_proof(self) -> None:
        cog = object.__new__(VendingArchiveCog)
        proof = SimpleNamespace(filename="proof.png", url="proof-url")
        logo = SimpleNamespace(filename="devilblox_icon.png")
        message = SimpleNamespace(attachments=[proof], edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(get_channel=lambda channel_id: channel)
        charge = {
            "status": "approved",
            "user_id": 123,
            "depositor_name": "입금자",
            "amount": 20_000,
            "processed_by": 456,
            "proof_filename": "proof.png",
        }

        with patch("cogs.cogs_vending_archive.branded_files", return_value=[logo]):
            await cog.edit_charge_message(
                guild,
                charge,
                channel_id=10,
                message_id=20,
                image_url="proof-url",
            )

        kwargs = message.edit.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"], [])
        self.assertIsInstance(kwargs["view"], ChargeAdminView)
        self.assertEqual(view_buttons(kwargs["view"]), [])
        self.assertEqual(kwargs["attachments"], [logo, proof])
        self.assertEqual(
            kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )

    async def test_archive_search_sends_only_the_component_result(self) -> None:
        repos = SimpleNamespace(
            archives=SimpleNamespace(
                find=AsyncMock(
                    return_value={
                        "product_id": "product-1",
                        "summary": "아카이브 요약",
                    }
                )
            ),
            products=SimpleNamespace(
                get=AsyncMock(
                    return_value={
                        "product_id": "product-1",
                        "title": "테스트 상품",
                        "price": 3_000,
                        "thread_id": 99,
                        "page_url": "https://example.com/product-1",
                    }
                )
            ),
        )
        cog = object.__new__(VendingArchiveCog)
        cog.bot = SimpleNamespace(repos=repos)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch("cogs.cogs_vending_archive.branded_files", return_value=[]):
            await cog.handle_archive_search(interaction, "https://youtu.be/video123")

        kwargs = interaction.followup.send.await_args.kwargs
        self.assertNotIn("embed", kwargs)
        self.assertNotIn("embeds", kwargs)
        self.assertTrue(kwargs["ephemeral"])
        self.assertIsInstance(kwargs["view"], ArchiveResultView)
        self.assertTrue(any(isinstance(item, discord.ui.MediaGallery) for item in kwargs["view"].walk_children()))
        self.assertIn("아카이브 요약", view_text(kwargs["view"]))

    async def test_approved_retry_repairs_panel_without_duplicate_notifications(self) -> None:
        cog = object.__new__(VendingArchiveCog)
        charge = {
            "status": "approved",
            "user_id": 123,
            "amount": 20_000,
            "processed_by": 456,
        }
        cog.commerce = SimpleNamespace(
            approve_charge=AsyncMock(
                return_value=SimpleNamespace(
                    status="approved",
                    charge=charge,
                    newly_completed=False,
                )
            )
        )
        cog.admin_allowed = AsyncMock(return_value=True)
        cog.edit_charge_messages = AsyncMock()
        cog.send_charge_log = AsyncMock()
        cog.send_user_dm = AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            message=SimpleNamespace(id=2),
            user=SimpleNamespace(id=456),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog.handle_approve_charge(interaction)

        cog.edit_charge_messages.assert_awaited_once_with(interaction.guild, charge)
        cog.send_charge_log.assert_not_awaited()
        cog.send_user_dm.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
