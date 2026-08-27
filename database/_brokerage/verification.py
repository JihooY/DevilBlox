from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .core import (
    DEFAULT_CONFIG,
    _clamp_score,
    _clean_text,
    _ledger_key,
    _now,
    _penalized_delta,
    _points_for_price,
    _posting_minutes,
    _preference_key,
    _problem_key,
    _profile_key,
    _require_id,
    _safe_datetime,
)


class BrokerageVerificationMixin:
    def _normalize_verification_target(self, kind: str, target: str) -> tuple[str, str]:
        kind = kind.strip().casefold()
        target = str(target or "").strip()
        if kind == "email":
            normalized = target.casefold()
            if normalized.count("@") != 1 or any(char.isspace() for char in normalized):
                raise ValueError("invalid email address")
            local, domain = normalized.split("@", 1)
            if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
                raise ValueError("invalid email address")
            mask = f"{local[:1]}***@{domain}"
        elif kind == "phone":
            prefix = "+" if target.startswith("+") else ""
            digits = "".join(char for char in target if char.isdigit())
            if target.startswith("00"):
                digits = digits[2:]
                prefix = "+"
            if not 7 <= len(digits) <= 15:
                raise ValueError("invalid phone number")
            normalized = f"{prefix or '+'}{digits}"
            mask = f"***-***-{digits[-4:]}"
        else:
            raise ValueError("verification kind must be 'email' or 'phone'")
        return normalized, mask

    def _target_hash(self, normalized: str) -> str:
        return hmac.new(self._verification_pepper, normalized.encode("utf-8"), hashlib.sha256).hexdigest()

    def _code_hash(self, code: str, salt: bytes) -> str:
        material = code.encode("utf-8") + self._verification_pepper
        return hashlib.pbkdf2_hmac("sha256", material, salt, 120_000).hex()

    async def create_verification_challenge(
        self,
        guild_id: int,
        user_id: int,
        kind: str,
        target: str,
        code: str,
        *,
        ttl_minutes: int = 10,
        max_attempts: int = 5,
    ) -> dict:
        guild_id = _require_id(guild_id, "guild_id")
        user_id = _require_id(user_id, "user_id")
        kind = kind.strip().casefold()
        normalized, target_mask = self._normalize_verification_target(kind, target)
        code = _clean_text(code, "code", maximum=100, required=True)
        ttl_minutes = int(ttl_minutes)
        max_attempts = int(max_attempts)
        if not 1 <= ttl_minutes <= 24 * 60 or not 1 <= max_attempts <= 20:
            raise ValueError("invalid verification challenge limits")
        now = _now()
        recent = await self.verifications.find_one(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "kind": kind,
                "status": {"$ne": "cancelled"},
                "created_at": {"$gt": now - timedelta(seconds=60)},
            },
            {"_id": 1},
        )
        if recent is not None:
            raise ValueError("verification challenge cooldown is active")
        await self.verifications.update_many(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "kind": kind,
                "status": "pending",
            },
            {
                "$set": {
                    "status": "superseded",
                    "superseded_at": now,
                    "updated_at": now,
                }
            },
        )
        salt = secrets.token_bytes(16)
        challenge_id = secrets.token_urlsafe(18)
        doc = {
            "_id": challenge_id,
            "guild_id": guild_id,
            "user_id": user_id,
            "kind": kind,
            "target_hash": self._target_hash(normalized),
            "target_mask": target_mask,
            "code_hash": self._code_hash(code, salt),
            "code_salt": salt.hex(),
            "status": "pending",
            "attempts": 0,
            "max_attempts": max_attempts,
            "expires_at": now + timedelta(minutes=ttl_minutes),
            "created_at": now,
            "updated_at": now,
        }
        try:
            await self.verifications.insert_one(doc)
        except DuplicateKeyError as exc:
            # A concurrent request won the one-pending-challenge constraint.
            raise ValueError("verification challenge cooldown is active") from exc
        return {key: value for key, value in doc.items() if key not in {"code_hash", "code_salt"}}

    async def cancel_verification_challenge(
        self,
        challenge_id: str,
        guild_id: int,
        user_id: int,
        *,
        reason: str = "delivery_failed",
    ) -> dict | None:
        now = _now()
        challenge = await self.verifications.find_one_and_update(
            {
                "_id": _clean_text(challenge_id, "challenge_id", maximum=100, required=True),
                "guild_id": _require_id(guild_id, "guild_id"),
                "user_id": _require_id(user_id, "user_id"),
                "status": "pending",
            },
            {
                "$set": {
                    "status": "cancelled",
                    "cancel_reason": _clean_text(reason, "reason", maximum=100, required=True),
                    "cancelled_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return self._public_challenge(challenge)

    async def consume_verification_challenge(
        self,
        challenge_id: str,
        guild_id: int,
        user_id: int,
        code: str,
    ) -> dict | None:
        challenge_id = _clean_text(challenge_id, "challenge_id", maximum=100, required=True)
        guild_id = _require_id(guild_id, "guild_id")
        user_id = _require_id(user_id, "user_id")
        code = _clean_text(code, "code", maximum=100, required=True)
        now = _now()
        challenge = await self.verifications.find_one(
            {"_id": challenge_id, "guild_id": guild_id, "user_id": user_id}
        )
        if challenge is None:
            return None
        if challenge.get("status") == "consumed":
            bonus = await self._finalize_verification(challenge)
            return {
                "challenge": self._public_challenge(challenge),
                "already_consumed": True,
                "bonus": bonus,
            }
        if (
            challenge.get("status") != "pending"
            or _safe_datetime(challenge.get("expires_at")) <= now
            or int(challenge.get("attempts", 0)) >= int(challenge.get("max_attempts", 5))
        ):
            return None
        salt = bytes.fromhex(challenge["code_salt"])
        valid = hmac.compare_digest(self._code_hash(code, salt), challenge["code_hash"])
        if not valid:
            failed = await self.verifications.find_one_and_update(
                {
                    "_id": challenge_id,
                    "status": "pending",
                    "expires_at": {"$gt": now},
                    "attempts": {"$lt": int(challenge.get("max_attempts", 5))},
                },
                {"$inc": {"attempts": 1}, "$set": {"updated_at": now}},
                return_document=ReturnDocument.AFTER,
            )
            return {"challenge": self._public_challenge(failed or challenge), "valid": False, "bonus": None}

        try:
            consumed = await self.verifications.find_one_and_update(
                {
                    "_id": challenge_id,
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "status": "pending",
                    "expires_at": {"$gt": now},
                },
                {"$set": {"status": "consumed", "consumed_at": now, "updated_at": now}},
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            rejected = await self.verifications.find_one_and_update(
                {"_id": challenge_id, "status": "pending"},
                {
                    "$set": {
                        "status": "target_in_use",
                        "rejected_at": now,
                        "updated_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            return {
                "challenge": self._public_challenge(rejected or challenge),
                "valid": False,
                "target_in_use": True,
                "bonus": None,
            }
        if consumed is None:
            consumed = await self.verifications.find_one({"_id": challenge_id})
            if consumed is None or consumed.get("status") != "consumed":
                return None
        bonus = await self._finalize_verification(consumed)
        return {"challenge": self._public_challenge(consumed), "valid": True, "bonus": bonus}

    async def _finalize_verification(self, challenge: Mapping[str, Any]) -> dict | None:
        guild_id = int(challenge["guild_id"])
        user_id = int(challenge["user_id"])
        kind = str(challenge["kind"])
        profile = await self.ensure_profile(guild_id, user_id)
        if kind in set(profile.get("verified_kinds") or []):
            return None
        operation_id = f"verification:{guild_id}:{user_id}:{kind}"
        existing_ledger = await self.score_ledger.find_one(
            {"_id": _ledger_key(guild_id, user_id, operation_id)}
        )
        if existing_ledger is not None:
            bonus_points = int(existing_ledger.get("requested_delta", 0))
        else:
            config = await self.get_config(guild_id)
            bonus_points = int(config[f"{kind}_verification_bonus"])
        bonus = await self.adjust_profile(
            guild_id,
            user_id,
            bonus_points,
            operation_id=operation_id,
            reason=f"{kind}_verification",
            metadata={
                "challenge_id": challenge["_id"],
                "target_hash": challenge["target_hash"],
            },
        )
        now = _now()
        await self.profiles.update_one(
            {"_id": _profile_key(guild_id, user_id)},
            {
                "$addToSet": {"verified_kinds": kind},
                "$set": {
                    f"verification.{kind}": {
                        "target_hash": challenge["target_hash"],
                        "target_mask": challenge["target_mask"],
                        "verified_at": challenge.get("consumed_at", now),
                    },
                    "updated_at": now,
                },
            },
        )
        return bonus

    @staticmethod
    def _public_challenge(challenge: dict | None) -> dict | None:
        if challenge is None:
            return None
        return {key: value for key, value in challenge.items() if key not in {"code_hash", "code_salt"}}

    async def list_pending_verifications(self, *, limit: int = 500) -> list[dict]:
        docs = await self.verifications.find(
            {"status": "pending", "expires_at": {"$gt": _now()}}
        ).sort("expires_at", 1).to_list(length=max(1, min(2_000, int(limit))))
        return [self._public_challenge(doc) for doc in docs]
