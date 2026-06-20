from __future__ import annotations

import secrets
from datetime import datetime, timezone

from pymongo import ReturnDocument


def _now():
    return datetime.now(timezone.utc)


def normalize_key(value: str) -> str:
    return value.strip().casefold()


def seller_rating_key(guild_id: int, seller_id: int) -> str:
    return f"{guild_id}:{seller_id}"


class ReviewStore:
    def __init__(self, db):
        self.collection = db["purchase_reviews"]
        self.seller_ratings = db["seller_review_ratings"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("status", 1), ("reviewed_at", -1)])
        await self.collection.create_index([("guild_id", 1), ("category_id_lower", 1), ("status", 1), ("reviewed_at", -1)])
        await self.collection.create_index([("guild_id", 1), ("product_id_lower", 1), ("status", 1), ("reviewed_at", -1)])
        await self.collection.create_index([("guild_id", 1), ("seller_id", 1), ("status", 1), ("reviewed_at", -1)])
        await self.collection.create_index([("guild_id", 1), ("buyer_id", 1), ("created_at", -1)])
        await self.seller_ratings.create_index([("guild_id", 1), ("seller_id", 1)], unique=True)
        await self.seller_ratings.create_index([("guild_id", 1), ("rating_average", -1), ("rating_count", -1)])

    async def create_pending(
        self,
        *,
        guild_id: int,
        buyer_id: int,
        seller_id: int | None,
        product_id: str = "",
        product_title: str,
        category_id: str = "",
        category_name: str = "",
        source: str,
        purchased_at=None,
        amount: int | None = None,
    ):
        now = _now()
        review_id = secrets.token_urlsafe(8)
        doc = {
            "_id": review_id,
            "guild_id": guild_id,
            "buyer_id": buyer_id,
            "seller_id": seller_id,
            "product_id": product_id.strip(),
            "product_id_lower": normalize_key(product_id) if product_id else "",
            "product_title": product_title.strip() or product_id.strip() or "상품",
            "category_id": category_id.strip(),
            "category_id_lower": normalize_key(category_id) if category_id else "",
            "category_name": category_name.strip(),
            "source": source,
            "amount": amount,
            "status": "pending",
            "purchased_at": purchased_at or now,
            "requested_at": now,
            "created_at": now,
            "updated_at": now,
        }
        await self.collection.insert_one(doc)
        return doc

    async def get(self, review_id: str):
        return await self.collection.find_one({"_id": review_id})

    async def list_pending(self, limit: int = 500):
        return (
            await self.collection.find({"status": "pending"})
            .sort("created_at", -1)
            .to_list(length=limit)
        )

    async def submit(
        self,
        review_id: str,
        buyer_id: int,
        *,
        rating: int,
        content: str,
        photos: list[dict] | None = None,
    ):
        now = _now()
        updates = {
            "status": "submitted",
            "rating": int(rating),
            "content": content.strip(),
            "reviewed_at": now,
            "updated_at": now,
        }
        photos = list(photos or [])
        if photos:
            updates["photos"] = photos
            first_photo = photos[0]
            updates.update(
                {
                    "photo_filename": first_photo.get("filename", ""),
                    "photo_content_type": first_photo.get("content_type", ""),
                    "photo_size": int(first_photo.get("size", 0) or 0),
                    "photo_count": len(photos),
                }
            )
        doc = await self.collection.find_one_and_update(
            {
                "_id": review_id,
                "buyer_id": buyer_id,
                "status": "pending",
            },
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            await self.add_seller_rating(doc)
        return doc

    async def add_seller_rating(self, review: dict):
        seller_id = review.get("seller_id")
        if not seller_id:
            return None

        rating = int(review.get("rating", 0) or 0)
        if rating < 1 or rating > 5:
            return None

        now = _now()
        doc = await self.seller_ratings.find_one_and_update(
            {"_id": seller_rating_key(review["guild_id"], seller_id)},
            {
                "$inc": {
                    "rating_total": rating,
                    "rating_count": 1,
                },
                "$set": {
                    "guild_id": review["guild_id"],
                    "seller_id": seller_id,
                    "last_rating": rating,
                    "last_review_id": review["_id"],
                    "last_reviewed_at": review.get("reviewed_at") or now,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": seller_rating_key(review["guild_id"], seller_id),
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            return None

        average = round(float(doc.get("rating_total", 0)) / max(1, int(doc.get("rating_count", 0))), 3)
        await self.seller_ratings.update_one(
            {"_id": doc["_id"]},
            {"$set": {"rating_average": average}},
        )
        doc["rating_average"] = average
        return doc

    async def rebuild_seller_rating(self, guild_id: int, seller_id: int):
        pipeline = [
            {
                "$match": {
                    "guild_id": guild_id,
                    "seller_id": seller_id,
                    "status": "submitted",
                }
            },
            {
                "$group": {
                    "_id": "$seller_id",
                    "rating_total": {"$sum": "$rating"},
                    "rating_count": {"$sum": 1},
                    "last_reviewed_at": {"$max": "$reviewed_at"},
                }
            },
        ]
        rows = await self.collection.aggregate(pipeline).to_list(length=1)
        if not rows:
            return None

        row = rows[0]
        rating_count = int(row.get("rating_count", 0) or 0)
        rating_total = int(row.get("rating_total", 0) or 0)
        average = round(float(rating_total) / max(1, rating_count), 3)
        now = _now()
        await self.seller_ratings.update_one(
            {"_id": seller_rating_key(guild_id, seller_id)},
            {
                "$set": {
                    "guild_id": guild_id,
                    "seller_id": seller_id,
                    "rating_total": rating_total,
                    "rating_count": rating_count,
                    "rating_average": average,
                    "last_reviewed_at": row.get("last_reviewed_at"),
                    "updated_at": now,
                },
                "$setOnInsert": {"_id": seller_rating_key(guild_id, seller_id), "created_at": now},
            },
            upsert=True,
        )
        return await self.get_seller_rating(guild_id, seller_id, rebuild_missing=False)

    async def get_seller_rating(self, guild_id: int, seller_id: int, *, rebuild_missing: bool = True):
        doc = await self.seller_ratings.find_one({"_id": seller_rating_key(guild_id, seller_id)})
        if doc is None and rebuild_missing:
            return await self.rebuild_seller_rating(guild_id, seller_id)
        return doc

    async def list_seller_ratings(self, guild_id: int, limit: int = 10):
        return (
            await self.seller_ratings.find({"guild_id": guild_id, "rating_count": {"$gt": 0}})
            .sort([("rating_average", -1), ("rating_count", -1), ("updated_at", -1)])
            .to_list(length=limit)
        )

    async def rebuild_all_seller_ratings(self, guild_id: int, limit: int = 25):
        pipeline = [
            {
                "$match": {
                    "guild_id": guild_id,
                    "status": "submitted",
                    "seller_id": {"$ne": None},
                }
            },
            {
                "$group": {
                    "_id": "$seller_id",
                    "rating_total": {"$sum": "$rating"},
                    "rating_count": {"$sum": 1},
                    "last_reviewed_at": {"$max": "$reviewed_at"},
                }
            },
            {"$sort": {"rating_count": -1}},
            {"$limit": limit},
        ]
        rows = await self.collection.aggregate(pipeline).to_list(length=limit)
        now = _now()
        for row in rows:
            seller_id = int(row["_id"])
            rating_count = int(row.get("rating_count", 0) or 0)
            rating_total = int(row.get("rating_total", 0) or 0)
            average = round(float(rating_total) / max(1, rating_count), 3)
            await self.seller_ratings.update_one(
                {"_id": seller_rating_key(guild_id, seller_id)},
                {
                    "$set": {
                        "guild_id": guild_id,
                        "seller_id": seller_id,
                        "rating_total": rating_total,
                        "rating_count": rating_count,
                        "rating_average": average,
                        "last_reviewed_at": row.get("last_reviewed_at"),
                        "updated_at": now,
                    },
                    "$setOnInsert": {"_id": seller_rating_key(guild_id, seller_id), "created_at": now},
                },
                upsert=True,
            )
        return await self.list_seller_ratings(guild_id, limit=limit)

    async def list_by_seller(self, guild_id: int, seller_id: int, limit: int = 5):
        return (
            await self.collection.find(
                {
                    "guild_id": guild_id,
                    "seller_id": seller_id,
                    "status": "submitted",
                }
            )
            .sort("reviewed_at", -1)
            .to_list(length=limit)
        )

    async def list_by_category(self, guild_id: int, category_id: str, limit: int = 10):
        return (
            await self.collection.find(
                {
                    "guild_id": guild_id,
                    "category_id_lower": normalize_key(category_id),
                    "status": "submitted",
                }
            )
            .sort("reviewed_at", -1)
            .to_list(length=limit)
        )

    async def list_recent(self, guild_id: int, limit: int = 10):
        return (
            await self.collection.find({"guild_id": guild_id, "status": "submitted"})
            .sort("reviewed_at", -1)
            .to_list(length=limit)
        )
