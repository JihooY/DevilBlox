from __future__ import annotations

import re


def safe_channel_name(prefix: str, *parts: str) -> str:
    raw = "-".join([prefix, *[part for part in parts if part]])
    raw = raw.lower()
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"[^0-9a-z가-힣_-]", "", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    return raw[:90] or prefix
