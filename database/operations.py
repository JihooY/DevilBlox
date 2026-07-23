from __future__ import annotations

from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc)


class OperationsStateStore:
    def __init__(self, db):
        self.collection = db["operations_state"]

    async def ensure_indexes(self) -> None:
        return None

    async def get(self) -> dict | None:
        return await self.collection.find_one({"_id": "global"})

    async def set_mitigation(
        self,
        *,
        enabled: bool,
        forced: bool,
        reason: str | None,
    ) -> None:
        await self.collection.update_one(
            {"_id": "global"},
            {
                "$set": {
                    "enabled": enabled,
                    "forced": forced,
                    "reason": reason,
                    "updated_at": _now(),
                }
            },
            upsert=True,
        )
