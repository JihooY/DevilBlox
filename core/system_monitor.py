from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psutil

from .config import OperationsConfig


@dataclass(frozen=True, slots=True)
class GpuMetrics:
    utilization_percent: float
    memory_used_bytes: int
    memory_total_bytes: int
    temperature_celsius: float | None
    device_count: int


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    captured_at: datetime
    cpu_percent: float
    memory_percent: float
    memory_used_bytes: int
    memory_total_bytes: int
    disk_percent: float
    disk_used_bytes: int
    disk_total_bytes: int
    process_cpu_percent: float
    process_memory_bytes: int
    network_download_mbps: float
    network_upload_mbps: float
    network_bytes_received: int
    network_bytes_sent: int
    gateway_latency_ms: float | None
    probe_latency_ms: float | None
    uptime_seconds: float
    gpu: GpuMetrics | None
    gpu_observation_revision: int | None = None
    probe_observation_revision: int | None = None


@dataclass(frozen=True, slots=True)
class MetricReading:
    key: str
    label: str
    value: float
    threshold: float
    unit: str
    observation_revision: int | None = None


@dataclass(frozen=True, slots=True)
class MetricTransition:
    event: str
    reading: MetricReading


@dataclass(slots=True)
class _MetricState:
    active: bool = False
    breach_streak: int = 0
    recovery_streak: int = 0
    last_alert_at: float = 0.0
    last_evaluated_at: float | None = None
    last_reading: MetricReading | None = None
    last_observation_revision: int | None = None


@dataclass(frozen=True, slots=True)
class MitigationTransition:
    enabled: bool
    reason: str


