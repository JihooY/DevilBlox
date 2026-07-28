from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

import cogs.cogs_operations as operations_module
import cogs.cogs_verify as verify_module
from cogs.cogs_operations import CleanupSummary, OperationsCog, OperationsPanelView
from cogs.cogs_verify import MAX_ATTEMPTS, VerificationCog, VerifyPad, VerifyStartView
from core.system_monitor import SystemSnapshot
from utils.embeds import BRAND_LOGO_FILENAME
from utils.panels import PanelCleanupResult


def _attachment(filename: str):
    return SimpleNamespace(filename=filename)


def _button(view: discord.ui.LayoutView, custom_id: str | None = None, label: str | None = None):
    return next(
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button)
        and (custom_id is None or item.custom_id == custom_id)
        and (label is None or item.label == label)
    )


def _snapshot() -> SystemSnapshot:
    return SystemSnapshot(
        captured_at=datetime.now(timezone.utc),
        cpu_percent=10,
        memory_percent=20,
        memory_used_bytes=2,
        memory_total_bytes=10,
        disk_percent=30,
        disk_used_bytes=3,
        disk_total_bytes=10,
        process_cpu_percent=1,
        process_memory_bytes=1,
        network_download_mbps=1,
        network_upload_mbps=1,
        network_bytes_received=1,
        network_bytes_sent=1,
        gateway_latency_ms=20,
        probe_latency_ms=20,
        uptime_seconds=100,
        gpu=None,
    )


def _operations_cog_stub():
    embed = discord.Embed(
        title="DEVILBLOX SERVER CONTROL",
        description="**현재 상태:** ✅ 정상",
        color=0x2ECC71,
    )
    embed.add_field(name="🖥️ 호스트", value="CPU `10.0%`", inline=False)
    embed.add_field(name="🌐 네트워크", value="수신 `1.00 Mbps`", inline=False)
    return SimpleNamespace(build_panel_embed=Mock(return_value=embed))


