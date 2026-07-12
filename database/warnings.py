from __future__ import annotations

from datetime import datetime, timezone

from pymongo import ReturnDocument


def _now():
    return datetime.now(timezone.utc)


def warning_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


class WarningStore:
    def __init__(self, db):
        self.collection = db["user_warnings"]
        self.events = db["warning_events"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
        await self.collection.create_index([("guild_id", 1), ("blocked", 1)])
        await self.events.create_index([("guild_id", 1), ("user_id", 1), ("created_at", -1)])

    async def ensure_user(self, guild_id: int, user_id: int, user_name: str = ""):
        now = _now()
        await self.collection.update_one(
            {"_id": warning_key(guild_id, user_id)},
            {
                "$set": {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "user_name": user_name or str(user_id),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": warning_key(guild_id, user_id),
                    "warning_count": 0,
                    "blocked": False,
                    "block_reason": "",
                    "blocked_by": None,
                    "blocked_at": None,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return await self.get(guild_id, user_id)

    async def get(self, guild_id: int, user_id: int):
        return await self.collection.find_one({"_id": warning_key(guild_id, user_id)})

    async def is_blocked(self, guild_id: int, user_id: int) -> bool:
        doc = await self.get(guild_id, user_id)
        return bool(doc and doc.get("blocked"))

    async def add_warnings(
        self,
        guild_id: int,
        user_id: int,
        user_name: str,
        amount: int,
        actor_id: int,
        reason: str = "",
    ):
        amount = max(1, int(amount))
        now = _now()
        doc = await self.collection.find_one_and_update(
            {"_id": warning_key(guild_id, user_id)},
            {
                "$set": {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "user_name": user_name or str(user_id),
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": warning_key(guild_id, user_id),
                    "blocked": False,
                    "block_reason": "",
                    "blocked_by": None,
                    "blocked_at": None,
                    "created_at": now,
                },
                "$inc": {"warning_count": amount},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        await self.record_event(guild_id, user_id, "add", amount, doc.get("warning_count", 0), actor_id, reason)
        return doc

    async def subtract_warnings(
        self,
        guild_id: int,
        user_id: int,
        user_name: str,
        amount: int,
        actor_id: int,
        reason: str = "",
    ):
        amount = max(1, int(amount))
        doc = await self.ensure_user(guild_id, user_id, user_name)
        current_count = max(0, int(doc.get("warning_count", 0) or 0))
        new_count = max(0, current_count - amount)
        actual_delta = new_count - current_count
        now = _now()
        await self.collection.update_one(
            {"_id": warning_key(guild_id, user_id)},
            {
                "$set": {
                    "warning_count": new_count,
                    "user_name": user_name or str(user_id),
                    "updated_at": now,
                }
            },
        )
        updated = await self.get(guild_id, user_id)
        await self.record_event(guild_id, user_id, "subtract", actual_delta, new_count, actor_id, reason)
        return updated

    async def set_blocked(
        self,
        guild_id: int,
        user_id: int,
        user_name: str,
        blocked: bool,
        actor_id: int,
        reason: str = "",
    ):
        now = _now()
        block_reason = reason.strip() if blocked else ""
        doc = await self.collection.find_one_and_update(
            {"_id": warning_key(guild_id, user_id)},
            {
                "$set": {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "user_name": user_name or str(user_id),
                    "blocked": blocked,
                    "block_reason": block_reason,
                    "blocked_by": actor_id if blocked else None,
                    "blocked_at": now if blocked else None,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": warning_key(guild_id, user_id),
                    "warning_count": 0,
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        action = "block" if blocked else "unblock"
        await self.record_event(guild_id, user_id, action, 0, doc.get("warning_count", 0), actor_id, reason)
        return doc

    async def record_event(
        self,
        guild_id: int,
        user_id: int,
        action: str,
        delta: int,
        count_after: int,
        actor_id: int,
        reason: str = "",
    ):
        await self.events.insert_one(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "action": action,
                "delta": int(delta),
                "count_after": int(count_after),
                "actor_id": actor_id,
                "reason": reason.strip(),
                "created_at": _now(),
            }
        )

    async def list_events(self, guild_id: int, user_id: int, limit: int = 5):
        return (
            await self.events.find({"guild_id": guild_id, "user_id": user_id})
            .sort("created_at", -1)
            .to_list(length=max(1, min(limit, 25)))
        )
