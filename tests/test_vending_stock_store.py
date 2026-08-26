from __future__ import annotations

import unittest

from database.vending import VendingStockUnitStore


class _FakeCursor:
    """Mimics an AsyncIOMotorCommandCursor: async-iterable and .to_list()-able."""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for doc in self._docs:
            yield doc

    async def to_list(self, length=None):
        return list(self._docs) if length is None else list(self._docs)[:length]


class _FakeCollection:
    """A tiny motor stand-in.

    Crucially, ``aggregate`` is itself a coroutine that resolves to a cursor
    (matching this project's motor version) rather than returning a cursor
    synchronously -- that mismatch is exactly what broke ``count_for_products``
    and ``take_random`` in production ("'async for' requires ... got coroutine").
    """

    def __init__(self, docs: list[dict]):
        self.docs = docs

    async def aggregate(self, pipeline: list[dict]) -> _FakeCursor:
        match = pipeline[0]["$match"]
        matched = [doc for doc in self.docs if self._matches(doc, match)]
        stage = pipeline[1]
        if "$group" in stage:
            group_key = stage["$group"]["_id"].lstrip("$")
            counts: dict[str, int] = {}
            for doc in matched:
                counts[doc[group_key]] = counts.get(doc[group_key], 0) + 1
            return _FakeCursor([{"_id": key, "count": count} for key, count in counts.items()])
        if "$sample" in stage:
            return _FakeCursor(matched[:1])
        return _FakeCursor(matched)

    @staticmethod
    def _matches(doc: dict, match: dict) -> bool:
        for key, value in match.items():
            if isinstance(value, dict) and "$in" in value:
                if doc.get(key) not in value["$in"]:
                    return False
            elif doc.get(key) != value:
                return False
        return True

    async def find_one_and_delete(self, query: dict):
        for index, doc in enumerate(self.docs):
            if all(doc.get(key) == value for key, value in query.items()):
                return self.docs.pop(index)
        return None


class VendingStockUnitStoreTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, docs: list[dict]) -> VendingStockUnitStore:
        store = VendingStockUnitStore.__new__(VendingStockUnitStore)
        store.collection = _FakeCollection(docs)
        return store

    async def test_count_for_products_awaits_the_aggregate_cursor(self):
        store = self._store(
            [
                {"guild_id": 1, "product_id_lower": "a", "_id": 1},
                {"guild_id": 1, "product_id_lower": "a", "_id": 2},
                {"guild_id": 1, "product_id_lower": "b", "_id": 3},
                {"guild_id": 2, "product_id_lower": "a", "_id": 4},
            ]
        )

        counts = await store.count_for_products(1, ["a", "b"])

        self.assertEqual(counts, {"a": 2, "b": 1})

    async def test_count_for_products_returns_empty_without_querying(self):
        store = self._store([])

        counts = await store.count_for_products(1, [])

        self.assertEqual(counts, {})

    async def test_take_random_awaits_the_sample_cursor_and_pops_the_unit(self):
        store = self._store(
            [{"guild_id": 1, "product_id_lower": "a", "_id": 1, "content": "id:pw"}]
        )

        unit = await store.take_random(1, "a")

        self.assertEqual(unit["content"], "id:pw")
        self.assertEqual(store.collection.docs, [])

    async def test_take_random_returns_none_when_the_pool_is_empty(self):
        store = self._store([])

        unit = await store.take_random(1, "a")

        self.assertIsNone(unit)


if __name__ == "__main__":
    unittest.main()
