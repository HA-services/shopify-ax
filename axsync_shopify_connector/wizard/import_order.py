# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import _, fields, models
from odoo.exceptions import UserError


class ShopifyImportOrderWizard(models.TransientModel):
    _name = 'axsync.shopify.import.order'
    _description = 'Import one Shopify order'

    store_id = fields.Many2one('axsync.shopify.store', required=True, domain="[('state', '=', 'connected')]")
    order_name = fields.Char('Order number', required=True, help='For example #1042 or 1042.')

    def action_import(self):
        self.ensure_one()
        order = self.env['axsync.shopify.svc.order'].import_by_name(self.store_id, self.order_name.strip())
        if not order:
            raise UserError(_('The order was not imported. Its payment status may be excluded in the store settings.'))
        return {'type': 'ir.actions.act_window', 'res_model': 'sale.order', 'res_id': order.id, 'view_mode': 'form', 'target': 'current'}
