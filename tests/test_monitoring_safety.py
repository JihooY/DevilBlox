from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.cogs_operations import CleanupSummary, OperationsCog
from core.config import OperationsConfig
from core.system_monitor import (
    GpuMetrics,
    MetricAlertEngine,
    MetricsSampler,
    NetworkMitigationController,
    SystemSnapshot,
)


def _config(**overrides) -> OperationsConfig:
    base = OperationsConfig(
        enabled=True,
        channel_id=None,
        sample_interval=5,
        panel_interval=30,
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


def _snapshot(**overrides) -> SystemSnapshot:
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
        probe_latency_ms=None,
        uptime_seconds=100,
        gpu=None,
    )
    return replace(base, **overrides)


def _host_sample() -> dict:
    return {
        "cpu_percent": 10,
        "memory_percent": 20,
        "memory_used_bytes": 2,
        "memory_total_bytes": 10,
        "disk_percent": 30,
        "disk_used_bytes": 3,
        "disk_total_bytes": 10,
        "process_cpu_percent": 1,
        "process_memory_bytes": 1,
        "network_download_mbps": 1,
        "network_upload_mbps": 1,
        "network_bytes_received": 1,
        "network_bytes_sent": 1,
        "uptime_seconds": 100,
    }


class FreshSampleCoalescingTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, sampler) -> OperationsCog:
        cog = object.__new__(OperationsCog)
        cog.config = _config()
        cog.bot = SimpleNamespace(latency=0.01)
        cog.sampler = sampler
        cog.latest_snapshot = _snapshot(
            captured_at=datetime.now(timezone.utc) - timedelta(minutes=5)
        )
        cog._sample_lock = asyncio.Lock()
        cog._sample_task = None
        return cog

    async def test_safety_sample_awaits_and_coalesces_inflight_sample(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        fresh = _snapshot(network_download_mbps=250)

        class DelayedSampler:
            calls = 0

            async def sample(self, gateway_latency_seconds):
                self.calls += 1
                started.set()
                await release.wait()
                return fresh

        sampler = DelayedSampler()
        cog = self._cog(sampler)
        first = asyncio.create_task(cog.collect_now(force=True))
        await started.wait()
        safety = asyncio.create_task(cog.collect_fresh_for_safety())
        await asyncio.sleep(0)

        self.assertFalse(safety.done())
        self.assertEqual(sampler.calls, 1)
        release.set()
        first_result, safety_result = await asyncio.gather(first, safety)

        self.assertIs(first_result, fresh)
        self.assertIs(safety_result, fresh)
        self.assertEqual(sampler.calls, 1)

    async def test_failed_fresh_sample_does_not_mutate_forced_protection(self) -> None:
        class FailingSampler:
            async def sample(self, gateway_latency_seconds):
                raise RuntimeError("sample failed")

        cog = self._cog(FailingSampler())
        cog.mitigation = NetworkMitigationController(cog.config)
        cog.mitigation.force_enable("test")

        with self.assertRaisesRegex(RuntimeError, "sample failed"):
            await cog.collect_fresh_for_safety()

        self.assertTrue(cog.mitigation.forced)
        self.assertTrue(cog.mitigation.effective)


class AuxiliaryObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_gpu_observation_counts_only_once(self) -> None:
        config = _config(auxiliary_interval=30)
        sampler = MetricsSampler(config)
        gpu_calls = 0

        def gpu_sample() -> GpuMetrics:
            nonlocal gpu_calls
            gpu_calls += 1
            return GpuMetrics(99, 1, 2, 40, 1)

        sampler._sample_host = _host_sample
        sampler._sample_nvidia_gpu = gpu_sample
        sampler._probe_latency = AsyncMock(return_value=None)

        snapshots = [await sampler.sample(None) for _ in range(3)]
        self.assertEqual(gpu_calls, 1)
        self.assertEqual(
            {item.gpu_observation_revision for item in snapshots},
            {1},
        )

        engine = MetricAlertEngine(config)
        transitions = []
        for now, item in enumerate(snapshots):
            transitions.extend(engine.evaluate(item, now=now))
        self.assertNotIn("gpu", engine.active_keys)
        self.assertNotIn(("raised", "gpu"), [(item.event, item.reading.key) for item in transitions])

        engine.evaluate(replace(snapshots[-1], gpu_observation_revision=2), now=30)
        raised = engine.evaluate(
            replace(snapshots[-1], gpu_observation_revision=3),
            now=60,
        )
        self.assertIn(("raised", "gpu"), [(item.event, item.reading.key) for item in raised])

    async def test_cached_probe_observation_counts_only_once(self) -> None:
        config = _config(probe_enabled=True, probe_host="example.test")
        high = _snapshot(probe_latency_ms=2_000, probe_observation_revision=1)
        engine = MetricAlertEngine(config)

        for now in range(3):
            self.assertEqual(engine.evaluate(high, now=now), [])
        self.assertNotIn("probe_latency", engine.active_keys)

        engine.evaluate(replace(high, probe_observation_revision=2), now=30)
        raised = engine.evaluate(replace(high, probe_observation_revision=3), now=60)
        self.assertIn(
            ("raised", "probe_latency"),
            [(item.event, item.reading.key) for item in raised],
        )


class NetworkInterfaceTests(unittest.TestCase):
    def test_named_interface_is_validated_and_selected(self) -> None:
        counters = SimpleNamespace(bytes_recv=10, bytes_sent=20)
        with patch(
            "core.system_monitor.psutil.net_io_counters",
            return_value={"eth-test": counters},
        ) as mocked:
            sampler = MetricsSampler(_config(network_interface="eth-test"))

        self.assertIs(sampler._previous_network, counters)
        mocked.assert_called_once_with(pernic=True)

    def test_unknown_interface_fails_instead_of_silent_aggregate_fallback(self) -> None:
        with patch(
            "core.system_monitor.psutil.net_io_counters",
            return_value={"eth-test": SimpleNamespace(bytes_recv=1, bytes_sent=1)},
        ):
            with self.assertRaisesRegex(ValueError, "missing-interface"):
                MetricsSampler(_config(network_interface="missing-interface"))

    def test_unspecified_interface_uses_aggregate_counters(self) -> None:
        counters = SimpleNamespace(bytes_recv=10, bytes_sent=20)
        with patch(
            "core.system_monitor.psutil.net_io_counters",
            return_value=counters,
        ) as mocked:
            sampler = MetricsSampler(_config(network_interface=None))

        self.assertIs(sampler._previous_network, counters)
        mocked.assert_called_once_with()


class MitigationStreakResetTests(unittest.TestCase):
    def test_manual_mode_transitions_reset_pending_network_streaks(self) -> None:
        controller = NetworkMitigationController(_config())
        high = _snapshot(network_download_mbps=150)
        low = _snapshot()
        controller.evaluate(high, now=0)
        controller.evaluate(high, now=1)
        self.assertEqual(controller.breach_streak, 2)

        controller.force_enable("manual")
        self.assertEqual(controller.breach_streak, 0)
        self.assertEqual(controller.recovery_streak, 0)
        controller.return_to_automatic(low)
        self.assertEqual(controller.breach_streak, 0)
        self.assertEqual(controller.recovery_streak, 0)


class BackgroundCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_cleanup_starts_background_task_without_waiting(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        cog = object.__new__(OperationsCog)
        cog.bot = SimpleNamespace(wait_until_ready=AsyncMock())
        cog._cleanup_lock = asyncio.Lock()
        cog._cleanup_task = None
        cog._manual_cleanup_task = None
        cog.last_cleanup = CleanupSummary()
        cog.last_action = ""
        cog.broadcast_content = AsyncMock()
        cog.refresh_saved_panels = AsyncMock()

        async def cleanup() -> CleanupSummary:
            started.set()
            await release.wait()
            return CleanupSummary(checked=1, changed=1)

        cog.cleanup_all_panel_gifs = cleanup
        self.assertTrue(cog.start_manual_cleanup("test"))
        task = cog._manual_cleanup_task
        self.assertIsNotNone(task)
        await started.wait()
        self.assertFalse(task.done())
        self.assertFalse(cog.start_manual_cleanup("duplicate"))

        release.set()
        await task
        self.assertIsNone(cog._manual_cleanup_task)
        cog.broadcast_content.assert_awaited_once()
        cog.refresh_saved_panels.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
