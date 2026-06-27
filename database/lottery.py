from __future__ import annotations

import secrets
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc)


def new_lottery_id() -> str:
    return secrets.token_hex(4)


class LotteryStore:
    def __init__(self, db):
        self.events = db["lottery_events"]
        self.entries = db["lottery_entries"]

    async def ensure_indexes(self):
        await self.events.create_index([("guild_id", 1), ("status", 1), ("created_at", -1)])
        await self.events.create_index([("guild_id", 1), ("message_id", 1)])
        await self.entries.create_index([("guild_id", 1), ("event_id", 1), ("user_id", 1)], unique=True)
        await self.entries.create_index([("guild_id", 1), ("user_id", 1), ("created_at", -1)])
        await self.entries.create_index([("guild_id", 1), ("event_id", 1), ("is_winner", 1)])

    async def create_event(self, guild_id: int, title: str, winner_count: int, created_by: int):
        now = _now()
        doc = {
            "_id": new_lottery_id(),
            "guild_id": guild_id,
            "title": title.strip()[:100] or "도형 복권 추첨",
            "winner_count": max(1, int(winner_count)),
            "status": "open",
            "created_by": created_by,
            "channel_id": None,
            "message_id": None,
            "winner_user_ids": [],
            "created_at": now,
            "updated_at": now,
        }
        await self.events.insert_one(doc)
        return doc

    async def attach_panel(self, guild_id: int, event_id: str, channel_id: int, message_id: int):
        await self.events.update_one(
            {"_id": event_id, "guild_id": guild_id},
            {"$set": {"channel_id": channel_id, "message_id": message_id, "updated_at": _now()}},
        )

    async def get_event(self, guild_id: int, event_id: str):
        return await self.events.find_one({"_id": event_id.strip(), "guild_id": guild_id})

    async def get_event_by_message(self, guild_id: int, message_id: int):
        return await self.events.find_one({"guild_id": guild_id, "message_id": message_id})

    async def latest_event(self, guild_id: int, statuses: tuple[str, ...] = ("open", "drawn")):
        return await self.events.find_one(
            {"guild_id": guild_id, "status": {"$in": list(statuses)}},
            sort=[("created_at", -1)],
        )

    async def update_winner_count(self, guild_id: int, event_id: str, winner_count: int):
        await self.events.update_one(
            {"_id": event_id, "guild_id": guild_id, "status": "open"},
            {"$set": {"winner_count": max(1, int(winner_count)), "updated_at": _now()}},
        )
        return await self.get_event(guild_id, event_id)

    async def add_entry(self, event: dict, user_id: int, user_name: str) -> tuple[dict, bool]:
        now = _now()
        entry_id = f"{event['_id']}:{user_id}"
        result = await self.entries.update_one(
            {"_id": entry_id},
            {
                "$set": {
                    "user_name": user_name,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "_id": entry_id,
                    "guild_id": event["guild_id"],
                    "event_id": event["_id"],
                    "user_id": user_id,
                    "status": "entered",
                    "is_winner": False,
                    "shapes": [],
                    "opened": False,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return await self.get_entry(event["guild_id"], event["_id"], user_id), result.upserted_id is not None

    async def get_entry(self, guild_id: int, event_id: str, user_id: int):
        return await self.entries.find_one({"guild_id": guild_id, "event_id": event_id, "user_id": user_id})

    async def count_entries(self, guild_id: int, event_id: str) -> int:
        return await self.entries.count_documents({"guild_id": guild_id, "event_id": event_id})

    async def list_entries(self, guild_id: int, event_id: str):
        return await self.entries.find({"guild_id": guild_id, "event_id": event_id}).sort("created_at", 1).to_list(length=None)

    async def save_draw(self, event: dict, assignments: dict[int, tuple[bool, list[str]]], winner_user_ids: list[int]):
        now = _now()
        for user_id, (is_winner, shapes) in assignments.items():
            await self.entries.update_one(
                {"guild_id": event["guild_id"], "event_id": event["_id"], "user_id": user_id},
                {
                    "$set": {
                        "status": "drawn",
                        "is_winner": is_winner,
                        "shapes": shapes,
                        "drawn_at": now,
                        "updated_at": now,
                    }
                },
            )

        await self.events.update_one(
            {"_id": event["_id"], "guild_id": event["guild_id"]},
            {
                "$set": {
                    "status": "drawn",
                    "winner_count": len(winner_user_ids),
                    "winner_user_ids": winner_user_ids,
                    "drawn_at": now,
                    "updated_at": now,
                }
            },
        )
        return await self.get_event(event["guild_id"], event["_id"])

    async def mark_opened(self, guild_id: int, event_id: str, user_id: int):
        now = _now()
        await self.entries.update_one(
            {"guild_id": guild_id, "event_id": event_id, "user_id": user_id},
            {"$set": {"opened": True, "opened_at": now, "updated_at": now}},
        )
        return await self.get_entry(guild_id, event_id, user_id)

    async def latest_drawn_for_user(self, guild_id: int, user_id: int):
        entries = await self.entries.find(
            {"guild_id": guild_id, "user_id": user_id},
        ).sort("created_at", -1).to_list(length=25)
        for entry in entries:
            event = await self.events.find_one(
                {"_id": entry["event_id"], "guild_id": guild_id, "status": "drawn"}
            )
            if event is not None:
                return event, entry
        return None, None

    async def latest_open_for_user(self, guild_id: int, user_id: int):
        entries = await self.entries.find(
            {"guild_id": guild_id, "user_id": user_id},
        ).sort("created_at", -1).to_list(length=25)
        for entry in entries:
            event = await self.events.find_one(
                {"_id": entry["event_id"], "guild_id": guild_id, "status": "open"}
            )
            if event is not None:
                return event, entry
        return None, None
