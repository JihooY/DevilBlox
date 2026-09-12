from __future__ import annotations

import asyncio
import os

import aiohttp


class TopupPendingError(RuntimeError):
    """Delivery must be retried with the persisted purchase reference."""


async def charge_tokens(user_id: int, tokens: int, reference: str) -> dict:
    return await _post_delivery(
        "/api/topup",
        {"discord_user_id": str(user_id), "tokens": tokens,
         "reason": "vending_purchase", "reference": reference},
    )


async def apply_plan(user_id: int, plan: str, months: int, reference: str) -> dict:
    if plan not in {"plus", "pro"} or months not in {1, 2, 3, 6}:
        raise ValueError("invalid vending plan delivery")
    return await _post_delivery(
        "/api/plan",
        {"discord_user_id": str(user_id), "plan": plan, "months": months,
         "reason": "vending_purchase", "reference": reference},
    )


async def _post_delivery(path: str, body: dict) -> dict:
    key = (
        os.getenv("VENDING_ADMIN_API_KEY", "").strip()
        or os.getenv("VENDING_TOPUP_API_KEY", "").strip()
    )
    if not key:
        raise TopupPendingError("충전 API 키가 설정되지 않았습니다.")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(
                f"https://backup.devilblox.shop{path}",
                headers={"X-Admin-Api-Key": key},
                json=body,
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise TopupPendingError("충전 서버가 성공을 확인하지 못했습니다.")
                payload = await response.json()
                # ok=true also covers an already processed reference.
                if not isinstance(payload, dict) or payload.get("ok") is not True:
                    raise TopupPendingError("충전 서버가 성공을 확인하지 못했습니다.")
                return payload
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise TopupPendingError("충전 서버 응답을 확인하지 못했습니다.") from exc
