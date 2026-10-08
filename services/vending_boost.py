from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlsplit

import aiohttp

from services.vending_topup import TopupPendingError


def validate_boost(months: int, quantity: int) -> None:
    if type(months) is not int or months not in {1, 3}:
        raise ValueError("BOOST months must be 1 or 3")
    if type(quantity) is not int or not 2 <= quantity <= 1000 or quantity % 2:
        raise ValueError("BOOST quantity must be even, between 2 and 1000")


def signed_headers(token: str, secret: str, method: str, target: str,
                   body: bytes, timestamp: str, nonce: str) -> dict[str, str]:
    if target.startswith('/boost-api/'):
        target = '/api/' + target[len('/boost-api/'):]
    message = '\n'.join((timestamp, nonce, method.upper(), target,
                         hashlib.sha256(body).hexdigest())).encode('utf-8')
    signature = hmac.new(secret.encode('ascii'), message, hashlib.sha256).hexdigest()
    return {'Authorization': 'Bearer ' + token, 'X-BOOST-Timestamp': timestamp,
            'X-BOOST-Nonce': nonce, 'X-BOOST-Signature': signature,
            'Content-Type': 'application/json', 'Accept': 'application/json'}


async def call_boost(method: str, resource: str, payload: dict | None = None) -> dict:
    base = os.getenv('BOOST_API_URL', 'https://devilblox.shop/boost-api').rstrip('/')
    parsed = urlsplit(base)
    token = os.getenv('API_TOKEN', '').strip()
    secret = os.getenv('API_SIGNING_SECRET', '').strip()
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment or not token or not secret):
        raise TopupPendingError('부스트 API 환경설정을 확인해주세요.')
    if not resource.startswith('/') or '?' in resource or '#' in resource:
        raise ValueError('invalid BOOST resource')
    url = base + resource
    body = b'' if payload is None else json.dumps(
        payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    try:
        headers = signed_headers(token, secret, method, urlsplit(url).path, body,
                                 str(int(time.time())), secrets.token_hex(16))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.request(method, url, data=body if payload is not None else None,
                                       headers=headers, allow_redirects=False) as response:
                if not 200 <= response.status < 300:
                    raise TopupPendingError('부스트 서버의 주문 접수를 확인하지 못했습니다.')
                result = await response.json()
                if not isinstance(result, dict):
                    raise TopupPendingError('부스트 서버 응답 형식을 확인하지 못했습니다.')
                if result.get('ok') is False or result.get('success') is False or result.get('error'):
                    raise TopupPendingError('부스트 서버가 요청 실패를 반환했습니다.')
                return {'http_status': response.status, 'result': result}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise TopupPendingError('부스트 서버 응답을 확인하지 못했습니다.') from exc


async def order_boost(user_id: int, months: int, quantity: int, reference: str) -> dict:
    validate_boost(months, quantity)
    return await call_boost('POST', '/orders', {
        'request_id': reference, 'user_id': str(user_id),
        'months': months, 'quantity': quantity,
    })
