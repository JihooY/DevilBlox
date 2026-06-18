from __future__ import annotations

from datetime import datetime, timezone

from pymongo import ReturnDocument


def _now():
    return datetime.now(timezone.utc)


def normalize_stock_id(item_id: str) -> str:
    return item_id.strip().casefold()


def stock_key(guild_id: int, item_id: str) -> str:
    return f"{guild_id}:{normalize_stock_id(item_id)}"


class StockStore:
    def __init__(self, db):
        self.collection = db["stock_items"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("item_id_lower", 1)], unique=True)
        await self.collection.create_index([("guild_id", 1), ("active", 1), ("name", 1)])

    async def upsert(
        self,
        guild_id: int,
        item_id: str,
        *,
        name: str,
        quantity: int = 0,
        created_by: int | None = None,
    ):
        now = _now()
        item_id = item_id.strip()
        item_id_lower = normalize_stock_id(item_id)
        quantity = max(0, int(quantity))
        doc = {
            "guild_id": guild_id,
            "item_id": item_id,
            "item_id_lower": item_id_lower,
            "name": name.strip() or item_id,
            "quantity": quantity,
            "active": True,
            "updated_at": now,
        }
        if created_by is not None:
            doc["updated_by"] = created_by

        await self.collection.update_one(
            {"_id": stock_key(guild_id, item_id)},
            {
                "$set": doc,
                "$setOnInsert": {
                    "_id": stock_key(guild_id, item_id),
                    "created_by": created_by,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return await self.get(guild_id, item_id)

    async def get(self, guild_id: int, item_id: str, *, include_inactive: bool = False):
        query = {"_id": stock_key(guild_id, item_id)}
        if not include_inactive:
            query["active"] = True
        return await self.collection.find_one(query)

    async def list_active(self, guild_id: int, limit: int = 25):
        return (
            await self.collection.find({"guild_id": guild_id, "active": True})
            .sort([("name", 1), ("item_id", 1)])
            .to_list(length=limit)
        )

    async def adjust_quantity(self, guild_id: int, item_id: str, delta: int):
        delta = int(delta)
        if delta == 0:
            return await self.get(guild_id, item_id)

        query = {"_id": stock_key(guild_id, item_id), "active": True}
        if delta < 0:
            query["quantity"] = {"$gte": abs(delta)}

        return await self.collection.find_one_and_update(
            query,
            {"$inc": {"quantity": delta}, "$set": {"updated_at": _now()}},
            return_document=ReturnDocument.AFTER,
        )

    async def deactivate(self, guild_id: int, item_id: str, deleted_by: int | None = None):
        update = {"active": False, "deleted_by": deleted_by, "updated_at": _now()}
        return await self.collection.find_one_and_update(
            {"_id": stock_key(guild_id, item_id), "active": True},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
