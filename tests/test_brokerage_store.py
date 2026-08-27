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


if __name__ == "__main__":
    unittest.main()
