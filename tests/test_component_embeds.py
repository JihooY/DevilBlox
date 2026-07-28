from __future__ import annotations

import unittest
from types import SimpleNamespace

import discord

from utils.components import MAX_COMPONENT_TEXT, embed_to_layout_view
from utils.embeds import BRAND_LOGO_URL, _componentize_edit_payload, _componentize_send_payload


class EmbedComponentConversionTests(unittest.TestCase):
    def test_embed_fields_and_media_become_one_branded_container(self) -> None:
        embed = discord.Embed(
            title="충전 요청",
            description="처리할 요청입니다.",
            color=0x2ECC71,
        )
        embed.add_field(name="금액", value="20,000원")
        embed.set_image(url="attachment://proof.png")
        view = embed_to_layout_view(embed)

        self.assertIsInstance(view, discord.ui.LayoutView)
        container = view.children[0]
        self.assertIsInstance(container, discord.ui.Container)
        section = container.children[0]
        self.assertIsInstance(section, discord.ui.Section)
        self.assertEqual(str(section.accessory.media.url), BRAND_LOGO_URL)
        self.assertIn("20,000원", section.children[0].content)
        self.assertTrue(any(isinstance(item, discord.ui.MediaGallery) for item in container.children))

    def test_long_embed_is_truncated_with_an_explanation(self) -> None:
        embed = discord.Embed(title="LONG", description="가" * 5_000)
        view = embed_to_layout_view(embed, include_brand_thumbnail=False)
        text = view.children[0].children[0].content
        self.assertLessEqual(len(text), MAX_COMPONENT_TEXT)
        self.assertIn("생략되었습니다", text)

    def test_simple_send_payload_is_componentized(self) -> None:
        payload = _componentize_send_payload({"embed": discord.Embed(title="완료")})
        self.assertNotIn("embed", payload)
        self.assertIsInstance(payload["view"], discord.ui.LayoutView)

    def test_message_content_mentions_move_into_the_container(self) -> None:
        payload = _componentize_send_payload(
            {
                "content": "<@123> <@&456>",
                "embed": discord.Embed(title="티켓 시작"),
            }
        )

        self.assertNotIn("content", payload)
        text = payload["view"].children[0].children[0].content
        self.assertIn("<@123> <@&456>", text)

    def test_unreferenced_photo_attachments_join_the_gallery(self) -> None:
        embed = discord.Embed(title="후기")
        embed.set_image(url="attachment://review_1.png")
        payload = _componentize_send_payload(
            {
                "embed": embed,
                "files": [
                    SimpleNamespace(filename="review_1.png"),
                    SimpleNamespace(filename="review_2.webp"),
                ],
            }
        )

        gallery = next(
            item
            for item in payload["view"].children[0].children
            if isinstance(item, discord.ui.MediaGallery)
        )
        self.assertEqual(
            {str(item.media.url) for item in gallery.items},
            {"attachment://review_1.png", "attachment://review_2.webp"},
        )

    def test_existing_interactive_view_is_left_untouched(self) -> None:
        legacy_view = discord.ui.View(timeout=None)
        embed = discord.Embed(title="KEEP")
        payload = _componentize_send_payload({"embed": embed, "view": legacy_view})
        self.assertIs(payload["embed"], embed)
        self.assertIs(payload["view"], legacy_view)

    def test_edit_payload_clears_legacy_embed(self) -> None:
        payload = _componentize_edit_payload(
            {"embed": discord.Embed(title="UPDATED")},
            has_brand_logo=False,
        )
        self.assertNotIn("embed", payload)
        self.assertEqual(payload["embeds"], [])
        self.assertIsNone(payload["content"])
        self.assertIsInstance(payload["view"], discord.ui.LayoutView)

    def test_edit_payload_explicitly_preserves_existing_attachments(self) -> None:
        attachments = [SimpleNamespace(filename="devilblox_icon.png")]
        payload = _componentize_edit_payload(
            {"embed": discord.Embed(title="UPDATED")},
            has_brand_logo=True,
            existing_attachments=attachments,
        )

        self.assertEqual(payload["attachments"], attachments)
        self.assertEqual(
            str(payload["view"].children[0].children[0].accessory.media.url),
            BRAND_LOGO_URL,
        )


if __name__ == "__main__":
    unittest.main()
