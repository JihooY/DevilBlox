from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.brokerage import BrokerageStore, DEFAULT_CONFIG


class _AsyncCursor:
    def __init__(self, documents: list[dict]):
        self._documents = iter(documents)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._documents)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _ListingCollection:
    def __init__(self, documents: list[dict]):
        self.documents = documents
        self.query: dict | None = None
        self.updates: list[tuple[dict, dict]] = []

    def find(self, query: dict) -> _AsyncCursor:
        self.query = query
        return _AsyncCursor(self.documents)

    async def update_one(self, query: dict, update: dict) -> SimpleNamespace:
        self.updates.append((query, update))
        return SimpleNamespace(modified_count=1)


class BrokerageListingScheduleStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_score_change_recalculates_existing_listing_immediately(self) -> None:
        now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        collection = _ListingCollection(
            [
                {
                    "_id": "listing-1",
                    "guild_id": 77,
                    "seller_id": 123,
                    "status": "open",
                    "like_count": 0,
                    "last_posted_at": now - timedelta(minutes=5),
                }
            ]
        )
        store = object.__new__(BrokerageStore)
        store.listings = collection
        store.get_config = AsyncMock()
        store.ensure_profile = AsyncMock()

        changed = await store.refresh_listing_intervals(
            77,
            seller_id=123,
            seller_score=80,
            config=deepcopy(DEFAULT_CONFIG),
            now=now,
        )

        self.assertEqual(changed, 1)
        self.assertEqual(collection.query["seller_id"], 123)
        values = collection.updates[0][1]["$set"]
        self.assertEqual(values["bump_interval_minutes"], 10)
        self.assertEqual(values["next_bump_at"], now + timedelta(minutes=5))
        store.get_config.assert_not_awaited()
        store.ensure_profile.assert_not_awaited()

    async def test_overdue_like_benefit_schedules_the_next_worker_run(self) -> None:
        now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        collection = _ListingCollection(
            [
                {
                    "_id": "listing-2",
                    "guild_id": 77,
                    "seller_id": 123,
                    "status": "reserved",
                    "like_count": 10,
                    "last_posted_at": now - timedelta(minutes=30),
                }
            ]
        )
        store = object.__new__(BrokerageStore)
        store.listings = collection
        store.get_config = AsyncMock()
        store.ensure_profile = AsyncMock()

        await store.refresh_listing_intervals(
            77,
            seller_id=123,
            seller_score=50,
            config=deepcopy(DEFAULT_CONFIG),
            now=now,
        )

        values = collection.updates[0][1]["$set"]
        self.assertEqual(values["bump_interval_minutes"], 18)
        self.assertEqual(values["next_bump_at"], now)


class BrokerageScoreConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_adjustment_returns_and_ledgers_the_actual_clamped_delta(self) -> None:
        operation_id = "problem:boundary:deduction"
        updated_profile = {
            "trust_score": -100,
            "score_operation_ids": [operation_id],
            "score_operation_results": [
                {
                    "operation_id": operation_id,
                    "score_before": -99,
                    "score_after": -100,
                    "applied_delta": -1,
                }
            ],
        }
        store = object.__new__(BrokerageStore)
        store.ensure_profile = AsyncMock(
            return_value={"trust_score": -99, "problem_count": 0, "penalty_level": 0}
        )
        store.profiles = SimpleNamespace(
            find_one_and_update=AsyncMock(return_value=updated_profile)
        )
        store.score_ledger = SimpleNamespace(
            insert_one=AsyncMock(),
            update_one=AsyncMock(),
        )
        store.refresh_listing_intervals = AsyncMock(return_value=0)

        result = await store.adjust_profile(
            77,
            123,
            -10,
            operation_id=operation_id,
            reason="boundary_test",
        )

        self.assertEqual(result["effective_delta"], -10)
        self.assertEqual(result["delta"], -1)
        self.assertEqual(result["ledger"]["applied_delta"], -1)
        ledger_update = store.score_ledger.update_one.await_args.args[1]["$set"]
        self.assertEqual(ledger_update["score_before"], -99)
        self.assertEqual(ledger_update["score_after"], -100)
        self.assertEqual(ledger_update["applied_delta"], -1)

    async def test_settlement_reversal_uses_actual_not_requested_award(self) -> None:
        store = object.__new__(BrokerageStore)
        store.score_ledger = SimpleNamespace(
            find_one=AsyncMock(
                side_effect=[
                    {"delta": 10, "applied_delta": 1},
                    {"delta": 10, "applied_delta": 4},
                ]
            )
        )
        store.adjust_profile = AsyncMock(return_value={"new_operation": True})
        listing = {
            "_id": "listing-boundary",
            "guild_id": 77,
            "seller_id": 10,
            "sold_to": 20,
            "settlement": {"operation_id": "listing:listing-boundary:settlement"},
        }

        await store.reverse_settlement_awards(
            listing,
            problem_id="problem-1",
            actor_id=999,
        )

        deltas = [call.args[2] for call in store.adjust_profile.await_args_list]
        self.assertEqual(deltas, [-1, -4])


class BrokerageConfigurationTests(unittest.TestCase):
    def test_reservations_have_a_bounded_default_timeout(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["reservation_timeout_minutes"], 30)


if __name__ == "__main__":
    unittest.main()
