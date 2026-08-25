from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from pymongo.errors import DuplicateKeyError

from database.users import UserStore
from database.vending import ProductStore, VendingLogStore
from services.vending import VendingCommerceService


class _CashCollection:
    """Small, concurrency-safe fake for the update pipelines used by UserStore.

    This is deliberately narrower than a MongoDB emulator.  It models the two
    properties these tests care about: the balance predicate and the operation
    marker are evaluated and written atomically.
    """

    def __init__(self, *, cash: int = 0):
        self.document = {
            "_id": "1:2",
            "guild_id": 1,
            "user_id": 2,
            "cash": cash,
            "accrued_spent": 0,
            "points": 0,
            "cash_operations": [],
        }
        self._lock = asyncio.Lock()

    async def find_one(self, query):
        if query.get("_id") != self.document["_id"]:
            return None
        return deepcopy(self.document)

    async def find_one_and_update(self, query, update, **_kwargs):
        async with self._lock:
            operation_id = query["cash_operations.operation_id"]["$ne"]
            if any(
                item.get("operation_id") == operation_id
                for item in self.document["cash_operations"]
            ):
                return None

            minimum_cash = query.get("cash", {}).get("$gte")
            if minimum_cash is not None and self.document["cash"] < minimum_cash:
                return None

            set_stage = update[0]["$set"]
            cash_expression = set_stage["cash"]
            before_cash = self.document["cash"]
            if "$add" in cash_expression:
                amount = int(cash_expression["$add"][1])
                after_cash = before_cash + amount
                kind = "credit"
            else:
                amount = int(cash_expression["$subtract"][1])
                after_cash = before_cash - amount
                kind = "debit"

            operation_template = set_stage["cash_operations"]["$concatArrays"][1][0]
            operation = {
                **operation_template,
                "before_cash": before_cash,
                "after_cash": after_cash,
            }
            self.document["cash"] = after_cash
            self.document["cash_operations"].append(operation)
            if kind == "debit":
                self.document["accrued_spent"] += amount
                self.document["points"] += amount // 1000
            return deepcopy(self.document)


def _matches(document: dict, query: dict) -> bool:
    return all(document.get(key) == value for key, value in query.items())


class _ChargeCollection:
    def __init__(self):
        self.document = {
            "_id": "charge-id",
            "guild_id": 1,
            "admin_message_id": 10,
            "user_id": 2,
            "amount": 500,
            "status": "pending",
        }
        self._lock = asyncio.Lock()

    async def find_one_and_update(self, query, update, **_kwargs):
        async with self._lock:
            if not _matches(self.document, query):
                return None
            self.document.update(deepcopy(update["$set"]))
            return deepcopy(self.document)

    async def find_one(self, query):
        async with self._lock:
            return deepcopy(self.document) if _matches(self.document, query) else None


class _EntitlementCollection:
    def __init__(self):
        self.documents: list[dict] = []
        self._lock = asyncio.Lock()

    async def insert_one(self, document):
        async with self._lock:
            key = (
                document["guild_id"],
                document["user_id"],
                document["product_id_lower"],
            )
            if any(
                (item["guild_id"], item["user_id"], item["product_id_lower"])
                == key
                for item in self.documents
            ):
                raise DuplicateKeyError("duplicate entitlement")
            self.documents.append(deepcopy(document))
            return SimpleNamespace(inserted_id=document.get("operation_id"))

    async def find_one(self, query):
        async with self._lock:
            for document in self.documents:
                if _matches(document, query):
                    return deepcopy(document)
        return None

    async def find_one_and_update(self, query, update, **_kwargs):
        async with self._lock:
            for document in self.documents:
                if _matches(document, query):
                    document.update(deepcopy(update["$set"]))
                    return deepcopy(document)
        return None


class _PurchaseLogCollection:
    def __init__(self):
        self.documents: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def update_one(self, query, update, *, upsert=False):
        async with self._lock:
            operation_id = query["_id"]
            inserted = operation_id not in self.documents
            if inserted and upsert:
                self.documents[operation_id] = deepcopy(update["$setOnInsert"])
            return SimpleNamespace(
                matched_count=not inserted,
                modified_count=0,
                upserted_id=operation_id if inserted else None,
            )

    async def find_one(self, query):
        async with self._lock:
            document = self.documents.get(query["_id"])
            return deepcopy(document) if document is not None else None


