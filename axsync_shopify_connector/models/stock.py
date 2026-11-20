# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import math

from odoo import _, api, fields, models

from . import compat, gql
from .client import ShopifyError

BATCH = 100


class ShopifyStockState(models.Model):
    _name = 'axsync.shopify.stock.state'
    _description = 'Last stock quantity sent to Shopify'

    variant_id = fields.Many2one('axsync.shopify.variant', required=True, ondelete='cascade', index=True)
    location_id = fields.Many2one('axsync.shopify.location', required=True, ondelete='cascade', index=True)
    quantity = fields.Float()
    sent_on = fields.Datetime(default=fields.Datetime.now)

    _sql_constraints = [('st_uniq', 'unique(variant_id, location_id)', 'One row per variant and location.')]


class ShopifyStockService(models.AbstractModel):
    _name = 'axsync.shopify.svc.stock'
    _description = 'Shopify stock synchronisation'

    def _raw_qty(self, store, product, warehouse):
        ctx = {'warehouse': warehouse.id, 'warehouse_id': warehouse.id}
        prod = product.with_context(**ctx)
        field = {'free': 'free_qty', 'onhand': 'qty_available', 'forecast': 'virtual_available'}[store.stock_source]
        return prod[field]

    def _export_qty(self, store, product, warehouse):
        qty = self._raw_qty(store, product, warehouse) * (store.stock_percent / 100.0) - (store.stock_buffer or 0.0)
        if store.stock_cap and qty > store.stock_cap:
            qty = store.stock_cap
        return max(0, int(math.floor(qty + 1e-9)))

    def export_stock(self, store, full=False):
        store._ensure_writable('stock export')
        State = self.env['axsync.shopify.stock.state']
        # quantities are computed, make sure none is left over from an earlier read in this transaction
        self.env['product.product'].invalidate_model(['free_qty', 'qty_available', 'virtual_available'])
        locations = store.location_ids.filtered(lambda l: l.export_stock and l.warehouse_id and l.is_active and not l.is_fulfillment_service)
        variants = self.env['axsync.shopify.variant'].search([
            ('store_id', '=', store.id), ('inventory_item_id', '!=', False),
            ('product_map_id.state', '!=', 'deleted'), ('product_id.product_tmpl_id.shopify_exclude', '=', False)])
        variants = variants.filtered(lambda v: compat.is_storable(v.product_id))
        pending = []
        for loc in locations:
            states = {s.variant_id.id: s for s in State.search([('location_id', '=', loc.id), ('variant_id', 'in', variants.ids)])}
            for var in variants:
                qty = self._export_qty(store, var.product_id, loc.warehouse_id)
                st = states.get(var.id)
                if st and st.quantity == qty and store.stock_changed_only and not full:
                    continue
                pending.append((var, loc, qty, st))
        sent = 0
        for i in range(0, len(pending), BATCH):
            chunk = pending[i:i + BATCH]
            self._send_batch(store, chunk)
            sent += len(chunk)
        store.last_stock_export = fields.Datetime.now()
        if sent:
            store._log('stock export', _('%s quantities sent') % sent)
        return sent

    def _send_batch(self, store, chunk):
        quantities = [{'inventoryItemId': v.inventory_item_id, 'locationId': l.shopify_id, 'quantity': q} for v, l, q, _s in chunk]
        variables = {'input': {'name': 'available', 'reason': 'correction', 'ignoreCompareQuantity': True, 'quantities': quantities}}
        data = store._query(gql.INVENTORY_SET, variables)
        errors = (data.get('inventorySetQuantities') or {}).get('userErrors') or []
        if errors:
            # items that were never stocked at the location need to be activated first
            for v, l, _q, _s in chunk:
                try:
                    store._query(gql.INVENTORY_ACTIVATE, {'inventoryItemId': v.inventory_item_id, 'locationId': l.shopify_id})
                except ShopifyError:
                    pass
            data = store._query(gql.INVENTORY_SET, variables)
            errors = (data.get('inventorySetQuantities') or {}).get('userErrors') or []
            if errors:
                raise ShopifyError('; '.join(e.get('message', '') for e in errors))
        State = self.env['axsync.shopify.stock.state']
        for v, l, q, st in chunk:
            if st:
                st.write({'quantity': q, 'sent_on': fields.Datetime.now()})
            else:
                State.create({'variant_id': v.id, 'location_id': l.id, 'quantity': q})

    def import_stock(self, store):
        """Pull the quantities from Shopify into the mapped warehouses as inventory adjustments."""
        Quant = self.env['stock.quant']
        locs = {l.shopify_id: l for l in store.location_ids if l.warehouse_id}
        count = 0
        variants = self.env['axsync.shopify.variant'].search([('store_id', '=', store.id), ('inventory_item_id', '!=', False)])
        for var in variants.filtered(lambda v: compat.is_storable(v.product_id)):
            data = store._query(gql.INVENTORY_LEVELS, {'id': var.inventory_item_id})
            levels = ((data.get('inventoryItem') or {}).get('inventoryLevels') or {}).get('nodes') or []
            for level in levels:
                loc = locs.get((level.get('location') or {}).get('id'))
                if not loc:
                    continue
                qty = 0
                for q in level.get('quantities') or []:
                    if q.get('name') == 'available':
                        qty = q.get('quantity') or 0
                wh_loc = loc.warehouse_id.lot_stock_id
                quant = Quant.with_context(inventory_mode=True).create({
                    'product_id': var.product_id.id, 'location_id': wh_loc.id, 'inventory_quantity': qty})
                quant.action_apply_inventory()
                count += 1
        store._log('stock import', _('%s quantities imported') % count)
        return count

    def handle_job(self, store, job, payload):
        """A stock level changed in Shopify: forget the quantity we sent so the next export puts it back."""
        item = payload.get('inventory_item_id')
        if not item:
            return
        gid = 'gid://shopify/InventoryItem/%s' % item
        states = self.env['axsync.shopify.stock.state'].search([('variant_id.store_id', '=', store.id), ('variant_id.inventory_item_id', '=', gid)])
        states.unlink()
