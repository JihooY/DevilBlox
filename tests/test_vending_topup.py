from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from database.vending import VendingLogStore
from services.vending import VendingCommerceService
from services.vending_topup import TopupPendingError, apply_plan, charge_tokens
from test_vending_financial_consistency import (
    _commerce_repos, _EntitlementCollection, _PurchaseLogCollection,
)


class Entitlements(_EntitlementCollection):
    async def delete_one(self, query):
        self.documents[:] = [doc for doc in self.documents
                             if not all(doc.get(key) == value for key, value in query.items())]

    async def find_one_and_update(self, query, update, **kwargs):
        result = await super().find_one_and_update(query, update, **kwargs)
        if result:
            for doc in self.documents:
                if doc['operation_id'] == result['operation_id']:
                    for key in update.get('$unset', {}):
                        doc.pop(key, None)
                    return deepcopy(doc)
        return result


class TopupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repos = _commerce_repos(cash=5000)
        self.entitlements = Entitlements()
        self.repos.vending = VendingLogStore({
            'user_products': self.entitlements,
            'vending_purchase_logs': _PurchaseLogCollection(),
            'vending_charge_logs': AsyncMock(),
        })
        self.service = VendingCommerceService(self.repos)
        self.product = dict(product_id='tokens', product_id_lower='tokens',
                            price=1000, topup_enabled=True, topup_tokens=100)

    async def test_timeout_retry_preserves_order_and_snapshot_then_repeat_is_new_order(self):
        with patch('services.vending.charge_tokens', new_callable=AsyncMock) as charge:
            charge.side_effect = [TopupPendingError(), {'ok': True, 'charged': False},
                                  {'ok': True, 'charged': True}]
            with self.assertRaises(TopupPendingError):
                await self.service.purchase(1, 2, self.product)
            self.assertEqual(self.repos.users.cash, 4000)
            self.assertEqual(self.entitlements.documents[0]['status'], 'pending')
            self.product['topup_tokens'] = 200
            result = await self.service.purchase(1, 2, self.product)
            self.assertEqual(result.product['topup_tokens'], 100)
            self.assertEqual(charge.await_args_list[0], charge.await_args_list[1])
            self.assertEqual(self.repos.users.cash, 4000)
            repeat = await self.service.purchase(1, 2, self.product)
            self.assertNotEqual(result.operation_id, repeat.operation_id)
            self.assertEqual(charge.await_args_list[2].args[1], 200)
            self.assertEqual(self.repos.users.cash, 3000)

    async def test_off_and_insufficient_balance_do_not_call_api(self):
        with patch('services.vending.charge_tokens', new_callable=AsyncMock) as charge:
            self.product['price'] = 6000
            self.assertEqual((await self.service.purchase(1, 2, self.product)).status,
                             'insufficient_funds')
            self.product.update(price=1000, topup_enabled=False)
            await self.service.purchase(1, 2, self.product)
            charge.assert_not_awaited()

    async def test_http_contract_and_duplicate_response(self):
        response = AsyncMock()
        response.status = 200
        response.json.return_value = {'ok': True, 'charged': False}
        session = MagicMock()
        session.post.return_value.__aenter__.return_value = response
        with patch.dict('os.environ', {'VENDING_ADMIN_API_KEY': 'test-key'}), \
             patch('services.vending_topup.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__.return_value = session
            self.assertEqual(await charge_tokens(2, 100, 'order-1'),
                             {'ok': True, 'charged': False})
            args = session.post.call_args
            self.assertEqual(args.args[0], 'https://backup.devilblox.shop/api/topup')
            self.assertEqual(args.kwargs['headers']['X-Admin-Api-Key'], 'test-key')
            self.assertEqual(args.kwargs['json'], {
                'discord_user_id': '2', 'tokens': 100,
                'reason': 'vending_purchase', 'reference': 'order-1',
            })
            response.status = 500
            with self.assertRaises(TopupPendingError):
                await charge_tokens(2, 100, 'order-1')
            response.status = 200
            response.json.return_value = {'ok': False}
            with self.assertRaises(TopupPendingError):
                await charge_tokens(2, 100, 'order-1')

    async def test_plan_contract_and_retry_reference(self):
        self.product.update(topup_kind='plan', topup_plan='pro', topup_months=3,
                            topup_tokens=0)
        with patch('services.vending.apply_plan', new_callable=AsyncMock) as delivery:
            delivery.side_effect = [TopupPendingError(), {'ok': True}]
            with self.assertRaises(TopupPendingError):
                await self.service.purchase(1, 2, self.product)
            result = await self.service.purchase(1, 2, self.product)
            self.assertEqual(delivery.await_args_list[0], delivery.await_args_list[1])
            self.assertEqual(delivery.await_args.args[:3], (2, 'pro', 3))
            self.assertEqual(delivery.await_args.args[3], result.operation_id)
            self.assertEqual(self.repos.users.cash, 4000)

        response = AsyncMock()
        response.status = 200
        response.json.return_value = {'ok': True, 'applied': True}
        session = MagicMock()
        session.post.return_value.__aenter__.return_value = response
        with patch.dict('os.environ', {'VENDING_ADMIN_API_KEY': 'test-key'}), \
             patch('services.vending_topup.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__.return_value = session
            await apply_plan(2, 'plus', 6, 'order-plan')
            args = session.post.call_args
            self.assertEqual(args.args[0], 'https://backup.devilblox.shop/api/plan')
            self.assertEqual(args.kwargs['json'], {
                'discord_user_id': '2', 'plan': 'plus', 'months': 6,
                'reason': 'vending_purchase', 'reference': 'order-plan',
            })

        for plan, months in [('basic', 1), ('plus', 12)]:
            with self.assertRaises(ValueError):
                await apply_plan(2, plan, months, 'bad-order')
