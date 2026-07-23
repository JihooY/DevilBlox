from __future__ import annotations

import asyncio
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from core.config import ConfigError, OperationsConfig
from core.error_reporting import DiscordExceptionReporter, build_exception_report
from core.system_monitor import (
    MetricAlertEngine,
    NetworkMitigationController,
    SystemSnapshot,
)
from utils.gifs import (
    ALL_GIFS,
    begin_gif_recovery,
    choose_gif,
    configure_gif_delivery,
    gif_delivery_status,
    gif_file,
    gif_media_url,
    set_gif_suppressed,
)
from utils.panels import strip_message_gifs


def operations_config(**overrides) -> OperationsConfig:
    base = OperationsConfig(
        enabled=True,
        channel_id=None,
        sample_interval=5,
        panel_interval=15,
        auxiliary_interval=30,
        cpu_critical_percent=90,
        memory_critical_percent=90,
        disk_critical_percent=95,
        gpu_critical_percent=95,
        network_download_critical_mbps=100,
        network_upload_critical_mbps=100,
        latency_critical_ms=1_000,
        trigger_samples=3,
        recovery_samples=2,
        recovery_ratio=0.7,
        mitigation_cooldown_seconds=10,
        alert_cooldown_seconds=300,
        probe_enabled=False,
        probe_host=None,
        probe_port=443,
        probe_timeout=1,
    )
    return replace(base, **overrides)


