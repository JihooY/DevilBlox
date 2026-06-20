from __future__ import annotations

import secrets
from datetime import datetime, timezone

from pymongo import ReturnDocument


def _now():
    return datetime.now(timezone.utc)


def normalize_key(value: str) -> str:
    return value.strip().casefold()


class ReviewStore:
    def __init__(self, db):
        self.collection = db["purchase_reviews"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("status", 1), ("reviewed_at", -1)])
        await self.collection.create_index([("guild_id", 1), ("category_id_lower", 1), ("status", 1), ("reviewed_at", -1)])
        await self.collection.create_index([("guild_id", 1), ("product_id_lower", 1), ("status", 1), ("reviewed_at", -1)])
        await self.collection.create_index([("guild_id", 1), ("seller_id", 1), ("status", 1), ("reviewed_at", -1)])
        await self.collection.create_index([("guild_id", 1), ("buyer_id", 1), ("created_at", -1)])

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

    async def submit(
        self,
        review_id: str,
        buyer_id: int,
        *,
        rating: int,
        content: str,
        photo_filename: str = "",
        photo_content_type: str = "",
        photo_size: int = 0,
    ):
        now = _now()
        updates = {
            "status": "submitted",
            "rating": int(rating),
            "content": content.strip(),
            "reviewed_at": now,
            "updated_at": now,
        }
        if photo_filename:
            updates.update(
                {
                    "photo_filename": photo_filename,
                    "photo_content_type": photo_content_type,
                    "photo_size": int(photo_size or 0),
                }
            )
        return await self.collection.find_one_and_update(
            {
                "_id": review_id,
                "buyer_id": buyer_id,
                "status": "pending",
            },
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
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
