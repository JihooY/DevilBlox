from __future__ import annotations

from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc)


def keyed(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


class TicketStore:
    def __init__(self, db):
        self.collection = db["tickets"]
        self.transcripts = db["ticket_transcripts"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("channel_id", 1)], unique=True)
        await self.collection.create_index([("guild_id", 1), ("user_id", 1), ("kind", 1), ("status", 1)])
        await self.collection.create_index([("guild_id", 1), ("seller_id", 1), ("kind", 1), ("status", 1)])
        await self.transcripts.create_index([("guild_id", 1), ("channel_id", 1), ("message_id", 1)], unique=True)
        await self.transcripts.create_index([("guild_id", 1), ("ticket_id", 1), ("created_at", 1)])

    async def create(self, guild_id: int, kind: str, user_id: int, channel_id: int, **extra):
        doc = {
            "_id": f"{guild_id}:{channel_id}",
            "guild_id": guild_id,
            "kind": kind,
            "user_id": user_id,
            "channel_id": channel_id,
            "status": "open",
            "created_at": _now(),
            "updated_at": _now(),
            **extra,
        }
        await self.collection.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
        return doc

    async def get_open_for_user(self, guild_id: int, user_id: int, kind: str):
        return await self.collection.find_one(
            {"guild_id": guild_id, "user_id": user_id, "kind": kind, "status": "open"}
        )

    async def get_by_channel(self, guild_id: int, channel_id: int, kind: str | None = None):
        query = {"guild_id": guild_id, "channel_id": channel_id}
        if kind:
            query["kind"] = kind
        return await self.collection.find_one(query)

    async def close(self, guild_id: int, channel_id: int, **extra):
        update = {"status": "closed", "closed_at": _now(), "updated_at": _now(), **extra}
        await self.collection.update_one(
            {"guild_id": guild_id, "channel_id": channel_id},
            {"$set": update},
        )

    async def save_transcript(self, ticket: dict, messages: list[dict]):
        now = _now()
        guild_id = ticket["guild_id"]
        channel_id = ticket["channel_id"]
        ticket_id = ticket["_id"]
        await self.transcripts.delete_many({"guild_id": guild_id, "channel_id": channel_id})
        if messages:
            docs = [
                {
                    "_id": f"{guild_id}:{channel_id}:{message['message_id']}",
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "ticket_id": ticket_id,
                    "kind": ticket.get("kind"),
                    "ticket_user_id": ticket.get("user_id"),
                    "saved_at": now,
                    **message,
                }
                for message in messages
            ]
            for start in range(0, len(docs), 500):
                await self.transcripts.insert_many(docs[start : start + 500])

        await self.collection.update_one(
            {"_id": ticket_id},
            {
                "$set": {
                    "transcript_saved_at": now,
                    "transcript_message_count": len(messages),
                    "updated_at": now,
                }
            },
        )

    async def count_open_for_seller(self, guild_id: int, seller_id: int) -> int:
        return await self.collection.count_documents(
            {
                "guild_id": guild_id,
                "seller_id": seller_id,
                "kind": "purchase",
                "status": "open",
            }
        )

    async def list_open_purchase_tickets(self, guild_id: int):
        return await self.collection.find(
            {"guild_id": guild_id, "kind": "purchase", "status": "open"},
            {"seller_id": 1, "channel_id": 1},
        ).to_list(length=None)


