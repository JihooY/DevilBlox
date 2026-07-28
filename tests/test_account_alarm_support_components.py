from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

import cogs.cogs_account as account_module
import cogs.cogs_alarms as alarm_module
import cogs.cogs_support as support_module
from cogs.cogs_account import AccountCog, AccountView, ToggleAnonymousView
from cogs.cogs_alarms import AlarmCog, AlarmView
from cogs.cogs_support import SupportCog, SupportView
from utils.embeds import BRAND_LOGO_FILENAME


def _attachment(filename: str):
    return SimpleNamespace(filename=filename)


def _button(view: discord.ui.LayoutView, custom_id: str | None = None):
    return next(
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button)
        and (custom_id is None or item.custom_id == custom_id)
    )


class PersistentPanelLayoutTests(unittest.TestCase):
    def setUp(self):
        self.cog = SimpleNamespace()

    def test_persistent_panels_are_components_v2_layouts(self):
        cases = (
            (
                AccountView(self.cog),
                {"devilblox:account:info", "devilblox:account:coupons"},
                "ACCOUNT INFO",
            ),
            (
                AlarmView(self.cog),
                {
                    "devilblox:alarm:announcement",
                    "devilblox:alarm:seller",
                    "devilblox:alarm:stock",
                },
                "ALARM SETTING",
            ),
            (
                SupportView(self.cog),
                {"devilblox:support:open"},
                "SUPPORT",
            ),
        )

        for view, expected_custom_ids, heading in cases:
            with self.subTest(view=type(view).__name__):
                self.assertIsInstance(view, discord.ui.LayoutView)
                self.assertTrue(view.is_persistent())
                children = list(view.walk_children())
                self.assertTrue(any(isinstance(item, discord.ui.Container) for item in children))
                self.assertTrue(any(isinstance(item, discord.ui.Section) for item in children))
                self.assertTrue(any(isinstance(item, discord.ui.ActionRow) for item in children))
                text = "\n".join(
                    item.content
                    for item in children
                    if isinstance(item, discord.ui.TextDisplay)
                )
                self.assertIn(heading, text)
                custom_ids = {
                    item.custom_id
                    for item in children
                    if isinstance(item, discord.ui.Button)
                }
                self.assertEqual(custom_ids, expected_custom_ids)

    def test_panel_gif_is_rendered_inside_media_gallery(self):
        cases = (
            (account_module, AccountView),
            (alarm_module, AlarmView),
            (support_module, SupportView),
        )
        for module, view_type in cases:
            with self.subTest(view=view_type.__name__), patch.object(
                module,
                "gif_media_url",
                return_value="attachment://panel.gif",
            ):
                view = view_type(self.cog, "panel.gif")
                galleries = [
                    item
                    for item in view.walk_children()
                    if isinstance(item, discord.ui.MediaGallery)
                ]
                self.assertEqual(len(galleries), 1)
                self.assertEqual(
                    str(galleries[0].items[0].media.url),
                    "attachment://panel.gif",
                )

    def test_account_detail_with_toggle_is_also_components_v2(self):
        view = ToggleAnonymousView(self.cog, "## ACCOUNT INFORMATION\n**유저** <@2>")

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertFalse(view.is_persistent())
        children = list(view.walk_children())
        self.assertTrue(any(isinstance(item, discord.ui.Section) for item in children))
        self.assertTrue(any(isinstance(item, discord.ui.ActionRow) for item in children))
        self.assertEqual(_button(view).label, "중개 로그 익명 토글")


class ComponentCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_info_keeps_ephemeral_behavior_and_uses_layout(self):
        cog = SimpleNamespace()
        detail = ToggleAnonymousView(cog, "## ACCOUNT INFORMATION")
        cog.build_account_view = AsyncMock(return_value=detail)
        view = AccountView(cog)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=2),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        logo = _attachment(BRAND_LOGO_FILENAME)

        with patch.object(account_module, "branded_files", return_value=[logo]):
            await _button(view, "devilblox:account:info").callback(interaction)

        cog.build_account_view.assert_awaited_once_with(interaction.guild, interaction.user)
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertIs(kwargs["view"], detail)
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(kwargs["files"], [logo])
        self.assertNotIn("embed", kwargs)

    async def test_alarm_buttons_keep_their_original_routes(self):
        view = AlarmView(SimpleNamespace())
        view.toggle_role = AsyncMock()
        interaction = SimpleNamespace()

        cases = (
            ("devilblox:alarm:announcement", "alarm_announcement", "공지 알림"),
            ("devilblox:alarm:seller", "alarm_seller", "티켓 상태 알림"),
            ("devilblox:alarm:stock", "alarm_stock", "입고 알림"),
        )
        for custom_id, role_key, label in cases:
            with self.subTest(custom_id=custom_id):
                view.toggle_role.reset_mock()
                await _button(view, custom_id).callback(interaction)
                view.toggle_role.assert_awaited_once_with(interaction, role_key, label)

    async def test_support_button_keeps_open_ticket_callback(self):
        cog = SimpleNamespace(open_support_ticket=AsyncMock())
        view = SupportView(cog)
        interaction = SimpleNamespace()

        await _button(view, "devilblox:support:open").callback(interaction)

        cog.open_support_ticket.assert_awaited_once_with(interaction)


class SavedPanelMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_migrates_legacy_message_and_preserves_attachments(self):
        cases = (
            (
                account_module,
                AccountCog,
                "refresh_account_panel",
                "account",
                "account_panel_message_id",
                AccountView,
            ),
            (
                alarm_module,
                AlarmCog,
                "refresh_alarm_panel",
                "alarm",
                "alarm_panel_message_id",
                AlarmView,
            ),
            (
                support_module,
                SupportCog,
                "refresh_support_panel",
                "support",
                "support_panel_message_id",
                SupportView,
            ),
        )

        for module, cog_type, refresh_name, channel_key, meta_key, view_type in cases:
            with self.subTest(cog=cog_type.__name__):
                settings = SimpleNamespace(
                    get=AsyncMock(
                        return_value={
                            "channels": {channel_key: 10},
                            "meta": {meta_key: 20},
                        }
                    ),
                    set_value=AsyncMock(),
                )
                repos = SimpleNamespace(settings=settings)
                bot = SimpleNamespace(repos=repos, add_view=Mock())
                cog = cog_type(bot)
                message = SimpleNamespace(
                    attachments=[
                        _attachment(BRAND_LOGO_FILENAME),
                        _attachment("instructions.pdf"),
                        _attachment("panel.gif"),
                    ],
                    embeds=[discord.Embed(title="legacy")],
                    edit=AsyncMock(),
                )
                channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
                guild = SimpleNamespace(id=1, get_channel=lambda channel_id: channel)

                with (
                    patch.object(module, "choose_gif", return_value="panel.gif") as choose,
                    patch.object(module, "message_media_urls", return_value=("attachment://panel.gif",)),
                    patch.object(module, "gif_media_url", return_value="attachment://panel.gif"),
                    patch.object(
                        module,
                        "gif_delivery_status",
                        return_value=SimpleNamespace(effective_mode="local"),
                    ),
                ):
                    await getattr(cog, refresh_name)(guild, rotate_image=True)

                choose.assert_called_once()
                self.assertTrue(choose.call_args.kwargs["force_new"])
                kwargs = message.edit.await_args.kwargs
                self.assertIsNone(kwargs["content"])
                self.assertEqual(kwargs["embeds"], [])
                self.assertIsInstance(kwargs["view"], view_type)
                self.assertEqual(
                    [item.filename for item in kwargs["attachments"]],
                    [BRAND_LOGO_FILENAME, "instructions.pdf", "panel.gif"],
                )

    def test_cdn_migration_drops_stale_local_gif_but_keeps_other_files(self):
        cases = (
            (account_module, AccountView),
            (alarm_module, AlarmView),
            (support_module, SupportView),
        )
        message = SimpleNamespace(
            attachments=[
                _attachment(BRAND_LOGO_FILENAME),
                _attachment("instructions.pdf"),
                _attachment("old.gif"),
            ]
        )

        for module, view_type in cases:
            with self.subTest(view=view_type.__name__), patch.object(
                module,
                "gif_delivery_status",
                return_value=SimpleNamespace(effective_mode="cdn"),
            ):
                view = view_type(SimpleNamespace())
                kwargs = module._panel_edit_kwargs(message, view, "new.gif")
                self.assertEqual(
                    [item.filename for item in kwargs["attachments"]],
                    [BRAND_LOGO_FILENAME, "instructions.pdf"],
                )
                self.assertIsNone(kwargs["content"])
                self.assertEqual(kwargs["embeds"], [])


if __name__ == "__main__":
    unittest.main()