class MetricsSampler:
    """Collect host metrics without blocking discord.py's event loop."""

    def __init__(self, config: OperationsConfig) -> None:
        self.config = config
        self._previous_network = self._read_network_counters()
        self._previous_network_at = time.monotonic()
        self._process = psutil.Process()
        psutil.cpu_percent(interval=None)
        self._process.cpu_percent(interval=None)
        self._nvidia_smi = shutil.which("nvidia-smi")
        self._gpu_cache: GpuMetrics | None = None
        self._gpu_cached_at = 0.0
        self._gpu_observation_revision = 0
        self._probe_cache: float | None = None
        self._probe_cached_at = 0.0
        self._probe_observation_revision = 0

    async def sample(self, gateway_latency_seconds: float | None) -> SystemSnapshot:
        host_task = asyncio.to_thread(self._sample_host)
        gpu_task = self._sample_gpu_cached()
        probe_task = self._sample_probe_cached()
        host, gpu_result, probe_result = await asyncio.gather(host_task, gpu_task, probe_task)
        gpu, gpu_observation_revision = gpu_result
        probe_latency_ms, probe_observation_revision = probe_result

        gateway_latency_ms = None
        if gateway_latency_seconds is not None:
            candidate = gateway_latency_seconds * 1_000
            if candidate >= 0:
                gateway_latency_ms = candidate

        return SystemSnapshot(
            captured_at=datetime.now(timezone.utc),
            gateway_latency_ms=gateway_latency_ms,
            probe_latency_ms=probe_latency_ms,
            gpu=gpu,
            gpu_observation_revision=gpu_observation_revision,
            probe_observation_revision=probe_observation_revision,
            **host,
        )

    def _sample_host(self) -> dict:
        now = time.monotonic()
        network = self._read_network_counters()
        elapsed = max(now - self._previous_network_at, 0.001)
        received_delta = max(network.bytes_recv - self._previous_network.bytes_recv, 0)
        sent_delta = max(network.bytes_sent - self._previous_network.bytes_sent, 0)
        self._previous_network = network
        self._previous_network_at = now

        memory = psutil.virtual_memory()
        disk_target = Path.cwd().anchor or str(Path.cwd())
        disk = psutil.disk_usage(disk_target)
        process_memory = self._process.memory_info().rss

        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": memory.percent,
            "memory_used_bytes": memory.total - memory.available,
            "memory_total_bytes": memory.total,
            "disk_percent": disk.percent,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "process_cpu_percent": self._process.cpu_percent(interval=None),
            "process_memory_bytes": process_memory,
            "network_download_mbps": received_delta * 8 / elapsed / 1_000_000,
            "network_upload_mbps": sent_delta * 8 / elapsed / 1_000_000,
            "network_bytes_received": network.bytes_recv,
            "network_bytes_sent": network.bytes_sent,
            "uptime_seconds": max(time.time() - psutil.boot_time(), 0.0),
        }

    def _read_network_counters(self):
        interface = self.config.network_interface
        if not interface:
            return psutil.net_io_counters()

        counters = psutil.net_io_counters(pernic=True)
        try:
            return counters[interface]
        except KeyError as exc:
            available = ", ".join(sorted(counters)) or "none"
            raise ValueError(
                f"MONITOR_NETWORK_INTERFACE={interface!r} was not found; "
                f"available interfaces: {available}"
            ) from exc

    def _sample_nvidia_gpu(self) -> GpuMetrics | None:
        if self._nvidia_smi is None:
            return None

        command = [
            self._nvidia_smi,
            "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        rows: list[tuple[float, float, float, float | None]] = []
        for line in completed.stdout.splitlines():
            values = [value.strip() for value in line.split(",")]
            if len(values) != 4:
                continue
            try:
                utilization = float(values[0])
                memory_used_mb = float(values[1])
                memory_total_mb = float(values[2])
                temperature = None if values[3].casefold() == "n/a" else float(values[3])
            except ValueError:
                continue
            rows.append((utilization, memory_used_mb, memory_total_mb, temperature))

        if not rows:
            return None

        temperatures = [row[3] for row in rows if row[3] is not None]
        return GpuMetrics(
            utilization_percent=max(row[0] for row in rows),
            memory_used_bytes=int(sum(row[1] for row in rows) * 1024 * 1024),
            memory_total_bytes=int(sum(row[2] for row in rows) * 1024 * 1024),
            temperature_celsius=max(temperatures) if temperatures else None,
            device_count=len(rows),
        )

    async def _sample_gpu_cached(self) -> tuple[GpuMetrics | None, int]:
        now = time.monotonic()
        if self._gpu_cached_at and now - self._gpu_cached_at < self.config.auxiliary_interval:
            return self._gpu_cache, self._gpu_observation_revision
        self._gpu_cache = await asyncio.to_thread(self._sample_nvidia_gpu)
        self._gpu_cached_at = time.monotonic()
        self._gpu_observation_revision += 1
        return self._gpu_cache, self._gpu_observation_revision

    async def _sample_probe_cached(self) -> tuple[float | None, int]:
        now = time.monotonic()
        if self._probe_cached_at and now - self._probe_cached_at < self.config.auxiliary_interval:
            return self._probe_cache, self._probe_observation_revision
        self._probe_cache = await self._probe_latency()
        self._probe_cached_at = time.monotonic()
        self._probe_observation_revision += 1
        return self._probe_cache, self._probe_observation_revision

    async def _probe_latency(self) -> float | None:
        if not self.config.probe_host:
            return None

        started = time.perf_counter()
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self.config.probe_timeout):
                _, writer = await asyncio.open_connection(
                    self.config.probe_host,
                    self.config.probe_port,
                )
            return (time.perf_counter() - started) * 1_000
        except (OSError, TimeoutError):
            return None
        finally:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(1):
                        await writer.wait_closed()
                except (OSError, TimeoutError):
                    pass


