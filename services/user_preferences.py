"""Use the same Discord roles for both the alarm panel and personal profiles."""
from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary

from utils.preferences import NOTIFICATION_ROLES

_role_locks = WeakValueDictionary()


async def toggle_notification(repos, guild, member, role_key: str):
    if role_key not in NOTIFICATION_ROLES:
        raise ValueError("unsupported notification")
    key = (guild.id, member.id)
    lock = _role_locks.setdefault(key, asyncio.Lock())
    async with lock:
        settings = await repos.settings.get(guild.id)
        role = guild.get_role(settings["roles"].get(role_key) or 0)
        if role is None:
            raise ValueError("notification role not configured")
        # A profile can stay open while the member changes the original alarm panel.
        member = await guild.fetch_member(member.id)
        enabled = role not in member.roles
        if enabled:
            await member.add_roles(role, reason="DevilBlox notification preference")
        else:
            await member.remove_roles(role, reason="DevilBlox notification preference")
        member = await guild.fetch_member(member.id)
        return member, enabled
