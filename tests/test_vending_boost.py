import hashlib
import hmac
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.vending_boost import order_boost, signed_headers, validate_boost
from services.vending_topup import TopupPendingError
from test_vending_topup import TopupTests


class BoostCommerceTests(TopupTests):
    async def test_boost_retry_snapshot_and_repeat(self):
        self.product.update(topup_enabled=False, boost_enabled=True,
                            boost_months=1, boost_quantity=2)
        with patch('services.vending.order_boost', new_callable=AsyncMock) as order:
            order.side_effect = [TopupPendingError(), {'http_status': 202},
                                 {'http_status': 202}]
            with self.assertRaises(TopupPendingError):
                await self.service.purchase(1, 2, self.product)
            self.product['boost_quantity'] = 4
            result = await self.service.purchase(1, 2, self.product)
            self.assertEqual(order.await_args_list[0], order.await_args_list[1])
            self.assertEqual(result.product['boost_quantity'], 2)
            self.assertEqual(self.repos.users.cash, 4000)
            repeat = await self.service.purchase(1, 2, self.product)
            self.assertNotEqual(result.operation_id, repeat.operation_id)
            self.assertEqual(order.await_args.args[2], 4)
            self.assertEqual(self.repos.users.cash, 3000)

    async def test_boost_off_and_insufficient_funds(self):
        with patch('services.vending.order_boost', new_callable=AsyncMock) as order:
            self.product.update(topup_enabled=False, boost_enabled=True,
                                boost_months=3, boost_quantity=14, price=6000)
            result = await self.service.purchase(1, 2, self.product)
            self.assertEqual(result.status, 'insufficient_funds')
            self.product.update(boost_enabled=False, price=1000)
            await self.service.purchase(1, 2, self.product)
            order.assert_not_awaited()


class BoostClientTests(unittest.IsolatedAsyncioTestCase):
    def test_signature_matches_provided_contract(self):
        body = b'{"quantity":2}'
        message = '\n'.join(('123', 'nonce', 'POST', '/api/orders',
                             hashlib.sha256(body).hexdigest())).encode()
        expected = hmac.new(b'secret', message, hashlib.sha256).hexdigest()
        headers = signed_headers('token', 'secret', 'POST', '/boost-api/orders',
                                 body, '123', 'nonce')
        self.assertEqual(headers['X-BOOST-Signature'], expected)

    def test_invalid_product_parameters(self):
        for months, quantity in [(2, 2), (1, 1), (3, 1002), (1, 3), (True, 2)]:
            with self.assertRaises(ValueError):
                validate_boost(months, quantity)
        validate_boost(1, 2)
        validate_boost(3, 1000)

    async def test_request_contract_and_rejection(self):
        response = AsyncMock()
        response.status = 202
        response.json.return_value = {'request_id': 'purchase:abc'}
        session = MagicMock()
        session.request.return_value.__aenter__.return_value = response
        with patch.dict('os.environ', {'API_TOKEN': 'token',
                                      'API_SIGNING_SECRET': 'secret',
                                      'BOOST_API_URL': 'https://devilblox.shop/boost-api'}), \
             patch('services.vending_boost.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__.return_value = session
            result = await order_boost(123, 3, 14, 'purchase:abc')
            self.assertEqual(result['http_status'], 202)
            args = session.request.call_args
            self.assertEqual(args.args, ('POST', 'https://devilblox.shop/boost-api/orders'))
            self.assertEqual(args.kwargs['data'],
                             b'{"request_id":"purchase:abc","user_id":"123","months":3,"quantity":14}')
            self.assertFalse(args.kwargs['allow_redirects'])
            response.json.return_value = {'ok': False}
            with self.assertRaises(TopupPendingError):
                await order_boost(123, 3, 14, 'purchase:abc')
            for status in (301, 401, 403, 500):
                response.status = status
                with self.assertRaises(TopupPendingError):
                    await order_boost(123, 3, 14, 'purchase:abc')
