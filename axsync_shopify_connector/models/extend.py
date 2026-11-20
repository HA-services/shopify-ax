# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .client import ShopifyError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shopify_store_id = fields.Many2one('axsync.shopify.store', string='Shopify store', copy=False, index=True, readonly=True)
    shopify_order_id = fields.Char('Shopify order ID', copy=False, index=True, readonly=True)
    shopify_order_name = fields.Char('Shopify order', copy=False, readonly=True)
    shopify_financial_status = fields.Char('Shopify payment status', copy=False, readonly=True)
    shopify_fulfillment_status = fields.Char('Shopify fulfillment status', copy=False, readonly=True)
    shopify_tags = fields.Char('Shopify tags', copy=False, readonly=True)
    shopify_gateway = fields.Char('Payment gateway', copy=False, readonly=True)
    shopify_cancelled = fields.Boolean('Cancelled in Shopify', copy=False, readonly=True)
    shopify_currency = fields.Char('Shopify currency', copy=False, readonly=True)
    shopify_error = fields.Text('Shopify problem', copy=False, readonly=True)

    _shopify_uniq = models.Constraint('UNIQUE(shopify_store_id, shopify_order_id)', 'This Shopify order was already imported.')

    def action_resync_shopify(self):
        for order in self.filtered('shopify_store_id'):
            self.env['axsync.shopify.svc.order'].sync_order(order.shopify_store_id, order.shopify_order_id)
        return True

    def action_open_in_shopify(self):
        self.ensure_one()
        if not self.shopify_order_id:
            raise UserError(_('This order does not come from Shopify.'))
        num = self.shopify_order_id.rsplit('/', 1)[-1]
        return {'type': 'ir.actions.act_url', 'target': 'new',
                'url': 'https://%s/admin/orders/%s' % (self.shopify_store_id.shop_domain, num)}

    def _action_cancel(self):
        res = super()._action_cancel()
        if not self.env.context.get('axsync_shopify_no_push'):
            for order in self.filtered(lambda o: o.shopify_store_id and not o.shopify_cancelled):
                store = order.shopify_store_id
                if store.export_cancellations and not store.read_only and store.state == 'connected':
                    try:
                        self.env['axsync.shopify.svc.order'].cancel_in_shopify(order)
                    except ShopifyError as exc:
                        order.message_post(body=_('Shopify could not cancel the order: %s') % exc)
                        store._log('cancel', str(exc), 'warning', order.name)
        return res


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    shopify_line_id = fields.Char('Shopify line ID', copy=False, index=True, readonly=True)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    shopify_customer_ids = fields.One2many('axsync.shopify.customer', 'partner_id', string='Shopify customers')
    shopify_count = fields.Integer(compute='_compute_shopify_count')

    def _compute_shopify_count(self):
        for rec in self:
            rec.shopify_count = len(rec.shopify_customer_ids)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    shopify_exclude = fields.Boolean('Do not sync with Shopify', help='Excluded products are never exported and their stock is not sent.')
    shopify_product_ids = fields.One2many('axsync.shopify.product', 'template_id', string='Shopify products')
    shopify_count = fields.Integer(compute='_compute_shopify_count')
    shopify_vendor = fields.Char('Shopify vendor')
    shopify_product_type = fields.Char('Shopify product type')
    shopify_tags = fields.Char('Shopify tags', help='Comma separated list.')
    shopify_seo_title = fields.Char('SEO title')
    shopify_seo_description = fields.Char('SEO description')
    shopify_publish = fields.Boolean('Published on Shopify', default=True, help='When off, the product is exported as draft.')

    def _compute_shopify_count(self):
        for rec in self:
            rec.shopify_count = len(rec.shopify_product_ids)

    def action_export_to_shopify(self):
        stores = self.env['axsync.shopify.store'].search([('state', '=', 'connected')])
        if not stores:
            raise UserError(_('Connect a Shopify store first.'))
        for store in stores:
            store._ensure_writable('product export')
            self.env['axsync.shopify.svc.product'].export_templates(store, self)
        return True


class ProductProduct(models.Model):
    _inherit = 'product.product'

    shopify_variant_ids = fields.One2many('axsync.shopify.variant', 'product_id')


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    shopify_tracking_number = fields.Char('Tracking number', copy=False)
    shopify_tracking_company = fields.Char('Carrier name', copy=False)
    shopify_tracking_url = fields.Char('Tracking URL', copy=False)
    shopify_sent = fields.Boolean('Sent to Shopify', copy=False, readonly=True)
    shopify_sent_date = fields.Datetime(copy=False, readonly=True)
    shopify_store_id = fields.Many2one('axsync.shopify.store', related='sale_id.shopify_store_id', store=True, index=True)

    def action_send_tracking_to_shopify(self):
        for picking in self:
            if not picking.shopify_store_id:
                raise UserError(_('This delivery does not belong to a Shopify order.'))
            picking.shopify_store_id._ensure_writable('tracking export')
            self.env['axsync.shopify.svc.fulfillment'].send_picking(picking)
        return True


class AccountMove(models.Model):
    _inherit = 'account.move'

    shopify_refund_id = fields.Char('Shopify refund ID', copy=False, index=True, readonly=True)
    shopify_store_id = fields.Many2one('axsync.shopify.store', copy=False, readonly=True)
