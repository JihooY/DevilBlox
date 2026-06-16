from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone


ROLE_KEYS = {
    "admin": "관리자",
    "verified": "인증",
    "customer": "고객",
    "seller": "셀러",
    "middleman": "중개자",
    "vip": "VIP",
    "vvip": "VVIP",
    "svip": "SVIP",
    "alarm_announcement": "공지 알림",
    "alarm_seller": "티켓 상태 알림",
    "alarm_stock": "입고 알림",
}

CHANNEL_KEYS = {
    "verify": "인증 패널",
    "purchase": "구매 패널",
    "account": "계정 정보",
    "support": "문의",
    "middleman": "중개",
    "alarm": "알림",
    "verify_log": "인증 로그",
    "join_leave_log": "입퇴장 로그",
    "purchase_log": "구매 로그",
    "vending_admin": "자판기 관리자",
    "vending_log": "자판기 로그",
    "middleman_log": "중개 로그",
    "seller_announce": "셀러 공지",
    "ticket_condition": "티켓 현황",
    "vending": "자판기 패널",
    "archive": "아카이브 패널",
}

CATEGORY_KEYS = {
    "purchase": "구매",
    "purchase_closed": "구매 종료",
    "support": "문의",
    "support_closed": "문의 종료",
    "middleman": "중개",
    "middleman_closed": "중개 종료",
}

LEGACY_SETTING_MAP = {
    "admin_role": ("roles", "admin"),
    "user_role": ("roles", "verified"),
    "customer_role": ("roles", "customer"),
    "seller_role": ("roles", "seller"),
    "mm_role": ("roles", "middleman"),
    "vip_role": ("roles", "vip"),
    "vvip_role": ("roles", "vvip"),
    "svip_role": ("roles", "svip"),
    "alarm_seller": ("roles", "alarm_seller"),
    "verify_channel": ("channels", "verify"),
    "purchase_channel": ("channels", "purchase"),
    "accinfo_channel": ("channels", "account"),
    "support_channel": ("channels", "support"),
    "mm_channel": ("channels", "middleman"),
    "alarm_channel": ("channels", "alarm"),
    "log_verify": ("channels", "verify_log"),
    "enter_exit_channel": ("channels", "join_leave_log"),
    "purchase_log": ("channels", "purchase_log"),
    "mm_log": ("channels", "middleman_log"),
    "seller_anc": ("channels", "seller_announce"),
    "ticket_condition": ("channels", "ticket_condition"),
    "purchase_category": ("categories", "purchase"),
    "purchase_closed_category": ("categories", "purchase_closed"),
    "support_category": ("categories", "support"),
    "support_closed_category": ("categories", "support_closed"),
    "mm_category": ("categories", "middleman"),
    "mm_closed_category": ("categories", "middleman_closed"),
    "ticket_condition_msg": ("meta", "ticket_condition_message_id"),
    "account_panel_msg": ("meta", "account_panel_message_id"),
    "alarm_panel_msg": ("meta", "alarm_panel_message_id"),
    "middleman_panel_msg": ("meta", "middleman_panel_message_id"),
    "purchase_panel_msg": ("meta", "purchase_panel_message_id"),
    "vending_panel_msg": ("meta", "vending_panel_message_id"),
    "archive_panel_msg": ("meta", "archive_panel_message_id"),
    "support_panel_msg": ("meta", "support_panel_message_id"),
    "verify_panel_msg": ("meta", "verify_panel_message_id"),
}


def _now():
    return datetime.now(timezone.utc)


def default_settings(guild_id: int) -> dict:
    return {
        "_id": guild_id,
        "guild_id": guild_id,
        "roles": {key: None for key in ROLE_KEYS},
        "channels": {key: None for key in CHANNEL_KEYS},
        "categories": {key: None for key in CATEGORY_KEYS},
        "meta": {
            "ticket_condition_message_id": None,
            "ticket_condition_reset_at": None,
            "account_panel_message_id": None,
            "alarm_panel_message_id": None,
            "middleman_panel_message_id": None,
            "purchase_panel_message_id": None,
            "vending_panel_message_id": None,
            "archive_panel_message_id": None,
            "support_panel_message_id": None,
            "verify_panel_message_id": None,
        },
        "created_at": _now(),
        "updated_at": _now(),
    }


class GuildSettingsStore:
    def __init__(self, db):
        self.collection = db["guild_settings"]

    async def ensure_indexes(self):
        await self.collection.create_index("guild_id", unique=True)

    async def ensure_guild(self, guild_id: int) -> dict:
        base = default_settings(guild_id)
        await self.collection.update_one(
            {"_id": guild_id},
            {"$setOnInsert": base},
            upsert=True,
        )
        return await self.get(guild_id)

    async def get(self, guild_id: int) -> dict:
        doc = await self.collection.find_one({"_id": guild_id})
        if doc is None:
            return await self.ensure_guild(guild_id)

        merged = default_settings(guild_id)
        for section in ("roles", "channels", "categories", "meta"):
            merged[section].update(doc.get(section, {}))
        merged.update({k: v for k, v in doc.items() if k not in merged})
        return merged

    async def get_value(self, guild_id: int, section: str, key: str):
        doc = await self.get(guild_id)
        return deepcopy(doc.get(section, {})).get(key)

    async def set_value(self, guild_id: int, section: str, key: str, value):
        await self.ensure_guild(guild_id)
        await self.collection.update_one(
            {"_id": guild_id},
            {"$set": {f"{section}.{key}": value, "updated_at": _now()}},
        )

    async def apply_legacy_settings(self, guild_id: int, settings: dict[str, int]):
        updates = {}
        for old_key, value in settings.items():
            target = LEGACY_SETTING_MAP.get(old_key)
            if target is None:
                continue
            section, key = target
            updates[f"{section}.{key}"] = value
        if updates:
            updates["updated_at"] = _now()
            await self.ensure_guild(guild_id)
            await self.collection.update_one({"_id": guild_id}, {"$set": updates})
