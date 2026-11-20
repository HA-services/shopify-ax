# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import compat, gql, helpers
from .client import ShopifyError


class ShopifyProduct(models.Model):
    _name = 'axsync.shopify.product'
    _description = 'Shopify product'
    _order = 'store_id, name'
    _rec_name = 'name'

    name = fields.Char(required=True)
    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade', index=True)
    template_id = fields.Many2one('product.template', required=True, ondelete='cascade', index=True)
    shopify_id = fields.Char('Shopify ID', index=True)
    handle = fields.Char()
    state = fields.Selection([('active', 'Active'), ('draft', 'Draft'), ('archived', 'Archived'), ('deleted', 'Deleted in Shopify')],
                             default='draft', required=True)
    variant_ids = fields.One2many('axsync.shopify.variant', 'product_map_id')
    variant_count = fields.Integer(compute='_compute_variant_count')
    last_import = fields.Datetime(readonly=True)
    last_export = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)

    _tmpl_uniq = models.Constraint('UNIQUE(store_id, template_id)', 'This product is already mapped for the store.')
    _sid_uniq = models.Constraint('UNIQUE(store_id, shopify_id)', 'This Shopify product is already mapped.')

    @api.depends('variant_ids')
    def _compute_variant_count(self):
        for rec in self:
            rec.variant_count = len(rec.variant_ids)

    def _set_status(self, status):
        for rec in self:
            rec.store_id._ensure_writable('product status')
            if not rec.shopify_id:
                raise UserError(_('Export the product first.'))
            data = rec.store_id._query(gql.PRODUCT_STATUS, {'input': {'id': rec.shopify_id, 'status': status}})
            rec.store_id._client().check_user_errors(data, 'productUpdate')
            rec.state = status.lower()
        return True

    def action_publish(self):
        return self._set_status('ACTIVE')

    def action_unpublish(self):
        return self._set_status('DRAFT')

    def action_archive_remote(self):
        return self._set_status('ARCHIVED')

    def action_export(self):
        for rec in self:
            self.env['axsync.shopify.svc.product'].export_templates(rec.store_id, rec.template_id)
        return True

    def action_refresh(self):
        for rec in self:
            self.env['axsync.shopify.svc.product'].sync_product(rec.store_id, rec.shopify_id)
        return True

    def action_delete_remote(self):
        for rec in self:
            rec.store_id._ensure_writable('product delete')
            data = rec.store_id._query(gql.PRODUCT_DELETE, {'input': {'id': rec.shopify_id}})
            rec.store_id._client().check_user_errors(data, 'productDelete')
            rec.write({'state': 'deleted', 'shopify_id': False})
        return True


class ShopifyVariant(models.Model):
    _name = 'axsync.shopify.variant'
    _description = 'Shopify variant'
    _order = 'product_map_id, id'

    product_map_id = fields.Many2one('axsync.shopify.product', required=True, ondelete='cascade', index=True)
    store_id = fields.Many2one(related='product_map_id.store_id', store=True, index=True)
    product_id = fields.Many2one('product.product', required=True, ondelete='cascade', index=True)
    shopify_id = fields.Char('Shopify ID', index=True)
    inventory_item_id = fields.Char('Inventory item ID', index=True)
    sku = fields.Char()
    barcode = fields.Char()
    price = fields.Float(digits=(16, 4))
    compare_at_price = fields.Float(digits=(16, 4))
    state_ids = fields.One2many('axsync.shopify.stock.state', 'variant_id')

    _sid_uniq = models.Constraint('UNIQUE(store_id, shopify_id)', 'This Shopify variant is already mapped.')


