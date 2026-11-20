# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import base64
import hashlib
import hmac
import json

from odoo.tests.common import HttpCase, tagged

from ..models import client as client_mod
from ..models import helpers
from .common import ShopifyCase
from .mock_shopify import MockShopify
from .test_products import PIXEL


def sign(secret, body):
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


@tagged('post_install', '-at_install', 'axsync_shopify')
class TestQueue(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.connect()
        self.Queue = self.env['axsync.shopify.queue']

    def test_open_job_is_not_duplicated(self):
        first = self.Queue.enqueue(self.store, 'order', 'ORDERS_UPDATED', 'gid://shopify/Order/1')
        second = self.Queue.enqueue(self.store, 'order', 'ORDERS_UPDATED', 'gid://shopify/Order/1')
        self.assertEqual(first, second)
        third = self.Queue.enqueue(self.store, 'order', 'ORDERS_UPDATED', 'gid://shopify/Order/2')
        self.assertNotEqual(first, third)

    def test_webhook_id_makes_delivery_idempotent(self):
        self.assertTrue(self.Queue.enqueue(self.store, 'order', 'ORDERS_CREATE', 'gid://shopify/Order/1', webhook_id='w-1'))
        self.assertFalse(self.Queue.enqueue(self.store, 'order', 'ORDERS_CREATE', 'gid://shopify/Order/1', webhook_id='w-1'))
        self.assertEqual(self.Queue.search_count([('webhook_id', '=', 'w-1')]), 1)

    def test_failing_job_backs_off_then_gives_up_and_can_be_retried(self):
        job = self.Queue.enqueue(self.store, 'order', 'ORDERS_UPDATED', 'gid://shopify/Order/404')
        for attempt in range(1, 6):
            self.assertFalse(job.run_job())
            self.assertEqual((job.state, job.attempts), ('failed', attempt))
            self.assertGreater(job.next_try, self.env.cr.now() if hasattr(self.env.cr, 'now') else job.create_date)
        self.assertEqual(self.process_queue(), 0, 'a job that must wait is not picked up')
        self.assertFalse(job.run_job())
        self.assertEqual(job.state, 'dead')
        self.assertIn('not found', job.error)
        self.assertTrue(self.env['axsync.shopify.log'].search([('store_id', '=', self.store.id), ('level', '=', 'error')]))
        self.mock.add_order('#9', [], financial='PENDING')
        job.action_retry()
        self.assertEqual((job.state, job.attempts), ('pending', 0))

    def test_queue_processes_due_jobs_in_order(self):
        self.make_product('MUG-01', 10.0, qty=5)
        node = self.mock.add_order('#3001', [{'sku': 'MUG-01', 'title': 'Mug', 'qty': 1, 'price': 10.0}], financial='PENDING', email='q@example.test')
        job = self.Queue.enqueue(self.store, 'order', 'ORDERS_CREATE', node['id'])
        self.assertEqual(self.process_queue(), 1)
        self.assertEqual(job.state, 'done')
        self.assertTrue(job.done_date)
        self.env['axsync.shopify.store']._cron_process_queue()

    def test_one_broken_store_does_not_stop_the_others(self):
        broken = self.store.copy({'name': 'Broken', 'shop_domain': 'broken.myshopify.com', 'client_secret': 'wrong'})
        broken.state = 'connected'
        self.mock.add_product('Mug', [{'sku': 'MUG', 'price': 1.0}])
        self.env['axsync.shopify.store']._cron_import_orders()
        errors = self.env['axsync.shopify.log'].search([('store_id', '=', broken.id), ('level', '=', 'error')])
        self.assertTrue(errors)
        self.assertEqual(broken.state, 'error')
        self.assertTrue(self.store.last_order_sync, 'the healthy store was still processed')

    def test_cleanup_removes_old_noise_but_keeps_errors(self):
        old = '2000-01-01 00:00:00'
        done = self.Queue.create({'store_id': self.store.id, 'kind': 'order', 'state': 'done'})
        info = self.env['axsync.shopify.log'].create({'store_id': self.store.id, 'level': 'info', 'message': 'x'})
        err = self.env['axsync.shopify.log'].create({'store_id': self.store.id, 'level': 'error', 'message': 'y'})
        self.env.cr.execute("UPDATE axsync_shopify_queue SET create_date=%s WHERE id=%s", (old, done.id))
        self.env.cr.execute("UPDATE axsync_shopify_log SET create_date=%s WHERE id IN %s", (old, (info.id, err.id)))
        self.env.invalidate_all()
        self.env['axsync.shopify.store']._cron_cleanup()
        self.assertFalse(done.exists())
        self.assertFalse(info.exists())
        self.assertTrue(err.exists())

    def test_app_uninstalled_marks_store(self):
        self.Queue.enqueue(self.store, 'app', 'APP_UNINSTALLED', 'app')
        self.process_queue()
        self.assertEqual(self.store.state, 'error')

    def test_checkout_job_imports_checkouts(self):
        self.mock.add_checkout('lena@example.test', 59.0, [('Mug', 2, 'MUG-01')])
        self.Queue.enqueue(self.store, 'checkout', 'CHECKOUTS_CREATE', 'gid://shopify/Checkout/1')
        self.process_queue()
        self.assertEqual(self.env['axsync.shopify.checkout'].search_count([('store_id', '=', self.store.id)]), 1)


@tagged('post_install', '-at_install', 'axsync_shopify')
class TestWebhookHttp(HttpCase):

    def setUp(self):
        super().setUp()
        self.mock = MockShopify()
        self._old = (client_mod.TRANSPORT, client_mod.SLEEP)
        client_mod.TRANSPORT, client_mod.SLEEP = self.mock, (lambda s: None)
        self.addCleanup(lambda: setattr(client_mod, 'TRANSPORT', self._old[0]))
        self.addCleanup(lambda: setattr(client_mod, 'SLEEP', self._old[1]))
        env = self.env
        self.store = env['axsync.shopify.store'].create({
            'name': 'Hook Shop', 'shop_domain': 'hook-shop.myshopify.com', 'client_id': 'cid', 'client_secret': 'csecret', 'state': 'connected'})
        self.url = '/axsync_shopify/webhook/%s' % self.store.id
        self.Queue = env['axsync.shopify.queue']

    def post(self, topic, payload, secret='csecret', webhook_id='wh-1', url=None, signature=None):
        body = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json', 'X-Shopify-Topic': topic, 'X-Shopify-Webhook-Id': webhook_id,
                   'X-Shopify-Shop-Domain': 'hook-shop.myshopify.com'}
        if signature is not False:
            headers['X-Shopify-Hmac-Sha256'] = signature or sign(secret, body)
        return self.url_open(url or self.url, data=body, headers=headers)

    def test_valid_signature_queues_a_job(self):
        res = self.post('orders/create', {'id': 55, 'admin_graphql_api_id': 'gid://shopify/Order/55'})
        self.assertEqual((res.status_code, res.text), (200, 'queued'))
        job = self.Queue.search([('store_id', '=', self.store.id)])
        self.assertEqual((job.kind, job.topic, job.shopify_id, job.webhook_id), ('order', 'ORDERS_CREATE', 'gid://shopify/Order/55', 'wh-1'))
        self.assertTrue(self.store.last_webhook_at)

    def test_bad_or_missing_signature_is_refused(self):
        self.assertEqual(self.post('orders/create', {'id': 1}, secret='wrong').status_code, 401)
        self.assertEqual(self.post('orders/create', {'id': 1}, signature=False).status_code, 401)
        self.assertEqual(self.post('orders/create', {'id': 1}, signature='AAAA').status_code, 401)
        self.assertFalse(self.Queue.search([('store_id', '=', self.store.id)]))

    def test_unknown_store_and_wrong_method(self):
        self.assertEqual(self.post('orders/create', {'id': 1}, url='/axsync_shopify/webhook/999999').status_code, 404)
        self.assertEqual(self.url_open(self.url).status_code, 405)

    def test_duplicate_delivery_is_acknowledged_once(self):
        payload = {'id': 55, 'admin_graphql_api_id': 'gid://shopify/Order/55'}
        self.assertEqual(self.post('orders/create', payload, webhook_id='same').status_code, 200)
        self.assertEqual(self.post('orders/create', payload, webhook_id='same').status_code, 200)
        self.assertEqual(self.Queue.search_count([('store_id', '=', self.store.id)]), 1)

    def test_topics_map_to_the_right_jobs(self):
        cases = [
            ('refunds/create', {'id': 9, 'order_id': 77}, 'refund', 'gid://shopify/Order/77'),
            ('fulfillments/create', {'id': 9, 'order_id': 78}, 'fulfillment', 'gid://shopify/Order/78'),
            ('products/update', {'id': 5, 'admin_graphql_api_id': 'gid://shopify/Product/5'}, 'product', 'gid://shopify/Product/5'),
            ('customers/create', {'id': 6}, 'customer', 'gid://shopify/Customer/6'),
            ('inventory_levels/update', {'inventory_item_id': 12, 'available': 3}, 'inventory', '12'),
            ('app/uninstalled', {'id': 1}, 'app', 'app'),
        ]
        for i, (topic, payload, kind, gid) in enumerate(cases):
            self.assertEqual(self.post(topic, payload, webhook_id='t-%s' % i).status_code, 200, topic)
            job = self.Queue.search([('store_id', '=', self.store.id), ('webhook_id', '=', 't-%s' % i)])
            self.assertEqual((job.kind, job.shopify_id), (kind, gid), topic)
        inv = self.Queue.search([('kind', '=', 'inventory')])
        self.assertEqual(json.loads(inv.payload)['inventory_item_id'], 12)

    def test_disabled_topic_and_switch(self):
        self.env['axsync.shopify.webhook'].create({'store_id': self.store.id, 'topic': 'ORDERS_UPDATED', 'enabled': False})
        res = self.post('orders/updated', {'id': 1, 'admin_graphql_api_id': 'gid://shopify/Order/1'})
        self.assertEqual(res.text, 'topic disabled')
        self.assertFalse(self.Queue.search([('store_id', '=', self.store.id)]))
        self.assertEqual(self.store.webhook_ids.received_count, 1)
        self.store.use_webhooks = False
        self.assertEqual(self.post('orders/create', {'id': 2, 'admin_graphql_api_id': 'gid://shopify/Order/2'}, webhook_id='x').text, 'webhooks disabled')

    def test_gdpr_requests_are_logged(self):
        for topic in ('customers/data_request', 'customers/redact', 'shop/redact'):
            res = self.post(topic, {'shop_domain': 'hook-shop.myshopify.com'}, webhook_id=topic)
            self.assertEqual(res.text, 'logged')
        self.assertEqual(self.env['axsync.shopify.log'].search_count([('store_id', '=', self.store.id), ('operation', '=', 'gdpr')]), 3)

    def test_access_token_mode_uses_the_webhook_secret(self):
        self.store.write({'auth_mode': 'token', 'access_token': self.mock.token, 'webhook_secret': 'app-secret'})
        self.assertEqual(self.post('orders/create', {'id': 1}, secret='csecret').status_code, 401)
        self.assertEqual(self.post('orders/create', {'id': 1, 'admin_graphql_api_id': 'gid://shopify/Order/1'}, secret='app-secret').status_code, 200)

    def test_webhook_to_imported_order(self):
        prod = self.env['product.product'].create({'name': 'Mug', 'default_code': 'MUG-01', 'list_price': 10.0})
        self.assertTrue(prod)
        pricelist = self.env['product.pricelist'].create({'name': 'USD shop', 'currency_id': self.env.company.currency_id.id})
        self.store.write({'pricelist_id': pricelist.id, 'warehouse_id': self.env['stock.warehouse'].search([], limit=1).id})
        self.store._set_defaults()
        self.mock.currency = self.env.company.currency_id.name
        node = self.mock.add_order('#4001', [{'sku': 'MUG-01', 'title': 'Mug', 'qty': 1, 'price': 10.0}], financial='PENDING', email='w@example.test')
        self.store.action_connect()
        self.post('orders/create', {'id': 1, 'admin_graphql_api_id': node['id']})
        self.env['axsync.shopify.queue'].process_store(self.store)
        order = self.env['sale.order'].search([('shopify_order_id', '=', node['id'])])
        self.assertEqual(order.shopify_order_name, '#4001')

    def test_product_image_route(self):
        tmpl = self.env['product.template'].create({'name': 'Pic', 'image_1920': PIXEL})
        token = helpers.sign_image(self.env, tmpl.id)
        ok = self.url_open('/axsync_shopify/img/%s/%s/123.png' % (tmpl.id, token))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.headers['Content-Type'], 'image/png')
        self.assertTrue(ok.content.startswith(b'\x89PNG'))
        self.assertEqual(self.url_open('/axsync_shopify/img/%s/%s/123.png' % (tmpl.id, 'f' * 32)).status_code, 403)
        other = self.env['product.template'].create({'name': 'No pic'})
        self.assertEqual(self.url_open('/axsync_shopify/img/%s/%s/1.png' % (other.id, helpers.sign_image(self.env, other.id))).status_code, 404)
