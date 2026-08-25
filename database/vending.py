from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

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
        price = int(price)
        if price < 0:
            raise ValueError("product price must be zero or greater")
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
            "price": price,
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

    async def list_by_category(self, guild_id: int, category_id: str, limit: int | None = None):
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


class RandomProductStore:
    """Products that are only obtainable through the random vending draw."""

    def __init__(self, db):
        self.collection = db["random_products"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("product_id_lower", 1)], unique=True)
        await self.collection.create_index([("guild_id", 1), ("active", 1)])

    async def upsert(
        self,
        guild_id: int,
        product_id: str,
        *,
        title: str,
        terabox_url: str,
        description: str = "",
        weight: int = 1,
        seller_id: int | None = None,
        created_by: int | None = None,
    ):
        now = _now()
        weight = max(1, int(weight))
        product_id = product_id.strip()
        product_id_lower = normalize_product_id(product_id)
        doc = {
            "guild_id": guild_id,
            "product_id": product_id,
            "product_id_lower": product_id_lower,
            "title": title.strip() or product_id,
            "terabox_url": terabox_url.strip(),
            "description": description.strip(),
            "weight": weight,
            "seller_id": seller_id,
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

    async def list_active(self, guild_id: int, limit: int | None = None):
        return (
            await self.collection.find({"guild_id": guild_id, "active": True})
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
        amount = int(amount)
        if amount <= 0:
            raise ValueError("charge amount must be greater than zero")
        doc = {
            "guild_id": guild_id,
            "user_id": user_id,
            "depositor_name": depositor_name.strip(),
            "amount": amount,
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
        charge = await self.charge_logs.find_one_and_update(
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
        if charge is not None:
            return charge

        # A process can stop after claiming but before credit/finalization.  A
        # later click must be able to resume that exact request safely.
        return await self.charge_logs.find_one(
            {
                "guild_id": guild_id,
                "admin_message_id": message_id,
                "status": "processing",
            }
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

    async def get_charge(self, request_id):
        return await self.charge_logs.find_one({"_id": request_id})

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

    async def upsert_purchase_log(
        self,
        *,
        operation_id: str,
        guild_id: int,
        user_id: int,
        product: dict,
        price: int,
        original_price: int,
        before_cash: int,
        after_cash: int,
        discount_code: str | None = None,
        kind: str = "purchase",
    ):
        price = int(price)
        original_price = int(original_price)
        if price < 0 or original_price < 0:
            raise ValueError("purchase prices must be zero or greater")
        operation_id = str(operation_id).strip()
        if not operation_id:
            raise ValueError("operation_id is required")

        now = _now()
        product_id = product["product_id"]
        product_id_lower = product["product_id_lower"]
        log_doc = {
            "_id": operation_id,
            "operation_id": operation_id,
            "guild_id": guild_id,
            "user_id": user_id,
            "product_id": product_id,
            "product_id_lower": product_id_lower,
            "title": product.get("title", product_id),
            "price": price,
            "original_price": original_price,
            "before_cash": int(before_cash),
            "after_cash": int(after_cash),
            "seller_id": product.get("seller_id"),
            "kind": kind,
            "purchased_at": now,
        }
        if discount_code:
            log_doc["discount_code"] = str(discount_code)
        await self.purchase_logs.update_one(
            {"_id": operation_id},
            {"$setOnInsert": log_doc},
            upsert=True,
        )
        return await self.purchase_logs.find_one({"_id": operation_id})

    async def complete_product_purchase(
        self,
        *,
        operation_id: str,
        guild_id: int,
        user_id: int,
        product: dict,
    ) -> tuple[dict, bool]:
        now = _now()
        product_id = product["product_id"]
        product_id_lower = product["product_id_lower"]
        entitlement = await self.user_products.find_one_and_update(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "product_id_lower": product_id_lower,
                "operation_id": operation_id,
                "status": "pending",
            },
            {
                "$set": {
                    "product_id": product_id,
                    "title": product.get("title", product_id),
                    "terabox_url": product.get("terabox_url", ""),
                    "status": "purchased",
                    "purchased_at": now,
                    "updated_at": now,
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        if entitlement is not None:
            return entitlement, True

        entitlement = await self.user_products.find_one(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "product_id_lower": product_id_lower,
            }
        )
        if entitlement and entitlement.get("status") == "purchased":
            return entitlement, False
        raise RuntimeError("purchase entitlement could not be completed")

    async def grant_owned_product(
        self,
        guild_id: int,
        user_id: int,
        product: dict,
        *,
        operation_id: str,
    ) -> dict:
        """Directly grant a purchased entitlement, e.g. after a random draw.

        Unlike :meth:`reserve_product`/:meth:`complete_product_purchase`, this
        skips the pending-reservation dance: the caller has already charged a
        fixed draw price up front, so a duplicate draw of the same item should
        simply refresh the entitlement instead of racing a unique-index error.
        """
        now = _now()
        product_id = product["product_id"]
        product_id_lower = normalize_product_id(product_id)
        await self.user_products.update_one(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "product_id_lower": product_id_lower,
            },
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
                    "operation_id": operation_id,
                    "reserved_at": now,
                },
            },
            upsert=True,
        )
        return await self.user_products.find_one(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "product_id_lower": product_id_lower,
            }
        )

    async def reserve_product(
        self,
        guild_id: int,
        user_id: int,
        product: dict,
        *,
        original_price: int | None = None,
        quoted_price: int | None = None,
        coupon_code: str | None = None,
        promotion_code: str | None = None,
    ) -> dict:
        now = _now()
        original_price = int(
            product.get("price", 0) if original_price is None else original_price
        )
        quoted_price = int(original_price if quoted_price is None else quoted_price)
        if original_price < 0 or quoted_price < 0:
            raise ValueError("purchase prices must be zero or greater")

        operation_id = f"purchase:{uuid4().hex}"
        reservation = {
            "guild_id": guild_id,
            "user_id": user_id,
            "product_id": product["product_id"],
            "product_id_lower": product["product_id_lower"],
            "title": product.get("title", product["product_id"]),
            "terabox_url": product.get("terabox_url", ""),
            "status": "pending",
            "operation_id": operation_id,
            "original_price": original_price,
            "quoted_price": quoted_price,
            "coupon_code": coupon_code,
            "promotion_code": promotion_code,
            "reserved_at": now,
            "updated_at": now,
        }
        try:
            await self.user_products.insert_one(reservation)
        except DuplicateKeyError:
            existing = await self.user_products.find_one(
                {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "product_id_lower": product["product_id_lower"],
                }
            )
            if existing is None:
                raise
            return existing
        return reservation

    async def update_purchase_progress(self, operation_id: str, **updates) -> dict | None:
        if not updates:
            return await self.user_products.find_one({"operation_id": operation_id})
        updates["updated_at"] = _now()
        return await self.user_products.find_one_and_update(
            {"operation_id": operation_id, "status": "pending"},
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )

    async def release_product_reservation(
        self,
        guild_id: int,
        user_id: int,
        product_id: str,
        *,
        operation_id: str | None = None,
    ):
        query = {
            "guild_id": guild_id,
            "user_id": user_id,
            "product_id_lower": normalize_product_id(product_id),
            "status": "pending",
        }
        if operation_id is not None:
            query["operation_id"] = operation_id
        await self.user_products.delete_one(
            query
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

    async def list_owned_products(self, guild_id: int, user_id: int, limit: int | None = None):
        return (
            await self.user_products.find({"guild_id": guild_id, "user_id": user_id, "status": "purchased"})
            .sort("purchased_at", -1)
            .to_list(length=limit)
        )
