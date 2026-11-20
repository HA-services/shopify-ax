# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import base64

from odoo.tests.common import tagged

from ..models import client as client_mod
from .common import ShopifyCase

PIXEL = base64.b64encode(
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82').decode()


@tagged('post_install', '-at_install', 'axsync_shopify')
class TestProducts(ShopifyCase):

    def setUp(self):
        super().setUp()
        self.connect()
        self.svc = self.env['axsync.shopify.svc.product']

    # ---------------------------------------------------------------- import
    def test_import_matches_by_sku_then_barcode(self):
        a = self.make_product('MUG-01', 12.0)
        b = self.make_product('BAG-01', 30.0, barcode='4006381333931')
        self.mock.add_product('Mug', [{'sku': 'MUG-01', 'price': 12.5}])
        self.mock.add_product('Bag', [{'sku': 'DIFFERENT', 'barcode': '4006381333931', 'price': 31.0}])
        self.assertEqual(self.store.action_import_products()['params']['type'], 'success')
        maps = self.env['axsync.shopify.product'].search([('store_id', '=', self.store.id)])
        self.assertEqual(len(maps), 2)
        self.assertEqual(maps.filtered(lambda m: m.name == 'Mug').template_id, a.product_tmpl_id)
        self.assertEqual(maps.filtered(lambda m: m.name == 'Bag').template_id, b.product_tmpl_id)
        var = self.env['axsync.shopify.variant'].search([('product_id', '=', a.id)])
        self.assertEqual(var.price, 12.5)
        self.assertTrue(var.inventory_item_id)

    def test_import_unknown_product_creates_activity_not_product(self):
        self.mock.add_product('Ghost', [{'sku': 'GHOST-1', 'price': 9.0}])
        self.store.action_import_products()
        self.assertFalse(self.env['product.product'].search([('default_code', '=', 'GHOST-1')]))
        self.assertTrue(self.store.activity_ids.filtered(lambda a: 'without Odoo match' in (a.summary or '')))
        self.assertTrue(self.env['axsync.shopify.log'].search([('store_id', '=', self.store.id), ('level', '=', 'warning')]))

    def test_import_creates_products_when_allowed(self):
        self.store.auto_create_products = True
        self.mock.add_product('Tee', [
            {'sku': 'TEE-S', 'price': 20.0, 'options': ['Small', 'Red'], 'barcode': '1111111111116'},
            {'sku': 'TEE-L', 'price': 22.0, 'options': ['Large', 'Red']}],
            options=[{'name': 'Size', 'values': ['Small', 'Large']}, {'name': 'Color', 'values': ['Red']}], product_type='Apparel')
        self.mock.add_product('Poster', [{'sku': 'POS-1', 'price': 8.0}])
        self.store.action_import_products()
        small = self.env['product.product'].search([('default_code', '=', 'TEE-S')])
        large = self.env['product.product'].search([('default_code', '=', 'TEE-L')])
        self.assertTrue(small and large, [(p.default_code, p.product_template_attribute_value_ids.mapped('name'), p.name) for p in self.env['product.product'].search([('name', '=', 'Tee')])])
        self.assertEqual(small.product_tmpl_id, large.product_tmpl_id)
        self.assertEqual(small.barcode, '1111111111116')
        self.assertEqual(self.env['axsync.shopify.variant'].search_count([('store_id', '=', self.store.id)]), 3)
        self.assertEqual(small.product_tmpl_id.categ_id.name, 'Apparel')
        poster = self.env['product.product'].search([('default_code', '=', 'POS-1')])
        self.assertEqual(poster.list_price, 8.0)

    def test_import_updates_instead_of_duplicating(self):
        self.make_product('MUG-01')
        node = self.mock.add_product('Mug', [{'sku': 'MUG-01', 'price': 12.0}])
        self.store.action_import_products()
        node['status'] = 'DRAFT'
        node['variants']['nodes'][0]['price'] = '14.00'
        self.store.action_import_products()
        self.assertEqual(self.env['axsync.shopify.product'].search_count([('store_id', '=', self.store.id)]), 1)
        self.assertEqual(self.env['axsync.shopify.product'].search([('store_id', '=', self.store.id)]).state, 'draft')
        self.assertEqual(self.env['axsync.shopify.variant'].search([('store_id', '=', self.store.id)]).price, 14.0)

    def test_inbound_metafield_mapping(self):
        field = self.env['ir.model.fields']._get('product.template', 'description_sale')
        self.env['axsync.shopify.metafield.map'].create({'store_id': self.store.id, 'owner': 'product', 'namespace': 'custom',
                                                         'key': 'pitch', 'odoo_field': field.id, 'direction': 'in'})
        prod = self.make_product('MUG-01')
        self.mock.add_product('Mug', [{'sku': 'MUG-01', 'price': 12.0}],
                              metafields=[{'namespace': 'custom', 'key': 'pitch', 'value': 'Dishwasher safe', 'type': 'single_line_text_field'}])
        self.store.action_import_products()
        self.assertEqual(prod.product_tmpl_id.description_sale, 'Dishwasher safe')

    # ---------------------------------------------------------------- export
    def test_export_creates_draft_with_price_rules(self):
        self.store.write({'price_markup_percent': 10.0, 'price_rounding': 0.05, 'compare_at_mode': 'list_price', 'vendor_name': 'Demo Co'})
        self.env['product.pricelist.item'].create({'pricelist_id': self.pricelist.id, 'applied_on': '3_global',
                                                   **self._pct_vals(20.0)})
        prod = self.make_product('MUG-01', 50.0, description_sale='A sturdy mug', image_1920=PIXEL)
        prod.product_tmpl_id.write({'shopify_tags': 'kitchen, gift', 'shopify_seo_title': 'Best mug'})
        self.export(prod)
        call = self.mock.ops('product_set')[0][1]
        inp = call['input']
        self.assertEqual(inp['status'], 'DRAFT')
        self.assertEqual(inp['vendor'], 'Demo Co')
        self.assertEqual(inp['tags'], ['kitchen', 'gift'])
        self.assertEqual(inp['seo']['title'], 'Best mug')
        self.assertEqual(inp['descriptionHtml'], 'A sturdy mug')
        var = inp['variants'][0]
        self.assertEqual(var['price'], '44.00', '50 - 20% = 40, +10% = 44')
        self.assertEqual(var['compareAtPrice'], '50.00')
        self.assertEqual(var['sku'], 'MUG-01')
        self.assertTrue(inp['files'][0]['originalSource'].endswith('.png'))
        self.assertNotIn('identifier', call)
        pmap = self.env['axsync.shopify.product'].search([('store_id', '=', self.store.id)])
        self.assertEqual(pmap.state, 'draft')
        self.assertTrue(pmap.shopify_id)
        vmap = pmap.variant_ids
        self.assertEqual(vmap.product_id, prod)
        self.assertTrue(vmap.inventory_item_id)

    def test_export_multi_variant_and_second_run_updates(self):
        attr = self.env['product.attribute'].create({'name': 'Size', 'create_variant': 'always', 'value_ids': [(0, 0, {'name': 'S'}), (0, 0, {'name': 'M'})]})
        tmpl = self.env['product.template'].create({'name': 'Hoodie', 'list_price': 40.0, 'default_code': 'HOOD',
                                                    'attribute_line_ids': [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)]})]})
        for i, prod in enumerate(tmpl.product_variant_ids):
            prod.default_code = 'HOOD-%s' % i
        self.store.export_as_draft = False
        self.svc.export_templates(self.store, tmpl)
        inp = self.mock.ops('product_set')[0][1]['input']
        self.assertEqual(inp['status'], 'ACTIVE')
        self.assertEqual(inp['productOptions'][0]['name'], 'Size')
        self.assertEqual(len(inp['variants']), 2)
        pmap = self.env['axsync.shopify.product'].search([('store_id', '=', self.store.id)])
        self.assertEqual(len(pmap.variant_ids), 2)
        # second export updates the same product
        self.svc.export_templates(self.store, tmpl)
        second = self.mock.ops('product_set')[1][1]
        self.assertEqual(second['identifier']['id'], pmap.shopify_id)
        self.assertEqual(len(self.mock.products), 1)
        self.assertEqual(self.env['axsync.shopify.variant'].search_count([('store_id', '=', self.store.id)]), 2)

    def test_excluded_products_are_never_exported(self):
        prod = self.make_product('SECRET-1')
        prod.product_tmpl_id.shopify_exclude = True
        self.export(prod, self.make_product('OPEN-1'))
        skus = [c[1]['input']['variants'][0]['sku'] for c in self.mock.ops('product_set')]
        self.assertIn('OPEN-1', skus)
        self.assertNotIn('SECRET-1', skus)

    def test_export_error_is_logged_and_kept_on_the_map_or_log(self):
        self.mock.product_set_errors = ['Title can\'t be blank']
        self.export(self.make_product('BAD-1'))
        log = self.env['axsync.shopify.log'].search([('store_id', '=', self.store.id), ('operation', '=', 'product export')])
        self.assertIn("Title can't be blank", log.message)
        self.assertFalse(self.env['axsync.shopify.product'].search([('store_id', '=', self.store.id)]))

    def test_publish_unpublish_archive(self):
        self.export(self.make_product('MUG-01'))
        pmap = self.env['axsync.shopify.product'].search([('store_id', '=', self.store.id)])
        pmap.action_publish()
        self.assertEqual(self.mock.products[pmap.shopify_id]['status'], 'ACTIVE')
        self.assertEqual(pmap.state, 'active')
        pmap.action_unpublish()
        self.assertEqual(self.mock.products[pmap.shopify_id]['status'], 'DRAFT')
        pmap.action_archive_remote()
        self.assertEqual(pmap.state, 'archived')
        self.store.read_only = True
        with self.assertRaises(client_mod.ShopifyError):
            pmap.action_publish()

    def test_outbound_metafield_mapping(self):
        field = self.env['ir.model.fields']._get('product.template', 'barcode')
        self.env['axsync.shopify.metafield.map'].create({'store_id': self.store.id, 'owner': 'product', 'namespace': 'erp',
                                                         'key': 'code', 'odoo_field': field.id, 'direction': 'out'})
        prod = self.make_product('MUG-01', barcode='9780201379624')
        self.export(prod)
        meta = self.mock.ops('product_set')[0][1]['input']['metafields']
        self.assertEqual(meta, [{'namespace': 'erp', 'key': 'code', 'type': 'single_line_text_field', 'value': '9780201379624'}])
        self.assertEqual(prod.barcode, '9780201379624')

    def test_product_deleted_in_shopify_is_flagged(self):
        self.make_product('MUG-01')
        node = self.mock.add_product('Mug', [{'sku': 'MUG-01', 'price': 12.0}])
        self.store.action_import_products()
        self.env['axsync.shopify.queue'].enqueue(self.store, 'product', 'PRODUCTS_DELETE', node['id'])
        self.process_queue()
        self.assertEqual(self.env['axsync.shopify.product'].search([('store_id', '=', self.store.id)]).state, 'deleted')