class _CommerceUsers:
    def __init__(self, *, cash: int):
        self.cash = cash
        self.accrued_spent = 0
        self.operations: dict[str, dict] = {}
        self.credit_failure_after_commit = 0
        self.debit_failure_after_commit = 0
        self._lock = asyncio.Lock()

    def _user(self):
        return {"cash": self.cash, "accrued_spent": self.accrued_spent}

    async def ensure_user(self, _guild_id, _user_id):
        return self._user()

    async def add_cash_once(
        self, _guild_id, _user_id, amount, *, operation_id, reason=""
    ):
        del reason
        amount = int(amount)
        if amount <= 0:
            raise ValueError("credit must be positive")
        async with self._lock:
            saved = self.operations.get(operation_id)
            if saved is not None:
                return {**deepcopy(saved), "applied": False, "user": self._user()}
            before = self.cash
            self.cash += amount
            saved = {
                "operation_id": operation_id,
                "before_cash": before,
                "after_cash": self.cash,
                "amount": amount,
                "kind": "credit",
            }
            self.operations[operation_id] = saved
            if self.credit_failure_after_commit:
                self.credit_failure_after_commit -= 1
                raise RuntimeError("ambiguous credit result")
            return {**deepcopy(saved), "applied": True, "user": self._user()}

    async def spend_cash_once(
        self, _guild_id, _user_id, amount, *, operation_id, reason=""
    ):
        del reason
        amount = int(amount)
        if amount < 0:
            raise ValueError("debit cannot be negative")
        async with self._lock:
            saved = self.operations.get(operation_id)
            if saved is not None:
                return {**deepcopy(saved), "applied": False, "user": self._user()}
            if self.cash < amount:
                return None
            before = self.cash
            self.cash -= amount
            self.accrued_spent += amount
            saved = {
                "operation_id": operation_id,
                "before_cash": before,
                "after_cash": self.cash,
                "amount": amount,
                "kind": "debit",
            }
            self.operations[operation_id] = saved
            if self.debit_failure_after_commit:
                self.debit_failure_after_commit -= 1
                raise RuntimeError("ambiguous debit result")
            return {**deepcopy(saved), "applied": True, "user": self._user()}


class _CommerceCoupons:
    def __init__(self, *, quantity: int = 0, discount: int = 50):
        self.quantity = quantity
        self.discount = discount
        self.code = "HALF" if quantity else None
        self.consumed: dict[str, int] = {}
        self.restored: set[str] = set()
        self.quote_parties = 0
        self._quote_count = 0
        self._quote_event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def quote(self, _guild_id, _user_id, _context, amount):
        if self.quote_parties:
            async with self._lock:
                self._quote_count += 1
                if self._quote_count >= self.quote_parties:
                    self._quote_event.set()
            await self._quote_event.wait()
        async with self._lock:
            if not self.code or self.quantity <= 0:
                return amount, None, None
            coupon = {
                "code": self.code,
                "kind": "general",
                "discount": self.discount,
                "discount_type": "percent",
            }
            return amount - amount * self.discount // 100, coupon, None

    async def consume_once(
        self,
        _guild_id,
        _user_id,
        code,
        _context,
        amount,
        *,
        operation_id,
    ):
        async with self._lock:
            if operation_id in self.consumed and operation_id not in self.restored:
                discounted = self.consumed[operation_id]
                return {"code": code, "kind": "general", "discount": self.discount}, discounted
            if self.quantity <= 0:
                return None
            self.quantity -= 1
            discounted = amount - amount * self.discount // 100
            self.consumed[operation_id] = discounted
            self.restored.discard(operation_id)
            return {"code": code, "kind": "general", "discount": self.discount}, discounted

    async def restore_consumption_once(
        self, _guild_id, _user_id, _code, *, operation_id
    ):
        async with self._lock:
            if operation_id not in self.consumed:
                return False
            if operation_id not in self.restored:
                self.quantity += 1
                self.restored.add(operation_id)
            return True

    async def validate_promotion(self, *_args, **_kwargs):
        return None


