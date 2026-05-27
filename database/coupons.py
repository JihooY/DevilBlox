from __future__ import annotations

from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc)


class CouponStore:
    def __init__(self, db):
        self.coupons = db["coupons"]
        self.user_coupons = db["user_coupons"]

    async def ensure_indexes(self):
        await self.coupons.create_index([("guild_id", 1), ("code", 1)], unique=True)
        await self.user_coupons.create_index([("guild_id", 1), ("user_id", 1), ("code", 1)])

    async def list_for_user(self, guild_id: int, user_id: int):
        owned = await self.user_coupons.find({"guild_id": guild_id, "user_id": user_id}).to_list(length=50)
        if not owned:
            return []
        codes = [doc["code"] for doc in owned]
        coupon_docs = await self.coupons.find({"guild_id": guild_id, "code": {"$in": codes}}).to_list(length=50)
        by_code = {doc["code"]: doc for doc in coupon_docs}
        return [{**doc, "coupon": by_code.get(doc["code"])} for doc in owned]

    async def import_legacy_coupon(self, guild_id: int, doc: dict):
        code = doc.get("code")
        if not code:
            return
        await self.coupons.update_one(
            {"guild_id": guild_id, "code": code},
            {
                "$set": {
                    "guild_id": guild_id,
                    "name": doc.get("name", code),
                    "code": code,
                    "description": doc.get("description", ""),
                    "degree": int(doc.get("degree", 0) or 0),
                    "updated_at": _now(),
                },
                "$setOnInsert": {"created_at": _now()},
            },
            upsert=True,
        )
