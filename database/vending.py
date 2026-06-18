from __future__ import annotations

from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument


def _now():
    return datetime.now(timezone.utc)


def product_key(guild_id: int, product_id: str) -> str:
    return f"{guild_id}:{normalize_product_id(product_id)}"


def normalize_product_id(product_id: str) -> str:
    return product_id.strip().casefold()


def category_key(guild_id: int, category_id: str) -> str:
    return f"{guild_id}:{normalize_product_id(category_id)}"


class ProductCategoryStore:
    def __init__(self, db):
        self.collection = db["product_categories"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("category_id_lower", 1)], unique=True)
        await self.collection.create_index([("guild_id", 1), ("active", 1), ("sort_order", 1)])

    async def upsert(
        self,
        guild_id: int,
        category_id: str,
        *,
        name: str,
        description: str = "",
        emoji: str = "",
        sort_order: int = 0,
        created_by: int | None = None,
    ):
        now = _now()
        category_id = category_id.strip()
        category_id_lower = normalize_product_id(category_id)
        doc = {
            "guild_id": guild_id,
            "category_id": category_id,
            "category_id_lower": category_id_lower,
            "name": name.strip() or category_id,
            "description": description.strip(),
            "emoji": emoji.strip(),
            "sort_order": int(sort_order),
            "active": True,
            "updated_at": now,
        }
        if created_by is not None:
            doc["updated_by"] = created_by

        await self.collection.update_one(
            {"_id": category_key(guild_id, category_id)},
            {
                "$set": doc,
                "$setOnInsert": {
                    "_id": category_key(guild_id, category_id),
                    "created_by": created_by,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return await self.get(guild_id, category_id, include_inactive=True)

    async def get(self, guild_id: int, category_id: str, *, include_inactive: bool = False):
        query = {"_id": category_key(guild_id, category_id)}
        if not include_inactive:
            query["active"] = True
        return await self.collection.find_one(query)

    async def list_active(self, guild_id: int, limit: int = 25):
        return (
            await self.collection.find({"guild_id": guild_id, "active": True})
            .sort([("sort_order", 1), ("name", 1), ("category_id", 1)])
            .to_list(length=limit)
        )

    async def deactivate(self, guild_id: int, category_id: str, deleted_by: int | None = None) -> bool:
        result = await self.collection.update_one(
            {"_id": category_key(guild_id, category_id), "active": True},
            {"$set": {"active": False, "deleted_by": deleted_by, "updated_at": _now()}},
        )
        return result.modified_count > 0


class ProductStore:
    def __init__(self, db):
        self.collection = db["products"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("product_id_lower", 1)], unique=True)
        await self.collection.create_index([("guild_id", 1), ("seller_id", 1)])
        await self.collection.create_index([("guild_id", 1), ("active", 1)])
        await self.collection.create_index([("guild_id", 1), ("category_id_lower", 1), ("active", 1)])

    async def upsert(
        self,
        guild_id: int,
        product_id: str,
        *,
        title: str,
        price: int,
        terabox_url: str,
        description: str = "",
        seller_id: int | None = None,
        category_id: str = "",
        thread_id: int | None = None,
        page_url: str = "",
        created_by: int | None = None,
    ):
        now = _now()
        product_id = product_id.strip()
        product_id_lower = normalize_product_id(product_id)
        category_id = category_id.strip()
        category_id_lower = normalize_product_id(category_id) if category_id else ""
        doc = {
            "guild_id": guild_id,
            "product_id": product_id,
            "product_id_lower": product_id_lower,
            "category_id": category_id,
            "category_id_lower": category_id_lower,
            "title": title.strip() or product_id,
            "price": int(price),
            "terabox_url": terabox_url.strip(),
            "description": description.strip(),
            "seller_id": seller_id,
            "thread_id": thread_id,
            "page_url": page_url.strip(),
            "active": True,
            "updated_at": now,
        }
        if created_by is not None:
            doc["updated_by"] = created_by

        await self.collection.update_one(
            {"_id": product_key(guild_id, product_id)},
            {
                "$set": doc,
                "$setOnInsert": {
                    "_id": product_key(guild_id, product_id),
                    "created_by": created_by,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return await self.get(guild_id, product_id, include_inactive=True)

    async def get(self, guild_id: int, product_id: str, *, include_inactive: bool = False):
        query = {"_id": product_key(guild_id, product_id)}
        if not include_inactive:
            query["active"] = True
        return await self.collection.find_one(query)

    async def list_active(self, guild_id: int, limit: int = 25):
        return (
            await self.collection.find({"guild_id": guild_id, "active": True})
            .sort([("title", 1), ("product_id", 1)])
            .to_list(length=limit)
        )

    async def list_by_category(self, guild_id: int, category_id: str, limit: int = 25):
        return (
            await self.collection.find(
                {
                    "guild_id": guild_id,
                    "category_id_lower": normalize_product_id(category_id),
                    "active": True,
                }
            )
            .sort([("title", 1), ("product_id", 1)])
            .to_list(length=limit)
        )

    async def deactivate(self, guild_id: int, product_id: str, deleted_by: int | None = None) -> bool:
        result = await self.collection.update_one(
            {"_id": product_key(guild_id, product_id), "active": True},
            {"$set": {"active": False, "deleted_by": deleted_by, "updated_at": _now()}},
        )
        return result.modified_count > 0


class ArchiveStore:
    def __init__(self, db):
        self.collection = db["archives"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("video_key", 1)], unique=True)
        await self.collection.create_index([("guild_id", 1), ("product_id_lower", 1)])

    async def upsert(
        self,
        guild_id: int,
        *,
        youtube_url: str,
        video_key: str,
        product_id: str,
        summary: str = "",
        created_by: int | None = None,
    ):
        now = _now()
        product_id_lower = normalize_product_id(product_id)
        doc = {
            "guild_id": guild_id,
            "youtube_url": youtube_url.strip(),
            "video_key": video_key,
            "product_id": product_id.strip(),
            "product_id_lower": product_id_lower,
            "summary": summary.strip(),
            "updated_at": now,
        }
        if created_by is not None:
            doc["updated_by"] = created_by

        await self.collection.update_one(
            {"_id": f"{guild_id}:{video_key}"},
            {
                "$set": doc,
                "$setOnInsert": {
                    "_id": f"{guild_id}:{video_key}",
                    "created_by": created_by,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return await self.find(guild_id, video_key)

    async def find(self, guild_id: int, video_key: str):
        return await self.collection.find_one({"guild_id": guild_id, "video_key": video_key})


class VendingLogStore:
    def __init__(self, db):
        self.charge_logs = db["vending_charge_logs"]
        self.purchase_logs = db["vending_purchase_logs"]
        self.user_products = db["user_products"]

    async def ensure_indexes(self):
        await self.charge_logs.create_index([("guild_id", 1), ("status", 1), ("requested_at", -1)])
        await self.charge_logs.create_index([("guild_id", 1), ("admin_message_id", 1)], sparse=True)
        await self.purchase_logs.create_index([("guild_id", 1), ("user_id", 1), ("purchased_at", -1)])
        await self.purchase_logs.create_index([("guild_id", 1), ("product_id_lower", 1), ("purchased_at", -1)])
        await self.user_products.create_index(
            [("guild_id", 1), ("user_id", 1), ("product_id_lower", 1)],
            unique=True,
        )

    async def create_charge_request(
        self,
        guild_id: int,
        user_id: int,
        depositor_name: str,
        amount: int,
        *,
        proof_filename: str = "",
        proof_content_type: str = "",
        proof_size: int = 0,
    ):
        doc = {
            "guild_id": guild_id,
            "user_id": user_id,
            "depositor_name": depositor_name.strip(),
            "amount": int(amount),
            "proof_filename": proof_filename,
            "proof_content_type": proof_content_type,
            "proof_size": int(proof_size or 0),
            "status": "pending",
            "requested_at": _now(),
            "updated_at": _now(),
        }
        result = await self.charge_logs.insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc

    async def attach_charge_message(
        self,
        request_id,
        channel_id: int,
        message_id: int,
        *,
        request_channel_id: int | None = None,
        request_message_id: int | None = None,
        log_channel_id: int | None = None,
        log_message_id: int | None = None,
        admin_proof_url: str = "",
        request_proof_url: str = "",
        log_proof_url: str = "",
    ):
        updates = {
            "admin_channel_id": channel_id,
            "admin_message_id": message_id,
            "updated_at": _now(),
        }
        if request_channel_id is not None:
            updates["request_channel_id"] = request_channel_id
        if request_message_id is not None:
            updates["request_message_id"] = request_message_id
        if log_channel_id is not None:
            updates["log_channel_id"] = log_channel_id
        if log_message_id is not None:
            updates["log_message_id"] = log_message_id
        if admin_proof_url:
            updates["admin_proof_url"] = admin_proof_url
        if request_proof_url:
            updates["request_proof_url"] = request_proof_url
        if log_proof_url:
            updates["log_proof_url"] = log_proof_url

        result = await self.charge_logs.find_one_and_update(
            {"_id": request_id},
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )
        return result

    async def get_charge_by_admin_message(self, guild_id: int, message_id: int):
        return await self.charge_logs.find_one({"guild_id": guild_id, "admin_message_id": message_id})

    async def claim_charge_request(self, guild_id: int, message_id: int, admin_id: int):
        return await self.charge_logs.find_one_and_update(
            {"guild_id": guild_id, "admin_message_id": message_id, "status": "pending"},
            {
                "$set": {
                    "status": "processing",
                    "processed_by": admin_id,
                    "updated_at": _now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def approve_charge_request(self, request_id, admin_id: int):
        return await self.charge_logs.find_one_and_update(
            {"_id": request_id, "status": "processing"},
            {
                "$set": {
                    "status": "approved",
                    "success": True,
                    "processed_by": admin_id,
                    "processed_at": _now(),
                    "updated_at": _now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def reject_charge_request(self, guild_id: int, message_id: int, admin_id: int, reason: str):
        return await self.charge_logs.find_one_and_update(
            {"guild_id": guild_id, "admin_message_id": message_id, "status": "pending"},
            {
                "$set": {
                    "status": "rejected",
                    "success": False,
                    "processed_by": admin_id,
                    "processed_at": _now(),
                    "reject_reason": reason.strip(),
                    "updated_at": _now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def record_purchase(
        self,
        *,
        guild_id: int,
        user_id: int,
        product: dict,
        before_cash: int,
        after_cash: int,
    ):
        now = _now()
        product_id = product["product_id"]
        product_id_lower = product["product_id_lower"]
        log_doc = {
            "guild_id": guild_id,
            "user_id": user_id,
            "product_id": product_id,
            "product_id_lower": product_id_lower,
            "title": product.get("title", product_id),
            "price": int(product.get("price", 0)),
            "before_cash": int(before_cash),
            "after_cash": int(after_cash),
            "seller_id": product.get("seller_id"),
            "purchased_at": now,
        }
        await self.purchase_logs.insert_one(log_doc)
        await self.user_products.update_one(
            {"guild_id": guild_id, "user_id": user_id, "product_id_lower": product_id_lower},
            {
                "$set": {
                    "product_id": product_id,
                    "title": product.get("title", product_id),
                    "terabox_url": product.get("terabox_url", ""),
                    "status": "purchased",
                    "purchased_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "product_id_lower": product_id_lower,
                },
            },
            upsert=True,
        )
        return log_doc

    async def reserve_product(self, guild_id: int, user_id: int, product: dict) -> bool:
        now = _now()
        try:
            await self.user_products.insert_one(
                {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "product_id": product["product_id"],
                    "product_id_lower": product["product_id_lower"],
                    "title": product.get("title", product["product_id"]),
                    "terabox_url": product.get("terabox_url", ""),
                    "status": "pending",
                    "reserved_at": now,
                    "updated_at": now,
                }
            )
        except DuplicateKeyError:
            return False
        return True

    async def release_product_reservation(self, guild_id: int, user_id: int, product_id: str):
        await self.user_products.delete_one(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "product_id_lower": normalize_product_id(product_id),
                "status": "pending",
            }
        )

    async def owns_product(self, guild_id: int, user_id: int, product_id: str) -> bool:
        doc = await self.user_products.find_one(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "product_id_lower": normalize_product_id(product_id),
                "status": "purchased",
            }
        )
        return doc is not None

    async def list_owned_products(self, guild_id: int, user_id: int, limit: int = 25):
        return (
            await self.user_products.find({"guild_id": guild_id, "user_id": user_id, "status": "purchased"})
            .sort("purchased_at", -1)
            .to_list(length=limit)
        )
