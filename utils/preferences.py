"""Shared language defaults and the notification roles used by personal settings."""
from __future__ import annotations

LANGUAGES = {"ko": "한국어", "ja": "日本語", "en": "English", "zh": "中文"}
NOTIFICATION_ROLES = ("alarm_announcement", "alarm_seller", "alarm_stock")


def default_language(user: dict | None) -> str:
    language = (user or {}).get("default_language", "ko")
    return language if language in LANGUAGES else "ko"
