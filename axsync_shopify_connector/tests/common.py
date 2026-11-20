# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo.tests.common import TransactionCase

from ..models import client as client_mod
from ..models import compat
from .mock_shopify import MockShopify


def groups_field(env):
    return 'group_ids' if 'group_ids' in env['res.users']._fields else 'groups_id'


class ShopifyCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old = (client_mod.TRANSPORT, client_mod.SLEEP)
        client_mod.SLEEP = lambda s: None
        cls.addClassCleanup(cls._restore)
        env = cls.env
        cls.company = env.company
        cls.currency = cls.company.currency_id
        cls.warehouse = env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.pricelist = env['product.pricelist'].create({'name': 'Shop USD', 'currency_id': cls.currency.id})
        cls.store = env['axsync.shopify.store'].create({
            'name': 'Demo Shop', 'shop_domain': 'https://Demo-Shop.myshopify.com/', 'client_id': 'cid', 'client_secret': 'csecret',
            'pricelist_id': cls.pricelist.id, 'warehouse_id': cls.warehouse.id, 'company_id': cls.company.id})
        cls.journal = env['account.journal'].search([('type', '=', 'bank'), ('company_id', '=', cls.company.id)], limit=1)

    def _pct_vals(self, pct):
        item = self.env['product.pricelist.item']
        if 'percent_price' in item._fields:
            return {'compute_price': 'percentage', 'percent_price': pct}
        return {'compute_price': 'discount', 'price_discount': pct}

    def setUp(self):
        super().setUp()
        # a fresh Shopify for every test, so nothing leaks between them
        self.mock = MockShopify(domain=self.store.shop_domain, currency=self.currency.name)
        client_mod.TRANSPORT = self.mock
        self.store.write({'access_token': False, 'token_expires': False})

    @classmethod
    def _restore(cls):
        client_mod.TRANSPORT, client_mod.SLEEP = cls._old

    # ------------------------------------------------------------ helpers
    def connect(self):
        self.store.action_connect()
        self.store.location_ids[:1].warehouse_id = self.warehouse
        return self.store

    def make_product(self, sku, price=10.0, qty=0, name=None, storable=True, **extra):
        vals = {'name': name or 'Item %s' % sku, 'default_code': sku, 'list_price': price}
        vals.update(compat.product_type_vals(self.env, storable))
        vals.update(extra)
        prod = self.env['product.product'].create(vals)
        if qty:
            self.env['stock.quant']._update_available_quantity(prod, self.warehouse.lot_stock_id, qty)
        return prod

    def process_queue(self):
        return self.env['axsync.shopify.queue'].process_store(self.store, limit=500)

    def export(self, *products):
        templates = self.env['product.template']
        for prod in products:
            templates |= prod.product_tmpl_id if prod._name == 'product.product' else prod
        return self.env['axsync.shopify.svc.product'].export_templates(self.store, templates)


def set_done(picking, quantities):
    """Mark exactly these quantities as done on a picking, whatever the Odoo version. quantities: {product: qty}."""
    for move in picking.move_ids:
        qty = quantities.get(move.product_id, 0)
        if 'quantity_done' in move._fields:
            move.quantity_done = qty
        else:
            move.quantity = qty
            if 'picked' in move._fields:
                move.picked = qty > 0
