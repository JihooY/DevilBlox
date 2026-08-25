from __future__ import annotations

import unittest

import discord

from cogs.cogs_museum import build_museum_view

TEST_MUSEUM_URL = "https://example.com/museum"


class MuseumAnnouncementViewTests(unittest.TestCase):
    def test_view_is_one_interactive_container(self) -> None:
        view = build_museum_view(TEST_MUSEUM_URL)

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(len(view.children), 1)
        container = view.children[0]
        self.assertIsInstance(container, discord.ui.Container)

    def test_container_has_announcement_text_and_link_button(self) -> None:
        view = build_museum_view(TEST_MUSEUM_URL)
        container = view.children[0]

        text = "\n".join(
            item.content
            for item in view.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertIn("2,000명", text)
        self.assertIn("DEVIL BLOX 역사 박물관", text)

        buttons = [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].style, discord.ButtonStyle.link)
        self.assertEqual(buttons[0].url, TEST_MUSEUM_URL)

        self.assertTrue(any(isinstance(item, discord.ui.ActionRow) for item in container.children))


if __name__ == "__main__":
    unittest.main()