class _CommerceVending:
    def __init__(self, *, charge_amount: int = 500):
        self.charge = {
            "_id": "charge-id",
            "guild_id": 1,
            "admin_message_id": 10,
            "user_id": 2,
            "amount": charge_amount,
            "status": "pending",
        }
        self.entitlements: dict[tuple[int, int, str], dict] = {}
        self.purchase_logs: dict[str, dict] = {}
        self.fail_approve_before_commit = 0
        self.fail_approve_after_commit = 0
        self.fail_log_before_commit = 0
        self.fail_log_after_commit = 0
        self.fail_complete_after_commit = 0
        self._operation_sequence = 0
        self._lock = asyncio.Lock()

    async def claim_charge_request(self, guild_id, message_id, admin_id):
        async with self._lock:
            if (
                self.charge["guild_id"] != guild_id
                or self.charge["admin_message_id"] != message_id
            ):
                return None
            if self.charge["status"] == "pending":
                self.charge["status"] = "processing"
                self.charge["processed_by"] = admin_id
            if self.charge["status"] != "processing":
                return None
            return deepcopy(self.charge)

    async def get_charge_by_admin_message(self, guild_id, message_id):
        if (
            self.charge["guild_id"] == guild_id
            and self.charge["admin_message_id"] == message_id
        ):
            return deepcopy(self.charge)
        return None

    async def approve_charge_request(self, request_id, admin_id):
        async with self._lock:
            if request_id != self.charge["_id"] or self.charge["status"] != "processing":
                return None
            if self.fail_approve_before_commit:
                self.fail_approve_before_commit -= 1
                raise RuntimeError("approval write failed")
            self.charge.update(
                status="approved", success=True, processed_by=admin_id
            )
            approved = deepcopy(self.charge)
            if self.fail_approve_after_commit:
                self.fail_approve_after_commit -= 1
                raise RuntimeError("ambiguous approval result")
            return approved

    async def get_charge(self, request_id):
        return deepcopy(self.charge) if request_id == self.charge["_id"] else None

    async def reserve_product(
        self,
        guild_id,
        user_id,
        product,
        *,
        original_price,
        quoted_price,
        coupon_code,
        promotion_code,
    ):
        key = (guild_id, user_id, product["product_id_lower"])
        async with self._lock:
            existing = self.entitlements.get(key)
            if existing is not None:
                return deepcopy(existing)
            self._operation_sequence += 1
            reservation = {
                "guild_id": guild_id,
                "user_id": user_id,
                "product_id": product["product_id"],
                "product_id_lower": product["product_id_lower"],
                "operation_id": f"purchase:{self._operation_sequence}",
                "status": "pending",
                "original_price": original_price,
                "quoted_price": quoted_price,
                "coupon_code": coupon_code,
                "promotion_code": promotion_code,
            }
            self.entitlements[key] = reservation
            return deepcopy(reservation)

    async def update_purchase_progress(self, operation_id, **updates):
        async with self._lock:
            entitlement = self._entitlement_by_operation(operation_id)
            if entitlement is None or entitlement["status"] != "pending":
                return None
            entitlement.update(deepcopy(updates))
            return deepcopy(entitlement)

    async def release_product_reservation(
        self, guild_id, user_id, product_id, *, operation_id=None
    ):
        key = (guild_id, user_id, product_id.casefold())
        async with self._lock:
            entitlement = self.entitlements.get(key)
            if (
                entitlement is not None
                and entitlement["status"] == "pending"
                and (operation_id is None or entitlement["operation_id"] == operation_id)
            ):
                del self.entitlements[key]

    async def upsert_purchase_log(
        self,
        *,
        operation_id,
        guild_id,
        user_id,
        product,
        price,
        original_price,
        before_cash,
        after_cash,
        discount_code=None,
        kind=None,
    ):
        async with self._lock:
            if operation_id not in self.purchase_logs:
                if self.fail_log_before_commit:
                    self.fail_log_before_commit -= 1
                    raise RuntimeError("purchase log write failed")
                self.purchase_logs[operation_id] = {
                    "_id": operation_id,
                    "operation_id": operation_id,
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "product_id": product["product_id"],
                    "price": price,
                    "original_price": original_price,
                    "before_cash": before_cash,
                    "after_cash": after_cash,
                    "discount_code": discount_code,
                    "kind": kind,
                }
                if self.fail_log_after_commit:
                    self.fail_log_after_commit -= 1
                    raise RuntimeError("ambiguous purchase log result")
            return deepcopy(self.purchase_logs[operation_id])

    async def grant_owned_product(self, guild_id, user_id, product, *, operation_id):
        key = (guild_id, user_id, product["product_id_lower"])
        async with self._lock:
            entitlement = {
                "guild_id": guild_id,
                "user_id": user_id,
                "product_id": product["product_id"],
                "product_id_lower": product["product_id_lower"],
                "title": product.get("title", product["product_id"]),
                "terabox_url": product.get("terabox_url", ""),
                "status": "purchased",
                "operation_id": operation_id,
            }
            self.entitlements[key] = entitlement
            return deepcopy(entitlement)

    async def complete_product_purchase(
        self, *, operation_id, guild_id, user_id, product
    ):
        key = (guild_id, user_id, product["product_id_lower"])
        async with self._lock:
            entitlement = self.entitlements[key]
            newly_completed = entitlement["status"] == "pending"
            if newly_completed:
                entitlement["status"] = "purchased"
                if self.fail_complete_after_commit:
                    self.fail_complete_after_commit -= 1
                    raise RuntimeError("ambiguous entitlement commit")
            if entitlement["operation_id"] != operation_id:
                raise RuntimeError("wrong purchase operation")
            return deepcopy(entitlement), newly_completed

    def _entitlement_by_operation(self, operation_id):
        return next(
            (
                item
                for item in self.entitlements.values()
                if item["operation_id"] == operation_id
            ),
            None,
        )


