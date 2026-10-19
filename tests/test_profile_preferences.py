from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
from discord.ext import commands

from cogs.cogs_alarms import AlarmView
from cogs.cogs_profile import ProfileCog
from cogs.cogs_verify import VerifyPad
from cogs.profile_views import ProfileView, send_profile
from database.users import UserStore, user_key
from services.user_preferences import toggle_notification
from utils.preferences import LANGUAGES, default_language


class Collection:
    def __init__(self):
        self.docs = {}

    async def update_one(self, query, update, *, upsert=False):
        key = query["_id"]
        if key not in self.docs:
            if not upsert:
                return
            self.docs[key] = deepcopy(update.get("$setOnInsert", {}))
        self.docs[key].update(deepcopy(update.get("$set", {})))

    async def find_one(self, query):
        return deepcopy(self.docs.get(query["_id"]))


def fixture(language="ko", role_ids=None):
    roles = {key: SimpleNamespace(id=index) for index, key in enumerate(
        ("alarm_announcement", "alarm_seller", "alarm_stock"), start=10,
    )}
    settings = {"roles": {key: role.id for key, role in roles.items()}}
    if role_ids is not None:
        settings["roles"] = role_ids
    guild = SimpleNamespace(id=1, get_role=lambda role_id: next(
        (role for role in roles.values() if role.id == role_id), None,
    ))
    member = SimpleNamespace(id=2, mention="<@2>", guild=guild, roles=[])

    async def add(role, **kwargs):
        await asyncio.sleep(0)
        member.roles.append(role)

    async def remove(role, **kwargs):
        await asyncio.sleep(0)
        member.roles.remove(role)

    member.add_roles = AsyncMock(side_effect=add)
    member.remove_roles = AsyncMock(side_effect=remove)
    guild.fetch_member = AsyncMock(return_value=member)
    collection = Collection()
    collection.docs[user_key(1, 2)] = {
        "_id": user_key(1, 2), "default_language": language, "cash": 12000,
        "points": 20, "accrued_spent": 5000, "verified_at": None,
    }
    repos = SimpleNamespace(
        users=UserStore({"users": collection}),
        settings=SimpleNamespace(get=AsyncMock(return_value=settings)),
    )
    event = SimpleNamespace(
        user=member, guild=guild,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(),
                                 is_done=lambda: False, edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
    )
    view = ProfileView(repos, member, collection.docs[user_key(1, 2)], settings)
    return repos, event, view


def displayed_text(view):
    return "\n".join(item.content for item in view.walk_children()
                     if isinstance(item, discord.ui.TextDisplay))


class PreferencePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_language_is_server_specific_and_survives_new_store_instance(self):
        repos, _, _ = fixture()
        await repos.users.set_default_language(1, 2, "ja")
        collection = repos.users.collection
        restarted = UserStore({"users": collection})
        self.assertEqual(default_language(await restarted.get(1, 2)), "ja")
        self.assertEqual(default_language(await restarted.ensure_user(9, 2)), "ko")
        user = await restarted.get(1, 2)
        self.assertEqual((user["cash"], user["points"], user["accrued_spent"]), (12000, 20, 5000))

    async def test_missing_invalid_and_legacy_language_fallback(self):
        for user in (None, {}, {"default_language": None}, {"default_language": "fr"}):
            self.assertEqual(default_language(user), "ko")
        repos, _, _ = fixture()
        with self.assertRaises(ValueError):
            await repos.users.set_default_language(1, 2, "fr")
        self.assertEqual(default_language(await repos.users.get(1, 2)), "ko")


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_has_brand_gif_language_default_and_three_alarm_buttons(self):
        for language in LANGUAGES:
            repos, event, _ = fixture(language)
            with patch("cogs.profile_views.choose_gif", return_value="profile.gif"), patch(
                "cogs.profile_views.gif_media_url", return_value="attachment://profile.gif",
            ), patch("cogs.profile_views.profile_send_kwargs", side_effect=lambda view: {"view": view}):
                view = await send_profile(event, repos, welcome=True)
            self.assertIsInstance(view, discord.ui.LayoutView)
            children = list(view.walk_children())
            self.assertTrue(any(isinstance(item, discord.ui.Thumbnail) for item in children))
            self.assertTrue(any(isinstance(item, discord.ui.MediaGallery) for item in children))
            select = next(item for item in children if isinstance(item, discord.ui.Select))
            self.assertEqual([item.value for item in select.options if item.default], [language])
            self.assertEqual(len([item for item in children if isinstance(item, discord.ui.Button)]), 3)
            self.assertTrue(event.followup.send.await_args.kwargs["ephemeral"])
            event.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            self.assertIsNotNone(view.message)

    async def test_language_change_saves_and_rebuilds_entire_profile_in_chosen_language(self):
        repos, event, view = fixture()
        await view.set_language(event, "en")
        self.assertEqual(default_language(await repos.users.get(1, 2)), "en")
        self.assertIn("Default language", displayed_text(view))
        self.assertIn("Ticket status", displayed_text(view))
        self.assertIs(event.edit_original_response.await_args.kwargs["view"], view)
        self.assertNotIn("attachments", event.edit_original_response.await_args.kwargs)

    async def test_other_user_and_other_guild_cannot_change_profile(self):
        repos, event, view = fixture()
        for change in ("user", "guild"):
            with self.subTest(change=change):
                event.user = SimpleNamespace(id=3 if change == "user" else 2)
                event.guild = SimpleNamespace(id=1 if change == "user" else 9)
                await view.set_language(event, "zh")
                await view.toggle_alarm(event, "alarm_stock")
        self.assertEqual(default_language(await repos.users.get(1, 2)), "ko")
        event.response.defer.assert_not_awaited()

    async def test_notifications_share_existing_alarm_roles_and_read_fresh_state(self):
        repos, event, view = fixture()
        await view.toggle_alarm(event, "alarm_stock")
        self.assertIn("입고 알림** · 켜짐", displayed_text(view))
        await AlarmView(SimpleNamespace(repos=repos)).stock(event)
        self.assertEqual(event.user.roles, [])
        # The profile still displays On but toggles according to current server roles.
        await view.toggle_alarm(event, "alarm_stock")
        self.assertEqual(len(event.user.roles), 1)
        self.assertIn("입고 알림** · 켜짐", displayed_text(view))
        self.assertEqual(default_language(await repos.users.get(1, 2)), "ko")

    async def test_concurrent_role_toggles_do_not_lose_one_toggle(self):
        repos, event, _ = fixture()
        await asyncio.gather(*(
            toggle_notification(repos, event.guild, event.user, "alarm_announcement") for _ in range(2)
        ))
        self.assertEqual(event.user.roles, [])
        event.user.add_roles.assert_awaited_once()
        event.user.remove_roles.assert_awaited_once()

    async def test_missing_role_and_permission_failure_leave_settings_unchanged(self):
        repos, event, view = fixture(role_ids={})
        self.assertTrue(all(item.disabled for item in view.walk_children()
                            if isinstance(item, discord.ui.Button)))
        await view.toggle_alarm(event, "alarm_stock")
        self.assertIn("역할", event.followup.send.await_args.args[0])
        repos, event, view = fixture()
        event.user.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no permission")
        await view.toggle_alarm(event, "alarm_stock")
        self.assertEqual(event.user.roles, [])
        event.edit_original_response.assert_not_awaited()
        self.assertIn("권한", event.followup.send.await_args.args[0])

    async def test_timeout_disables_all_profile_controls(self):
        _, _, view = fixture()
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(item.disabled for item in view.walk_children()
                            if isinstance(item, (discord.ui.Button, discord.ui.Select))))
        view.message.edit.assert_awaited_once_with(view=view)

    async def test_profile_command_is_hybrid_and_slash_uses_private_profile(self):
        repos, event, _ = fixture()
        cog = ProfileCog(SimpleNamespace(repos=repos))
        ctx = SimpleNamespace(interaction=event)
        self.assertIsInstance(cog.profile, commands.HybridCommand)
        with patch("cogs.cogs_profile.send_profile", new_callable=AsyncMock) as send:
            await cog.profile.callback(cog, ctx)
        send.assert_awaited_once_with(event, repos)


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_verification_saves_chosen_language_and_opens_profile(self):
        for saved, expected in ((None, "ja"), ("en", "en")):
            repos, event, _ = fixture(saved)
            role = SimpleNamespace(id=50, mention="<@&50>")
            event.guild.get_role = lambda _: role
            repos.settings.get.return_value = {"roles": {"verified": 50}}
            cog = SimpleNamespace(repos=repos, settings=repos.settings, users=repos.users, send_verify_log=AsyncMock())
            pad = VerifyPad(cog, event.user.id, "1234", None, "ja")
            pad.input_code = "1234"
            with patch("cogs.profile_views.send_profile", new_callable=AsyncMock) as send:
                await pad.confirm(event)
            self.assertEqual(default_language(await repos.users.get(1, 2)), expected)
            send.assert_awaited_once_with(event, repos, welcome=True)
            self.assertEqual(pad.display_status, "VERIFIED")
            self.assertTrue(pad.is_finished())


if __name__ == "__main__":
    unittest.main()
