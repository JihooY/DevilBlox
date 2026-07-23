from __future__ import annotations

import asyncio
import logging
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import runtime
from core.bot import DevilBloxBot
from core.error_reporting import DiscordExceptionReporter, build_exception_report


class RuntimeFatalTests(unittest.IsolatedAsyncioTestCase):
    async def test_fatal_is_logged_and_enqueued_before_close(self) -> None:
        events: list[str] = []

        class Reporter:
            def enqueue_exception(self, context, error, *, guild_id=None):
                events.append("enqueue")
                self.context = context
                self.error = error
                self.guild_id = guild_id
                return "ERR-TEST"

        class Bot:
            def __init__(self) -> None:
                self.error_reporter = Reporter()

            async def start(self, token: str) -> None:
                events.append("start")
                raise RuntimeError("startup failed")

            async def close(self) -> None:
                events.append("close")

        bot = Bot()
        config = SimpleNamespace(
            discord_token="token",
            log_level="INFO",
            discord_log_level="WARNING",
            log_file="unused.log",
            log_max_bytes=1,
            log_backup_count=1,
        )

        with (
            patch.object(runtime.AppConfig, "from_env", return_value=config),
            patch.object(runtime, "configure_logging"),
            patch.object(runtime, "_print_runtime_plan"),
            patch.object(runtime, "DevilBloxBot", return_value=bot),
            self.assertLogs("devilblox", level=logging.CRITICAL) as captured,
        ):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                await runtime.async_main()

        self.assertEqual(events, ["start", "enqueue", "close"])
        self.assertEqual(len(captured.records), 1)
        self.assertIn("ERR-TEST", captured.records[0].getMessage())


class ReporterRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_guild_report_only_resolves_that_guild_channel(self) -> None:
        channel_one = SimpleNamespace(id=101, send=lambda *args, **kwargs: None)
        channel_two = SimpleNamespace(id=202, send=lambda *args, **kwargs: None)

        class Guild:
            def __init__(self, guild_id: int, channel) -> None:
                self.id = guild_id
                self.channel = channel

            def get_channel(self, channel_id: int):
                return self.channel if channel_id == self.channel.id else None

        guilds = {1: Guild(1, channel_one), 2: Guild(2, channel_two)}

        class Settings:
            def __init__(self) -> None:
                self.calls: list[int] = []

            async def get(self, guild_id: int):
                self.calls.append(guild_id)
                return {"channels": {"operations": guilds[guild_id].channel.id}}

        settings = Settings()
        bot = SimpleNamespace(
            config=SimpleNamespace(
                discord_token="token",
                mongo=SimpleNamespace(password=None, uri=None),
                operations=SimpleNamespace(channel_id=None),
            ),
            repos=SimpleNamespace(settings=settings),
            get_guild=lambda guild_id: guilds.get(guild_id),
            get_channel=lambda channel_id: None,
        )
        reporter = DiscordExceptionReporter(bot)

        channels = await reporter._resolve_channels(1)

        self.assertEqual(channels, [channel_one])
        self.assertEqual(settings.calls, [1])
        self.assertNotIn(channel_two, channels)

    async def test_global_report_is_not_broadcast_without_global_channel(self) -> None:
        settings = SimpleNamespace(get=unittest.mock.AsyncMock())
        bot = SimpleNamespace(
            config=SimpleNamespace(
                discord_token="token",
                mongo=SimpleNamespace(password=None, uri=None),
                operations=SimpleNamespace(channel_id=None),
            ),
            repos=SimpleNamespace(settings=settings),
            get_guild=lambda guild_id: None,
            get_channel=lambda channel_id: None,
        )
        reporter = DiscordExceptionReporter(bot)

        self.assertEqual(await reporter._resolve_channels(None), [])
        settings.get.assert_not_awaited()

    async def test_explicit_global_channel_receives_global_report(self) -> None:
        global_channel = SimpleNamespace(id=999, send=lambda *args, **kwargs: None)
        bot = SimpleNamespace(
            config=SimpleNamespace(
                discord_token="token",
                mongo=SimpleNamespace(password=None, uri=None),
                operations=SimpleNamespace(channel_id=999),
            ),
            repos=None,
            get_channel=lambda channel_id: global_channel if channel_id == 999 else None,
        )
        reporter = DiscordExceptionReporter(bot)

        self.assertEqual(await reporter._resolve_channels(None), [global_channel])