class OperationsComponentsTests(unittest.IsolatedAsyncioTestCase):
    def test_persistent_panel_preserves_metrics_and_control_ids(self):
        view = OperationsPanelView(_operations_cog_stub(), _snapshot())

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertTrue(view.is_persistent())
        children = list(view.walk_children())
        self.assertTrue(any(isinstance(item, discord.ui.Container) for item in children))
        self.assertTrue(any(isinstance(item, discord.ui.Section) for item in children))
        self.assertTrue(any(isinstance(item, discord.ui.ActionRow) for item in children))
        text = "\n".join(
            item.content for item in children if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertIn("DEVILBLOX SERVER CONTROL", text)
        self.assertIn("🖥️ 호스트", text)
        self.assertIn("🌐 네트워크", text)
        self.assertEqual(
            {
                item.custom_id
                for item in children
                if isinstance(item, discord.ui.Button)
            },
            {
                "devilblox:operations:refresh",
                "devilblox:operations:force-mitigation",
                "devilblox:operations:auto-mode",
                "devilblox:operations:cleanup-gifs",
            },
        )

    async def test_permission_check_still_rejects_non_admin(self):
        view = OperationsPanelView(_operations_cog_stub())
        interaction = SimpleNamespace(
            user=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False)),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        allowed = await view.interaction_check(interaction)

        self.assertFalse(allowed)
        interaction.response.send_message.assert_awaited_once_with(
            "관리자만 서버 제어 버튼을 사용할 수 있습니다.",
            ephemeral=True,
        )

    async def test_refresh_button_replaces_legacy_payload_and_preserves_logo(self):
        cog = _operations_cog_stub()
        snapshot = _snapshot()
        replacement = OperationsPanelView(cog, snapshot)
        cog.collect_now = AsyncMock(return_value=snapshot)
        cog.build_panel_view = Mock(return_value=replacement)
        view = OperationsPanelView(cog)
        message = SimpleNamespace(
            attachments=[
                _attachment(BRAND_LOGO_FILENAME),
                _attachment("instructions.pdf"),
                _attachment("legacy.gif"),
            ],
            edit=AsyncMock(),
        )
        interaction = SimpleNamespace(
            message=message,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await _button(view, custom_id="devilblox:operations:refresh").callback(interaction)

        kwargs = message.edit.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"], [])
        self.assertIs(kwargs["view"], replacement)
        self.assertEqual(
            [item.filename for item in kwargs["attachments"]],
            [BRAND_LOGO_FILENAME, "instructions.pdf"],
        )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.followup.send.assert_awaited_once_with(
            "최신 시스템 지표로 갱신했습니다.",
            ephemeral=True,
        )

    async def test_saved_panel_refresh_fetches_and_migrates_legacy_message(self):
        cog = object.__new__(OperationsCog)
        cog.latest_snapshot = _snapshot()
        cog._panel_lock = asyncio.Lock()
        replacement = discord.ui.LayoutView()
        replacement.add_item(discord.ui.Container(discord.ui.TextDisplay("replacement")))
        cog.build_panel_view = Mock(return_value=replacement)
        settings = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "channels": {"operations": 10},
                    "meta": {"operations_panel_message_id": 20},
                }
            ),
            set_value=AsyncMock(),
        )
        message = SimpleNamespace(
            attachments=[_attachment(BRAND_LOGO_FILENAME)],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(id=1, get_channel=lambda _: channel)
        cog.bot = SimpleNamespace(
            repos=SimpleNamespace(settings=settings),
            guilds=[guild],
        )

        await cog.refresh_saved_panels()

        channel.fetch_message.assert_awaited_once_with(20)
        kwargs = message.edit.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"], [])
        self.assertIs(kwargs["view"], replacement)

    async def test_gif_cleanup_renderer_knows_operations_panel(self):
        cog = object.__new__(OperationsCog)
        cog._cleanup_lock = asyncio.Lock()
        cog.last_cleanup = CleanupSummary()
        cog.latest_snapshot = _snapshot()
        replacement = discord.ui.LayoutView()
        replacement.add_item(discord.ui.Container(discord.ui.TextDisplay("operations")))
        cog.build_panel_view = Mock(return_value=replacement)
        cog.cleanup_open_ticket_gifs = AsyncMock()
        guild = SimpleNamespace(id=1)
        cog.bot = SimpleNamespace(
            repos=SimpleNamespace(),
            guilds=[guild],
            get_cog=lambda _: None,
        )
        rendered = []

        async def run_cleanup(_repos, target_guild, *, layout_renderer):
            rendered.append(await layout_renderer("operations", SimpleNamespace()))
            return PanelCleanupResult(checked=1)

        with patch.object(
            operations_module,
            "strip_saved_panel_gifs",
            side_effect=run_cleanup,
        ):
            summary = await cog.cleanup_all_panel_gifs()

        self.assertEqual(summary.checked, 1)
        self.assertEqual(rendered, [replacement])
        cog.build_panel_view.assert_called_once_with(cog.latest_snapshot)


class VerifyComponentsTests(unittest.IsolatedAsyncioTestCase):
    def test_start_panel_is_persistent_components_v2(self):
        with patch.object(
            verify_module,
            "gif_media_url",
            return_value="attachment://verify_panel.gif",
        ):
            view = VerifyStartView(SimpleNamespace())

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertTrue(view.is_persistent())
        children = list(view.walk_children())
        self.assertTrue(any(isinstance(item, discord.ui.Container) for item in children))
        self.assertTrue(any(isinstance(item, discord.ui.Section) for item in children))
        self.assertTrue(any(isinstance(item, discord.ui.MediaGallery) for item in children))
        self.assertEqual(
            _button(view, custom_id="devilblox:verify:start").label,
            "START VERIFICATION",
        )

    def test_verify_pad_nests_random_keypad_in_four_action_rows(self):
        with patch.object(verify_module, "gif_media_url", return_value=None):
            pad = VerifyPad(SimpleNamespace(), user_id=2, code="1234", gif_name=None)

        self.assertIsInstance(pad, discord.ui.LayoutView)
        children = list(pad.walk_children())
        action_rows = [item for item in children if isinstance(item, discord.ui.ActionRow)]
        buttons = [item for item in children if isinstance(item, discord.ui.Button)]
        self.assertEqual(len(action_rows), 4)
        self.assertEqual(len(buttons), 12)
        self.assertEqual(set(pad.number_order), set("123456789"))
        self.assertEqual(
            {button.label for button in buttons},
            set("0123456789") | {"DELETE", "CONFIRM"},
        )
        text = "\n".join(
            item.content for item in children if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertIn("1234", text)
        self.assertIn("WAITING INPUT", text)

    async def test_key_press_updates_components_without_legacy_embed(self):
        pad = VerifyPad(SimpleNamespace(), user_id=2, code="1234", gif_name=None)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=2),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )

        await pad.press_number(interaction, "1")

        self.assertEqual(pad.input_code, "1")
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"], [])
        self.assertIs(kwargs["view"], pad)

    async def test_invalid_final_attempt_locks_every_nested_button(self):
        pad = VerifyPad(SimpleNamespace(), user_id=2, code="1234", gif_name=None)
        pad.input_code = "9999"
        pad.attempts = MAX_ATTEMPTS - 1
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=2),
            response=SimpleNamespace(edit_message=AsyncMock()),
        )

        await pad.confirm(interaction)

        self.assertEqual(pad.display_status, "LOCKED")
        self.assertTrue(pad.controls_disabled)
        self.assertTrue(
            all(
                item.disabled
                for item in pad.walk_children()
                if isinstance(item, discord.ui.Button)
            )
        )
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertEqual(kwargs["embeds"], [])

    async def test_success_disables_pad_and_preserves_role_flow(self):
        role = SimpleNamespace(id=30, mention="<@&30>")
        cog = SimpleNamespace(
            settings=SimpleNamespace(
                get=AsyncMock(return_value={"roles": {"verified": role.id}})
            ),
            users=SimpleNamespace(set_verified=AsyncMock()),
            send_verify_log=AsyncMock(),
        )
        pad = VerifyPad(cog, user_id=2, code="1234", gif_name=None)
        pad.input_code = "1234"
        user = SimpleNamespace(id=2, add_roles=AsyncMock())
        guild = SimpleNamespace(id=1, get_role=lambda _: role)
        interaction = SimpleNamespace(
            user=user,
            guild=guild,
            response=SimpleNamespace(edit_message=AsyncMock()),
        )

        await pad.confirm(interaction)

        user.add_roles.assert_awaited_once_with(
            role,
            reason="DevilBlox verification completed",
        )
        cog.users.set_verified.assert_awaited_once_with(1, 2, 30)
        cog.send_verify_log.assert_awaited_once_with(interaction, role)
        self.assertEqual(pad.display_status, "VERIFIED")
        self.assertTrue(pad.controls_disabled)
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"], [])

    async def test_timeout_edits_same_layout_and_disables_controls(self):
        pad = VerifyPad(SimpleNamespace(), user_id=2, code="1234", gif_name=None)
        pad.message = SimpleNamespace(edit=AsyncMock())

        await pad.on_timeout()

        self.assertEqual(pad.display_status, "EXPIRED")
        self.assertTrue(pad.controls_disabled)
        kwargs = pad.message.edit.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"], [])
        self.assertIs(kwargs["view"], pad)

    async def test_refresh_migrates_legacy_verify_panel_and_keeps_files(self):
        message = SimpleNamespace(
            attachments=[
                _attachment("panel-note.png"),
                _attachment("instructions.pdf"),
                _attachment("verify_panel.gif"),
            ],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(id=1, get_channel=lambda _: channel)
        settings = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "channels": {"verify": 10},
                    "meta": {"verify_panel_message_id": 20},
                }
            ),
            set_value=AsyncMock(),
        )
        bot = SimpleNamespace(
            repos=SimpleNamespace(settings=settings),
            add_view=Mock(),
        )
        cog = VerificationCog(bot)
        logo = _attachment(BRAND_LOGO_FILENAME)

        with (
            patch.object(verify_module, "branded_files", return_value=[logo]),
            patch.object(
                verify_module,
                "gif_delivery_status",
                return_value=SimpleNamespace(effective_mode="local"),
            ),
            patch.object(
                verify_module,
                "gif_media_url",
                return_value="attachment://verify_panel.gif",
            ),
        ):
            await cog.refresh_verify_panel(guild)

        kwargs = message.edit.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["embeds"], [])
        self.assertIsInstance(kwargs["view"], VerifyStartView)
        self.assertEqual(
            [item.filename for item in kwargs["attachments"]],
            [BRAND_LOGO_FILENAME, "panel-note.png", "instructions.pdf", "verify_panel.gif"],
        )


if __name__ == "__main__":
    unittest.main()
