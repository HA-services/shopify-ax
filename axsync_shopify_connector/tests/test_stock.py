# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from unittest import mock

from odoo.tests.common import tagged

from ..models import stock as stock_mod
from .common import ShopifyCase


@tagged('post_install', '-at_install', 'axsync_shopify')
class TestStock(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.connect()
        self.svc = self.env['axsync.shopify.svc.stock']
        self.prod = self.make_product('MUG-01', 10.0, qty=10)
        self.export(self.prod)
        self.vmap = self.env['axsync.shopify.variant'].search([('product_id', '=', self.prod.id)])
        self.loc1, self.loc2 = self.store.location_ids[0], self.store.location_ids[1]

    def sent(self, loc=None):
        loc = loc or self.loc1
        return self.mock.inventory.get((self.vmap.inventory_item_id, loc.shopify_id))

    def test_export_sends_quantity_to_mapped_location_only(self):
        self.assertEqual(self.svc.export_stock(self.store), 1)
        self.assertEqual(self.sent(), 10)
        self.assertIsNone(self.sent(self.loc2), 'a location without a warehouse is not touched')

    def test_changed_only(self):
        self.svc.export_stock(self.store)
        self.assertEqual(self.svc.export_stock(self.store), 0, 'nothing changed, nothing sent')
        self.assertEqual(len(self.mock.ops('inventory_set')), 1)
        self.env['stock.quant']._update_available_quantity(self.prod, self.warehouse.lot_stock_id, -3)
        self.assertEqual(self.svc.export_stock(self.store), 1)
        self.assertEqual(self.sent(), 7)
        self.assertEqual(self.svc.export_stock(self.store, full=True), 1, 'full export ignores the change check')

    def test_free_quantity_excludes_reservations(self):
        quant = self.env['stock.quant'].search([('product_id', '=', self.prod.id), ('location_id', '=', self.warehouse.lot_stock_id.id)])
        quant.reserved_quantity = 4
        self.svc.export_stock(self.store)
        self.assertEqual(self.sent(), 6)
        self.store.stock_source = 'onhand'
        self.svc.export_stock(self.store)
        self.assertEqual(self.sent(), 10)

    def test_percentage_buffer_and_cap(self):
        self.store.write({'stock_percent': 50.0, 'stock_buffer': 1.0})
        self.svc.export_stock(self.store)
        self.assertEqual(self.sent(), 4)
        self.store.write({'stock_cap': 3.0, 'stock_percent': 100.0, 'stock_buffer': 0.0})
        self.svc.export_stock(self.store)
        self.assertEqual(self.sent(), 3)
        self.store.write({'stock_cap': 0.0, 'stock_buffer': 50.0})
        self.svc.export_stock(self.store)
        self.assertEqual(self.sent(), 0, 'never negative')

    def test_services_and_excluded_products_are_skipped(self):
        svc_prod = self.make_product('CONSU-1', 5.0, storable=False)
        self.export(svc_prod)
        self.svc.export_stock(self.store)
        item = self.env['axsync.shopify.variant'].search([('product_id', '=', svc_prod.id)]).inventory_item_id
        self.assertFalse([k for k in self.mock.inventory if k[0] == item])
        self.prod.product_tmpl_id.shopify_exclude = True
        self.env['axsync.shopify.stock.state'].search([]).unlink()
        self.mock.inventory.clear()
        self.svc.export_stock(self.store)
        self.assertFalse(self.mock.inventory)

    def test_item_not_stocked_is_activated_and_retried(self):
        self.mock.inventory_errors_once = True
        self.svc.export_stock(self.store)
        self.assertTrue(self.mock.ops('inventory_activate'))
        self.assertEqual(self.sent(), 10)

    def test_batches(self):
        for i in range(3):
            self.export(self.make_product('B-%s' % i, 5.0, qty=2))
        with mock.patch.object(stock_mod, 'BATCH', 2):
            sent = self.svc.export_stock(self.store, full=True)
        self.assertEqual(sent, 4)
        self.assertEqual(len(self.mock.ops('inventory_set')), 2)

    def test_import_stock_adjusts_odoo(self):
        self.mock.inventory[(self.vmap.inventory_item_id, self.loc1.shopify_id)] = 25
        self.assertEqual(self.svc.import_stock(self.store), 1)
        self.assertEqual(self.prod.with_context(warehouse=self.warehouse.id).qty_available, 25)

    def test_stock_change_in_shopify_is_overwritten_on_next_export(self):
        self.svc.export_stock(self.store)
        self.mock.inventory[(self.vmap.inventory_item_id, self.loc1.shopify_id)] = 99
        item_id = self.vmap.inventory_item_id.rsplit('/', 1)[-1]
        job = self.env['axsync.shopify.queue'].enqueue(self.store, 'inventory', 'INVENTORY_LEVELS_UPDATE', item_id, payload={'inventory_item_id': int(item_id)})
        self.process_queue()
        self.assertEqual(job.state, 'done')
        self.svc.export_stock(self.store)
        self.assertEqual(self.sent(), 10)

    def test_cron_exports_for_connected_stores_only(self):
        self.env['axsync.shopify.store']._cron_export_stock()
        self.assertEqual(self.sent(), 10)
        self.store.read_only = True
        self.env['axsync.shopify.stock.state'].search([]).unlink()
        self.mock.inventory.clear()
        self.env['axsync.shopify.store']._cron_export_stock()
        self.assertFalse(self.mock.inventory, 'safe mode stops the schedule too')