class ReporterFingerprintTests(unittest.TestCase):
    def _report(self, identifier: int):
        try:
            raise RuntimeError(f"request {identifier} failed")
        except RuntimeError as error:
            return build_exception_report(
                f"guild={identifier}",
                error,
                guild_id=identifier,
            )

    def test_numeric_ids_in_last_trace_line_do_not_split_fingerprint(self) -> None:
        first = self._report(123456789)
        second = self._report(987654321)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.guild_id, 123456789)
        self.assertEqual(second.guild_id, 987654321)


class ReporterCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_accepts_already_scheduled_thread_submission(self) -> None:
        bot = SimpleNamespace(
            config=SimpleNamespace(
                discord_token="token",
                mongo=SimpleNamespace(password=None, uri=None),
                operations=SimpleNamespace(channel_id=None),
            ),
            repos=None,
        )
        reporter = DiscordExceptionReporter(bot)
        closing_states: list[bool] = []
        original_enqueue = reporter._enqueue

        def recording_enqueue(report) -> None:
            closing_states.append(reporter._closing)
            original_enqueue(report)

        reporter._enqueue = recording_enqueue
        thread = threading.Thread(
            target=lambda: reporter.enqueue_exception("threaded", RuntimeError("boom"))
        )
        thread.start()
        thread.join()

        with self.assertLogs("devilblox.alerts", level=logging.WARNING):
            await reporter.close()

        self.assertEqual(closing_states, [False])
        self.assertEqual(reporter.queue.qsize(), 0)
        self.assertEqual(reporter.dropped_reports, 1)


class BotCloseTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, reporter_close, discord_close, mongo_close) -> DevilBloxBot:
        bot = object.__new__(DevilBloxBot)
        bot._devilblox_close_task = None
        bot._loop_handler_installed = False
        bot._previous_loop_exception_handler = None
        bot._traceback_handler = logging.NullHandler()
        bot.error_reporter = SimpleNamespace(close=reporter_close)
        bot._close_discord_client = discord_close
        bot.mongo_runtime = SimpleNamespace(close=mongo_close)
        return bot

    async def test_concurrent_callers_share_cleanup_and_cancellation_does_not_stop_it(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def reporter_close() -> None:
            calls.append("reporter")
            entered.set()
            await release.wait()

        async def discord_close() -> None:
            calls.append("discord")

        async def mongo_close() -> None:
            calls.append("mongo")

        bot = self._bot(reporter_close, discord_close, mongo_close)
        first = asyncio.create_task(bot.close())
        await entered.wait()
        shared_cleanup = bot._devilblox_close_task
        second = asyncio.create_task(bot.close())

        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertIs(bot._devilblox_close_task, shared_cleanup)
        self.assertFalse(shared_cleanup.done())

        release.set()
        await second

        self.assertEqual(calls, ["reporter", "discord", "mongo"])
        self.assertTrue(shared_cleanup.done())

    async def test_every_cleanup_stage_is_attempted_after_errors(self) -> None:
        calls: list[str] = []

        async def reporter_close() -> None:
            calls.append("reporter")
            raise RuntimeError("reporter close failed")

        async def discord_close() -> None:
            calls.append("discord")
            raise RuntimeError("discord close failed")

        async def mongo_close() -> None:
            calls.append("mongo")
            raise RuntimeError("mongo close failed")

        bot = self._bot(reporter_close, discord_close, mongo_close)
        with self.assertLogs("devilblox", level=logging.ERROR):
            await bot.close()

        self.assertEqual(calls, ["reporter", "discord", "mongo"])


if __name__ == "__main__":
    unittest.main()