class MetricAlertEngine:
    """Debounce threshold alerts and emit recovery/reminder transitions."""

    def __init__(self, config: OperationsConfig) -> None:
        self.config = config
        self.states: dict[str, _MetricState] = {}

    def evaluate(self, snapshot: SystemSnapshot, now: float | None = None) -> list[MetricTransition]:
        checked_at = time.monotonic() if now is None else now
        transitions: list[MetricTransition] = []
        readings = metric_readings(snapshot, self.config)
        seen_keys = {reading.key for reading in readings}
        for reading in readings:
            state = self.states.setdefault(reading.key, _MetricState())
            if (
                reading.observation_revision is not None
                and state.last_observation_revision == reading.observation_revision
            ):
                continue
            evaluation_interval = (
                self.config.auxiliary_interval
                if reading.observation_revision is not None
                else self.config.sample_interval
            )
            if (
                state.last_evaluated_at is not None
                and checked_at - state.last_evaluated_at > evaluation_interval * 2.5
            ):
                state.breach_streak = 0
                state.recovery_streak = 0
            state.last_evaluated_at = checked_at
            state.last_reading = reading
            state.last_observation_revision = reading.observation_revision
            breached = reading.value >= reading.threshold
            recovered = reading.value <= reading.threshold * self.config.recovery_ratio

            if not state.active:
                state.recovery_streak = 0
                state.breach_streak = state.breach_streak + 1 if breached else 0
                if state.breach_streak >= self.config.trigger_samples:
                    state.active = True
                    state.last_alert_at = checked_at
                    transitions.append(MetricTransition("raised", reading))
                continue

            if breached:
                state.recovery_streak = 0
                if checked_at - state.last_alert_at >= self.config.alert_cooldown_seconds:
                    state.last_alert_at = checked_at
                    transitions.append(MetricTransition("reminder", reading))
                continue

            state.recovery_streak = state.recovery_streak + 1 if recovered else 0
            if state.recovery_streak >= self.config.recovery_samples:
                state.active = False
                state.breach_streak = 0
                state.recovery_streak = 0
                transitions.append(MetricTransition("resolved", reading))

        for key, state in self.states.items():
            if key in seen_keys:
                continue
            observation_revision = _metric_observation_revision(snapshot, key)
            if (
                observation_revision is not None
                and state.last_observation_revision == observation_revision
            ):
                continue
            evaluation_interval = (
                self.config.auxiliary_interval
                if observation_revision is not None
                else self.config.sample_interval
            )
            if (
                state.last_evaluated_at is not None
                and checked_at - state.last_evaluated_at > evaluation_interval * 2.5
            ):
                state.recovery_streak = 0
            state.last_evaluated_at = checked_at
            state.last_observation_revision = observation_revision
            state.breach_streak = 0
            if not state.active or state.last_reading is None:
                continue
            state.recovery_streak += 1
            if state.recovery_streak >= self.config.recovery_samples:
                state.active = False
                state.recovery_streak = 0
                transitions.append(MetricTransition("resolved", state.last_reading))

        return transitions

    @property
    def active_keys(self) -> frozenset[str]:
        return frozenset(key for key, state in self.states.items() if state.active)


