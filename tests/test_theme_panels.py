from __future__ import annotations

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cogs.cogs_themes import ThemesCog
from cogs.theme_views import ThemeManagerView
from database.settings import default_settings
from database.themes import default_themes


class ThemePanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings_doc = default_settings(11)

        async def set_value(guild_id, section, key, value):
            self.assertEqual(guild_id, 11)
            self.settings_doc[section][key] = value

        self.settings = SimpleNamespace(
            get=AsyncMock(side_effect=lambda _: deepcopy(self.settings_doc)),
            set_value=AsyncMock(side_effect=set_value), register_panel=AsyncMock(), unregister_panel=AsyncMock(),
        )
        self.message = SimpleNamespace(id=500, edit=AsyncMock(), jump_url="https://discord.example/panel")
        self.guild = SimpleNamespace(id=11)
        self.channels = {}
        for channel_id in (20, 30):
            channel = Mock(spec=discord.TextChannel)
            channel.id = channel_id
            channel.guild = self.guild
            channel.send = AsyncMock(return_value=self.message)
            channel.fetch_message = AsyncMock(return_value=self.message)
            self.channels[channel_id] = channel
        self.guild.get_channel = lambda channel_id: self.channels.get(channel_id)
        self.bot = SimpleNamespace(
            themes=SimpleNamespace(state=AsyncMock(return_value=default_themes(11))),
            repos=SimpleNamespace(settings=self.settings), add_view=Mock(),
            get_guild=lambda guild_id: self.guild if guild_id == 11 else None,
            guilds=[self.guild], wait_until_ready=AsyncMock(),
        )
        self.cog = ThemesCog(self.bot)

    def interaction(self, *, admin=True):
        return SimpleNamespace(
            guild=self.guild, guild_id=11, channel=self.channels[20],
            user=SimpleNamespace(id=99, guild_permissions=SimpleNamespace(administrator=admin)),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_command_installs_container_in_explicit_channel_and_saves_location(self):
        event = self.interaction()
        await ThemesCog.theme_panel.callback(self.cog, event, self.channels[30])
        self.channels[20].send.assert_not_awaited()
        payload = self.channels[30].send.await_args.kwargs
        self.assertIsInstance(payload["view"], ThemeManagerView)
        self.assertTrue(payload["view"].is_persistent())
        self.assertIsNone(payload["view"].owner_id)
        self.assertNotIn("content", payload)
        self.assertNotIn("ephemeral", payload)
        self.assertTrue(any(isinstance(c, discord.ui.Container) for c in payload["view"].children))
        self.assertEqual(self.settings_doc["channels"]["themes"], 30)
        self.assertEqual(self.settings_doc["meta"]["theme_panel_message_id"], 500)
        self.settings.register_panel.assert_awaited_once()
        self.bot.add_view.assert_called_once_with(payload["view"], message_id=500)
        self.assertIn(self.message.jump_url, event.followup.send.await_args.args[0])

    async def test_command_uses_configured_channel_and_updates_existing_message(self):
        self.settings_doc["channels"]["themes"] = 30
        self.settings_doc["meta"]["theme_panel_message_id"] = 500
        event = self.interaction()
        await ThemesCog.theme_panel.callback(self.cog, event)
        self.channels[20].send.assert_not_awaited()
        self.channels[30].send.assert_not_awaited()
        self.channels[30].fetch_message.assert_awaited_once_with(500)
        payload = self.message.edit.await_args.kwargs
        self.assertIsNone(payload["content"])
        self.assertEqual(payload["embeds"], [])
        self.assertTrue(payload["view"].is_persistent())

    async def test_command_uses_current_channel_if_unconfigured_and_denies_non_admin(self):
        denied = self.interaction(admin=False)
        await ThemesCog.theme_panel.callback(self.cog, denied)
        denied.response.send_message.assert_awaited_once()
        self.channels[20].send.assert_not_awaited()
        await ThemesCog.theme_panel.callback(self.cog, self.interaction())
        self.channels[20].send.assert_awaited_once()

    async def test_restart_restores_selected_theme_and_callbacks_for_saved_message(self):
        state = default_themes(11)
        state["themes"]["stella"] = {"name": "스텔", "gifs": []}
        self.bot.themes.state.return_value = state
        self.settings_doc["channels"]["themes"] = 30
        self.settings_doc["meta"].update(theme_panel_message_id=500, theme_panel_selected="stella")
        await self.cog.restore_saved_panels()
        self.bot.wait_until_ready.assert_awaited_once()
        restored = self.message.edit.await_args.kwargs["view"]
        self.assertEqual(restored.selected, "stella")
        self.assertTrue(restored.is_persistent())
        self.bot.add_view.assert_called_once_with(restored, message_id=500)
        self.channels[30].send.assert_not_awaited()

    async def test_public_selection_is_saved_and_private_change_refreshes_public_theme(self):
        state = default_themes(11)
        state["themes"]["stella"] = {"name": "스텔", "gifs": []}
        manager = ThemeManagerView(self.bot, 11, None, state)
        event = self.interaction()
        event.response.is_done = Mock(return_value=True)
        event.edit_original_response = AsyncMock()
        await manager.update(event, state, "stella")
        self.assertEqual(self.settings_doc["meta"]["theme_panel_selected"], "stella")
        self.assertTrue(event.edit_original_response.await_args.kwargs["view"].is_persistent())
        self.settings_doc["channels"]["themes"] = 30
        self.settings_doc["meta"]["theme_panel_message_id"] = 500
        state["active"] = "stella"
        state["themes"]["stella"]["gifs"] = ["success.gif"]
        self.bot.themes.state.return_value = state
        await self.cog.refresh_panel(11)
        text = "\n".join(item.content for item in self.message.edit.await_args.kwargs["view"].walk_children()
                         if isinstance(item, discord.ui.TextDisplay))
        self.assertIn("사용 중: **스텔**", text)

    async def test_missing_saved_message_is_recreated_by_install_command(self):
        self.settings_doc["channels"]["themes"] = 30
        self.settings_doc["meta"]["theme_panel_message_id"] = 499
        self.channels[30].fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not found"), "missing",
        )
        await self.cog.ensure_panel(self.guild, self.channels[30])
        self.channels[30].send.assert_awaited_once()
        self.assertEqual(self.settings_doc["meta"]["theme_panel_message_id"], 500)


if __name__ == "__main__":
    unittest.main()
