from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.system_monitor import (
    MetricAlertEngine,
    MetricTransition,
    MetricsSampler,
    MitigationTransition,
    NetworkMitigationController,
    SystemSnapshot,
)
from utils.embeds import brand_embed
from utils.gifs import (
    begin_gif_recovery,
    end_gif_recovery,
    gif_delivery_status,
    set_gif_suppressed,
)
from utils.panels import (
    PanelCleanupResult,
    save_panel_location,
    strip_message_gifs,
    strip_saved_panel_gifs,
)


log = logging.getLogger(__name__)


@dataclass(slots=True)
class CleanupSummary:
    checked: int = 0
    changed: int = 0
    gif_attachments_removed: int = 0
    media_references_removed: int = 0
    failed: int = 0

    def add(self, result: PanelCleanupResult) -> None:
        self.checked += result.checked
        self.changed += result.changed
        self.gif_attachments_removed += result.gif_attachments_removed
        self.media_references_removed += result.media_references_removed
        self.failed += result.failed

    def describe(self) -> str:
        return (
            f"패널 {self.checked}개 확인 · {self.changed}개 변경 · "
            f"첨부 {self.gif_attachments_removed}개/미디어 참조 "
            f"{self.media_references_removed}개 제거 · 실패 {self.failed}개"
        )


