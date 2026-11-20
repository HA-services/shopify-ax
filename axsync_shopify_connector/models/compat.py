# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
"""Small helpers that hide the differences between Odoo 16 and Odoo 20."""


def line_tax_field(env):
    return 'tax_ids' if 'tax_ids' in env['sale.order.line']._fields else 'tax_id'


def line_uom_field(env):
    return 'product_uom_id' if 'product_uom_id' in env['sale.order.line']._fields else 'product_uom'


def product_type_vals(env, storable=True):
    """Values that make a product stockable on any supported version."""
    fields_ = env['product.template']._fields
    if 'detailed_type' in fields_:
        return {'detailed_type': 'product' if storable else 'consu'}
    vals = {'type': 'consu'}
    if 'is_storable' in fields_:
        vals['is_storable'] = bool(storable)
    return vals


def service_type_vals(env):
    fields_ = env['product.template']._fields
    if 'detailed_type' in fields_:
        return {'detailed_type': 'service'}
    return {'type': 'service'}


def is_storable(product):
    tmpl = product.product_tmpl_id if product._name == 'product.product' else product
    if 'is_storable' in tmpl._fields:
        return bool(tmpl.is_storable)
    return tmpl.type == 'product'


def create_invoices(order):
    try:
        return order._create_invoices(final=True)
    except TypeError:
        return order._create_invoices()


def order_cancel(order):
    if hasattr(order, '_action_cancel'):
        order.with_context(disable_cancel_warning=True)._action_cancel()
    else:
        order.action_cancel()


def set_done_quantities(picking):
    for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
        if 'quantity_done' in move._fields:
            move.quantity_done = move.product_uom_qty
        else:
            move.quantity = move.product_uom_qty
            if 'picked' in move._fields:
                move.picked = True


def validate_picking(picking):
    picking.action_assign()
    set_done_quantities(picking)
    return picking.with_context(skip_backorder=True, skip_immediate=True, skip_sms=True, skip_expired=True).button_validate()


def move_done_qty(move):
    if 'quantity_done' in move._fields:
        return move.quantity_done
    return move.quantity
