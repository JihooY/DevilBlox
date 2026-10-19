from __future__ import annotations

import asyncio
import io
import warnings
from collections import defaultdict
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from utils.assets import asset_path
from utils.gifs import ALL_GIFS, clear_gif_asset_caches, set_active_gif_theme

MAX_THEMES = 25
MAX_THEME_GIFS = 100
MAX_GIF_BYTES = 8 * 1024 * 1024


def validate_gif(data: bytes) -> None:
    if not data or len(data) > MAX_GIF_BYTES:
        raise ValueError("GIF 파일은 8MB 이하여야 합니다.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "GIF":
                    raise ValueError("실제 GIF 형식의 파일만 등록할 수 있습니다.")
                if image.width * image.height > 16_000_000:
                    raise ValueError("GIF 해상도가 너무 큽니다.")
                image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("올바른 GIF 파일을 첨부해주세요.") from exc


class ThemeService:
    def __init__(self, store):
        self.store = store
        self.locks = defaultdict(asyncio.Lock)

    @staticmethod
    def apply(state: dict) -> None:
        theme = state["themes"].get(state["active"])
        set_active_gif_theme(state["_id"], tuple(theme["gifs"]) if theme else ())

    async def load(self) -> None:
        # Files are backed by MongoDB; rebuild the local cache after a redeploy.
        async for media in self.store.media.find({}):
            await asyncio.to_thread(self._cache_media, media["_id"], bytes(media["data"]))
        async for state in self.store.collection.find({}):
            self.apply(state)

    @staticmethod
    def _cache_media(filename: str, data: bytes) -> None:
        # Only service-generated UUID names may become filesystem paths.
        if not (filename.startswith("theme_") and len(filename) == 42
                and filename[6:-4].isalnum() and filename.endswith(".gif")):
            raise ValueError("Invalid theme GIF filename")
        path = asset_path("gifs_uploaded", filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        clear_gif_asset_caches()

    async def state(self, guild_id: int) -> dict:
        async with self.locks[guild_id]:
            state = await self.store.get(guild_id)
            self.apply(state)
            return state

    async def _save(self, state: dict) -> dict:
        await self.store.save(state)
        self.apply(state)
        return state

    @staticmethod
    def theme(state: dict, theme_id: str) -> dict:
        if theme_id not in state["themes"]:
            raise ValueError("삭제된 테마입니다. 관리 화면을 다시 열어주세요.")
        return state["themes"][theme_id]

    async def create(self, guild_id: int, name: str) -> tuple[dict, str]:
        name = name.strip()
        if not name or len(name) > 40:
            raise ValueError("테마 이름은 1~40자로 입력해주세요.")
        async with self.locks[guild_id]:
            state = await self.store.get(guild_id)
            if len(state["themes"]) >= MAX_THEMES:
                raise ValueError("테마는 서버당 최대 25개까지 만들 수 있습니다.")
            if any(t["name"].casefold() == name.casefold() for t in state["themes"].values()):
                raise ValueError("이미 같은 이름의 테마가 있습니다.")
            theme_id = uuid4().hex
            state["themes"][theme_id] = {"name": name, "gifs": []}
            return await self._save(state), theme_id

    async def activate(self, guild_id: int, theme_id: str) -> dict:
        async with self.locks[guild_id]:
            state = await self.store.get(guild_id)
            if not self.theme(state, theme_id)["gifs"]:
                raise ValueError("GIF를 먼저 추가한 뒤 테마를 적용해주세요.")
            state["active"] = theme_id
            return await self._save(state)

    async def add_existing(self, guild_id: int, theme_id: str, filenames: list[str]) -> dict:
        async with self.locks[guild_id]:
            state = await self.store.get(guild_id)
            theme = self.theme(state, theme_id)
            if any(name not in ALL_GIFS for name in filenames):
                raise ValueError("기본 GIF 목록에서 선택해주세요.")
            gifs = list(dict.fromkeys([*theme["gifs"], *filenames]))
            if len(gifs) > MAX_THEME_GIFS:
                raise ValueError("테마당 GIF는 최대 100개까지 등록할 수 있습니다.")
            theme["gifs"] = gifs
            return await self._save(state)

    async def upload(self, guild_id: int, theme_id: str, uploads: list[tuple[str, bytes]]) -> dict:
        if not uploads or len(uploads) > 5:
            raise ValueError("한 번에 GIF 1~5개를 첨부해주세요.")
        for label, data in uploads:
            if not label.casefold().endswith(".gif"):
                raise ValueError(".gif 파일만 첨부해주세요.")
            await asyncio.to_thread(validate_gif, data)
        async with self.locks[guild_id]:
            state = await self.store.get(guild_id)
            theme = self.theme(state, theme_id)
            if len(theme["gifs"]) + len(uploads) > MAX_THEME_GIFS:
                raise ValueError("테마당 GIF는 최대 100개까지 등록할 수 있습니다.")
            saved = []
            try:
                for label, data in uploads:
                    filename = f"theme_{uuid4().hex}.gif"
                    await self.store.save_media(guild_id, filename, label[:100], data)
                    saved.append(filename)
                    await asyncio.to_thread(self._cache_media, filename, data)
            except Exception:
                for filename in saved:
                    await self.store.delete_media(guild_id, filename)
                raise
            theme["gifs"].extend(saved)
            # Retain media if a metadata write has an uncertain outcome so a
            # committed theme can always be restored after reconnecting.
            return await self._save(state)

    async def remove(self, guild_id: int, theme_id: str, filenames: list[str]) -> dict:
        async with self.locks[guild_id]:
            state = await self.store.get(guild_id)
            theme = self.theme(state, theme_id)
            remaining = [name for name in theme["gifs"] if name not in filenames]
            if state["active"] == theme_id and not remaining:
                raise ValueError("사용 중인 테마에는 GIF가 최소 1개 필요합니다.")
            removed = set(theme["gifs"]) - set(remaining)
            theme["gifs"] = remaining
            await self._save(state)
            await self._remove_unused_media(state, removed)
            return state

    async def delete(self, guild_id: int, theme_id: str) -> dict:
        async with self.locks[guild_id]:
            state = await self.store.get(guild_id)
            self.theme(state, theme_id)
            if theme_id == "default" or state["active"] == theme_id:
                raise ValueError("기본 테마나 사용 중인 테마는 삭제할 수 없습니다.")
            removed = state["themes"].pop(theme_id)["gifs"]
            await self._save(state)
            await self._remove_unused_media(state, removed)
            return state

    async def _remove_unused_media(self, state: dict, filenames) -> None:
        referenced = {name for theme in state["themes"].values() for name in theme["gifs"]}
        for filename in filenames:
            if filename not in ALL_GIFS and filename not in referenced:
                await self.store.delete_media(state["_id"], filename)
        # Keep cache files for sends already in flight. MongoDB is authoritative
        # for restoration; removed GIFs never re-enter any selection pool.