class OperationsPanelView(discord.ui.View):
    def __init__(self, cog: "OperationsCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is not None and permissions.administrator:
            return True
        await interaction.response.send_message("관리자만 서버 제어 버튼을 사용할 수 있습니다.", ephemeral=True)
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        error_id = self.cog.bot.error_reporter.enqueue_exception(
            f"operations panel component={getattr(item, 'custom_id', None)} guild={interaction.guild_id}",
            error,
            guild_id=interaction.guild_id,
        )
        log.error(
            "Operations panel callback failed: error_id=%s",
            error_id,
            exc_info=(type(error), error, error.__traceback__),
            extra={"skip_discord_report": True},
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"오류가 발생했습니다: `{error_id}`", ephemeral=True)
            else:
                await interaction.response.send_message(f"오류가 발생했습니다: `{error_id}`", ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="즉시 새로고침",
        style=discord.ButtonStyle.secondary,
        custom_id="devilblox:operations:refresh",
    )
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = await self.cog.collect_now(force=True)
        if interaction.message is not None:
            await interaction.message.edit(embed=self.cog.build_panel_embed(snapshot), view=self)
        await interaction.followup.send("최신 시스템 지표로 갱신했습니다.", ephemeral=True)

    @discord.ui.button(
        label="비상 절전 ON",
        style=discord.ButtonStyle.danger,
        custom_id="devilblox:operations:force-mitigation",
    )
    async def force_mitigation(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        reason = f"administrator {interaction.user.id} enabled emergency mode"
        transition = self.cog.mitigation.force_enable(reason) or MitigationTransition(
            self.cog.mitigation.effective,
            reason,
        )
        await self.cog.apply_mitigation_transition(transition)
        await interaction.followup.send(
            "비상 절전 모드가 활성화되었습니다. 패널 GIF 정리는 백그라운드에서 진행합니다.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="자동 모드",
        style=discord.ButtonStyle.success,
        custom_id="devilblox:operations:auto-mode",
    )
    async def automatic_mode(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            snapshot = await self.cog.collect_fresh_for_safety()
        except Exception as error:
            error_id = self.cog.bot.error_reporter.enqueue_exception(
                "fresh sample before automatic protection mode",
                error,
                guild_id=interaction.guild_id,
            )
            await interaction.followup.send(
                f"최신 상태를 확인하지 못해 절전 상태를 유지합니다. 오류 ID: `{error_id}`",
                ephemeral=True,
            )
            return
        reason = f"administrator {interaction.user.id} returned protection to automatic mode"
        transition = self.cog.mitigation.return_to_automatic(snapshot) or MitigationTransition(
            self.cog.mitigation.effective,
            reason,
        )
        await self.cog.apply_mitigation_transition(transition)
        state = "절전 유지" if self.cog.mitigation.effective else "정상"
        await interaction.followup.send(
            f"자동 판단 모드로 전환했습니다. 현재 상태: **{state}**",
            ephemeral=True,
        )

    @discord.ui.button(
        label="GIF 즉시 정리",
        style=discord.ButtonStyle.primary,
        custom_id="devilblox:operations:cleanup-gifs",
    )
    async def cleanup_gifs(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        started = self.cog.start_manual_cleanup(
            f"administrator {interaction.user.id} requested GIF cleanup"
        )
        message = (
            "GIF 정리를 백그라운드에서 시작했습니다. 완료 결과는 운영 채널로 전송합니다."
            if started
            else "이미 GIF 정리를 진행 중입니다."
        )
        await interaction.followup.send(message, ephemeral=True)


class OperationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = bot.config.operations
        self.sampler = MetricsSampler(self.config)
        self.alert_engine = MetricAlertEngine(self.config)
        self.mitigation = NetworkMitigationController(self.config)
        self.latest_snapshot: SystemSnapshot | None = None
        self.last_cleanup = CleanupSummary()
        self.last_action = "아직 실행된 완화 조치가 없습니다."
        self._sample_lock = asyncio.Lock()
        self._sample_task: asyncio.Task[SystemSnapshot] | None = None
        self._cleanup_lock = asyncio.Lock()
        self._panel_lock = asyncio.Lock()
        self._mitigation_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._manual_cleanup_task: asyncio.Task | None = None
        self._last_panel_update = 0.0
        self._configured_panel_ensured = False
        self.panel_view = OperationsPanelView(self)
        self.bot.add_view(self.panel_view)

    async def cog_load(self) -> None:
        restored = await self.restore_mitigation_state()
        if restored:
            self._cleanup_task = asyncio.create_task(
                self.finish_mitigation_cleanup("restored persisted emergency state"),
                name="devilblox-restored-gif-cleanup",
            )
        if not self.config.enabled:
            return
        self.monitor_loop.change_interval(seconds=self.config.sample_interval)
        self.monitor_loop.start()

    async def cog_unload(self) -> None:
        self.monitor_loop.cancel()
        pending_tasks = [self._sample_task, self._cleanup_task, self._manual_cleanup_task]
        for task in pending_tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in pending_tasks:
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Operations background task failed during cog unload")

    @tasks.loop(seconds=5)
    async def monitor_loop(self) -> None:
        try:
            snapshot = await self.collect_now(force=True)
            transitions = self.alert_engine.evaluate(snapshot)
            mitigation_transition = self.mitigation.evaluate(snapshot)
            if mitigation_transition is not None:
                await self.apply_mitigation_transition(mitigation_transition)
            if transitions:
                await self.notify_metric_transitions(transitions)

            if not self._configured_panel_ensured:
                self._configured_panel_ensured = await self.ensure_configured_panel()

            now = time.monotonic()
            if now - self._last_panel_update >= self.config.panel_interval:
                await self.refresh_saved_panels()
                self._last_panel_update = now
        except Exception:
            log.exception("Operations monitoring iteration failed")

    @monitor_loop.before_loop
    async def before_monitor_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def collect_now(self, *, force: bool = False) -> SystemSnapshot:
        if (
            not force
            and self.latest_snapshot is not None
            and (time.time() - self.latest_snapshot.captured_at.timestamp())
            <= self.config.sample_interval
        ):
            return self.latest_snapshot
        async with self._sample_lock:
            if (
                not force
                and self.latest_snapshot is not None
                and (time.time() - self.latest_snapshot.captured_at.timestamp())
                <= self.config.sample_interval
            ):
                return self.latest_snapshot
            task = self._sample_task
            if task is None or task.done():
                task = asyncio.create_task(
                    self._collect_sample(),
                    name="devilblox-system-metrics-sample",
                )
                task.add_done_callback(self._consume_sample_task_exception)
                self._sample_task = task
        return await asyncio.shield(task)

    async def collect_fresh_for_safety(self) -> SystemSnapshot:
        timeout = max(5.0, self.config.probe_timeout + 2.0)
        async with asyncio.timeout(timeout):
            return await self.collect_now(force=True)

    async def _collect_sample(self) -> SystemSnapshot:
        latency = self.bot.latency
        gateway_latency = latency if math.isfinite(latency) and latency >= 0 else None
        snapshot = await self.sampler.sample(gateway_latency)
        self.latest_snapshot = snapshot
        return snapshot

    @staticmethod
    def _consume_sample_task_exception(task: asyncio.Task[SystemSnapshot]) -> None:
        if task.cancelled():
            return
        task.exception()

    async def apply_mitigation_transition(self, transition: MitigationTransition) -> None:
        async with self._mitigation_lock:
            desired = self.mitigation.effective
            if transition.enabled != desired:
                transition = MitigationTransition(
                    desired,
                    f"state reconciliation after: {transition.reason}",
                )
            if transition.enabled:
                end_gif_recovery()
            changed = set_gif_suppressed(transition.enabled, transition.reason)
            if not transition.enabled and changed:
                begin_gif_recovery(self.bot.config.gif_recovery_upload_interval)
            try:
                await self.bot.repos.operations.set_mitigation(
                    enabled=transition.enabled,
                    forced=self.mitigation.forced,
                    reason=transition.reason,
                )
            except Exception:
                log.exception("Failed to persist operations mitigation state")

            if transition.enabled:
                self.last_action = f"GIF 절전 활성화: {transition.reason}"
                level = logging.CRITICAL
                icon = "🚨"
                state = "활성화"
            else:
                self.last_action = f"GIF 절전 해제: {transition.reason}"
                level = logging.WARNING
                icon = "✅"
                state = "해제"

            log.log(
                level,
                "GIF traffic mitigation changed: enabled=%s changed=%s reason=%s cleanup=%s",
                transition.enabled,
                changed,
                transition.reason,
                self.last_cleanup.describe(),
                extra={"skip_discord_report": True},
            )
            if changed:
                await self.broadcast_content(
                    f"{icon} **GIF 트래픽 절전 모드 {state}**\n사유: `{transition.reason}`\n"
                    + (
                        "신규 GIF를 먼저 차단했으며 기존 패널은 백그라운드에서 정리합니다."
                        if transition.enabled
                        else "트래픽이 안전 구간을 유지해 자동 복구합니다."
                    )
                )

            if transition.enabled and changed:
                if self._cleanup_task is None or self._cleanup_task.done():
                    self._cleanup_task = asyncio.create_task(
                        self.finish_mitigation_cleanup(transition.reason),
                        name="devilblox-gif-cleanup",
                    )
            elif not transition.enabled and self._cleanup_task is not None:
                self._cleanup_task.cancel()
                self._cleanup_task = None
            await self.refresh_saved_panels()

    async def restore_mitigation_state(self) -> bool:
        try:
            state = await self.bot.repos.operations.get()
        except Exception:
            log.exception("Failed to restore operations mitigation state")
            return False
        if not state or not state.get("enabled"):
            set_gif_suppressed(False)
            return False
        self.mitigation.forced = bool(state.get("forced"))
        self.mitigation.auto_active = not self.mitigation.forced
        self.mitigation.active_since = time.monotonic()
        reason = str(state.get("reason") or "restored persisted emergency state")
        set_gif_suppressed(True, reason)
        self.last_action = f"재시작 후 GIF 절전 상태 복원: {reason}"
        return True

    def start_manual_cleanup(self, reason: str) -> bool:
        cleanup_running = self._cleanup_task is not None and not self._cleanup_task.done()
        manual_running = (
            self._manual_cleanup_task is not None and not self._manual_cleanup_task.done()
        )
        if cleanup_running or manual_running or self._cleanup_lock.locked():
            return False
        self._manual_cleanup_task = asyncio.create_task(
            self.finish_manual_cleanup(reason),
            name="devilblox-manual-gif-cleanup",
        )
        return True

    async def finish_manual_cleanup(self, reason: str) -> None:
        try:
            await self.bot.wait_until_ready()
            summary = await self.cleanup_all_panel_gifs()
            self.last_cleanup = summary
            self.last_action = f"수동 GIF 정리: {reason} · {summary.describe()}"
            await self.broadcast_content(f"🧹 **수동 GIF 정리 완료**\n{summary.describe()}")
            await self.refresh_saved_panels()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background manual GIF cleanup failed")
        finally:
            if self._manual_cleanup_task is asyncio.current_task():
                self._manual_cleanup_task = None

    async def finish_mitigation_cleanup(self, reason: str) -> None:
        try:
            await self.bot.wait_until_ready()
            first = await self.cleanup_all_panel_gifs()
            await asyncio.sleep(2)
            second = await self.cleanup_all_panel_gifs()
            first.checked += second.checked
            first.changed += second.changed
            first.gif_attachments_removed += second.gif_attachments_removed
            first.media_references_removed += second.media_references_removed
            first.failed += second.failed
            self.last_cleanup = first
            self.last_action = f"GIF 절전 활성화: {reason} · {first.describe()}"
            await self.broadcast_content(f"🧹 **GIF 패널 정리 완료**\n{first.describe()}")
            await self.refresh_saved_panels()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background GIF cleanup failed")

    async def cleanup_all_panel_gifs(self) -> CleanupSummary:
        async with self._cleanup_lock:
            summary = CleanupSummary()
            if self.bot.repos is None:
                summary.failed = 1
                self.last_cleanup = summary
                return summary
            for guild in self.bot.guilds:
                async def render_layout(
                    channel_key: str | None,
                    _: discord.Message,
                ) -> discord.ui.LayoutView | None:
                    vending_cog = self.bot.get_cog("VendingArchiveCog")
                    if vending_cog is None or not hasattr(vending_cog, "build_tracked_panel_view"):
                        return None
                    return await vending_cog.build_tracked_panel_view(
                        guild.id,
                        channel_key,
                        None,
                    )

                result = await strip_saved_panel_gifs(
                    self.bot.repos,
                    guild,
                    layout_renderer=render_layout,
                )
                summary.add(result)
                await self.cleanup_open_ticket_gifs(guild, summary)
            self.last_cleanup = summary
            return summary

    async def cleanup_open_ticket_gifs(
        self,
        guild: discord.Guild,
        summary: CleanupSummary,
    ) -> None:
        tickets = await self.bot.repos.tickets.list_open_for_media_cleanup(guild.id)
        for ticket in tickets:
            channel = guild.get_channel(ticket.get("channel_id") or 0)
            if channel is None or not isinstance(channel, discord.TextChannel):
                summary.failed += 1
                continue

            messages: list[discord.Message] = []
            message_id = ticket.get("panel_message_id")
            try:
                if message_id:
                    messages.append(await channel.fetch_message(message_id))
                else:
                    async for candidate in channel.history(limit=20, oldest_first=True):
                        if self.bot.user is not None and candidate.author.id == self.bot.user.id:
                            messages.append(candidate)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                summary.failed += 1
                continue

            for message in messages:
                summary.checked += 1
                try:
                    removed_attachments, removed_references = await strip_message_gifs(message)
                except discord.HTTPException:
                    summary.failed += 1
                    continue
                if removed_attachments or removed_references:
                    summary.changed += 1
                    summary.gif_attachments_removed += removed_attachments
                    summary.media_references_removed += removed_references
                    if not message_id:
                        await self.bot.repos.tickets.set_panel_message(
                            guild.id,
                            channel.id,
                            message.id,
                        )
                    break

    async def notify_metric_transitions(self, transitions: list[MetricTransition]) -> None:
        lines = []
        labels = {"raised": "임계치 초과", "reminder": "초과 지속", "resolved": "정상 복구"}
        for transition in transitions:
            reading = transition.reading
            icon = "✅" if transition.event == "resolved" else "⚠️"
            lines.append(
                f"{icon} **{reading.label} {labels[transition.event]}:** "
                f"{reading.value:.1f}{reading.unit} / 기준 {reading.threshold:.1f}{reading.unit}"
            )
        await self.broadcast_content("**서버 상태 변화**\n" + "\n".join(lines))

    async def broadcast_content(self, content: str) -> None:
        for channel in await self.operations_channels():
            try:
                await channel.send(content[:1_990], allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                log.warning("Failed to send operations alert: channel_id=%s", channel.id)

    async def operations_channels(self) -> list[discord.abc.Messageable]:
        configured_id = self.config.channel_id
        if configured_id:
            channel = self.bot.get_channel(configured_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(configured_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    return []
            return [channel] if hasattr(channel, "send") else []

        channels: list[discord.abc.Messageable] = []
        seen: set[int] = set()
        for guild in self.bot.guilds:
            settings = await self.bot.repos.settings.get(guild.id)
            channel_id = settings["channels"].get("operations")
            channel = guild.get_channel(channel_id or 0)
            if channel is not None and hasattr(channel, "send") and channel.id not in seen:
                seen.add(channel.id)
                channels.append(channel)
        return channels

    async def ensure_configured_panel(self) -> bool:
        if not self.config.channel_id or self.latest_snapshot is None:
            return not self.config.channel_id
        channel = self.bot.get_channel(self.config.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.config.channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return False
        if not isinstance(channel, discord.TextChannel):
            return False
        await self.ensure_panel(channel.guild, channel)
        return True

    async def ensure_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
    ) -> discord.Message:
        settings = await self.bot.repos.settings.get(guild.id)
        message_id = settings["meta"].get("operations_panel_message_id")
        saved_channel_id = settings["channels"].get("operations")
        if message_id and saved_channel_id == channel.id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=self.build_panel_embed(self.latest_snapshot), view=self.panel_view)
                return message
            except discord.NotFound:
                pass

        message = await channel.send(
            embed=self.build_panel_embed(self.latest_snapshot),
            view=self.panel_view,
        )
        await save_panel_location(
            self.bot.repos,
            guild.id,
            "operations",
            "operations_panel_message_id",
            channel.id,
            message.id,
        )
        return message

    async def refresh_saved_panels(self) -> None:
        if self.latest_snapshot is None or self.bot.repos is None:
            return
        async with self._panel_lock:
            embed = self.build_panel_embed(self.latest_snapshot)
            for guild in self.bot.guilds:
                try:
                    settings = await self.bot.repos.settings.get(guild.id)
                    channel_id = settings["channels"].get("operations")
                    message_id = settings["meta"].get("operations_panel_message_id")
                    channel = guild.get_channel(channel_id or 0)
                    if channel is None or not message_id or not hasattr(channel, "get_partial_message"):
                        continue
                    message = channel.get_partial_message(message_id)
                    await message.edit(embed=embed, view=self.panel_view)
                except discord.NotFound:
                    await self.bot.repos.settings.set_value(
                        guild.id,
                        "meta",
                        "operations_panel_message_id",
                        None,
                    )
                except discord.HTTPException:
                    continue

    def build_panel_embed(self, snapshot: SystemSnapshot | None) -> discord.Embed:
        media = gif_delivery_status()
        active_alerts = self.alert_engine.active_keys
        if media.suppressed:
            color = 0xE5484D
            status_text = "🚨 비상 절전 모드"
        elif active_alerts:
            color = 0xF39C12
            status_text = "⚠️ 임계치 경고"
        else:
            color = 0x2ECC71
            status_text = "✅ 정상"

        embed = discord.Embed(
            title="DEVILBLOX SERVER CONTROL",
            description=f"**현재 상태:** {status_text}",
            color=color,
        )
        if snapshot is None:
            embed.description += "\n첫 시스템 샘플을 수집하는 중입니다."
            return brand_embed(embed)

        embed.add_field(
            name="🖥️ 호스트",
            value=(
                f"CPU `{snapshot.cpu_percent:5.1f}%` {_bar(snapshot.cpu_percent)}\n"
                f"RAM `{_format_bytes(snapshot.memory_used_bytes)} / {_format_bytes(snapshot.memory_total_bytes)}` "
                f"(`{snapshot.memory_percent:.1f}%`)\n"
                f"Disk `{_format_bytes(snapshot.disk_used_bytes)} / {_format_bytes(snapshot.disk_total_bytes)}` "
                f"(`{snapshot.disk_percent:.1f}%`)\n"
                f"Bot process `CPU {snapshot.process_cpu_percent:.1f}% · RAM {_format_bytes(snapshot.process_memory_bytes)}`"
            ),
            inline=False,
        )

        if snapshot.gpu is None:
            gpu_text = "`nvidia-smi 미지원 또는 GPU 없음`"
        else:
            temperature = (
                f" · {snapshot.gpu.temperature_celsius:.0f}°C"
                if snapshot.gpu.temperature_celsius is not None
                else ""
            )
            gpu_text = (
                f"사용률 `{snapshot.gpu.utilization_percent:.1f}%` {_bar(snapshot.gpu.utilization_percent)}\n"
                f"VRAM `{_format_bytes(snapshot.gpu.memory_used_bytes)} / "
                f"{_format_bytes(snapshot.gpu.memory_total_bytes)}`{temperature} · "
                f"GPU `{snapshot.gpu.device_count}`개"
            )
        embed.add_field(name="🎮 GPU", value=gpu_text, inline=False)

        embed.add_field(
            name="🌐 네트워크",
            value=(
                f"수신 `↓ {snapshot.network_download_mbps:.2f} Mbps` "
                f"(기준 `{self.config.network_download_critical_mbps:.1f}`)\n"
                f"송신 `↑ {snapshot.network_upload_mbps:.2f} Mbps` "
                f"(기준 `{self.config.network_upload_critical_mbps:.1f}`)\n"
                f"누적 `↓ {_format_bytes(snapshot.network_bytes_received)} · "
                f"↑ {_format_bytes(snapshot.network_bytes_sent)}`\n"
                f"Discord `{_format_latency(snapshot.gateway_latency_ms)}` · "
                f"TCP probe `{_format_latency(snapshot.probe_latency_ms)}`"
            ),
            inline=False,
        )

        source = media.configured_mode
        if media.configured_mode == "auto":
            source += f" → {('cdn' if media.cdn_base_url else 'local')}"
        embed.add_field(
            name="🛡️ 트래픽 보호",
            value=(
                f"판단 모드 `{self.mitigation.mode}` · GIF 상태 `{media.effective_mode}`\n"
                f"전달 방식 `{source}` · 로컬 `{media.local_variant}` · "
                f"자동 회전 `{'ON' if media.rotation_enabled else 'OFF'}`\n"
                f"순차 복구 `{'진행 중' if media.recovering else '대기 없음'}` · "
                f"진입 연속값 `{self.mitigation.breach_streak}/{self.config.trigger_samples}` · "
                f"복구 연속값 `{self.mitigation.recovery_streak}/{self.config.recovery_samples}`\n"
                f"최근 조치: {self.last_action[:600]}"
            ),
            inline=False,
        )

        captured_timestamp = int(snapshot.captured_at.timestamp())
        embed.set_footer(
            text=(
                f"Host uptime {_format_duration(snapshot.uptime_seconds)} · "
                f"sample {captured_timestamp} · 임계치 연속 초과 후 동작 / 낮은 복구선으로 히스테리시스"
            )
        )
        return brand_embed(embed)

    @app_commands.command(
        name="서버관리패널",
        description="현재 채널에 서버 상태 및 비상 제어 패널을 설치합니다.",
    )
    @app_commands.default_permissions(administrator=True)
    async def operations_panel(self, interaction: discord.Interaction) -> None:
        if not getattr(interaction.user, "guild_permissions", discord.Permissions.none()).administrator:
            await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("서버의 텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = self.latest_snapshot or await self.collect_now(force=True)
        self.latest_snapshot = snapshot
        message = await self.ensure_panel(interaction.guild, interaction.channel)
        await interaction.followup.send(f"관리 패널을 설치했습니다: {message.jump_url}", ephemeral=True)

    @app_commands.command(name="서버상태", description="현재 서버 자원과 네트워크 상태를 즉시 확인합니다.")
    @app_commands.default_permissions(administrator=True)
    async def operations_status(self, interaction: discord.Interaction) -> None:
        if not getattr(interaction.user, "guild_permissions", discord.Permissions.none()).administrator:
            await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = await self.collect_now(force=True)
        await interaction.followup.send(embed=self.build_panel_embed(snapshot), ephemeral=True)


def _format_bytes(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _format_latency(value: float | None) -> str:
    return "측정 실패" if value is None else f"{value:.0f} ms"


def _format_duration(seconds: float) -> str:
    days, remainder = divmod(int(seconds), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    return f"{days}d {hours}h {minutes}m"


def _bar(percent: float, width: int = 10) -> str:
    filled = round(max(0.0, min(percent, 100.0)) / 100 * width)
    return "`" + "█" * filled + "░" * (width - filled) + "`"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OperationsCog(bot))
