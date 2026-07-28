from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from database.vending import normalize_product_id


@dataclass(slots=True)
class ChargeApprovalResult:
    status: Literal["approved", "already_processed", "not_found"]
    charge: dict | None
    newly_completed: bool = False
    credit: dict | None = None


@dataclass(slots=True)
class PurchaseResult:
    status: Literal["purchased", "already_owned", "insufficient_funds"]
    product: dict
    operation_id: str | None = None
    price: int = 0
    original_price: int = 0
    applied_code: str | None = None
    discount_kind: Literal["coupon", "promotion"] | None = None
    spent: dict | None = None
    log: dict | None = None
    newly_completed: bool = False
    current_cash: int = 0


class VendingCommerceService:
    """Retry-safe orchestration for charge approvals and vending purchases.

    Every balance, coupon and seller mutation receives a stable operation ID.
    The entitlement's ``purchased`` status is written last and acts as the
    commit marker, so an interrupted request can safely continue on retry even
    when MongoDB is running without replica-set transactions.
    """

    def __init__(self, repos) -> None:
        self.repos = repos

    async def approve_charge(
        self,
        guild_id: int,
        message_id: int,
        admin_id: int,
    ) -> ChargeApprovalResult:
        charge = await self.repos.vending.claim_charge_request(
            guild_id,
            message_id,
            admin_id,
        )
        if charge is None:
            current = await self.repos.vending.get_charge_by_admin_message(
                guild_id,
                message_id,
            )
            if current is None:
                return ChargeApprovalResult(status="not_found", charge=None)
            if current.get("status") == "approved":
                # The database commit can succeed even when the caller loses
                # the response.  Surface the approved record so the cog can
                # repair stale panels without repeating logs or user DMs.
                return ChargeApprovalResult(
                    status="approved",
                    charge=current,
                    newly_completed=False,
                )
            return ChargeApprovalResult(status="already_processed", charge=current)

        operation_id = f"charge:{charge['_id']}"
        credit = await self.repos.users.add_cash_once(
            guild_id,
            int(charge["user_id"]),
            int(charge["amount"]),
            operation_id=operation_id,
            reason="vending charge approval",
        )
        approved = await self.repos.vending.approve_charge_request(
            charge["_id"],
            admin_id,
        )
        newly_completed = approved is not None
        if approved is None:
            approved = await self.repos.vending.get_charge(charge["_id"])
        if approved is None:
            raise RuntimeError("charge disappeared during approval")
        if approved.get("status") != "approved":
            raise RuntimeError("charge credit was applied but approval was not finalized")
        return ChargeApprovalResult(
            status="approved",
            charge=approved,
            newly_completed=newly_completed,
            credit=credit,
        )

    async def purchase(
        self,
        guild_id: int,
        user_id: int,
        product: dict,
    ) -> PurchaseResult:
        original_price = int(product.get("price", 0))
        if original_price < 0:
            raise ValueError("product price must be zero or greater")

        product_id = str(product["product_id"])
        context = f"vending:{normalize_product_id(product_id)}"
        quoted_price, selected_coupon, selected_promotion = await self.repos.coupons.quote(
            guild_id,
            user_id,
            context,
            original_price,
        )
        reservation = await self.repos.vending.reserve_product(
            guild_id,
            user_id,
            product,
            original_price=original_price,
            quoted_price=quoted_price,
            coupon_code=(selected_coupon or {}).get("code"),
            promotion_code=(selected_promotion or {}).get("code"),
        )
        if reservation.get("status") == "purchased":
            return PurchaseResult(
                status="already_owned",
                product=product,
                operation_id=reservation.get("operation_id"),
                price=int(reservation.get("price", original_price)),
                original_price=int(reservation.get("original_price", original_price)),
            )
        if reservation.get("status") != "pending":
            raise RuntimeError("purchase entitlement has an unknown state")

        operation_id = str(reservation["operation_id"])
        original_price = int(reservation.get("original_price", original_price))
        price = original_price
        applied_code: str | None = None
        discount_kind: Literal["coupon", "promotion"] | None = None
        coupon_code = reservation.get("coupon_code")
        promotion_code = reservation.get("promotion_code")
        coupon_consumed = False

        if coupon_code:
            consumed = await self.repos.coupons.consume_once(
                guild_id,
                user_id,
                str(coupon_code),
                "vending",
                original_price,
                operation_id=operation_id,
            )
            if consumed is not None:
                _, price = consumed
                coupon_consumed = True
                applied_code = str(coupon_code)
                discount_kind = "coupon"
        elif promotion_code:
            promotion = await self.repos.coupons.validate_promotion(
                guild_id,
                user_id,
                str(promotion_code),
            )
            if promotion is not None:
                price = int(reservation.get("quoted_price", original_price))
                applied_code = str(promotion_code)
                discount_kind = "promotion"

        await self.repos.vending.update_purchase_progress(
            operation_id,
            price=price,
            applied_code=applied_code,
            discount_kind=discount_kind,
        )
        spent = await self.repos.users.spend_cash_once(
            guild_id,
            user_id,
            price,
            operation_id=operation_id,
            reason=f"vending purchase {product_id}",
        )
        if spent is None:
            if coupon_consumed and coupon_code:
                await self.repos.coupons.restore_consumption_once(
                    guild_id,
                    user_id,
                    str(coupon_code),
                    operation_id=operation_id,
                )
            await self.repos.vending.release_product_reservation(
                guild_id,
                user_id,
                product_id,
                operation_id=operation_id,
            )
            user = await self.repos.users.ensure_user(guild_id, user_id)
            return PurchaseResult(
                status="insufficient_funds",
                product=product,
                operation_id=operation_id,
                price=price,
                original_price=original_price,
                current_cash=int(user.get("cash", 0)),
            )

        await self.repos.vending.update_purchase_progress(
            operation_id,
            before_cash=spent["before_cash"],
            after_cash=spent["after_cash"],
            cash_debited=True,
        )
        log = await self.repos.vending.upsert_purchase_log(
            operation_id=operation_id,
            guild_id=guild_id,
            user_id=user_id,
            product=product,
            price=price,
            original_price=original_price,
            before_cash=spent["before_cash"],
            after_cash=spent["after_cash"],
            discount_code=applied_code,
        )
        seller_id = product.get("seller_id")
        if seller_id:
            seller_recorded = await self.repos.sellers.add_sale_once(
                guild_id,
                int(seller_id),
                price,
                operation_id=operation_id,
            )
            if not seller_recorded:
                raise RuntimeError("seller sale could not be recorded")

        _, newly_completed = await self.repos.vending.complete_product_purchase(
            operation_id=operation_id,
            guild_id=guild_id,
            user_id=user_id,
            product=product,
        )
        return PurchaseResult(
            status="purchased",
            product=product,
            operation_id=operation_id,
            price=price,
            original_price=original_price,
            applied_code=applied_code,
            discount_kind=discount_kind,
            spent=spent,
            log=log,
            newly_completed=newly_completed,
            current_cash=int(spent["after_cash"]),
        )
