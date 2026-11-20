# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo.tests.common import tagged

from ..models import client as client_mod
from ..models import compat
from .common import ShopifyCase, set_done


@tagged('post_install', '-at_install', 'axsync_shopify')
class TestFulfillment(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.connect()
        self.mug = self.make_product('MUG-01', 10.0, qty=50)
        self.bag = self.make_product('BAG-01', 30.0, qty=50)
        self.customer = self.mock.add_customer('ana@example.test', 'Ana', 'Buyer')
        self.node = self.mock.add_order('#2001', [
            {'sku': 'MUG-01', 'title': 'Mug', 'qty': 2, 'price': 10.0},
            {'sku': 'BAG-01', 'title': 'Bag', 'qty': 1, 'price': 30.0}], customer=self.customer, financial='PENDING')
        self.store.action_import_orders()
        self.order = self.env['sale.order'].search([('shopify_store_id', '=', self.store.id)])
        self.picking = self.order.picking_ids
        self.svc = self.env['axsync.shopify.svc.fulfillment']

    def fulfilled(self, line_title):
        for fo in self.node['fulfillmentOrders']['nodes']:
            for li in fo['lineItems']['nodes']:
                if li['lineItem']['id'] == [x for x in self.node['lineItems']['nodes'] if x['title'] == line_title][0]['id']:
                    return li['totalQuantity'] - li['remainingQuantity']

    def test_full_delivery_sends_tracking(self):
        self.assertEqual(len(self.picking), 1)
        self.picking.write({'shopify_tracking_number': 'TRK123', 'shopify_tracking_company': 'DHL', 'shopify_tracking_url': 'https://track.example.test/TRK123'})
        compat.validate_picking(self.picking)
        self.assertEqual(self.picking.state, 'done')
        self.assertEqual(self.store.action_export_tracking()['params']['type'], 'success')
        ful = self.mock.fulfillments[0]
        self.assertEqual(ful['trackingInfo'], {'number': 'TRK123', 'company': 'DHL', 'url': 'https://track.example.test/TRK123'})
        self.assertTrue(ful['notifyCustomer'])
        self.assertEqual(self.fulfilled('Mug'), 2)
        self.assertEqual(self.fulfilled('Bag'), 1)
        self.assertTrue(self.picking.shopify_sent)
        self.assertEqual(self.node['displayFulfillmentStatus'], 'FULFILLED')
        self.assertEqual(self.svc.export_tracking(self.store), 0, 'sent only once')
        self.assertEqual(len(self.mock.fulfillments), 1)

    def test_partial_shipment_and_backorder(self):
        set_done(self.picking, {self.mug: 1})
        self.picking.with_context(skip_backorder=True, skip_immediate=True, skip_sms=True).button_validate()
        self.assertEqual(self.picking.state, 'done')
        backorder = self.order.picking_ids - self.picking
        self.assertEqual(len(backorder), 1)
        self.svc.export_tracking(self.store)
        self.assertEqual(self.fulfilled('Mug'), 1)
        self.assertEqual(self.fulfilled('Bag'), 0)
        self.assertEqual(self.mock.fulfillments[0]['lineItemsByFulfillmentOrder'][0]['fulfillmentOrderLineItems'][0]['quantity'], 1)
        backorder.write({'shopify_tracking_number': 'TRK-B'})
        compat.validate_picking(backorder)
        self.svc.export_tracking(self.store)
        self.assertEqual(self.fulfilled('Mug'), 2)
        self.assertEqual(self.fulfilled('Bag'), 1)
        self.assertEqual(len(self.mock.fulfillments), 2)
        self.assertEqual(self.mock.fulfillments[1]['trackingInfo']['number'], 'TRK-B')

    def test_no_tracking_number_still_fulfils(self):
        compat.validate_picking(self.picking)
        self.svc.export_tracking(self.store)
        self.assertNotIn('trackingInfo', self.mock.fulfillments[0])

    def test_already_fulfilled_in_shopify_is_not_sent_again(self):
        for fo in self.node['fulfillmentOrders']['nodes']:
            fo['status'] = 'CLOSED'
        compat.validate_picking(self.picking)
        self.svc.export_tracking(self.store)
        self.assertFalse(self.mock.fulfillments)
        self.assertTrue(self.picking.shopify_sent)

    def test_unfinished_deliveries_and_safe_mode(self):
        self.assertEqual(self.svc.export_tracking(self.store), 0, 'a delivery that is not done is ignored')
        compat.validate_picking(self.picking)
        self.store.read_only = True
        with self.assertRaises(client_mod.ShopifyError):
            self.picking.action_send_tracking_to_shopify()
        self.env['axsync.shopify.store']._cron_export_tracking()
        self.assertFalse(self.mock.fulfillments)
        self.store.read_only = False
        self.picking.action_send_tracking_to_shopify()
        self.assertEqual(len(self.mock.fulfillments), 1)

    def test_failure_is_logged_and_retried_later(self):
        compat.validate_picking(self.picking)
        self.mock.fail_next = [(500, 'boom')]
        self.assertEqual(self.svc.export_tracking(self.store), 0)
        self.assertFalse(self.picking.shopify_sent)
        self.assertTrue(self.env['axsync.shopify.log'].search([('store_id', '=', self.store.id), ('operation', '=', 'tracking export')]))
        self.assertEqual(self.svc.export_tracking(self.store), 1)
        self.assertTrue(self.picking.shopify_sent)
