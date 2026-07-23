from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import DevilBloxBot


alert_log = logging.getLogger("devilblox.alerts")


@dataclass(frozen=True, slots=True)
class ExceptionReport:
    error_id: str
    context: str
    exception_type: str
    message: str
    traceback_text: str
    fingerprint: str
    created_at: datetime
    guild_id: int | None = None


class DiscordExceptionReporter:
    """Bounded, deduplicated traceback delivery to the operations channel."""

    def __init__(self, bot: "DevilBloxBot", *, duplicate_window: float = 60.0) -> None:
        self.bot = bot
        self.duplicate_window = duplicate_window
        self.queue: asyncio.Queue[ExceptionReport] = asyncio.Queue(maxsize=100)
        self._worker: asyncio.Task | None = None
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._loop_thread_id = threading.get_ident() if self._loop is not None else None
        self._closing = False
        self._last_sent: dict[str, float] = {}
        self._sent_times: deque[float] = deque()
        self.dropped_reports = 0

    def enqueue_exception(
        self,
        context: str,
        error: BaseException,
        *,
        guild_id: int | None = None,
    ) -> str:
        report = build_exception_report(
            context,
            error,
            secrets_to_hide=self._secrets_to_hide(),
            guild_id=guild_id,
        )
        self._submit(report)
        return report.error_id

    def enqueue_log_record(self, record: logging.LogRecord) -> str | None:
        if record.levelno < logging.ERROR or record.name == alert_log.name:
            return None
        if getattr(record, "skip_discord_report", False):
            return None

        if record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
        else:
            error = RuntimeError(record.getMessage())
        context = f"logger={record.name}: {record.getMessage()}"
        guild_id = getattr(record, "guild_id", None)
        if not isinstance(guild_id, int):
            guild_id = None
        return self.enqueue_exception(context, error, guild_id=guild_id)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="devilblox-error-reporter")

    async def close(self) -> None:
        # The bot removes the logging handler before calling close. Give any
        # call_soon_threadsafe submissions already made by logging threads one
        # event-loop turn to enter the queue before closing the ingress gate.
        await asyncio.sleep(0)
        self._closing = True
        if self._worker is None:
            self._discard_pending_reports("Discord exception reporter never reached ready state")
            return
        try:
            async with asyncio.timeout(2):
                await self.queue.join()
        except TimeoutError:
            pass
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None
        self._discard_pending_reports("Discord exception reporter closed before delivery")

    def _discard_pending_reports(self, reason: str) -> None:
        discarded = 0
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self.queue.task_done()
                discarded += 1
        if discarded:
            self.dropped_reports += discarded
            alert_log.warning("%s; %s report(s) remain available in local logs", reason, discarded)

    def _enqueue(self, report: ExceptionReport) -> None:
        if self._closing:
            self.dropped_reports += 1
            return
        try:
            self.queue.put_nowait(report)
        except asyncio.QueueFull:
            self.dropped_reports += 1

    def _submit(self, report: ExceptionReport) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self.dropped_reports += 1
            return
        if threading.get_ident() == self._loop_thread_id:
            self._enqueue(report)
        else:
            loop.call_soon_threadsafe(self._enqueue, report)

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        while not self._closing or not self.queue.empty():
            report = await self.queue.get()
            try:
                await self._deliver(report)
            except asyncio.CancelledError:
                raise
            except Exception:
                alert_log.warning("Failed to deliver Discord traceback report", exc_info=True)
            finally:
                self.queue.task_done()

    async def _deliver(self, report: ExceptionReport) -> None:
        now = time.monotonic()
        self._last_sent = {
            fingerprint: sent_at
            for fingerprint, sent_at in self._last_sent.items()
            if sent_at > now - self.duplicate_window
        }
        previous = self._last_sent.get(report.fingerprint, 0.0)
        if now - previous < self.duplicate_window:
            return
        while self._sent_times and self._sent_times[0] <= now - 60:
            self._sent_times.popleft()
        if len(self._sent_times) >= 10:
            self.dropped_reports += 1
            return

        channels = await self._resolve_channels(report.guild_id)
        if not channels:
            return
        content = format_discord_report(report)
        sent = False
        for channel in channels:
            try:
                await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
                sent = True
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                alert_log.warning(
                    "Could not send traceback to operations channel: channel_id=%s",
                    channel.id,
                )
        if sent:
            self._last_sent[report.fingerprint] = now
            self._sent_times.append(now)

    async def _resolve_channels(self, guild_id: int | None) -> list[discord.abc.Messageable]:
        configured_id = self.bot.config.operations.channel_id
        if configured_id:
            channel = self.bot.get_channel(configured_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(configured_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    return []
            return [channel] if hasattr(channel, "send") else []

        if guild_id is None or self.bot.repos is None:
            return []
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return []
        try:
            settings = await self.bot.repos.settings.get(guild_id)
        except Exception:
            return []
        channel_id = settings["channels"].get("operations")
        channel = guild.get_channel(channel_id or 0)
        return [channel] if channel is not None and hasattr(channel, "send") else []

    def _secrets_to_hide(self) -> tuple[str, ...]:
        values = [
            self.bot.config.discord_token,
            self.bot.config.mongo.password,
            self.bot.config.mongo.uri,
        ]
        return tuple(value for value in values if value)


class DiscordTracebackHandler(logging.Handler):
    def __init__(self, reporter: DiscordExceptionReporter) -> None:
        super().__init__(level=logging.ERROR)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.reporter.enqueue_log_record(record)
        except Exception:
            self.handleError(record)


def build_exception_report(
    context: str,
    error: BaseException,
    *,
    secrets_to_hide: tuple[str, ...] = (),
    guild_id: int | None = None,
) -> ExceptionReport:
    trace = "".join(traceback.TracebackException.from_exception(error, capture_locals=False).format())
    message = str(error) or error.__class__.__name__
    context = _redact(context, secrets_to_hide)
    message = _redact(message, secrets_to_hide)
    trace = _redact(trace, secrets_to_hide)
    normalized_context = re.sub(r"\d{5,}", "#", context)
    normalized_message = re.sub(r"\d{5,}", "#", message)
    normalized_trace_line = re.sub(r"\d{5,}", "#", _last_trace_line(trace))
    fingerprint_source = (
        f"{normalized_context}\n{error.__class__.__qualname__}\n"
        f"{normalized_message}\n{normalized_trace_line}"
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8", "replace")).hexdigest()[:16]
    return ExceptionReport(
        error_id=f"ERR-{secrets.token_hex(4).upper()}",
        context=context[:500],
        exception_type=error.__class__.__qualname__,
        message=message[:500],
        traceback_text=trace[-8_000:],
        fingerprint=fingerprint,
        created_at=datetime.now(timezone.utc),
        guild_id=guild_id,
    )


def format_discord_report(report: ExceptionReport) -> str:
    trace_tail = report.traceback_text[-1_000:].replace("```", "'''" )
    content = (
        f"🚨 **DevilBlox traceback** · `{report.error_id}`\n"
        f"**Context:** {report.context}\n"
        f"**Error:** `{report.exception_type}` · {report.message}\n"
        f"```py\n{trace_tail}\n```"
    )
    return content[:1_990]


def _redact(text: str, secrets_to_hide: tuple[str, ...]) -> str:
    result = text
    for secret in secrets_to_hide:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = re.sub(r"(?i)(token|password|authorization)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", result)
    result = re.sub(r"(?i)(mongodb(?:\+srv)?://[^:/\s]+:)[^@\s]+@", r"\1[REDACTED]@", result)
    return result


def _last_trace_line(trace: str) -> str:
    lines = [line.strip() for line in trace.splitlines() if line.strip()]
    return lines[-1] if lines else ""