def snapshot(**overrides) -> SystemSnapshot:
    base = SystemSnapshot(
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
    return replace(base, **overrides)


class MetricAlertEngineTests(unittest.TestCase):
    def test_requires_consecutive_breaches_and_consecutive_recovery(self) -> None:
        engine = MetricAlertEngine(operations_config())
        high = snapshot(cpu_percent=99)
        self.assertEqual(engine.evaluate(high, now=0), [])
        self.assertEqual(engine.evaluate(high, now=1), [])
        transitions = engine.evaluate(high, now=2)
        self.assertEqual([(item.event, item.reading.key) for item in transitions], [("raised", "cpu")])

        low = snapshot(cpu_percent=40)
        self.assertEqual(engine.evaluate(low, now=3), [])
        transitions = engine.evaluate(low, now=4)
        self.assertEqual([(item.event, item.reading.key) for item in transitions], [("resolved", "cpu")])

    def test_large_sample_gap_resets_pending_streak(self) -> None:
        engine = MetricAlertEngine(operations_config())
        high = snapshot(cpu_percent=99)
        self.assertEqual(engine.evaluate(high, now=0), [])
        self.assertEqual(engine.evaluate(high, now=100), [])
        self.assertEqual(engine.evaluate(high, now=200), [])
        self.assertNotIn("cpu", engine.active_keys)

    def test_missing_optional_latency_clears_old_alert_and_raises_reachability(self) -> None:
        engine = MetricAlertEngine(operations_config(probe_enabled=True, probe_host="example.test"))
        high = snapshot(probe_latency_ms=2_000)
        engine.evaluate(high, now=0)
        engine.evaluate(high, now=1)
        raised = engine.evaluate(high, now=2)
        self.assertIn(("raised", "probe_latency"), [(item.event, item.reading.key) for item in raised])

        unavailable = snapshot(probe_latency_ms=None)
        engine.evaluate(unavailable, now=3)
        resolved = engine.evaluate(unavailable, now=4)
        self.assertIn(
            ("resolved", "probe_latency"),
            [(item.event, item.reading.key) for item in resolved],
        )
        unreachable = engine.evaluate(unavailable, now=5)
        self.assertIn(
            ("raised", "probe_reachability"),
            [(item.event, item.reading.key) for item in unreachable],
        )


class OperationsConfigTests(unittest.TestCase):
    def test_non_finite_monitor_value_is_rejected(self) -> None:
        with patch.dict("os.environ", {"MONITOR_SAMPLE_INTERVAL": "nan"}):
            with self.assertRaises(ConfigError):
                OperationsConfig.from_env()


class NetworkMitigationTests(unittest.TestCase):
    def test_hysteresis_and_minimum_cooldown(self) -> None:
        controller = NetworkMitigationController(operations_config())
        high = snapshot(network_download_mbps=150)
        self.assertIsNone(controller.evaluate(high, now=0))
        self.assertIsNone(controller.evaluate(high, now=1))
        transition = controller.evaluate(high, now=2)
        self.assertTrue(transition and transition.enabled)

        low = snapshot(network_download_mbps=10, network_upload_mbps=10)
        self.assertIsNone(controller.evaluate(low, now=3))
        self.assertIsNone(controller.evaluate(low, now=4))
        self.assertIsNone(controller.evaluate(low, now=11))
        transition = controller.evaluate(low, now=12)
        self.assertTrue(transition and not transition.enabled)

    def test_manual_mode_does_not_auto_disable(self) -> None:
        controller = NetworkMitigationController(operations_config())
        transition = controller.force_enable("test")
        self.assertTrue(transition and transition.enabled)
        self.assertIsNone(controller.evaluate(snapshot(), now=1_000))
        self.assertTrue(controller.effective)

    def test_large_sample_gap_resets_network_streak(self) -> None:
        controller = NetworkMitigationController(operations_config())
        high = snapshot(network_download_mbps=150)
        self.assertIsNone(controller.evaluate(high, now=0))
        self.assertIsNone(controller.evaluate(high, now=100))
        self.assertIsNone(controller.evaluate(high, now=200))
        self.assertFalse(controller.effective)


class GifDeliveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_gif_delivery(
            mode="local",
            cdn_base_url=None,
            rotation_enabled=False,
            local_variant="original",
        )
        set_gif_suppressed(False)

    def test_cdn_mode_uses_url_without_local_file(self) -> None:
        configure_gif_delivery(
            mode="cdn",
            cdn_base_url="https://cdn.example.test/gifs/",
            rotation_enabled=False,
        )
        self.assertEqual(
            gif_media_url("success.gif"),
            "https://cdn.example.test/gifs/success.gif",
        )
        self.assertIsNone(gif_file("success.gif"))

    def test_emergency_mode_disables_both_sources(self) -> None:
        configure_gif_delivery(mode="auto", cdn_base_url="https://cdn.example.test/gifs")
        set_gif_suppressed(True, "traffic")
        self.assertEqual(gif_delivery_status().effective_mode, "disabled")
        self.assertIsNone(gif_media_url("success.gif"))
        self.assertIsNone(choose_gif(ALL_GIFS))

    def test_rotation_off_reuses_existing_attachment(self) -> None:
        configure_gif_delivery(mode="local", cdn_base_url=None, rotation_enabled=False)
        existing = SimpleNamespace(filename="success.gif")
        selected = choose_gif(ALL_GIFS, [existing], force_new=True)
        self.assertEqual(selected, "success.gif")

    def test_local_recovery_spaces_new_uploads_but_keeps_existing_media(self) -> None:
        configure_gif_delivery(mode="local", cdn_base_url=None, rotation_enabled=False)
        begin_gif_recovery(60)
        self.assertIsNotNone(choose_gif(ALL_GIFS))
        self.assertIsNone(choose_gif(ALL_GIFS))
        existing = SimpleNamespace(filename="success.gif")
        self.assertEqual(choose_gif(ALL_GIFS, [existing]), "success.gif")


class ErrorReportTests(unittest.TestCase):
    def test_traceback_redacts_runtime_secrets(self) -> None:
        try:
            raise RuntimeError("password=super-secret")
        except RuntimeError as error:
            report = build_exception_report(
                "token=abc123",
                error,
                secrets_to_hide=("super-secret", "abc123"),
            )
        self.assertNotIn("super-secret", report.traceback_text)
        self.assertNotIn("abc123", report.context)
        self.assertTrue(report.error_id.startswith("ERR-"))


class ErrorReporterConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_logging_thread_submits_to_event_loop_queue(self) -> None:
        bot = SimpleNamespace(
            config=SimpleNamespace(
                discord_token="token",
                mongo=SimpleNamespace(password=None, uri=None),
            )
        )
        reporter = DiscordExceptionReporter(bot)
        worker = threading.Thread(
            target=lambda: reporter.enqueue_exception("threaded logger", RuntimeError("boom"))
        )
        worker.start()
        worker.join()
        await asyncio.sleep(0)
        self.assertEqual(reporter.queue.qsize(), 1)


class PanelCleanupTests(unittest.IsolatedAsyncioTestCase):
    def interactive_layout(self) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView()
        container = discord.ui.Container()
        container.add_item(
            discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://panel.gif"))
        )
        container.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(label="Open", custom_id="test:open")
            )
        )
        view.add_item(container)
        return view

    async def test_interactive_generic_layout_is_not_installed_without_real_renderer(self) -> None:
        parsed = self.interactive_layout()
        message = SimpleNamespace(
            attachments=[SimpleNamespace(filename="panel.gif")],
            embeds=[],
            edit=AsyncMock(),
        )
        with patch.object(discord.ui.LayoutView, "from_message", return_value=parsed):
            removed_attachments, removed_references = await strip_message_gifs(message)
        self.assertEqual((removed_attachments, removed_references), (0, 0))
        message.edit.assert_not_awaited()

    async def test_real_replacement_renderer_removes_interactive_gif_safely(self) -> None:
        parsed = self.interactive_layout()
        replacement = discord.ui.LayoutView()
        replacement.add_item(
            discord.ui.Container(
                discord.ui.ActionRow(
                    discord.ui.Button(label="Open", custom_id="test:open")
                )
            )
        )
        message = SimpleNamespace(
            attachments=[
                SimpleNamespace(filename="devilblox_icon.png"),
                SimpleNamespace(filename="panel.gif"),
            ],
            embeds=[],
            edit=AsyncMock(),
        )
        with patch.object(discord.ui.LayoutView, "from_message", return_value=parsed):
            removed_attachments, removed_references = await strip_message_gifs(
                message,
                replacement_view=replacement,
            )
        self.assertEqual((removed_attachments, removed_references), (1, 1))
        kwargs = message.edit.await_args.kwargs
        self.assertIs(kwargs["view"], replacement)
        self.assertEqual([item.filename for item in kwargs["attachments"]], ["devilblox_icon.png"])


if __name__ == "__main__":
    unittest.main()