class SellerStore:
    def __init__(self, db):
        self.collection = db["sellers"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("user_id", 1)], unique=True)

    async def upsert(self, guild_id: int, user_id: int, user_name: str):
        now = _now()
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {
                "$set": {"user_name": user_name, "guild_id": guild_id, "user_id": user_id, "updated_at": now},
                "$setOnInsert": {
                    "_id": keyed(guild_id, user_id),
                    "accrued_sell_money": 0,
                    "accrued_sell_count": 0,
                    "current_ticket_channel_ids": [],
                    "current_ticket_count": 0,
                    "ticket_disabled": False,
                    "disabled_reason": "",
                    "created_at": now,
                },
            },
            upsert=True,
        )

    async def list_active_options(self, guild_id: int):
        return await self.collection.find({"guild_id": guild_id}).sort("user_name", 1).to_list(length=25)

    async def get(self, guild_id: int, user_id: int):
        return await self.collection.find_one({"_id": keyed(guild_id, user_id)})

    async def set_ticket_state(self, guild_id: int, user_id: int, disabled: bool, reason: str = ""):
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {"$set": {"ticket_disabled": disabled, "disabled_reason": reason, "updated_at": _now()}},
        )

    async def _refresh_current_ticket_count(self, guild_id: int, user_id: int):
        doc = await self.collection.find_one(
            {"_id": keyed(guild_id, user_id)},
            {"current_ticket_channel_ids": 1},
        )
        channel_ids = (doc.get("current_ticket_channel_ids") or []) if doc else []
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {"$set": {"current_ticket_count": len(channel_ids), "updated_at": _now()}},
        )

    async def add_current_ticket(self, guild_id: int, user_id: int, channel_id: int):
        now = _now()
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {
                "$set": {"guild_id": guild_id, "user_id": user_id, "updated_at": now},
                "$setOnInsert": {
                    "_id": keyed(guild_id, user_id),
                    "user_name": str(user_id),
                    "accrued_sell_money": 0,
                    "accrued_sell_count": 0,
                    "current_ticket_count": 0,
                    "ticket_disabled": False,
                    "disabled_reason": "",
                    "created_at": now,
                },
                "$addToSet": {"current_ticket_channel_ids": channel_id},
            },
            upsert=True,
        )
        await self._refresh_current_ticket_count(guild_id, user_id)

    async def remove_current_ticket(self, guild_id: int, user_id: int, channel_id: int):
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {
                "$pull": {"current_ticket_channel_ids": channel_id},
                "$set": {"updated_at": _now()},
            },
        )
        await self._refresh_current_ticket_count(guild_id, user_id)

    async def set_current_tickets(self, guild_id: int, tickets_by_seller: dict[int, list[int]]):
        sellers = await self.collection.find({"guild_id": guild_id}, {"user_id": 1}).to_list(length=None)
        now = _now()
        for seller in sellers:
            user_id = seller["user_id"]
            channel_ids = list(dict.fromkeys(tickets_by_seller.get(user_id, [])))
            await self.collection.update_one(
                {"_id": keyed(guild_id, user_id)},
                {
                    "$set": {
                        "current_ticket_channel_ids": channel_ids,
                        "current_ticket_count": len(channel_ids),
                        "updated_at": now,
                    }
                },
            )

    async def add_sale(self, guild_id: int, user_id: int, amount: int):
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {
                "$inc": {"accrued_sell_money": amount, "accrued_sell_count": 1},
                "$set": {"updated_at": _now()},
            },
        )

    async def import_legacy_seller(self, guild_id: int, doc: dict):
        user_id = int(doc["user_id"])
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {
                "$set": {
                    "_id": keyed(guild_id, user_id),
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "user_name": doc.get("user_name", str(user_id)),
                    "accrued_sell_money": int(doc.get("accrue_sell_money", 0) or 0),
                    "accrued_sell_count": int(doc.get("accrue_sell_number", 0) or 0),
                    "current_ticket_channel_ids": doc.get("current_ticket_channel_ids", []),
                    "current_ticket_count": int(doc.get("current_ticket_count", 0) or 0),
                    "ticket_disabled": bool(doc.get("ticket_disable", 0)),
                    "disabled_reason": doc.get("reason") or "",
                    "updated_at": _now(),
                },
                "$setOnInsert": {"created_at": _now()},
            },
            upsert=True,
        )


class MiddlemanStore:
    def __init__(self, db):
        self.collection = db["middlemen"]

    async def ensure_indexes(self):
        await self.collection.create_index([("guild_id", 1), ("user_id", 1)], unique=True)

    async def upsert(self, guild_id: int, user_id: int, user_name: str):
        now = _now()
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {
                "$set": {"user_name": user_name, "guild_id": guild_id, "user_id": user_id, "updated_at": now},
                "$setOnInsert": {
                    "_id": keyed(guild_id, user_id),
                    "accrued_trade_money": 0,
                    "accrued_trade_count": 0,
                    "created_at": now,
                },
            },
            upsert=True,
        )

    async def get(self, guild_id: int, user_id: int):
        return await self.collection.find_one({"_id": keyed(guild_id, user_id)})

    async def list_all(self, guild_id: int):
        return await self.collection.find({"guild_id": guild_id}).sort("user_name", 1).to_list(length=25)

    async def add_trade(self, guild_id: int, user_id: int, amount: int):
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {
                "$inc": {"accrued_trade_money": amount, "accrued_trade_count": 1},
                "$set": {"updated_at": _now()},
            },
        )

    async def import_legacy_middleman(self, guild_id: int, doc: dict):
        user_id = int(doc["user_id"])
        await self.collection.update_one(
            {"_id": keyed(guild_id, user_id)},
            {
                "$set": {
                    "_id": keyed(guild_id, user_id),
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "user_name": doc.get("user_name", str(user_id)),
                    "accrued_trade_money": int(doc.get("accrue_trade_money", 0) or 0),
                    "accrued_trade_count": int(doc.get("accrue_trade_number", 0) or 0),
                    "updated_at": _now(),
                },
                "$setOnInsert": {"created_at": _now()},
            },
            upsert=True,
        )
