# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo.tests.common import tagged

from ..models import client as client_mod
from ..models import compat
from .common import ShopifyCase


@tagged('post_install', '-at_install', 'axsync_shopify')
class TestOrders(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.connect()
        self.svc = self.env['axsync.shopify.svc.order']
        self.mug = self.make_product('MUG-01', 10.0, qty=50)
        self.bag = self.make_product('BAG-01', 30.0, qty=50)
        self.env['axsync.shopify.gateway'].create({'store_id': self.store.id, 'name': 'shopify_payments', 'journal_id': self.journal.id})
        self.customer = self.mock.add_customer('ana@example.test', 'Ana', 'Buyer', tags=['vip'], phone='+15550001')
        self.lines = [
            {'sku': 'MUG-01', 'title': 'Mug', 'qty': 2, 'price': 10.0, 'discount': 4.0},
            {'sku': 'BAG-01', 'title': 'Bag', 'qty': 1, 'price': 30.0}]

    def order(self, domain_extra=None):
        return self.env['sale.order'].search([('shopify_store_id', '=', self.store.id)] + (domain_extra or []))

    # ------------------------------------------------------------ happy paths
    def test_paid_order_goes_all_the_way(self):
        node = self.mock.add_order('#1001', self.lines, customer=self.customer, shipping=5.0, note='Leave at the door', tags=['gift'])
        self.assertEqual(self.store.action_import_orders()['params']['type'], 'success')
        order = self.order()
        self.assertEqual(len(order), 1)
        self.assertEqual(order.shopify_order_id, node['id'])
        self.assertEqual(order.client_order_ref, '#1001')
        self.assertEqual(order.partner_id.email, 'ana@example.test')
        self.assertIn('vip', order.partner_id.category_id.mapped('name'))
        self.assertEqual(len(order.order_line), 3, 'two products and shipping')
        mug = order.order_line.filtered(lambda l: l.product_id == self.mug)
        self.assertEqual(mug.product_uom_qty, 2)
        self.assertAlmostEqual(mug.discount, 20.0)
        self.assertEqual(mug.shopify_line_id, node['lineItems']['nodes'][0]['id'])
        ship = order.order_line.filtered(lambda l: l.product_id == self.store.shipping_product_id)
        self.assertEqual(ship.price_unit, 5.0)
        self.assertAlmostEqual(order.amount_total, 51.0)
        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.shopify_financial_status, 'paid')
        self.assertIn('Leave at the door', ' '.join(order.message_ids.mapped('body')))
        invoice = order.invoice_ids
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, 'posted')
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertEqual(order.partner_shipping_id.type, 'delivery')
        self.assertEqual(order.partner_shipping_id.city, 'Seattle')
        self.assertEqual(order.partner_shipping_id.state_id.code, 'WA')

    def test_importing_twice_never_duplicates(self):
        self.mock.add_order('#1001', self.lines, customer=self.customer)
        self.store.action_import_orders()
        self.store.last_order_sync = False
        self.store.action_import_orders()
        self.assertEqual(len(self.order()), 1)
        self.assertEqual(len(self.order().invoice_ids), 1)

    def test_pending_order_is_confirmed_only(self):
        self.mock.add_order('#1002', self.lines, customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        order = self.order()
        self.assertEqual(order.state, 'sale')
        self.assertFalse(order.invoice_ids)

    def test_authorized_workflow_and_gateway_override(self):
        wf = self.env['axsync.shopify.workflow'].create({'store_id': self.store.id, 'name': 'Manual gateway: quotation only',
                                                          'financial_status': 'paid', 'auto_confirm': False})
        self.env['axsync.shopify.gateway'].create({'store_id': self.store.id, 'name': 'bank_transfer', 'workflow_id': wf.id})
        self.mock.add_order('#1003', self.lines, customer=self.customer, gateways=('bank_transfer',))
        self.store.action_import_orders()
        self.assertEqual(self.order().state, 'draft')

    def test_excluded_payment_status_is_not_imported(self):
        self.mock.add_order('#1004', self.lines, customer=self.customer, financial='REFUNDED')
        self.mock.add_order('#1005', self.lines, customer=self.customer, financial='VOIDED')
        self.store.action_import_orders()
        self.assertFalse(self.order())
        self.store.imp_refunded = True
        self.store.last_order_sync = False
        self.store.action_import_orders()
        self.assertEqual(len(self.order()), 1)

    def test_delivery_validated_by_workflow(self):
        wf = self.store.workflow_ids.filtered(lambda w: w.financial_status == 'paid')
        wf.auto_validate_delivery = True
        self.mock.add_order('#1006', self.lines, customer=self.customer)
        self.store.action_import_orders()
        order = self.order()
        self.assertTrue(order.picking_ids)
        self.assertEqual(set(order.picking_ids.mapped('state')), {'done'})

    # ------------------------------------------------------------ guests, companies, currencies
    def test_guest_checkout_reuses_partner_by_email(self):
        self.mock.add_order('#1010', self.lines, customer=None, email='guest@example.test', financial='PENDING')
        self.mock.add_order('#1011', self.lines, customer=None, email='guest@example.test', financial='PENDING')
        self.store.action_import_orders()
        orders = self.order()
        self.assertEqual(len(orders), 2)
        self.assertEqual(len(orders.mapped('partner_id')), 1)
        self.assertEqual(orders[0].partner_id.email, 'guest@example.test')

    def test_company_customer_gets_a_company_parent(self):
        cust = self.mock.add_customer('buyer@acme.test', 'Bo', 'Acme', company='Acme Corp')
        self.mock.add_order('#1012', self.lines, customer=cust, financial='PENDING')
        self.store.action_import_orders()
        partner = self.order().partner_id
        self.assertEqual(partner.parent_id.name, 'Acme Corp')
        self.assertFalse(partner.parent_id.parent_id)

    def test_existing_partner_matched_by_email(self):
        existing = self.env['res.partner'].create({'name': 'Ana Existing', 'email': 'ANA@example.test'})
        self.mock.add_order('#1013', self.lines, customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        self.assertEqual(self.order().partner_id, existing)

    def test_presentment_currency(self):
        eur = self.env.ref('base.EUR')
        eur.active = True
        self.env['product.pricelist'].create({'name': 'Shop EUR', 'currency_id': eur.id})
        self.store.use_presentment_currency = True
        node = self.mock.add_order('#1014', self.lines, customer=self.customer, financial='PENDING', presentment='EUR')
        for money in (node['currentTotalPriceSet'],):
            money['presentmentMoney']['currencyCode'] = 'EUR'
        for li in node['lineItems']['nodes']:
            for key in ('originalUnitPriceSet', 'totalDiscountSet', 'discountedUnitPriceSet'):
                li[key]['presentmentMoney']['currencyCode'] = 'EUR'
        for sl in node['shippingLines']['nodes']:
            for key in ('originalPriceSet', 'discountedPriceSet'):
                sl[key]['presentmentMoney']['currencyCode'] = 'EUR'
        self.store.action_import_orders()
        self.assertEqual(self.order().currency_id, eur)

    def test_missing_pricelist_for_currency_waits_in_queue(self):
        self.mock.add_order('#1015', self.lines, customer=self.customer, currency='JPY')
        self.env.ref('base.JPY').active = True
        self.store.action_import_orders()
        self.assertFalse(self.order())
        job = self.env['axsync.shopify.queue'].search([('store_id', '=', self.store.id), ('kind', '=', 'order')])
        self.assertEqual(len(job), 1)

    # ------------------------------------------------------------ taxes
    def _vat(self, included=False):
        return self.env['account.tax'].create({'name': 'VAT 20 test %s' % included, 'amount': 20.0, 'amount_type': 'percent',
                                               'type_tax_use': 'sale', 'price_include': included, 'company_id': self.company.id})

    def test_tax_excluded_order_total_matches(self):
        tax = self._vat()
        self.env['axsync.shopify.tax.map'].create({'store_id': self.store.id, 'title': 'VAT', 'rate': 20.0, 'tax_id': tax.id})
        node = self.mock.add_order('#1020', self.lines, customer=self.customer, tax_rate=0.2, financial='PENDING')
        self.store.action_import_orders()
        order = self.order()
        total = float(node['currentTotalPriceSet']['shopMoney']['amount'])
        self.assertAlmostEqual(order.amount_total, total, places=2)
        tax_field = compat.line_tax_field(self.env)
        self.assertEqual(order.order_line.filtered(lambda l: l.product_id == self.bag)[tax_field], tax)

    def test_tax_included_prices_are_converted(self):
        tax = self._vat(included=False)
        self.env['axsync.shopify.tax.map'].create({'store_id': self.store.id, 'title': 'VAT', 'rate': 20.0, 'tax_id': tax.id})
        node = self.mock.add_order('#1021', self.lines, customer=self.customer, tax_rate=0.2, taxes_included=True, financial='PENDING')
        self.store.action_import_orders()
        total = float(node['currentTotalPriceSet']['shopMoney']['amount'])
        self.assertAlmostEqual(self.order().amount_total, total, delta=0.1)

    def test_tax_is_matched_automatically_by_rate(self):
        tax = self._vat()
        node = self.mock.add_order('#1022', self.lines, customer=self.customer, tax_rate=0.2, financial='PENDING')
        self.store.action_import_orders()
        tax_field = compat.line_tax_field(self.env)
        self.assertTrue(self.order().order_line.filtered(lambda l: l.product_id == self.bag)[tax_field])
        self.assertAlmostEqual(self.order().amount_total, float(node['currentTotalPriceSet']['shopMoney']['amount']), places=2)
        self.assertTrue(tax)

    # ------------------------------------------------------------ unknown products
    def test_unknown_sku_waits_then_imports_once_fixed(self):
        self.mock.add_order('#1030', self.lines + [{'sku': 'NEW-01', 'title': 'New thing', 'qty': 1, 'price': 7.0}],
                            customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        self.assertFalse(self.order())
        job = self.env['axsync.shopify.queue'].search([('store_id', '=', self.store.id), ('kind', '=', 'order')])
        self.assertEqual(len(job), 1)
        self.assertTrue(self.store.activity_ids.filtered(lambda a: 'without Odoo product' in (a.summary or '')))
        self.make_product('NEW-01', 7.0, qty=5)
        job.next_try = '2000-01-01 00:00:00'
        self.process_queue()
        self.assertEqual(job.state, 'done')
        self.assertEqual(len(self.order()), 1)

    def test_unknown_sku_can_create_the_product(self):
        self.store.auto_create_products = True
        self.mock.add_order('#1031', [{'sku': 'NEW-02', 'title': 'Brand new', 'qty': 3, 'price': 4.0}], customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        prod = self.env['product.product'].search([('default_code', '=', 'NEW-02')])
        self.assertTrue(prod)
        self.assertEqual(self.order().order_line.filtered(lambda l: l.product_id == prod).price_unit, 4.0)

    def test_variant_map_wins_over_sku(self):
        node = self.mock.add_product('Mug', [{'sku': 'RENAMED', 'price': 10.0}])
        self.env['axsync.shopify.product'].create({'name': 'Mug', 'store_id': self.store.id, 'template_id': self.mug.product_tmpl_id.id, 'shopify_id': node['id']})
        self.env['axsync.shopify.variant'].create({'product_map_id': self.env['axsync.shopify.product'].search([('shopify_id', '=', node['id'])]).id,
                                                   'product_id': self.mug.id, 'shopify_id': node['variants']['nodes'][0]['id']})
        self.mock.add_order('#1032', [{'sku': 'RENAMED', 'title': 'Mug', 'qty': 1, 'price': 10.0, 'variant': node['variants']['nodes'][0]['id']}],
                            customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        self.assertEqual(self.order().order_line.filtered(lambda l: l.product_id == self.mug).product_uom_qty, 1)

    # ------------------------------------------------------------ cancel
    def test_cancelled_in_shopify_cancels_in_odoo(self):
        node = self.mock.add_order('#1040', self.lines, customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        node['cancelledAt'] = '2026-11-02T10:00:00Z'
        self.svc.sync_order(self.store, node['id'])
        order = self.order()
        self.assertEqual(order.state, 'cancel')
        self.assertTrue(order.shopify_cancelled)
        self.assertFalse(self.mock.cancelled, 'no echo back to Shopify')

    def test_cancelled_in_odoo_cancels_in_shopify(self):
        node = self.mock.add_order('#1041', self.lines, customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        self.order().with_context(disable_cancel_warning=True).action_cancel()
        self.assertEqual(self.mock.cancelled, [node['id']])
        self.assertTrue(self.order().shopify_cancelled)

    def test_cancel_in_odoo_respects_safe_mode_and_switch(self):
        node = self.mock.add_order('#1042', self.lines, customer=self.customer, financial='PENDING')
        self.mock.add_order('#1043', self.lines, customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        first, second = self.order().sorted('id')
        self.store.read_only = True
        first.with_context(disable_cancel_warning=True).action_cancel()
        self.assertEqual(first.state, 'cancel')
        self.store.write({'read_only': False, 'export_cancellations': False})
        second.with_context(disable_cancel_warning=True).action_cancel()
        self.assertFalse(self.mock.cancelled)
        self.assertTrue(node)

    def test_order_not_from_shopify_never_calls_shopify(self):
        order = self.env['sale.order'].create({'partner_id': self.env['res.partner'].create({'name': 'Plain'}).id,
                                               'order_line': [(0, 0, {'product_id': self.mug.id, 'product_uom_qty': 1})]})
        order.action_confirm()
        order.with_context(disable_cancel_warning=True).action_cancel()
        self.assertFalse(self.mock.cancelled)

    # ------------------------------------------------------------ refunds
    def test_refund_creates_credit_note_once(self):
        node = self.mock.add_order('#1050', self.lines, customer=self.customer)
        self.store.action_import_orders()
        refund = self.mock.add_refund(node, 1, 1, 30.0)
        self.svc.sync_order(self.store, node['id'])
        credit = self.env['account.move'].search([('shopify_refund_id', '=', refund['id'])])
        self.assertEqual(len(credit), 1)
        self.assertEqual(credit.move_type, 'out_refund')
        self.assertEqual(len(credit.invoice_line_ids), 1)
        self.assertEqual(credit.invoice_line_ids.product_id, self.bag)
        self.assertEqual(credit.invoice_line_ids.quantity, 1)
        self.assertEqual(credit.state, 'posted')
        self.svc.sync_order(self.store, node['id'])
        self.assertEqual(self.env['account.move'].search_count([('shopify_refund_id', '=', refund['id'])]), 1)

    def test_refund_without_invoice_is_only_noted(self):
        node = self.mock.add_order('#1051', self.lines, customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        self.mock.add_refund(node, 0, 1, 10.0)
        self.svc.sync_order(self.store, node['id'])
        self.svc.sync_order(self.store, node['id'])
        notes = [m for m in self.order().message_ids if 'refunded' in (m.body or '')]
        self.assertEqual(len(notes), 1, 'announced once only')

    def test_import_one_order_by_name(self):
        self.mock.add_order('#1060', self.lines, customer=self.customer, financial='PENDING')
        self.mock.add_order('#1061', self.lines, customer=self.customer, financial='PENDING')
        order = self.svc.import_by_name(self.store, '1061')
        self.assertEqual(order.shopify_order_name, '#1061')
        with self.assertRaises(client_mod.ShopifyError):
            self.svc.import_by_name(self.store, '9999')

    def test_wizard_imports_one_order(self):
        self.mock.add_order('#1062', self.lines, customer=self.customer, financial='PENDING')
        wiz = self.env['axsync.shopify.import.order'].create({'store_id': self.store.id, 'order_name': '#1062'})
        action = wiz.action_import()
        self.assertEqual(action['res_model'], 'sale.order')
        self.assertEqual(self.env['sale.order'].browse(action['res_id']).shopify_order_name, '#1062')

    def test_orders_older_than_cutoff_are_skipped_by_query(self):
        self.mock.add_order('#1063', self.lines, customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        self.assertTrue(self.store.last_order_sync)
        queries = [c[1].get('query') for c in self.mock.ops('orders')]
        self.assertTrue(queries[0].startswith('created_at:>='))
        self.store.action_import_orders()
        self.assertTrue([c[1].get('query') for c in self.mock.ops('orders')][-1].startswith('updated_at:>='))

    def test_confirmed_order_keeps_shopify_date(self):
        node = self.mock.add_order('#1099', self.lines, customer=self.customer, financial='PAID')
        node['processedAt'] = node['createdAt'] = '2026-10-05T10:00:00Z'
        self.store.action_import_orders()
        order = self.order()
        self.assertEqual(order.state, 'sale')
        self.assertEqual(str(order.date_order.date()), '2026-10-05')