class NetworkMitigationController:
    """Control automatic GIF suppression using network-only thresholds."""

    def __init__(self, config: OperationsConfig) -> None:
        self.config = config
        self.forced = False
        self.auto_active = False
        self.breach_streak = 0
        self.recovery_streak = 0
        self.active_since = 0.0
        self.last_evaluated_at: float | None = None

    @property
    def effective(self) -> bool:
        return self.forced or self.auto_active

    @property
    def mode(self) -> str:
        return "manual" if self.forced else "auto"

    def force_enable(self, reason: str = "administrator request") -> MitigationTransition | None:
        was_effective = self.effective
        self._reset_streaks()
        self.forced = True
        if was_effective:
            return None
        self.active_since = time.monotonic()
        return MitigationTransition(True, reason)

    def return_to_automatic(self, snapshot: SystemSnapshot | None) -> MitigationTransition | None:
        was_effective = self.effective
        self._reset_streaks()
        self.forced = False
        if snapshot is not None and self._network_breached(snapshot):
            self.auto_active = True
            if not self.active_since:
                self.active_since = time.monotonic()
        elif not self.auto_active:
            self.active_since = 0.0
        if was_effective and not self.effective:
            return MitigationTransition(False, "administrator returned protection to automatic mode")
        return None

    def evaluate(
        self,
        snapshot: SystemSnapshot,
        now: float | None = None,
    ) -> MitigationTransition | None:
        checked_at = time.monotonic() if now is None else now
        if (
            self.last_evaluated_at is not None
            and checked_at - self.last_evaluated_at > self.config.sample_interval * 2.5
        ):
            self.breach_streak = 0
            self.recovery_streak = 0
        self.last_evaluated_at = checked_at
        if self.forced:
            return None

        if not self.auto_active:
            self.recovery_streak = 0
            self.breach_streak = self.breach_streak + 1 if self._network_breached(snapshot) else 0
            if self.breach_streak < self.config.trigger_samples:
                return None
            self.auto_active = True
            self.active_since = checked_at
            return MitigationTransition(True, self._network_reason(snapshot))

        if self._network_breached(snapshot):
            self.recovery_streak = 0
            return None

        if not self._network_recovered(snapshot):
            self.recovery_streak = 0
            return None

        self.recovery_streak += 1
        cooldown_complete = checked_at - self.active_since >= self.config.mitigation_cooldown_seconds
        if not cooldown_complete or self.recovery_streak < self.config.recovery_samples:
            return None

        self.auto_active = False
        self.breach_streak = 0
        self.recovery_streak = 0
        self.active_since = 0.0
        return MitigationTransition(False, "network traffic remained below the recovery boundary")

    def _reset_streaks(self) -> None:
        self.breach_streak = 0
        self.recovery_streak = 0
        self.last_evaluated_at = None

    def _network_breached(self, snapshot: SystemSnapshot) -> bool:
        return (
            snapshot.network_download_mbps >= self.config.network_download_critical_mbps
            or snapshot.network_upload_mbps >= self.config.network_upload_critical_mbps
        )

    def _network_recovered(self, snapshot: SystemSnapshot) -> bool:
        return (
            snapshot.network_download_mbps
            <= self.config.network_download_critical_mbps * self.config.recovery_ratio
            and snapshot.network_upload_mbps
            <= self.config.network_upload_critical_mbps * self.config.recovery_ratio
        )

    def _network_reason(self, snapshot: SystemSnapshot) -> str:
        return (
            "network threshold exceeded "
            f"(download={snapshot.network_download_mbps:.1f} Mbps, "
            f"upload={snapshot.network_upload_mbps:.1f} Mbps)"
        )


def metric_readings(snapshot: SystemSnapshot, config: OperationsConfig) -> tuple[MetricReading, ...]:
    readings = [
        MetricReading("cpu", "CPU", snapshot.cpu_percent, config.cpu_critical_percent, "%"),
        MetricReading("memory", "Memory", snapshot.memory_percent, config.memory_critical_percent, "%"),
        MetricReading("disk", "Disk", snapshot.disk_percent, config.disk_critical_percent, "%"),
        MetricReading(
            "network_download",
            "Network download",
            snapshot.network_download_mbps,
            config.network_download_critical_mbps,
            "Mbps",
        ),
        MetricReading(
            "network_upload",
            "Network upload",
            snapshot.network_upload_mbps,
            config.network_upload_critical_mbps,
            "Mbps",
        ),
    ]
    if snapshot.gpu is not None:
        readings.append(
            MetricReading(
                "gpu",
                "GPU",
                snapshot.gpu.utilization_percent,
                config.gpu_critical_percent,
                "%",
                snapshot.gpu_observation_revision,
            )
        )
    if snapshot.gateway_latency_ms is not None:
        readings.append(
            MetricReading(
                "gateway_latency",
                "Discord gateway latency",
                snapshot.gateway_latency_ms,
                config.latency_critical_ms,
                "ms",
            )
        )
    if snapshot.probe_latency_ms is not None:
        readings.append(
            MetricReading(
                "probe_latency",
                "TCP probe latency",
                snapshot.probe_latency_ms,
                config.latency_critical_ms,
                "ms",
                snapshot.probe_observation_revision,
            )
        )
    if config.probe_enabled:
        readings.append(
            MetricReading(
                "probe_reachability",
                "TCP probe failure",
                1.0 if snapshot.probe_latency_ms is None else 0.0,
                0.5,
                "",
                snapshot.probe_observation_revision,
            )
        )
    return tuple(readings)


def _metric_observation_revision(snapshot: SystemSnapshot, key: str) -> int | None:
    if key == "gpu":
        return snapshot.gpu_observation_revision
    if key in {"probe_latency", "probe_reachability"}:
        return snapshot.probe_observation_revision
    return None
