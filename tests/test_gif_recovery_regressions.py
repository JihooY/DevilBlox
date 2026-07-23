from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.cogs_verify import VerificationCog
from utils.embeds import BRAND_LOGO_FILENAME
from utils.gifs import (
    begin_gif_recovery,
    claim_local_gif_upload_slot,
    configure_gif_delivery,
    end_gif_recovery,
    gif_recovery_active,
    set_gif_suppressed,
)


def attachment(filename: str):
    return SimpleNamespace(filename=filename)


class GifRecoveryLifetimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        end_gif_recovery()
        set_gif_suppressed(False)
        configure_gif_delivery(
            mode="local",
            cdn_base_url=None,
            rotation_enabled=False,
            local_variant="original",
        )

    def test_local_gate_does_not_expire_until_explicitly_ended(self) -> None:
        configure_gif_delivery(mode="local", cdn_base_url=None)
        with patch("utils.gifs.time.monotonic", return_value=100.0):
            begin_gif_recovery(5.0, duration=1.0)
            self.assertTrue(gif_recovery_active())
            self.assertTrue(claim_local_gif_upload_slot())

        with patch("utils.gifs.time.monotonic", return_value=102.0):
            self.assertFalse(claim_local_gif_upload_slot())

        with patch("utils.gifs.time.monotonic", return_value=1_000_000.0):
            self.assertTrue(gif_recovery_active())
            self.assertTrue(claim_local_gif_upload_slot())

        with patch("utils.gifs.time.monotonic", return_value=1_000_001.0):
            self.assertFalse(claim_local_gif_upload_slot())

        end_gif_recovery()
        self.assertFalse(gif_recovery_active())

    def test_cdn_mode_is_not_blocked_by_local_upload_gate(self) -> None:
        configure_gif_delivery(
            mode="cdn",
            cdn_base_url="https://cdn.example.test/gifs",
        )
        begin_gif_recovery(60.0)
        self.assertFalse(gif_recovery_active())
        self.assertTrue(claim_local_gif_upload_slot())
        self.assertTrue(claim_local_gif_upload_slot())


class VerifyAttachmentPreservationTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        end_gif_recovery()
        set_gif_suppressed(False)
        configure_gif_delivery(
            mode="local",
            cdn_base_url=None,
            rotation_enabled=False,
            local_variant="original",
        )

    async def _refresh_with_mode(self, mode: str, *, suppressed: bool = False) -> list[str]:
        cdn_base_url = "https://cdn.example.test/gifs" if mode == "cdn" else None
        configure_gif_delivery(mode=mode, cdn_base_url=cdn_base_url)
        set_gif_suppressed(suppressed, "test" if suppressed else None)

        message = SimpleNamespace(
            attachments=[
                attachment("panel-note.png"),
                attachment("instructions.pdf"),
                attachment("verify_panel.gif"),
            ],
            edit=AsyncMock(),
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(id=123, get_channel=lambda _: channel)
        settings = SimpleNamespace(
            get=AsyncMock(
                return_value={
                    "channels": {"verify": 456},
                    "meta": {"verify_panel_message_id": 789},
                }
            ),
            set_value=AsyncMock(),
        )
        bot = SimpleNamespace(
            repos=SimpleNamespace(settings=settings),
            add_view=lambda _: None,
        )
        cog = VerificationCog(bot)
        logo = attachment(BRAND_LOGO_FILENAME)

        with patch("cogs.cogs_verify.branded_files", return_value=[logo]):
            await cog.refresh_verify_panel(guild)

        edited = message.edit.await_args.kwargs["attachments"]
        return [item.filename for item in edited]

    async def test_local_logo_repair_preserves_png_pdf_and_gif(self) -> None:
        filenames = await self._refresh_with_mode("local")
        self.assertEqual(
            filenames,
            [BRAND_LOGO_FILENAME, "panel-note.png", "instructions.pdf", "verify_panel.gif"],
        )

    async def test_cdn_logo_repair_preserves_non_gif_attachments(self) -> None:
        filenames = await self._refresh_with_mode("cdn")
        self.assertEqual(
            filenames,
            [BRAND_LOGO_FILENAME, "panel-note.png", "instructions.pdf"],
        )

    async def test_disabled_logo_repair_preserves_non_gif_attachments(self) -> None:
        filenames = await self._refresh_with_mode("local", suppressed=True)
        self.assertEqual(
            filenames,
            [BRAND_LOGO_FILENAME, "panel-note.png", "instructions.pdf"],
        )


if __name__ == "__main__":
    unittest.main()
