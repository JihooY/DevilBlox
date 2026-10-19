from __future__ import annotations

from copy import deepcopy

from utils.gifs import ALL_GIFS


def default_themes(guild_id: int) -> dict:
    return {
        "_id": guild_id,
        "active": "default",
        "themes": {"default": {"name": "기본", "gifs": list(ALL_GIFS)}},
    }


class ThemeStore:
    def __init__(self, db):
        self.collection = db["gif_themes"]
        self.media = db["gif_theme_media"]

    async def ensure_indexes(self):
        await self.media.create_index("guild_id")

    async def get(self, guild_id: int) -> dict:
        return await self.collection.find_one({"_id": guild_id}) or default_themes(guild_id)

    async def save(self, state: dict) -> None:
        await self.collection.replace_one({"_id": state["_id"]}, deepcopy(state), upsert=True)

    async def save_media(self, guild_id: int, filename: str, label: str, data: bytes) -> None:
        await self.media.insert_one({
            "_id": filename, "guild_id": guild_id, "label": label, "data": data,
        })

    async def delete_media(self, guild_id: int, filename: str) -> None:
        await self.media.delete_one({"_id": filename, "guild_id": guild_id})