class ShopifyProductService(models.AbstractModel):
    _name = 'axsync.shopify.svc.product'
    _description = 'Shopify product synchronisation'

    # ------------------------------------------------------------------ import
    def import_products(self, store, incremental=False):
        variables = {}
        if incremental and store.last_product_sync:
            variables['query'] = 'updated_at:>%s' % store.last_product_sync.strftime('%Y-%m-%dT%H:%M:%SZ')
        started = fields.Datetime.now()
        count = 0
        for node in store._client().paginate(store, gql.PRODUCTS, ['products'], variables, page_size=25):
            try:
                with self.env.cr.savepoint():
                    self._upsert(store, node)
                count += 1
            except Exception as exc:  # noqa: BLE001
                store._log('product import', '%s: %s' % (node.get('title'), exc), 'error', node.get('id'))
        store.last_product_sync = started
        store._log('product import', _('%s products imported or updated') % count)
        return count

    def sync_product(self, store, gid):
        data = store._query(gql.PRODUCT, {'id': gid})
        node = data.get('product')
        if node:
            self._upsert(store, node)
        return bool(node)

    def handle_job(self, store, job, payload):
        gid = job.shopify_id
        if job.topic == 'PRODUCTS_DELETE':
            pm = self.env['axsync.shopify.product'].search([('store_id', '=', store.id), ('shopify_id', '=', gid)])
            pm.write({'state': 'deleted'})
            return
        if not self.sync_product(store, gid):
            store._log('product', _('Product %s no longer exists in Shopify') % gid, 'warning', gid)

    def _find_variant(self, store, node):
        """Locate the Odoo product for one Shopify variant: map, SKU, then barcode."""
        Variant, Product = self.env['axsync.shopify.variant'], self.env['product.product']
        mapped = Variant.search([('store_id', '=', store.id), ('shopify_id', '=', node['id'])], limit=1)
        if mapped:
            return mapped.product_id, mapped
        sku, barcode = (node.get('sku') or '').strip(), (node.get('barcode') or '').strip()
        company = [('company_id', 'in', (False, store.company_id.id))]
        prod = Product.browse()
        if sku:
            prod = Product.search([('default_code', '=', sku)] + company, limit=1)
        if not prod and barcode:
            prod = Product.search([('barcode', '=', barcode)] + company, limit=1)
        return prod, Variant.browse()

    def _upsert(self, store, node):
        Map, Variant = self.env['axsync.shopify.product'], self.env['axsync.shopify.variant']
        variants = node.get('variants', {}).get('nodes') or []
        options = [o for o in node.get('options') or []]
        found = {v['id']: self._find_variant(store, v)[0] for v in variants}
        pm = Map.search([('store_id', '=', store.id), ('shopify_id', '=', node['id'])], limit=1)
        template = pm.template_id
        if not template:
            tmpls = Map.browse().env['product.template']
            for prod in found.values():
                if prod:
                    template = prod.product_tmpl_id
                    break
        if not template:
            if not store.auto_create_products and not self.env.context.get('axsync_force_create'):
                skus = ', '.join(v.get('sku') or '-' for v in variants)
                store._mismatch_activity(_('Shopify product without Odoo match'),
                                         _('"%(title)s" (SKU %(skus)s) has no matching product. Create it, set the SKU, or enable product creation on the store.') % {
                                             'title': node.get('title'), 'skus': skus})
                store._log('product import', _('No match for %s') % node.get('title'), 'warning', node['id'])
                return False
            template = self._create_template(store, node)
            found = {v['id']: self._find_variant(store, v)[0] for v in variants}
        vals = {'name': node.get('title'), 'store_id': store.id, 'template_id': template.id, 'shopify_id': node['id'],
                'handle': node.get('handle'), 'state': (node.get('status') or 'DRAFT').lower(), 'last_import': fields.Datetime.now(), 'last_error': False}
        if pm:
            pm.write(vals)
        else:
            pm = Map.search([('store_id', '=', store.id), ('template_id', '=', template.id)], limit=1)
            if pm:
                pm.write(vals)
            else:
                pm = Map.create(vals)
        for v in variants:
            prod = found.get(v['id'])
            if not prod:
                store._mismatch_activity(_('Shopify variant without Odoo match'),
                                         _('Variant %(sku)s of "%(title)s" has no matching product variant.') % {'sku': v.get('sku') or v['id'], 'title': node.get('title')})
                continue
            vvals = {'product_map_id': pm.id, 'product_id': prod.id, 'shopify_id': v['id'], 'sku': v.get('sku'),
                     'barcode': v.get('barcode'), 'price': float(v.get('price') or 0),
                     'compare_at_price': float(v.get('compareAtPrice') or 0),
                     'inventory_item_id': (v.get('inventoryItem') or {}).get('id')}
            existing = Variant.search([('store_id', '=', store.id), ('shopify_id', '=', v['id'])], limit=1)
            if existing:
                existing.write(vvals)
            else:
                Variant.create(vvals)
            if store.create_missing_categories is not None and not prod.barcode and v.get('barcode'):
                prod.barcode = v['barcode']
        if template and store.create_missing_categories and node.get('productType'):
            self._ensure_category(template, node['productType'])
        helpers.apply_metafields(store, 'product', template, (node.get('metafields') or {}).get('nodes'))
        return pm

    def _ensure_category(self, template, name):
        Category = self.env['product.category']
        cat = Category.search([('name', '=', name)], limit=1)
        if not cat:
            cat = Category.create({'name': name})
        if template.categ_id == self.env.ref('product.product_category_all', raise_if_not_found=False) or not template.categ_id:
            template.categ_id = cat.id

    def _create_template(self, store, node):
        """Create a product, with attributes when the Shopify product has real options."""
        Attribute, Value = self.env['product.attribute'], self.env['product.attribute.value']
        variants = node.get('variants', {}).get('nodes') or []
        vals = {'name': node.get('title'), 'sale_ok': True, 'purchase_ok': True,
                'description_sale': False, 'company_id': False}
        vals.update(compat.product_type_vals(self.env, storable=True))
        if len(variants) <= 1:
            v = variants[0] if variants else {}
            vals.update({'default_code': v.get('sku') or False, 'barcode': v.get('barcode') or False,
                         'list_price': float(v.get('price') or 0)})
            return self.env['product.template'].create(vals)
        lines = []
        for opt in node.get('options') or []:
            if opt.get('name') == 'Title' and opt.get('values') == ['Default Title']:
                continue
            # only an attribute that creates its variants can carry a Shopify option
            attr = Attribute.search([('name', '=', opt['name']), ('create_variant', '=', 'always')], limit=1) or \
                Attribute.create({'name': opt['name'], 'create_variant': 'always'})
            ids = []
            for val in opt.get('values') or []:
                rec = Value.search([('attribute_id', '=', attr.id), ('name', '=', val)], limit=1) or Value.create({'attribute_id': attr.id, 'name': val})
                ids.append(rec.id)
            lines.append((0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, ids)]}))
        vals['attribute_line_ids'] = lines
        vals['list_price'] = min(float(v.get('price') or 0) for v in variants)
        tmpl = self.env['product.template'].create(vals)
        # give every generated variant its SKU, barcode and extra price
        for v in variants:
            wanted = {o['value'] for o in v.get('selectedOptions') or []}
            for prod in tmpl.product_variant_ids:
                names = set(prod.product_template_attribute_value_ids.mapped('name'))
                if names == wanted:
                    prod.write({'default_code': v.get('sku') or False, 'barcode': v.get('barcode') or False})
                    extra = float(v.get('price') or 0) - tmpl.list_price
                    if extra:
                        for ptav in prod.product_template_attribute_value_ids:
                            ptav.price_extra = extra
                    break
        return tmpl

    # ------------------------------------------------------------------ export
    def export_products(self, store):
        domain = [('sale_ok', '=', True), ('shopify_exclude', '=', False),
                  '|', ('company_id', '=', False), ('company_id', '=', store.company_id.id)]
        templates = self.env['product.template'].search(domain)
        return self.export_templates(store, templates)

    def _price(self, store, product):
        pricelist = store.pricelist_id
        if pricelist:
            try:
                price = pricelist._get_product_price(product, 1.0)
            except Exception:  # noqa: BLE001
                price = product.lst_price
        else:
            price = product.lst_price
        price = price * (1 + (store.price_markup_percent or 0.0) / 100.0)
        step = store.price_rounding
        if step:
            price = round(price / step) * step
        return round(price, 2)

    def _build_input(self, store, template, pm):
        products = template.product_variant_ids.filtered(lambda p: p.active)
        lines = template.attribute_line_ids.filtered(lambda l: len(l.value_ids) >= 1 and l.attribute_id.create_variant != 'no_variant')
        single = not lines or len(products) <= 1
        if single:
            options = [{'name': 'Title', 'values': [{'name': 'Default Title'}]}]
        else:
            options = [{'name': l.attribute_id.name, 'values': [{'name': v.name} for v in l.value_ids]} for l in lines]
        variants = []
        for prod in products[:100] if not single else products[:1]:
            if single:
                ov = [{'optionName': 'Title', 'name': 'Default Title'}]
            else:
                ov = [{'optionName': ptav.attribute_id.name, 'name': ptav.name} for ptav in prod.product_template_attribute_value_ids
                      if ptav.attribute_id.create_variant != 'no_variant']
            price = self._price(store, prod)
            item = {'optionValues': ov, 'price': '%.2f' % price, 'sku': prod.default_code or False,
                    'barcode': prod.barcode or False, 'taxable': bool(prod.taxes_id),
                    'inventoryItem': {'tracked': compat.is_storable(prod), 'sku': prod.default_code or False}}
            if prod.weight:
                item['inventoryItem']['measurement'] = {'weight': {'value': prod.weight, 'unit': 'KILOGRAMS'}}
            if store.compare_at_mode == 'list_price' and prod.lst_price > price + 0.005:
                item['compareAtPrice'] = '%.2f' % prod.lst_price
            variants.append({k: v for k, v in item.items() if v is not False})
        status = 'DRAFT' if (store.export_as_draft and not pm) or not template.shopify_publish else None
        if pm and pm.state in ('draft', 'archived', 'active') and not status:
            status = pm.state.upper() if pm.state != 'deleted' else 'DRAFT'
        if not status:
            status = 'ACTIVE'
        inp = {'title': template.name, 'status': status, 'productOptions': options, 'variants': variants}
        if store.export_description:
            inp['descriptionHtml'] = template.description_sale or ''
        vendor = template.shopify_vendor or store.vendor_name
        if vendor:
            inp['vendor'] = vendor
        ptype = template.shopify_product_type or (template.categ_id.name if template.categ_id else False)
        if ptype:
            inp['productType'] = ptype
        tags = helpers.split_tags(template.shopify_tags)
        if tags:
            inp['tags'] = tags
        if template.shopify_seo_title or template.shopify_seo_description:
            inp['seo'] = {'title': template.shopify_seo_title or template.name, 'description': template.shopify_seo_description or ''}
        if store.export_images and template.image_1920:
            inp['files'] = [{'originalSource': helpers.image_url(self.env, store, template), 'contentType': 'IMAGE', 'alt': template.name}]
        meta = helpers.outbound_metafields(store, 'product', template)
        if meta:
            inp['metafields'] = meta
        return inp, single, products

    def export_templates(self, store, templates):
        store._ensure_writable('product export')
        count = 0
        for template in templates.filtered(lambda t: not t.shopify_exclude):
            pm = self.env['axsync.shopify.product'].search([('store_id', '=', store.id), ('template_id', '=', template.id)], limit=1)
            try:
                with self.env.cr.savepoint():
                    self._export_one(store, template, pm)
                count += 1
            except ShopifyError as exc:
                store._log('product export', '%s: %s' % (template.display_name, exc), 'error', template.default_code or '')
                if pm:
                    pm.last_error = str(exc)
        store.last_product_sync = fields.Datetime.now()
        return count

    def _export_one(self, store, template, pm):
        inp, single, products = self._build_input(store, template, pm)
        variables = {'input': inp}
        if pm and pm.shopify_id:
            variables['identifier'] = {'id': pm.shopify_id}
        data = store._query(gql.PRODUCT_SET, variables)
        part = data.get('productSet') or {}
        errors = part.get('userErrors') or []
        if errors:
            raise ShopifyError('; '.join('%s' % e.get('message') for e in errors))
        remote = part.get('product') or {}
        vals = {'name': template.name, 'store_id': store.id, 'template_id': template.id, 'shopify_id': remote.get('id'),
                'handle': remote.get('handle'), 'state': (remote.get('status') or 'DRAFT').lower(),
                'last_export': fields.Datetime.now(), 'last_error': False}
        if pm:
            pm.write(vals)
        else:
            pm = self.env['axsync.shopify.product'].create(vals)
        Variant = self.env['axsync.shopify.variant']
        for node in (remote.get('variants') or {}).get('nodes') or []:
            prod = self._match_exported(products, node, single)
            if not prod:
                continue
            vvals = {'product_map_id': pm.id, 'product_id': prod.id, 'shopify_id': node['id'],
                     'inventory_item_id': (node.get('inventoryItem') or {}).get('id'), 'sku': node.get('sku') or prod.default_code,
                     'barcode': prod.barcode, 'price': self._price(store, prod)}
            existing = Variant.search([('store_id', '=', store.id), ('shopify_id', '=', node['id'])], limit=1)
            if existing:
                existing.write(vvals)
            else:
                Variant.create(vvals)
        # a fresh export has no stock yet, forget what we believed we had sent
        self.env['axsync.shopify.stock.state'].search([('variant_id.product_map_id', '=', pm.id)]).unlink()
        return pm

    def _match_exported(self, products, node, single):
        if single:
            return products[:1]
        wanted = {o['value'] for o in node.get('selectedOptions') or []}
        for prod in products:
            if set(prod.product_template_attribute_value_ids.mapped('name')) == wanted:
                return prod
        sku = node.get('sku')
        return products.filtered(lambda p: p.default_code == sku)[:1] if sku else products.browse()
