from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from pymongo import ReturnDocument


def _now():
    return datetime.now(timezone.utc)


def normalize_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9_-]", "", value.strip().upper())[:40]


class CouponStore:
    """Coupon definitions, user inventory, promotions and invite attribution."""

    def __init__(self, db):
        self.coupons = db["coupons"]
        self.user_coupons = db["user_coupons"]
        self.promotions = db["promotion_codes"]
        self.invites = db["invite_attributions"]
        self.selections = db["coupon_selections"]
        self.redemptions = db["coupon_redemptions"]

    async def ensure_indexes(self):
        await self.coupons.create_index([("guild_id", 1), ("code", 1)], unique=True)
        await self.user_coupons.create_index(
            [("guild_id", 1), ("user_id", 1), ("code", 1)], unique=True
        )
        await self.promotions.create_index([("guild_id", 1), ("code", 1)], unique=True)
        await self.promotions.create_index([("guild_id", 1), ("invite_code", 1)], unique=True)
        await self.invites.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
        await self.selections.create_index([("guild_id", 1), ("user_id", 1), ("context", 1)], unique=True)
        await self.redemptions.create_index([("guild_id", 1), ("user_id", 1), ("created_at", -1)])

    async def create_coupon(self, guild_id: int, code: str, name: str, kind: str, discount: int,
                            created_by: int, discount_type: str = "percent"):
        code = normalize_code(code)
        if kind not in {"general", "special"} or discount_type not in {"percent", "fixed"} or not code:
            raise ValueError("invalid coupon")
        if discount < 1 or (discount_type == "percent" and discount > 100):
            raise ValueError("invalid coupon")
        if discount_type == "fixed" and kind != "special":
            raise ValueError("invalid coupon")
        now = _now()
        await self.coupons.update_one(
            {"guild_id": guild_id, "code": code},
            {"$set": {"name": name.strip()[:100] or code, "kind": kind, "discount": discount,
                      "discount_type": discount_type,
                      "active": True, "updated_at": now, "created_by": created_by},
             "$setOnInsert": {"guild_id": guild_id, "code": code, "created_at": now}},
            upsert=True,
        )
        return await self.coupons.find_one({"guild_id": guild_id, "code": code})

    async def create_promotion(self, guild_id: int, code: str, name: str, invite_code: str, invite_url: str,
                               discount: int, created_by: int):
        code = normalize_code(code)
        if not code or not invite_code or not 1 <= discount <= 100:
            raise ValueError("invalid promotion")
        now = _now()
        await self.promotions.update_one(
            {"guild_id": guild_id, "code": code},
            {"$set": {"name": name.strip()[:100] or code, "invite_code": invite_code,
                      "invite_url": invite_url, "discount": discount, "active": True,
                      "updated_at": now, "created_by": created_by},
             "$setOnInsert": {"guild_id": guild_id, "code": code, "created_at": now}},
            upsert=True,
        )
        return await self.promotions.find_one({"guild_id": guild_id, "code": code})

    async def list_definitions(self, guild_id: int, include_inactive: bool = False):
        query = {"guild_id": guild_id}
        if not include_inactive:
            query["active"] = True
        coupons = await self.coupons.find(query).sort("created_at", -1).to_list(length=100)
        promotions = await self.promotions.find(query).sort("created_at", -1).to_list(length=100)
        return coupons, promotions

    async def deactivate(self, guild_id: int, code: str):
        code = normalize_code(code)
        result = await self.coupons.update_one({"guild_id": guild_id, "code": code}, {"$set": {"active": False, "updated_at": _now()}})
        if not result.matched_count:
            result = await self.promotions.update_one({"guild_id": guild_id, "code": code}, {"$set": {"active": False, "updated_at": _now()}})
        return bool(result.matched_count)

    async def grant(self, guild_id: int, user_id: int, code: str, quantity: int = 1, granted_by: int | None = None):
        code = normalize_code(code)
        coupon = await self.coupons.find_one({"guild_id": guild_id, "code": code, "active": True})
        if coupon is None or quantity < 1:
            return None
        return await self.user_coupons.find_one_and_update(
            {"guild_id": guild_id, "user_id": user_id, "code": code},
            {"$inc": {"quantity": quantity}, "$set": {"updated_at": _now()},
             "$setOnInsert": {"_id": uuid4().hex, "guild_id": guild_id, "user_id": user_id,
                              "code": code, "granted_by": granted_by, "created_at": _now()}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )

    async def list_for_user(self, guild_id: int, user_id: int, kind: str | None = None):
        owned = await self.user_coupons.find({"guild_id": guild_id, "user_id": user_id, "quantity": {"$gt": 0}}).to_list(length=100)
        if not owned:
            return []
        query = {"guild_id": guild_id, "code": {"$in": [x["code"] for x in owned]}, "active": True}
        if kind:
            query["kind"] = kind
        definitions = await self.coupons.find(query).to_list(length=100)
        by_code = {x["code"]: x for x in definitions}
        return [{**x, "coupon": by_code[x["code"]]} for x in owned if x["code"] in by_code]

    async def get_owned_coupon(self, guild_id: int, user_id: int, code: str, kind: str | None = None):
        code = normalize_code(code)
        owned = await self.user_coupons.find_one(
            {"guild_id": guild_id, "user_id": user_id, "code": code, "quantity": {"$gt": 0}}
        )
        if not owned:
            return None
        query = {"guild_id": guild_id, "code": code, "active": True}
        if kind:
            query["kind"] = kind
        coupon = await self.coupons.find_one(query)
        return {**owned, "coupon": coupon} if coupon else None

    async def select(self, guild_id: int, user_id: int, context: str, code: str | None, *, channel_id: int | None = None):
        await self.selections.update_one(
            {"guild_id": guild_id, "user_id": user_id, "context": context},
            {"$set": {"code": normalize_code(code or "") or None, "promotion_code": None,
                      "channel_id": channel_id, "updated_at": _now()}},
            upsert=True,
        )

    async def select_promotion(self, guild_id: int, user_id: int, context: str, code: str):
        await self.selections.update_one(
            {"guild_id": guild_id, "user_id": user_id, "context": context},
            {"$set": {"code": None, "promotion_code": normalize_code(code), "updated_at": _now()}},
            upsert=True,
        )

    async def quote(self, guild_id: int, user_id: int, context: str, amount: int):
        selection = await self.get_selection(guild_id, user_id, context)
        if not selection:
            return amount, None, None
        if selection.get("promotion_code"):
            promo = await self.validate_promotion(guild_id, user_id, selection["promotion_code"])
            if promo:
                return max(0, amount - amount * int(promo["discount"]) // 100), None, promo
        if selection.get("code"):
            owned = await self.user_coupons.find_one({"guild_id": guild_id, "user_id": user_id,
                                                       "code": selection["code"], "quantity": {"$gt": 0}})
            coupon = await self.coupons.find_one({"guild_id": guild_id, "code": selection["code"],
                                                  "kind": "general", "active": True})
            if owned and coupon:
                return max(0, amount - amount * int(coupon["discount"]) // 100), coupon, None
        return amount, None, None

    async def get_selection(self, guild_id: int, user_id: int, context: str):
        return await self.selections.find_one({"guild_id": guild_id, "user_id": user_id, "context": context})

    async def consume(self, guild_id: int, user_id: int, code: str, context: str, amount: int):
        code = normalize_code(code)
        coupon = await self.coupons.find_one({"guild_id": guild_id, "code": code, "active": True})
        if coupon is None:
            return None
        owned = await self.user_coupons.find_one_and_update(
            {"guild_id": guild_id, "user_id": user_id, "code": code, "quantity": {"$gt": 0}},
            {"$inc": {"quantity": -1}, "$set": {"updated_at": _now()}},
            return_document=ReturnDocument.AFTER,
        )
        if owned is None:
            return None
        value = int(coupon["discount"])
        discounted = (
            max(0, amount - value)
            if coupon.get("discount_type", "percent") == "fixed"
            else max(0, amount - (amount * value // 100))
        )
        await self.redemptions.insert_one({"guild_id": guild_id, "user_id": user_id, "code": code,
                                           "kind": coupon["kind"], "context": context, "amount": amount,
                                           "discounted_amount": discounted, "created_at": _now()})
        return coupon, discounted

    async def attribute_invite(self, guild_id: int, user_id: int, invite_code: str, inviter_id: int | None):
        await self.invites.update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {"$setOnInsert": {"guild_id": guild_id, "user_id": user_id, "invite_code": invite_code,
                              "inviter_id": inviter_id, "joined_at": _now()}}, upsert=True,
        )

    async def validate_promotion(self, guild_id: int, user_id: int, code: str):
        promotion = await self.promotions.find_one({"guild_id": guild_id, "code": normalize_code(code), "active": True})
        if promotion is None:
            return None
        attribution = await self.invites.find_one({"guild_id": guild_id, "user_id": user_id,
                                                   "invite_code": promotion["invite_code"]})
        return promotion if attribution else None

    async def import_legacy_coupon(self, guild_id: int, doc: dict):
        code = doc.get("code")
        if code:
            await self.create_coupon(guild_id, code, doc.get("name", code), "general",
                                     int(doc.get("degree", 0) or 1), 0)
