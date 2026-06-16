from __future__ import annotations

from datetime import datetime, timezone

from pymongo import ReturnDocument


def _now():
    return datetime.now(timezone.utc)


def user_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


class UserStore:
    def __init__(self, db):
        self.collection = db["users"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
        await self.collection.create_index("verified_at")

    async def ensure_user(self, guild_id: int, user_id: int, grade_role_id: int | None = None):
        now = _now()
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {
                "$setOnInsert": {
                    "_id": user_key(guild_id, user_id),
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "accrued_spent": 0,
                    "grade_role_id": grade_role_id,
                    "middleman_anonymous": False,
                    "points": 0,
                    "cash": 0,
                    "verified_at": None,
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        user = await self.get(guild_id, user_id)
        if user is not None and "cash" not in user:
            await self.collection.update_one(
                {"_id": user_key(guild_id, user_id)},
                {"$set": {"cash": 0, "updated_at": _now()}},
            )
            user["cash"] = 0
        return user

    async def get(self, guild_id: int, user_id: int) -> dict | None:
        return await self.collection.find_one({"_id": user_key(guild_id, user_id)})

    async def add_spent(self, guild_id: int, user_id: int, amount: int):
        await self.ensure_user(guild_id, user_id)
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {"$inc": {"accrued_spent": amount}, "$set": {"updated_at": _now()}},
        )
        return await self.get(guild_id, user_id)

    async def add_points(self, guild_id: int, user_id: int, amount: int):
        await self.ensure_user(guild_id, user_id)
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {"$inc": {"points": amount}, "$set": {"updated_at": _now()}},
        )

    async def add_cash(self, guild_id: int, user_id: int, amount: int):
        await self.ensure_user(guild_id, user_id)
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {"$inc": {"cash": amount}, "$set": {"updated_at": _now()}},
        )
        return await self.get(guild_id, user_id)

    async def spend_cash(self, guild_id: int, user_id: int, amount: int):
        await self.ensure_user(guild_id, user_id)
        before = await self.collection.find_one_and_update(
            {"_id": user_key(guild_id, user_id), "cash": {"$gte": amount}},
            {
                "$inc": {
                    "cash": -amount,
                    "accrued_spent": amount,
                    "points": amount // 1000,
                },
                "$set": {"updated_at": _now()},
            },
            return_document=ReturnDocument.BEFORE,
        )
        if before is None:
            return None
        return {
            "before_cash": int(before.get("cash", 0)),
            "after_cash": int(before.get("cash", 0)) - amount,
            "user": await self.get(guild_id, user_id),
        }

    async def set_grade(self, guild_id: int, user_id: int, role_id: int | None):
        await self.ensure_user(guild_id, user_id)
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {"$set": {"grade_role_id": role_id, "updated_at": _now()}},
        )

    async def set_verified(self, guild_id: int, user_id: int, role_id: int | None):
        await self.ensure_user(guild_id, user_id, role_id)
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {
                "$set": {
                    "verified_at": _now(),
                    "grade_role_id": role_id,
                    "updated_at": _now(),
                }
            },
        )

    async def toggle_middleman_anonymous(self, guild_id: int, user_id: int) -> bool:
        user = await self.ensure_user(guild_id, user_id)
        new_value = not bool(user.get("middleman_anonymous", False))
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {"$set": {"middleman_anonymous": new_value, "updated_at": _now()}},
        )
        return new_value

    async def import_legacy_user(self, guild_id: int, doc: dict):
        user_id = int(doc["user_id"])
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {
                "$set": {
                    "_id": user_key(guild_id, user_id),
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "accrued_spent": int(doc.get("accrue_used_money", 0) or 0),
                    "grade_role_id": doc.get("grade"),
                    "middleman_anonymous": bool(doc.get("middleman_anonymous", 0)),
                    "points": int(doc.get("point", 0) or 0),
                    "updated_at": _now(),
                },
                "$setOnInsert": {"created_at": _now(), "verified_at": None},
            },
            upsert=True,
        )
