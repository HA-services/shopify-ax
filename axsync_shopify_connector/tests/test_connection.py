# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import tagged

from ..models import client as client_mod
from ..models import gql
from ..models.helpers import set_param
from .common import ShopifyCase, groups_field


@tagged('post_install', '-at_install', 'axsync_shopify')
class TestConnection(ShopifyCase):

    def test_domain_is_cleaned(self):
        self.assertEqual(self.store.shop_domain, 'demo-shop.myshopify.com')
        self.assertEqual(self.store._clean_domain('my-shop'), 'my-shop.myshopify.com')

    def test_connect_reads_shop_scopes_locations(self):
        self.connect()
        self.assertEqual(self.store.state, 'connected')
        self.assertEqual(self.store.shop_name, 'Demo Shop')
        self.assertEqual(self.store.shop_currency_id, self.currency)
        self.assertEqual(len(self.store.location_ids), 2)
        self.assertIn('read_orders', self.store.granted_scopes)
        self.assertFalse(self.store.missing_scopes)
        self.assertTrue(self.store.workflow_ids, 'default workflows are created')
        self.assertTrue(self.store.shipping_product_id)

    def test_missing_scope_is_reported(self):
        self.mock.scopes.remove('write_inventory')
        self.store.write({'access_token': False, 'token_expires': False})
        self.connect()
        self.assertIn('write_inventory', self.store.missing_scopes)
        self.mock.scopes.append('write_inventory')

    def test_token_is_cached_and_renewed(self):
        self.store.write({'access_token': False, 'token_expires': False})
        before = self.mock.token_calls
        self.store.action_connect()
        self.store._query(gql.SHOP)
        self.assertEqual(self.mock.token_calls, before + 1, 'one token request serves many calls')
        self.assertTrue(self.store.token_expires)
        # the token is revoked on the Shopify side: one 401 triggers one silent renewal
        self.mock.rotate_token()
        data = self.store._query(gql.SHOP)
        self.assertEqual(data['shop']['name'], 'Demo Shop')
        self.assertEqual(self.mock.token_calls, before + 2)

    def test_expired_token_is_renewed_before_use(self):
        self.store.action_connect()
        self.store.token_expires = '2000-01-01 00:00:00'
        before = self.mock.token_calls
        self.store._query(gql.SHOP)
        self.assertEqual(self.mock.token_calls, before + 1)

    def test_wrong_secret_sets_error_state(self):
        bad = self.store.copy({'name': 'Bad', 'shop_domain': 'bad.myshopify.com', 'client_secret': 'nope'})
        res = bad.action_connect()
        self.assertEqual(res['params']['type'], 'danger')
        self.assertEqual(bad.state, 'error')
        self.assertIn('refused', bad.last_error)

    def test_token_mode(self):
        store = self.env['axsync.shopify.store'].create({
            'name': 'Token shop', 'shop_domain': 'token-shop', 'auth_mode': 'token', 'access_token': self.mock.token})
        before = self.mock.token_calls
        store.action_connect()
        self.assertEqual(store.state, 'connected')
        self.assertEqual(self.mock.token_calls, before, 'a pasted token never calls the token endpoint')

    def test_throttling_and_busy_responses_are_retried(self):
        self.store.action_connect()
        self.mock.throttle_next = 2
        self.assertEqual(self.store._query(gql.SHOP)['shop']['name'], 'Demo Shop')
        self.mock.fail_next = [(429, 'slow down'), (503, 'busy')]
        self.assertEqual(self.store._query(gql.SHOP)['shop']['name'], 'Demo Shop')

    def test_permanent_busy_gives_up(self):
        self.store.action_connect()
        self.mock.fail_next = [(503, 'busy')] * 10
        with self.assertRaises(client_mod.ShopifyError):
            self.store._query(gql.SHOP)
        self.mock.fail_next = []

    def test_safe_mode_blocks_writes_not_reads(self):
        self.connect()
        self.store.read_only = True
        for method in ('action_export_products', 'action_export_stock', 'action_register_webhooks', 'action_export_customers',
                       'action_export_tracking'):
            with self.assertRaises(client_mod.ShopifyError, msg=method):
                getattr(self.store, method)()
        self.store.action_import_products()
        self.store.action_import_orders()
        self.assertFalse(self.mock.ops('product_set'))

    def test_webhook_registration(self):
        self.connect()
        set_param(self.env, 'web.base.url', 'https://odoo.example.test')
        self.store.invalidate_recordset(['webhook_url'])
        self.assertEqual(self.store.webhook_url, 'https://odoo.example.test/axsync_shopify/webhook/%s' % self.store.id)
        self.store.action_register_webhooks()
        topics = {w['topic'] for w in self.mock.webhooks.values()}
        self.assertIn('ORDERS_CREATE', topics)
        self.assertIn('REFUNDS_CREATE', topics)
        self.assertNotIn('CHECKOUTS_CREATE', topics, 'checkout topics only when abandoned checkouts are used')
        count = len(self.mock.webhooks)
        self.store.action_register_webhooks()
        self.assertEqual(len(self.mock.webhooks), count, 'running it again creates nothing new')
        self.store.action_sync_webhooks()
        self.assertTrue(all(w.registered_remote for w in self.store.webhook_ids if w.shopify_id))
        self.store.action_remove_webhooks()
        self.assertFalse(self.mock.webhooks)

    def test_webhooks_need_https(self):
        self.connect()
        set_param(self.env, 'web.base.url', 'http://localhost:8069')
        self.store.invalidate_recordset(['webhook_url'])
        with self.assertRaises(UserError):
            self.store.action_register_webhooks()

    def test_graphql_documents_parse(self):
        try:
            from graphql import parse
        except ImportError:
            self.skipTest('graphql-core is not installed')
        names = [n for n in dir(gql) if n.isupper() and isinstance(getattr(gql, n), str) and 'query' in getattr(gql, n).lower() + 'mutation']
        parsed = 0
        for name in names:
            text = getattr(gql, name)
            if text.lstrip().startswith(('query', 'mutation')):
                parse(text)
                parsed += 1
        self.assertGreaterEqual(parsed, 20)

    def test_user_rights_and_secrets(self):
        env = self.env
        gf = groups_field(env)
        ref = env.ref
        plain = env['res.users'].create({'name': 'Plain', 'login': 'plain_shp', 'email': 'p@example.test', gf: [(6, 0, [ref('base.group_user').id])]})
        viewer = env['res.users'].create({'name': 'View', 'login': 'view_shp', 'email': 'v@example.test',
                                          gf: [(6, 0, [ref('base.group_user').id, ref('axsync_shopify_connector.group_axsync_shopify_user').id])]})
        manager = env['res.users'].create({'name': 'Mgr', 'login': 'mgr_shp', 'email': 'm@example.test',
                                           gf: [(6, 0, [ref('base.group_user').id, ref('axsync_shopify_connector.group_axsync_shopify_manager').id])]})
        store = self.store
        with self.assertRaises(AccessError):
            store.with_user(plain).read(['name'])
        self.assertEqual(store.with_user(viewer).read(['name'])[0]['name'], 'Demo Shop')
        with self.assertRaises(AccessError):
            store.with_user(viewer).write({'name': 'Hacked'})
        with self.assertRaises(AccessError):
            store.with_user(viewer).read(['client_secret'])
        self.assertEqual(store.with_user(manager).read(['client_secret'])[0]['client_secret'], 'csecret')
        store.with_user(manager).write({'name': 'Renamed'})
        self.assertEqual(store.name, 'Renamed')
