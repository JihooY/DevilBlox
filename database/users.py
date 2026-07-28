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
                    "cash_operations": [],
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
        amount = int(amount)
        if amount <= 0:
            raise ValueError("cash credit amount must be greater than zero")
        await self.ensure_user(guild_id, user_id)
        await self.collection.update_one(
            {"_id": user_key(guild_id, user_id)},
            {"$inc": {"cash": amount}, "$set": {"updated_at": _now()}},
        )
        return await self.get(guild_id, user_id)

    async def spend_cash(self, guild_id: int, user_id: int, amount: int):
        amount = int(amount)
        if amount < 0:
            raise ValueError("cash debit amount must be zero or greater")
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

    async def add_cash_once(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        operation_id: str,
        reason: str = "",
    ):
        """Credit cash exactly once for a stable business operation ID.

        The balance and operation marker live in the same user document, so a
        retry after a process or network failure cannot credit the user twice.
        """
        amount = int(amount)
        operation_id = str(operation_id).strip()
        if amount <= 0:
            raise ValueError("cash credit amount must be greater than zero")
        if not operation_id:
            raise ValueError("operation_id is required")

        await self.ensure_user(guild_id, user_id)
        now = _now()
        current_cash = {"$ifNull": ["$cash", 0]}
        operation = {
            "operation_id": operation_id,
            "kind": "credit",
            "amount": amount,
            "before_cash": current_cash,
            "after_cash": {"$add": [current_cash, amount]},
            "reason": str(reason).strip()[:200],
            "created_at": now,
        }
        user = await self.collection.find_one_and_update(
            {
                "_id": user_key(guild_id, user_id),
                "cash_operations.operation_id": {"$ne": operation_id},
            },
            [
                {
                    "$set": {
                        "cash": {"$add": [current_cash, amount]},
                        "cash_operations": {
                            "$concatArrays": [
                                {"$ifNull": ["$cash_operations", []]},
                                [operation],
                            ]
                        },
                        "updated_at": now,
                    }
                }
            ],
            return_document=ReturnDocument.AFTER,
        )
        applied = user is not None
        if user is None:
            user = await self.get(guild_id, user_id)
        saved = _cash_operation(user, operation_id)
        if saved is None:
            raise RuntimeError("cash credit operation was not persisted")
        return _cash_operation_result(user, saved, applied=applied)

    async def spend_cash_once(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
        *,
        operation_id: str,
        reason: str = "",
    ):
        """Debit cash once, returning the original result on every retry."""
        amount = int(amount)
        operation_id = str(operation_id).strip()
        if amount < 0:
            raise ValueError("cash debit amount must be zero or greater")
        if not operation_id:
            raise ValueError("operation_id is required")

        await self.ensure_user(guild_id, user_id)
        now = _now()
        current_cash = {"$ifNull": ["$cash", 0]}
        current_spent = {"$ifNull": ["$accrued_spent", 0]}
        current_points = {"$ifNull": ["$points", 0]}
        operation = {
            "operation_id": operation_id,
            "kind": "debit",
            "amount": amount,
            "before_cash": current_cash,
            "after_cash": {"$subtract": [current_cash, amount]},
            "reason": str(reason).strip()[:200],
            "created_at": now,
        }
        user = await self.collection.find_one_and_update(
            {
                "_id": user_key(guild_id, user_id),
                "cash": {"$gte": amount},
                "cash_operations.operation_id": {"$ne": operation_id},
            },
            [
                {
                    "$set": {
                        "cash": {"$subtract": [current_cash, amount]},
                        "accrued_spent": {"$add": [current_spent, amount]},
                        "points": {"$add": [current_points, amount // 1000]},
                        "cash_operations": {
                            "$concatArrays": [
                                {"$ifNull": ["$cash_operations", []]},
                                [operation],
                            ]
                        },
                        "updated_at": now,
                    }
                }
            ],
            return_document=ReturnDocument.AFTER,
        )
        applied = user is not None
        if user is None:
            user = await self.get(guild_id, user_id)
        saved = _cash_operation(user, operation_id)
        if saved is None:
            return None
        return _cash_operation_result(user, saved, applied=applied)

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


def _cash_operation(user: dict | None, operation_id: str) -> dict | None:
    if not user:
        return None
    return next(
        (
            operation
            for operation in user.get("cash_operations", [])
            if operation.get("operation_id") == operation_id
        ),
        None,
    )


def _cash_operation_result(user: dict, operation: dict, *, applied: bool) -> dict:
    return {
        "applied": applied,
        "operation_id": operation["operation_id"],
        "before_cash": int(operation.get("before_cash", 0)),
        "after_cash": int(operation.get("after_cash", 0)),
        "user": user,
    }