class _CommerceSellers:
    def __init__(self):
        self.operations: set[str] = set()
        self.total = 0
        self.fail_after_commit = 0
        self._lock = asyncio.Lock()

    async def add_sale_once(
        self, _guild_id, _seller_id, amount, *, operation_id
    ):
        async with self._lock:
            if operation_id not in self.operations:
                self.operations.add(operation_id)
                self.total += amount
                if self.fail_after_commit:
                    self.fail_after_commit -= 1
                    raise RuntimeError("ambiguous seller sale result")
            return True


def _commerce_repos(*, cash=2_000, coupon_quantity=0, charge_amount=500):
    return SimpleNamespace(
        users=_CommerceUsers(cash=cash),
        coupons=_CommerceCoupons(quantity=coupon_quantity),
        vending=_CommerceVending(charge_amount=charge_amount),
        sellers=_CommerceSellers(),
    )


class UserCashOperationContractTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, *, cash: int = 0):
        collection = _CashCollection(cash=cash)
        store = UserStore({"users": collection})
        store.ensure_user = AsyncMock(return_value=deepcopy(collection.document))
        return store, collection

    async def test_negative_debit_is_rejected_before_touching_storage(self):
        store, collection = self.make_store(cash=1_000)

        with self.assertRaises(ValueError):
            await store.spend_cash(1, 2, -500)

        store.ensure_user.assert_not_awaited()
        self.assertEqual(collection.document["cash"], 1_000)

    async def test_non_positive_credit_is_rejected_before_touching_storage(self):
        store, collection = self.make_store(cash=1_000)

        for amount in (0, -1):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                await store.add_cash(1, 2, amount)

        store.ensure_user.assert_not_awaited()
        self.assertEqual(collection.document["cash"], 1_000)

    async def test_concurrent_credit_retries_apply_one_balance_change(self):
        store, collection = self.make_store(cash=100)

        results = await asyncio.gather(
            store.add_cash_once(1, 2, 500, operation_id="charge:abc"),
            store.add_cash_once(1, 2, 500, operation_id="charge:abc"),
        )

        self.assertEqual(collection.document["cash"], 600)
        self.assertEqual(len(collection.document["cash_operations"]), 1)
        self.assertEqual(sum(result["applied"] for result in results), 1)
        self.assertEqual(
            {(result["before_cash"], result["after_cash"]) for result in results},
            {(100, 600)},
        )

    async def test_concurrent_debit_retries_return_the_original_result(self):
        store, collection = self.make_store(cash=1_000)

        results = await asyncio.gather(
            store.spend_cash_once(1, 2, 400, operation_id="purchase:abc"),
            store.spend_cash_once(1, 2, 400, operation_id="purchase:abc"),
        )

        self.assertEqual(collection.document["cash"], 600)
        self.assertEqual(collection.document["accrued_spent"], 400)
        self.assertEqual(len(collection.document["cash_operations"]), 1)
        self.assertEqual(sum(result["applied"] for result in results), 1)
        self.assertEqual(
            {(result["before_cash"], result["after_cash"]) for result in results},
            {(1_000, 600)},
        )

    async def test_different_concurrent_debits_cannot_overdraw_balance(self):
        store, collection = self.make_store(cash=1_000)

        results = await asyncio.gather(
            store.spend_cash_once(1, 2, 800, operation_id="purchase:a"),
            store.spend_cash_once(1, 2, 800, operation_id="purchase:b"),
        )

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(collection.document["cash"], 200)
        self.assertGreaterEqual(collection.document["cash"], 0)
        self.assertEqual(collection.document["accrued_spent"], 800)


class InvalidMoneyAtRepositoryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_charge_request_rejects_non_positive_amount(self):
        charge_logs = SimpleNamespace(insert_one=AsyncMock())
        store = VendingLogStore(
            {
                "vending_charge_logs": charge_logs,
                "vending_purchase_logs": AsyncMock(),
                "user_products": AsyncMock(),
            }
        )

        for amount in (0, -10):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                await store.create_charge_request(1, 2, "payer", amount)

        charge_logs.insert_one.assert_not_awaited()

    async def test_product_repository_rejects_negative_price(self):
        products = SimpleNamespace(update_one=AsyncMock())
        store = ProductStore({"products": products})

        with self.assertRaises(ValueError):
            await store.upsert(
                1,
                "product-a",
                title="Product A",
                price=-1,
                terabox_url="https://example.test/file",
            )

        products.update_one.assert_not_awaited()


class VendingOperationContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def product(*, product_id: str = "product-a", price: int = 500):
        return {
            "product_id": product_id,
            "product_id_lower": product_id.casefold(),
            "title": product_id,
            "price": price,
            "terabox_url": "https://example.test/file",
        }

    async def test_concurrent_charge_claims_resume_one_business_operation(self):
        charge_logs = _ChargeCollection()
        store = VendingLogStore(
            {
                "vending_charge_logs": charge_logs,
                "vending_purchase_logs": AsyncMock(),
                "user_products": AsyncMock(),
            }
        )

        claims = await asyncio.gather(
            store.claim_charge_request(1, 10, 100),
            store.claim_charge_request(1, 10, 200),
        )

        self.assertEqual({item["_id"] for item in claims}, {"charge-id"})
        self.assertEqual({item["status"] for item in claims}, {"processing"})
        self.assertEqual(charge_logs.document["status"], "processing")

    async def test_concurrent_reservations_reuse_one_operation_id(self):
        entitlements = _EntitlementCollection()
        store = VendingLogStore(
            {
                "vending_charge_logs": AsyncMock(),
                "vending_purchase_logs": AsyncMock(),
                "user_products": entitlements,
            }
        )
        product = self.product()

        reservations = await asyncio.gather(
            store.reserve_product(1, 2, product, original_price=500, quoted_price=500),
            store.reserve_product(1, 2, product, original_price=500, quoted_price=500),
        )

        self.assertEqual(len(entitlements.documents), 1)
        self.assertEqual(
            {item["operation_id"] for item in reservations},
            {entitlements.documents[0]["operation_id"]},
        )
        self.assertEqual({item["status"] for item in reservations}, {"pending"})

    async def test_purchase_log_retry_preserves_first_committed_values(self):
        purchase_logs = _PurchaseLogCollection()
        store = VendingLogStore(
            {
                "vending_charge_logs": AsyncMock(),
                "vending_purchase_logs": purchase_logs,
                "user_products": AsyncMock(),
            }
        )
        product = self.product()

        first, retry = await asyncio.gather(
            store.upsert_purchase_log(
                operation_id="purchase:abc",
                guild_id=1,
                user_id=2,
                product=product,
                price=500,
                original_price=500,
                before_cash=1_000,
                after_cash=500,
            ),
            store.upsert_purchase_log(
                operation_id="purchase:abc",
                guild_id=1,
                user_id=2,
                product=product,
                price=999,
                original_price=999,
                before_cash=9_999,
                after_cash=9_000,
            ),
        )

        self.assertEqual(len(purchase_logs.documents), 1)
        self.assertEqual(first, retry)
        self.assertIn(first["price"], {500, 999})
        self.assertEqual(first["before_cash"] - first["after_cash"], first["price"])

    async def test_entitlement_is_the_idempotent_final_commit_marker(self):
        entitlements = _EntitlementCollection()
        product = self.product()
        entitlements.documents.append(
            {
                "guild_id": 1,
                "user_id": 2,
                "product_id": product["product_id"],
                "product_id_lower": product["product_id_lower"],
                "operation_id": "purchase:abc",
                "status": "pending",
            }
        )
        store = VendingLogStore(
            {
                "vending_charge_logs": AsyncMock(),
                "vending_purchase_logs": AsyncMock(),
                "user_products": entitlements,
            }
        )

        completions = await asyncio.gather(
            store.complete_product_purchase(
                operation_id="purchase:abc", guild_id=1, user_id=2, product=product
            ),
            store.complete_product_purchase(
                operation_id="purchase:abc", guild_id=1, user_id=2, product=product
            ),
        )

        self.assertEqual(entitlements.documents[0]["status"], "purchased")
        self.assertEqual(sum(committed for _, committed in completions), 1)
        self.assertEqual({item[0]["status"] for item in completions}, {"purchased"})


class ChargeApprovalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_ambiguous_credit_does_not_double_credit(self):
        repos = _commerce_repos(cash=100)
        repos.users.credit_failure_after_commit = 1
        service = VendingCommerceService(repos)

        with self.assertRaisesRegex(RuntimeError, "ambiguous credit"):
            await service.approve_charge(1, 10, 100)

        self.assertEqual(repos.users.cash, 600)
        self.assertEqual(repos.vending.charge["status"], "processing")

        result = await service.approve_charge(1, 10, 100)

        self.assertEqual(result.status, "approved")
        self.assertEqual(repos.users.cash, 600)
        self.assertEqual(len(repos.users.operations), 1)
        self.assertEqual(repos.vending.charge["status"], "approved")

    async def test_retry_after_credit_before_approval_commit_resumes_processing(self):
        repos = _commerce_repos(cash=100)
        repos.vending.fail_approve_before_commit = 1
        service = VendingCommerceService(repos)

        with self.assertRaisesRegex(RuntimeError, "approval write failed"):
            await service.approve_charge(1, 10, 100)

        self.assertEqual(repos.users.cash, 600)
        self.assertEqual(repos.vending.charge["status"], "processing")

        result = await service.approve_charge(1, 10, 100)

        self.assertEqual(result.status, "approved")
        self.assertEqual(repos.users.cash, 600)
        self.assertEqual(len(repos.users.operations), 1)

    async def test_retry_after_ambiguous_approval_commit_is_financially_safe(self):
        repos = _commerce_repos(cash=100)
        repos.vending.fail_approve_after_commit = 1
        service = VendingCommerceService(repos)

        with self.assertRaisesRegex(RuntimeError, "ambiguous approval"):
            await service.approve_charge(1, 10, 100)

        result = await service.approve_charge(1, 10, 100)

        self.assertEqual(result.status, "approved")
        self.assertFalse(result.newly_completed)
        self.assertEqual(repos.users.cash, 600)
        self.assertEqual(len(repos.users.operations), 1)
        self.assertEqual(repos.vending.charge["status"], "approved")

    async def test_concurrent_charge_approval_calls_credit_once(self):
        repos = _commerce_repos(cash=100)
        service = VendingCommerceService(repos)

        results = await asyncio.gather(
            service.approve_charge(1, 10, 100),
            service.approve_charge(1, 10, 200),
        )

        self.assertEqual(repos.users.cash, 600)
        self.assertEqual(len(repos.users.operations), 1)
        self.assertEqual(repos.vending.charge["status"], "approved")
        self.assertTrue(all(result.charge is not None for result in results))

    async def test_corrupt_negative_charge_cannot_credit_or_be_approved(self):
        repos = _commerce_repos(cash=100, charge_amount=-500)
        service = VendingCommerceService(repos)

        with self.assertRaises(ValueError):
            await service.approve_charge(1, 10, 100)

        self.assertEqual(repos.users.cash, 100)
        self.assertFalse(repos.users.operations)
        self.assertNotEqual(repos.vending.charge["status"], "approved")


class PurchaseServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def product(product_id="product-a", *, price=1_000, seller_id=None):
        product = {
            "product_id": product_id,
            "product_id_lower": product_id.casefold(),
            "title": product_id,
            "price": price,
            "terabox_url": "https://example.test/file",
        }
        if seller_id is not None:
            product["seller_id"] = seller_id
        return product

    async def _assert_retry_after_purchase_fault(self, attribute):
        repos = _commerce_repos(cash=1_000, coupon_quantity=1)
        setattr(repos.vending, attribute, 1)
        service = VendingCommerceService(repos)
        product = self.product()

        with self.assertRaises(RuntimeError):
            await service.purchase(1, 2, product)

        result = await service.purchase(1, 2, product)

        self.assertEqual(result.status, "purchased")
        self.assertEqual(result.price, 500)
        self.assertEqual(repos.users.cash, 500)
        self.assertEqual(len(repos.users.operations), 1)
        self.assertEqual(repos.coupons.quantity, 0)
        self.assertEqual(len(repos.coupons.consumed), 1)
        self.assertEqual(len(repos.vending.purchase_logs), 1)
        self.assertEqual(
            {item["status"] for item in repos.vending.entitlements.values()},
            {"purchased"},
        )

    async def test_retry_after_debit_before_log_commit_reuses_operation(self):
        await self._assert_retry_after_purchase_fault("fail_log_before_commit")

    async def test_retry_after_ambiguous_log_commit_does_not_duplicate(self):
        await self._assert_retry_after_purchase_fault("fail_log_after_commit")

    async def test_retry_after_ambiguous_debit_does_not_charge_twice(self):
        repos = _commerce_repos(cash=1_000, coupon_quantity=1)
        repos.users.debit_failure_after_commit = 1
        service = VendingCommerceService(repos)
        product = self.product()

        with self.assertRaisesRegex(RuntimeError, "ambiguous debit"):
            await service.purchase(1, 2, product)

        result = await service.purchase(1, 2, product)

        self.assertEqual(result.status, "purchased")
        self.assertEqual(repos.users.cash, 500)
        self.assertEqual(len(repos.users.operations), 1)
        self.assertEqual(len(repos.vending.purchase_logs), 1)

    async def test_retry_after_entitlement_commit_returns_already_owned(self):
        repos = _commerce_repos(cash=1_000, coupon_quantity=1)
        repos.vending.fail_complete_after_commit = 1
        service = VendingCommerceService(repos)
        product = self.product()

        with self.assertRaisesRegex(RuntimeError, "ambiguous entitlement"):
            await service.purchase(1, 2, product)

        retry = await service.purchase(1, 2, product)

        self.assertEqual(retry.status, "already_owned")
        self.assertEqual(repos.users.cash, 500)
        self.assertEqual(len(repos.users.operations), 1)
        self.assertEqual(len(repos.vending.purchase_logs), 1)

    async def test_retry_after_ambiguous_seller_credit_records_sale_once(self):
        repos = _commerce_repos(cash=1_000, coupon_quantity=1)
        repos.sellers.fail_after_commit = 1
        service = VendingCommerceService(repos)
        product = self.product(seller_id=30)

        with self.assertRaisesRegex(RuntimeError, "ambiguous seller"):
            await service.purchase(1, 2, product)

        result = await service.purchase(1, 2, product)

        self.assertEqual(result.status, "purchased")
        self.assertEqual(repos.users.cash, 500)
        self.assertEqual(repos.sellers.total, 500)
        self.assertEqual(len(repos.sellers.operations), 1)
        self.assertEqual(len(repos.vending.purchase_logs), 1)

    async def test_concurrent_same_product_purchase_has_one_financial_effect(self):
        repos = _commerce_repos(cash=2_000, coupon_quantity=1)
        service = VendingCommerceService(repos)
        product = self.product(seller_id=30)

        results = await asyncio.gather(
            service.purchase(1, 2, product),
            service.purchase(1, 2, product),
        )

        self.assertEqual(repos.users.cash, 1_500)
        self.assertEqual(len(repos.users.operations), 1)
        self.assertEqual(len(repos.vending.purchase_logs), 1)
        self.assertEqual(len(repos.vending.entitlements), 1)
        self.assertEqual(repos.coupons.quantity, 0)
        self.assertEqual(repos.sellers.total, 500)
        self.assertEqual(len(repos.sellers.operations), 1)
        self.assertTrue(all(result.status in {"purchased", "already_owned"} for result in results))

    async def test_concurrent_different_products_cannot_reuse_one_coupon(self):
        repos = _commerce_repos(cash=2_000, coupon_quantity=1)
        repos.coupons.quote_parties = 2
        service = VendingCommerceService(repos)

        results = await asyncio.gather(
            service.purchase(1, 2, self.product("product-a")),
            service.purchase(1, 2, self.product("product-b")),
        )

        self.assertEqual({result.status for result in results}, {"purchased"})
        self.assertEqual(sorted(result.price for result in results), [500, 1_000])
        self.assertEqual(repos.users.cash, 500)
        self.assertEqual(repos.coupons.quantity, 0)
        self.assertEqual(len(repos.vending.purchase_logs), 2)
        self.assertEqual(len(repos.vending.entitlements), 2)

    async def test_insufficient_funds_restores_coupon_and_releases_reservation(self):
        repos = _commerce_repos(cash=100, coupon_quantity=1)
        service = VendingCommerceService(repos)

        result = await service.purchase(1, 2, self.product())

        self.assertEqual(result.status, "insufficient_funds")
        self.assertEqual(repos.users.cash, 100)
        self.assertFalse(repos.users.operations)
        self.assertEqual(repos.coupons.quantity, 1)
        self.assertFalse(repos.vending.entitlements)
        self.assertFalse(repos.vending.purchase_logs)

    async def test_negative_product_price_is_rejected_before_any_side_effect(self):
        repos = _commerce_repos(cash=100)
        service = VendingCommerceService(repos)

        with self.assertRaises(ValueError):
            await service.purchase(1, 2, self.product(price=-100))

        self.assertEqual(repos.users.cash, 100)
        self.assertFalse(repos.users.operations)
        self.assertFalse(repos.vending.entitlements)
        self.assertFalse(repos.vending.purchase_logs)


class RandomPurchaseServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def product(product_id="prize-a", *, seller_id=None, weight=1):
        product = {
            "product_id": product_id,
            "product_id_lower": product_id.casefold(),
            "title": product_id,
            "terabox_url": "https://example.test/file",
            "weight": weight,
        }
        if seller_id is not None:
            product["seller_id"] = seller_id
        return product

    async def test_empty_pool_is_rejected_without_charging(self):
        repos = _commerce_repos(cash=1_000)
        service = VendingCommerceService(repos)

        result = await service.random_purchase(1, 2, [], 300, source="exclusive")

        self.assertEqual(result.status, "empty_pool")
        self.assertEqual(repos.users.cash, 1_000)
        self.assertFalse(repos.users.operations)
        self.assertFalse(repos.vending.purchase_logs)

    async def test_non_positive_price_is_rejected_before_any_side_effect(self):
        repos = _commerce_repos(cash=1_000)
        service = VendingCommerceService(repos)

        with self.assertRaises(ValueError):
            await service.random_purchase(1, 2, [self.product()], 0, source="catalog")

        self.assertEqual(repos.users.cash, 1_000)
        self.assertFalse(repos.users.operations)

    async def test_insufficient_funds_does_not_grant_a_product(self):
        repos = _commerce_repos(cash=100)
        service = VendingCommerceService(repos)

        result = await service.random_purchase(1, 2, [self.product()], 300, source="catalog")

        self.assertEqual(result.status, "insufficient_funds")
        self.assertEqual(result.current_cash, 100)
        self.assertEqual(repos.users.cash, 100)
        self.assertFalse(repos.users.operations)
        self.assertFalse(repos.vending.entitlements)
        self.assertFalse(repos.vending.purchase_logs)

    async def test_successful_draw_charges_the_fixed_price_and_grants_the_product(self):
        repos = _commerce_repos(cash=1_000)
        service = VendingCommerceService(repos)
        product = self.product(seller_id=30)

        result = await service.random_purchase(1, 2, [product], 300, source="exclusive")

        self.assertEqual(result.status, "purchased")
        self.assertEqual(result.price, 300)
        self.assertEqual(result.product["product_id"], "prize-a")
        self.assertEqual(repos.users.cash, 700)
        self.assertEqual(len(repos.users.operations), 1)
        self.assertEqual(len(repos.vending.purchase_logs), 1)
        log = next(iter(repos.vending.purchase_logs.values()))
        self.assertEqual(log["kind"], "random_exclusive")
        self.assertEqual(log["price"], 300)
        entitlement = repos.vending.entitlements[(1, 2, "prize-a")]
        self.assertEqual(entitlement["status"], "purchased")
        self.assertEqual(repos.sellers.total, 300)

    async def test_drawing_an_already_owned_prize_still_charges_and_does_not_crash(self):
        repos = _commerce_repos(cash=1_000)
        service = VendingCommerceService(repos)
        product = self.product()

        first = await service.random_purchase(1, 2, [product], 300, source="catalog")
        second = await service.random_purchase(1, 2, [product], 300, source="catalog")

        self.assertEqual(first.status, "purchased")
        self.assertEqual(second.status, "purchased")
        self.assertEqual(repos.users.cash, 400)
        self.assertEqual(len(repos.users.operations), 2)
        self.assertEqual(len(repos.vending.purchase_logs), 2)
        self.assertEqual(len(repos.vending.entitlements), 1)

    async def test_draw_only_ever_selects_from_the_given_pool(self):
        repos = _commerce_repos(cash=10_000)
        service = VendingCommerceService(repos)
        pool = [self.product("a"), self.product("b"), self.product("c")]

        drawn_ids = set()
        for _ in range(20):
            result = await service.random_purchase(1, 2, pool, 100, source="catalog")
            drawn_ids.add(result.product["product_id"])

        self.assertTrue(drawn_ids.issubset({"a", "b", "c"}))


if __name__ == "__main__":
    unittest.main()
