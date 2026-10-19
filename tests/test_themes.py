from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.webhook.async_ import handle_message_parameters
from PIL import Image

from cogs.cogs_verify import VerificationCog
from cogs.theme_views import GifPickerView, ThemeManagerView, ThemeModal, open_theme_manager
from database.themes import ThemeStore, default_themes
from services.themes import MAX_GIF_BYTES, ThemeService, validate_gif
from utils.embeds import BRAND_LOGO_FILENAME
from utils.gifs import (
    ALL_GIFS, active_gif_theme, available_gifs, choose_gif, clear_gif_asset_caches,
    configure_gif_delivery, gif_file, gif_media_url, panel_embed_edit_kwargs,
    set_active_gif_theme, set_gif_suppressed,
)


def gif_bytes():
    data = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(data, format="GIF")
    return data.getvalue()


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def __aiter__(self):
        for doc in deepcopy(self.docs):
            yield doc


class Collection:
    def __init__(self):
        self.docs = {}

    async def find_one(self, query):
        return deepcopy(self.docs.get(query["_id"]))

    async def replace_one(self, query, doc, *, upsert):
        await asyncio.sleep(0)
        self.docs[query["_id"]] = deepcopy(doc)

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = deepcopy(doc)

    async def delete_one(self, query):
        self.docs.pop(query["_id"], None)

    def find(self, query, projection=None):
        return Cursor([doc for doc in self.docs.values()
                       if all(doc.get(key) == value for key, value in query.items())])


def interaction(*, admin=True, owner=99, guild_id=11):
    return SimpleNamespace(
        guild_id=guild_id,
        guild=SimpleNamespace(id=guild_id, filesize_limit=10 * 1024 * 1024),
        user=SimpleNamespace(id=owner, guild_permissions=SimpleNamespace(administrator=admin)),
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(),
                                 send_message=AsyncMock(), send_modal=AsyncMock(),
                                 is_done=Mock(return_value=True)),
        edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
    )


class ThemeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = {"gif_themes": Collection(), "gif_theme_media": Collection()}
        self.store = ThemeStore(self.db)
        self.service = ThemeService(self.store)
        self.bot = SimpleNamespace(themes=self.service, get_cog=lambda _: None)
        self.cache = tempfile.TemporaryDirectory()
        self.asset_patch = patch("utils.assets.ASSETS_DIR", Path(self.cache.name))
        self.asset_patch.start()
        configure_gif_delivery(mode="cdn", cdn_base_url="https://cdn.example.test/gifs")
        set_gif_suppressed(False)
        clear_gif_asset_caches()

    def tearDown(self):
        for guild_id in (11, 22):
            set_active_gif_theme(guild_id, None)
        self.asset_patch.stop()
        self.cache.cleanup()
        clear_gif_asset_caches()
        set_gif_suppressed(False)
        configure_gif_delivery(mode="local", cdn_base_url=None)

    async def create_theme(self, name="할로윈"):
        state, theme_id = await self.service.create(11, name)
        return theme_id

    async def test_active_pool_is_isolated_and_replaces_old_attachments_and_urls(self):
        theme_id = await self.create_theme()
        await self.service.add_existing(11, theme_id, ["success.gif"])
        await self.service.activate(11, theme_id)
        set_active_gif_theme(22, ("denied.gif",))
        old = SimpleNamespace(filename="denied.gif")
        self.assertEqual(choose_gif(ALL_GIFS, [old], guild_id=11,
                                   existing_urls=["https://cdn.example.test/denied.gif"]), "success.gif")
        self.assertEqual(choose_gif(ALL_GIFS, guild_id=22), "denied.gif")
        self.assertEqual(available_gifs(ALL_GIFS), ALL_GIFS)
        self.assertIsNone(choose_gif(None, guild_id=11))
        set_gif_suppressed(True)
        self.assertIsNone(choose_gif(ALL_GIFS, guild_id=11))

    async def test_empty_theme_and_last_active_gif_are_guarded(self):
        theme_id = await self.create_theme()
        with self.assertRaisesRegex(ValueError, "먼저 추가"):
            await self.service.activate(11, theme_id)
        await self.service.add_existing(11, theme_id, ["success.gif", "success.gif"])
        state = await self.service.activate(11, theme_id)
        self.assertEqual(state["themes"][theme_id]["gifs"], ["success.gif"])
        with self.assertRaisesRegex(ValueError, "최소 1개"):
            await self.service.remove(11, theme_id, ["success.gif"])
        with self.assertRaisesRegex(ValueError, "삭제할 수"):
            await self.service.delete(11, theme_id)
        with self.assertRaisesRegex(ValueError, "삭제할 수"):
            await self.service.delete(11, "default")

    async def test_concurrent_edits_do_not_drop_gifs_or_leak_themes(self):
        theme_id = await self.create_theme()
        await asyncio.gather(
            self.service.add_existing(11, theme_id, ["success.gif"]),
            self.service.add_existing(11, theme_id, ["denied.gif"]),
        )
        state = await self.service.state(11)
        self.assertEqual(set(state["themes"][theme_id]["gifs"]), {"success.gif", "denied.gif"})
        other = await self.service.state(22)
        self.assertEqual(other, default_themes(22))
        with self.assertRaisesRegex(ValueError, "같은 이름"):
            await self.service.create(11, " 할로윈 ")

    async def test_uploaded_media_survives_restart_and_attaches_in_cdn_mode(self):
        theme_id = await self.create_theme()
        state = await self.service.upload(11, theme_id, [("halloween.gif", gif_bytes())])
        filename = state["themes"][theme_id]["gifs"][0]
        await self.service.activate(11, theme_id)
        (Path(self.cache.name) / "gifs_uploaded" / filename).unlink()
        set_active_gif_theme(11, None)
        clear_gif_asset_caches()
        await ThemeService(self.store).load()
        self.assertEqual(active_gif_theme(11), (filename,))
        self.assertEqual(choose_gif(ALL_GIFS, guild_id=11), filename)
        self.assertEqual(gif_media_url(filename), f"attachment://{filename}")
        file = gif_file(filename)
        self.assertIsInstance(file, discord.File)
        self.assertEqual(file.fp.read(), gif_bytes())
        file.close()

    async def test_invalid_upload_batch_makes_no_partial_changes(self):
        theme_id = await self.create_theme()
        with self.assertRaises(ValueError):
            await self.service.upload(11, theme_id, [("ok.gif", gif_bytes()), ("fake.gif", b"invalid")])
        self.assertEqual(self.store.media.docs, {})
        state = await self.service.state(11)
        self.assertEqual(state["themes"][theme_id]["gifs"], [])
        with self.assertRaisesRegex(ValueError, "8MB"):
            validate_gif(b"x" * (MAX_GIF_BYTES + 1))
        with self.assertRaisesRegex(ValueError, ".gif"):
            await self.service.upload(11, theme_id, [("image.png", gif_bytes())])

    async def test_removing_upload_deletes_stored_bytes_without_affecting_other_themes(self):
        theme_id = await self.create_theme()
        state = await self.service.upload(11, theme_id, [("halloween.gif", gif_bytes())])
        filename = state["themes"][theme_id]["gifs"][0]
        await self.service.remove(11, theme_id, [filename])
        self.assertNotIn(filename, self.store.media.docs)
        await self.service.delete(11, theme_id)
        self.assertNotIn(theme_id, (await self.service.state(11))["themes"])

    async def test_theme_switch_clears_stale_gif_while_preserving_other_attachments(self):
        set_active_gif_theme(11, ("success.gif",))
        message = SimpleNamespace(
            guild=SimpleNamespace(id=11), embeds=[], components=[],
            attachments=[SimpleNamespace(filename="denied.gif"), SimpleNamespace(filename="notes.pdf")],
        )
        with patch("utils.gifs.branded_files", return_value=[SimpleNamespace(filename=BRAND_LOGO_FILENAME)]):
            kwargs = panel_embed_edit_kwargs(discord.Embed(title="test"), message, ALL_GIFS)
        self.assertEqual(kwargs["embed"].image.url, "https://cdn.example.test/gifs/success.gif")
        self.assertEqual([a.filename for a in kwargs["attachments"]], [BRAND_LOGO_FILENAME, "notes.pdf"])

    async def test_verification_panel_uses_theme_and_removes_old_banner(self):
        theme_id = await self.create_theme()
        await self.service.upload(11, theme_id, [("halloween.gif", gif_bytes())])
        state = await self.service.activate(11, theme_id)
        filename = state["themes"][theme_id]["gifs"][0]
        message = SimpleNamespace(
            embeds=[], components=[], edit=AsyncMock(),
            attachments=[SimpleNamespace(filename=BRAND_LOGO_FILENAME),
                         SimpleNamespace(filename="verify_panel.gif"), SimpleNamespace(filename="notes.pdf")],
        )
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        guild = SimpleNamespace(id=11, get_channel=lambda _: channel)
        settings = SimpleNamespace(get=AsyncMock(return_value={
            "channels": {"verify": 1}, "meta": {"verify_panel_message_id": 2},
        }))
        cog = VerificationCog(SimpleNamespace(add_view=Mock(), repos=SimpleNamespace(settings=settings)))
        await cog.refresh_verify_panel(guild)
        kwargs = message.edit.await_args.kwargs
        self.assertEqual([a.filename for a in kwargs["attachments"]], [BRAND_LOGO_FILENAME, "notes.pdf", filename])
        gallery = next(c for c in kwargs["view"].walk_children() if isinstance(c, discord.ui.MediaGallery))
        self.assertEqual(str(gallery.items[0].media.url), f"attachment://{filename}")
        kwargs["attachments"][-1].close()

    async def test_manager_and_modal_recheck_permissions_and_server(self):
        state = await self.service.state(11)
        manager = ThemeManagerView(self.bot, 11, 99, state)
        for event in (interaction(admin=False), interaction(owner=100), interaction(guild_id=22)):
            event.response.is_done.return_value = False
            self.assertFalse(await manager.interaction_check(event))
            event.response.send_message.assert_awaited_once()
        modal = ThemeModal(manager, upload=True)
        self.assertFalse(await modal.interaction_check(interaction(admin=False)))
        self.assertTrue(await modal.interaction_check(interaction()))
        event = interaction(admin=False)
        await open_theme_manager(self.bot, event)
        event.followup.send.assert_not_awaited()

    async def test_modal_upload_and_gif_pagination_serialize_as_valid_components(self):
        theme_id = await self.create_theme()
        manager = ThemeManagerView(self.bot, 11, 99, await self.service.state(11), theme_id)
        modal = ThemeModal(manager, upload=True)
        modal.files._values = [SimpleNamespace(
            size=len(gif_bytes()), filename="halloween.gif", read=AsyncMock(return_value=gif_bytes()),
        )]
        event = interaction()
        await modal.on_submit(event)
        updated = event.edit_original_response.await_args.kwargs["view"]
        self.assertIsNone(event.edit_original_response.await_args.kwargs["content"])
        self.assertIn("GIF를 추가했습니다.", "\n".join(
            item.content for item in updated.walk_children() if isinstance(item, discord.ui.TextDisplay)
        ))
        with handle_message_parameters(**event.edit_original_response.await_args.kwargs) as params:
            self.assertIsNone(params.payload["content"])
            self.assertTrue(discord.MessageFlags._from_value(params.payload["flags"]).components_v2)
        self.assertTrue(updated.to_components())
        self.assertEqual(len((await self.service.state(11))["themes"][theme_id]["gifs"]), 1)
        self.assertTrue(modal.to_components())
        picker = GifPickerView(manager, await self.service.state(11), {}, "add")
        selector = next(c for c in picker.walk_children() if isinstance(c, discord.ui.Select))
        self.assertEqual(len(selector.options), 25)
        next_page = GifPickerView(manager, await self.service.state(11), {}, "add", 1)
        selector = next(c for c in next_page.walk_children() if isinstance(c, discord.ui.Select))
        self.assertEqual(len(selector.options), len(ALL_GIFS) - 25)
        self.assertTrue(next_page.to_components())

    async def test_create_modal_puts_success_notice_in_container_instead_of_content(self):
        manager = ThemeManagerView(self.bot, 11, 99, await self.service.state(11))
        modal = ThemeModal(manager, upload=False)
        modal.name._value = "스텔"
        event = interaction()
        await modal.on_submit(event)
        kwargs = event.edit_original_response.await_args.kwargs
        with handle_message_parameters(**kwargs) as params:
            self.assertIsNone(params.payload["content"])
            self.assertTrue(discord.MessageFlags._from_value(params.payload["flags"]).components_v2)
        text = "\n".join(item.content for item in kwargs["view"].walk_children()
                         if isinstance(item, discord.ui.TextDisplay))
        self.assertIn("테마를 만들었습니다.", text)
        self.assertIn("스텔", text)

    async def test_public_panel_and_picker_are_persistent_and_modal_is_bound_to_clicking_admin(self):
        manager = ThemeManagerView(self.bot, 11, None, await self.service.state(11))
        self.assertTrue(manager.is_persistent())
        ids = [c.custom_id for c in manager.walk_children() if c.is_dispatchable()]
        self.assertEqual(len(ids), len(set(ids)))
        event = interaction(owner=100)
        self.assertTrue(await manager.interaction_check(event))
        self.assertFalse(await manager.interaction_check(interaction(admin=False)))
        await manager.create(event)
        modal = event.response.send_modal.await_args.args[0]
        self.assertTrue(await modal.interaction_check(interaction(owner=100)))
        self.assertFalse(await modal.interaction_check(interaction(owner=99)))
        self.assertTrue(GifPickerView(manager, await self.service.state(11), {}, "remove").is_persistent())

    async def test_public_modal_returns_private_result_and_refreshes_saved_panel(self):
        cog = SimpleNamespace(refresh_panel=AsyncMock())
        self.bot.get_cog = lambda _: cog
        manager = ThemeManagerView(self.bot, 11, None, await self.service.state(11))
        modal = ThemeModal(manager, upload=False, owner_id=100)
        modal.name._value = "스텔"
        event = interaction(owner=100)
        await modal.on_submit(event)
        result = event.edit_original_response.await_args.kwargs["view"]
        self.assertEqual(result.owner_id, 100)
        self.assertEqual(result.timeout, 900)
        cog.refresh_panel.assert_awaited_once_with(11, selected=result.selected)


if __name__ == "__main__":
    unittest.main()
