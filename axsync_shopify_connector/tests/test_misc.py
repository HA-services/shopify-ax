# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo.tests.common import tagged

from .common import ShopifyCase


@tagged('post_install', '-at_install', 'axsync_shopify')
class TestMisc(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.connect()

    def test_import_customers(self):
        self.mock.add_customer('ana@example.test', 'Ana', 'Buyer', tags=['vip', 'wholesale'], phone='+15550001')
        self.mock.add_customer('bo@acme.test', 'Bo', 'Acme', company='Acme Corp')
        self.env['res.partner'].create({'name': 'Old Bo', 'email': 'bo@acme.test'})
        self.assertEqual(self.store.action_import_customers()['params']['type'], 'success')
        maps = self.env['axsync.shopify.customer'].search([('store_id', '=', self.store.id)])
        self.assertEqual(len(maps), 2)
        ana = maps.filtered(lambda m: m.email == 'ana@example.test').partner_id
        self.assertEqual(sorted(ana.category_id.mapped('name')), ['vip', 'wholesale'])
        self.assertEqual(ana.city, 'Portland')
        self.assertEqual(ana.state_id.code, 'OR')
        bo = maps.filtered(lambda m: m.email == 'bo@acme.test').partner_id
        self.assertEqual(bo.name, 'Old Bo', 'matched by email, not duplicated')
        self.store.action_import_customers()
        self.assertEqual(self.env['axsync.shopify.customer'].search_count([('store_id', '=', self.store.id)]), 2)

    def test_export_customers_creates_then_updates(self):
        partner = self.env['res.partner'].create({'name': 'Cleo Patra', 'email': 'cleo@example.test', 'customer_rank': 1,
                                                   'street': '1 Nile Street', 'city': 'Cairo', 'zip': '11511',
                                                   'country_id': self.env.ref('base.us').id})
        self.assertEqual(self.store.action_export_customers()['params']['type'], 'success')
        create = self.mock.ops('customer_create')[0][1]['input']
        self.assertEqual((create['firstName'], create['lastName']), ('Cleo', 'Patra'))
        self.assertEqual(create['addresses'][0]['countryCode'], 'US')
        cmap = self.env['axsync.shopify.customer'].search([('partner_id', '=', partner.id)])
        self.assertTrue(cmap.shopify_id)
        partner.name = 'Cleo Patra-Smith'
        self.store.action_export_customers()
        update = self.mock.ops('customer_update')[0][1]['input']
        self.assertEqual(update['id'], cmap.shopify_id)
        self.assertEqual(len(self.mock.customers), 1)

    def test_customer_webhook_job_refreshes_one_customer(self):
        node = self.mock.add_customer('ana@example.test', 'Ana', 'Buyer', tags=['new'])
        self.env['axsync.shopify.queue'].enqueue(self.store, 'customer', 'CUSTOMERS_CREATE', node['id'])
        self.process_queue()
        self.assertTrue(self.env['axsync.shopify.customer'].search([('shopify_id', '=', node['id'])]))

    def test_abandoned_checkouts(self):
        self.mock.add_checkout('lena@example.test', 59.0, [('Mug', 2, 'MUG-01'), ('Bag', 1, 'BAG-01')])
        self.env['res.partner'].create({'name': 'Lena', 'email': 'lena@example.test'})
        self.assertEqual(self.store.action_import_checkouts()['params']['type'], 'success')
        chk = self.env['axsync.shopify.checkout'].search([('store_id', '=', self.store.id)])
        self.assertEqual((chk.email, chk.total), ('lena@example.test', 59.0))
        self.assertTrue(chk.partner_id)
        self.assertIn('2 x Mug', chk.items)
        self.store.action_import_checkouts()
        self.assertEqual(self.env['axsync.shopify.checkout'].search_count([('store_id', '=', self.store.id)]), 1)
        chk.action_create_activity()
        self.assertTrue(chk.followed_up)
        self.assertTrue(self.store.activity_ids.filtered(lambda a: 'abandoned checkout' in (a.summary or '').lower()))

    def test_payouts(self):
        self.mock.add_payout(net=96.5, gross=100.0, fee=3.5)
        self.mock.add_payout(net=48.0, gross=50.0, fee=2.0)
        self.assertEqual(self.store.action_import_payouts()['params']['type'], 'success')
        payouts = self.env['axsync.shopify.payout'].search([('store_id', '=', self.store.id)])
        self.assertEqual(len(payouts), 2)
        self.assertEqual(sorted(payouts.mapped('net')), [48.0, 96.5])
        self.assertTrue(payouts[0].statement_ref.startswith('SHOPIFY-'))
        self.store.action_import_payouts()
        self.assertEqual(self.env['axsync.shopify.payout'].search_count([('store_id', '=', self.store.id)]), 2)

    def test_dashboard_numbers(self):
        self.make_product('MUG-01', 10.0, qty=5)
        self.mock.add_order('#5001', [{'sku': 'MUG-01', 'title': 'Mug', 'qty': 2, 'price': 10.0}], financial='PENDING', email='d@example.test')
        self.store.action_import_orders()
        self.env['axsync.shopify.queue'].enqueue(self.store, 'order', 'ORDERS_UPDATED', 'gid://shopify/Order/1')
        self.store.invalidate_recordset()
        self.assertEqual(self.store.order_count, 1)
        self.assertEqual(self.store.queue_pending, 1)
        self.assertAlmostEqual(self.store.sales_total, 25.0)
        self.assertTrue(self.store.token_status)
        self.assertEqual(self.store.action_view_orders()['res_model'], 'sale.order')
        for method in ('action_view_queue', 'action_view_logs', 'action_view_products', 'action_view_customers'):
            self.assertTrue(getattr(self.store, method)()['domain'])

    def test_product_template_server_action(self):
        prod = self.make_product('MUG-01', 10.0)
        prod.product_tmpl_id.action_export_to_shopify()
        self.assertEqual(len(self.mock.ops('product_set')), 1)
