# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from datetime import datetime, timedelta

from odoo import _, api, fields, models

from . import compat, gql, helpers
from .client import ShopifyError, ShopifyMismatch

FINANCIAL_FLAGS = {
    'pending': 'imp_pending', 'authorized': 'imp_authorized', 'paid': 'imp_paid', 'partially_paid': 'imp_partially_paid',
    'partially_refunded': 'imp_partially_refunded', 'refunded': 'imp_refunded', 'voided': 'imp_voided', 'expired': 'imp_voided',
}


def parse_dt(value):
    if not value:
        return False
    return datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')


class ShopifyOrderService(models.AbstractModel):
    _name = 'axsync.shopify.svc.order'
    _description = 'Shopify order synchronisation'

    # ------------------------------------------------------------------ entry points
    def import_orders(self, store):
        started = fields.Datetime.now()
        if store.last_order_sync:
            since = store.last_order_sync - timedelta(minutes=10)
            query = 'updated_at:>=%s' % since.strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            since = store.import_orders_after or (started - timedelta(days=30))
            query = 'created_at:>=%s' % since.strftime('%Y-%m-%dT%H:%M:%SZ')
        count = 0
        for node in store._client().paginate(store, gql.ORDERS, ['orders'], {'query': query}, page_size=25):
            count += 1 if self._safe_process(store, node) else 0
        store.last_order_sync = started
        store._log('order import', _('%s orders processed') % count)
        return count

    def _safe_process(self, store, node):
        try:
            with self.env.cr.savepoint():
                return bool(self.process_order(store, node))
        except ShopifyMismatch as exc:
            store._log('order import', '%s: %s' % (node.get('name'), exc), 'warning', node.get('name'))
            if exc.summary:
                store._mismatch_activity(exc.summary, exc.note)
            self.env['axsync.shopify.queue'].enqueue(store, 'order', 'ORDERS_UPDATED', node['id'])
        except Exception as exc:  # noqa: BLE001
            store._log('order import', '%s: %s' % (node.get('name'), exc), 'error', node.get('name'))
            self.env['axsync.shopify.queue'].enqueue(store, 'order', 'ORDERS_UPDATED', node['id'])
        return False

    def sync_order(self, store, gid):
        data = store._query(gql.ORDER, {'id': gid})
        node = data.get('order')
        if not node:
            raise ShopifyError(_('Order %s was not found in Shopify.') % gid)
        return self.process_order(store, node)

    def import_by_name(self, store, name):
        name = name if name.startswith('#') else '#%s' % name
        for node in store._client().paginate(store, gql.ORDERS, ['orders'], {'query': 'name:%s' % name}, page_size=5, limit=5):
            if node.get('name') == name:
                return self.process_order(store, node)
        raise ShopifyError(_('No order named %s in Shopify.') % name)

    def handle_job(self, store, job, payload):
        self.sync_order(store, job.shopify_id)

    def handle_refund_job(self, store, job, payload):
        self.sync_order(store, job.shopify_id)

    def handle_fulfillment_job(self, store, job, payload):
        order = self.sync_order(store, job.shopify_id)
        if order:
            order.message_post(body=_('Shopify reports a fulfillment for this order.'))

    # ------------------------------------------------------------------ main processing
    def process_order(self, store, node):
        Order = self.env['sale.order']
        order = Order.search([('shopify_store_id', '=', store.id), ('shopify_order_id', '=', node['id'])], limit=1)
        fin = (node.get('displayFinancialStatus') or '').lower()
        if not order:
            flag = FINANCIAL_FLAGS.get(fin)
            if flag and not store[flag]:
                return False
            if node.get('cancelledAt'):
                return False
            order = self._create_order(store, node)
        else:
            self._update_status(order, node)
        if node.get('cancelledAt') and store.sync_cancellations and not order.shopify_cancelled:
            self._cancel_local(order)
        self._import_refunds(store, order, node)
        return order

    def _update_status(self, order, node):
        order.write({
            'shopify_financial_status': (node.get('displayFinancialStatus') or '').lower(),
            'shopify_fulfillment_status': (node.get('displayFulfillmentStatus') or '').lower(),
            'shopify_tags': ', '.join(node.get('tags') or []),
        })
        helpers.apply_metafields(order.shopify_store_id, 'order', order, (node.get('metafields') or {}).get('nodes'))

    def _cancel_local(self, order):
        order.shopify_cancelled = True
        if order.state in ('cancel',):
            return
        try:
            with self.env.cr.savepoint():
                compat.order_cancel(order.with_context(axsync_shopify_no_push=True))
            order.message_post(body=_('Cancelled because the order was cancelled in Shopify.'))
        except Exception as exc:  # noqa: BLE001
            order.message_post(body=_('The order was cancelled in Shopify but Odoo could not cancel it: %s') % exc)
            order.shopify_error = str(exc)

    # ------------------------------------------------------------------ creation
    def _pricelist(self, store, code):
        if store.pricelist_id and store.pricelist_id.currency_id.name == code:
            return store.pricelist_id
        found = self.env['product.pricelist'].search([('currency_id.name', '=', code),
                                                      ('company_id', 'in', (False, store.company_id.id))], limit=1)
        if found:
            return found
        raise ShopifyMismatch(_('No pricelist in %s. Create one or choose a pricelist on the store.') % code)

    def _product_for_line(self, store, line):
        Variant, Product = self.env['axsync.shopify.variant'], self.env['product.product']
        var = (line.get('variant') or {})
        if var.get('id'):
            mapped = Variant.search([('store_id', '=', store.id), ('shopify_id', '=', var['id'])], limit=1)
            if mapped:
                return mapped.product_id
        sku = (line.get('sku') or var.get('sku') or '').strip()
        company = [('company_id', 'in', (False, store.company_id.id))]
        if sku:
            prod = Product.search([('default_code', '=', sku)] + company, limit=1)
            if prod:
                return prod
        if store.auto_create_products:
            tmpl = self.env['product.template'].create(dict(
                {'name': line.get('title') or line.get('name') or sku, 'default_code': sku or False,
                 'list_price': float((line.get('originalUnitPriceSet') or {}).get('shopMoney', {}).get('amount') or 0),
                 'company_id': False}, **compat.product_type_vals(self.env, storable=bool(line.get('requiresShipping')))))
            return tmpl.product_variant_id
        raise ShopifyMismatch(
            _('No product for "%(title)s" (SKU %(sku)s)') % {'title': line.get('title'), 'sku': sku or '-'},
            summary=_('Order line without Odoo product'),
            note=_('Line "%(title)s" (SKU %(sku)s) of a Shopify order has no matching product. Fix the SKU or the mapping, then retry the queue job.') % {
                'title': line.get('title'), 'sku': sku or '-'})

    def _tax_for(self, store, tax_line):
        rate = float(tax_line.get('rate') or 0.0) * 100.0
        title = (tax_line.get('title') or '').strip().lower()
        for m in store.tax_map_ids:
            if abs(m.rate - rate) < 0.01 and (not m.title or m.title.strip().lower() == title):
                return m.tax_id
        auto = self.env['account.tax'].search([('type_tax_use', '=', 'sale'), ('amount_type', '=', 'percent'),
                                               ('company_id', '=', store.company_id.id), ('amount', '>=', rate - 0.01),
                                               ('amount', '<=', rate + 0.01)], limit=1)
        return auto or store.default_tax_id

    def _taxes(self, store, tax_lines):
        taxes = self.env['account.tax']
        for tl in tax_lines or []:
            tax = self._tax_for(store, tl)
            if tax:
                taxes |= tax
        return taxes

    @staticmethod
    def _adjust_price(price, taxes, taxes_included):
        rate = sum(t.amount for t in taxes if t.amount_type == 'percent') / 100.0
        included = any(t.price_include for t in taxes)
        if rate and taxes_included and not included:
            return price / (1 + rate)
        if rate and not taxes_included and included:
            return price * (1 + rate)
        return price

    def _order_lines(self, store, node, presentment):
        tax_field = compat.line_tax_field(self.env)
        lines = []
        included = bool(node.get('taxesIncluded'))
        for li in (node.get('lineItems') or {}).get('nodes') or []:
            qty = li.get('quantity') or 0
            if qty <= 0:
                continue
            prod = self._product_for_line(store, li)
            unit = store._money(li.get('originalUnitPriceSet'), presentment)[0]
            disc = store._money(li.get('totalDiscountSet'), presentment)[0]
            pct = round(disc / (unit * qty) * 100.0, 4) if unit and qty and disc else 0.0
            taxes = self._taxes(store, li.get('taxLines')) if li.get('taxable', True) else self.env['account.tax']
            vals = {'product_id': prod.id, 'name': li.get('title') or prod.display_name, 'product_uom_qty': qty,
                    'price_unit': self._adjust_price(unit, taxes, included), 'discount': min(pct, 100.0),
                    'shopify_line_id': li['id'], tax_field: [(6, 0, taxes.ids)]}
            if li.get('name') and li.get('name') != li.get('title'):
                vals['name'] = li['name']
            lines.append((0, 0, vals))
        for sl in (node.get('shippingLines') or {}).get('nodes') or []:
            price = store._money(sl.get('discountedPriceSet') or sl.get('originalPriceSet'), presentment)[0]
            if not price or not store.shipping_product_id:
                continue
            taxes = self._taxes(store, sl.get('taxLines'))
            lines.append((0, 0, {'product_id': store.shipping_product_id.id, 'name': sl.get('title') or _('Shipping'),
                                 'product_uom_qty': 1, 'price_unit': self._adjust_price(price, taxes, included),
                                 'shopify_line_id': sl.get('id'), tax_field: [(6, 0, taxes.ids)]}))
        return lines

    def _partners(self, store, node):
        svc = self.env['axsync.shopify.svc.customer']
        cust = node.get('customer')
        bill, ship = node.get('billingAddress'), node.get('shippingAddress')
        email = node.get('email') or (cust or {}).get('email')
        partner = svc.partner_from_customer(store, cust, order_email=email, default_address=bill or ship)
        if cust:
            svc._apply_tags(store, partner, cust)
        if not partner.email and email:
            partner.email = email
        invoice = svc.address_partner(store, partner, bill, 'invoice') if bill else partner
        shipping = svc.address_partner(store, partner, ship, 'delivery') if ship else partner
        return partner, invoice, shipping

    def _create_order(self, store, node):
        presentment = bool(store.use_presentment_currency and node.get('presentmentCurrencyCode') and
                           node.get('presentmentCurrencyCode') != node.get('currencyCode'))
        code = node.get('presentmentCurrencyCode') if presentment else node.get('currencyCode')
        pricelist = self._pricelist(store, code)
        partner, invoice, shipping = self._partners(store, node)
        lines = self._order_lines(store, node, presentment)
        if not lines:
            raise ShopifyMismatch(_('Order %s has no importable lines.') % node.get('name'))
        vals = {
            'partner_id': partner.id, 'partner_invoice_id': invoice.id, 'partner_shipping_id': shipping.id,
            'company_id': store.company_id.id, 'pricelist_id': pricelist.id,
            'warehouse_id': store.warehouse_id.id, 'team_id': store.team_id.id or False,
            'user_id': store.user_id.id or False, 'payment_term_id': store.payment_term_id.id or False,
            'client_order_ref': node.get('name'), 'origin': '%s%s' % (store.order_origin_prefix or '', node.get('name')),
            'date_order': parse_dt(node.get('processedAt') or node.get('createdAt')) or fields.Datetime.now(),
            'shopify_store_id': store.id, 'shopify_order_id': node['id'], 'shopify_order_name': node.get('name'),
            'shopify_financial_status': (node.get('displayFinancialStatus') or '').lower(),
            'shopify_fulfillment_status': (node.get('displayFulfillmentStatus') or '').lower(),
            'shopify_tags': ', '.join(node.get('tags') or []), 'shopify_currency': code,
            'shopify_gateway': ', '.join(node.get('paymentGatewayNames') or []),
            'order_line': lines,
        }
        order = self.env['sale.order'].with_context(mail_create_nosubscribe=True).create(vals)
        if store.order_tags_to_note:
            bits = []
            if node.get('note'):
                bits.append(_('Customer note: %s') % node['note'])
            if node.get('tags'):
                bits.append(_('Tags: %s') % ', '.join(node['tags']))
            if node.get('sourceName'):
                bits.append(_('Channel: %s') % node['sourceName'])
            if node.get('test'):
                bits.append(_('This is a test order.'))
            if bits:
                order.message_post(body='<br/>'.join(bits))
        helpers.apply_metafields(store, 'order', order, (node.get('metafields') or {}).get('nodes'))
        self._balance(store, order, node, presentment)
        self._apply_workflow(store, order, node)
        return order

    def _balance(self, store, order, node, presentment):
        """Absorb small rounding differences so the Odoo total equals the Shopify total."""
        if node.get('refunds') or not store.discount_product_id:
            return
        total = store._money(node.get('currentTotalPriceSet'), presentment)[0]
        diff = round(total - order.amount_total, 2)
        if diff and abs(diff) <= 0.1:
            tax_field = compat.line_tax_field(self.env)
            self.env['sale.order.line'].create({
                'order_id': order.id, 'product_id': store.discount_product_id.id, 'name': _('Rounding difference'),
                'product_uom_qty': 1, 'price_unit': diff, tax_field: [(5, 0, 0)]})
        elif diff:
            order.message_post(body=_('The Odoo total (%(odoo).2f) differs from the Shopify total (%(shopify).2f). Check taxes and discounts.') % {
                'odoo': order.amount_total, 'shopify': total})
            store._log('order import', _('Total mismatch on %s: Odoo %.2f, Shopify %.2f') % (order.name, order.amount_total, total), 'warning', order.name)

    # ------------------------------------------------------------------ workflow
    def _workflow(self, store, order, node):
        fin = (node.get('displayFinancialStatus') or '').lower()
        names = node.get('paymentGatewayNames') or []
        for name in names:
            gw = store.gateway_ids.filtered(lambda g: g.name == name and g.workflow_id)
            if gw:
                return gw[0].workflow_id
        wf = store.workflow_ids.filtered(lambda w: w.financial_status == fin)
        return wf[:1] or store.workflow_ids.filtered(lambda w: w.financial_status == 'any')[:1]

    def _journal(self, store, wf, node):
        for name in node.get('paymentGatewayNames') or []:
            gw = store.gateway_ids.filtered(lambda g: g.name == name and g.journal_id)
            if gw:
                return gw[0].journal_id
        return wf.payment_journal_id

    def _apply_workflow(self, store, order, node):
        wf = self._workflow(store, order, node)
        if not wf:
            return
        try:
            with self.env.cr.savepoint():
                if wf.auto_confirm:
                    shopify_date = order.date_order
                    order.action_confirm()
                    if order.date_order != shopify_date:
                        # confirming resets the date to now; keep the Shopify order date
                        order.date_order = shopify_date
                if wf.auto_validate_delivery and order.state == 'sale':
                    for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                        compat.validate_picking(picking)
                if wf.create_invoice and order.state == 'sale':
                    invoices = compat.create_invoices(order)
                    if wf.validate_invoice or wf.register_payment:
                        invoices.action_post()
                    if wf.register_payment:
                        journal = self._journal(store, wf, node)
                        if journal:
                            self._register_payment(invoices, journal)
        except Exception as exc:  # noqa: BLE001
            order.shopify_error = str(exc)
            order.message_post(body=_('The automatic workflow stopped: %s') % exc)
            store._log('workflow', '%s: %s' % (order.name, exc), 'warning', order.name)

    def _register_payment(self, invoices, journal):
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=invoices.ids).create({'journal_id': journal.id})
        wizard.action_create_payments()

    # ------------------------------------------------------------------ refunds
    def _import_refunds(self, store, order, node):
        for refund in node.get('refunds') or []:
            rid = refund.get('id')
            if not rid or self.env['account.move'].search_count([('shopify_refund_id', '=', rid)]) or \
                    self.env['axsync.shopify.log'].sudo().search_count([('operation', '=', 'refund'), ('reference', '=', rid)]):
                continue
            items = {}
            for r in (refund.get('refundLineItems') or {}).get('nodes') or []:
                items[(r.get('lineItem') or {}).get('id')] = items.get((r.get('lineItem') or {}).get('id'), 0) + (r.get('quantity') or 0)
            invoices = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state == 'posted')
            amount = store._money(refund.get('totalRefundedSet'))[0]
            if not invoices or not items:
                order.message_post(body=_('Shopify refunded %(amount).2f%(note)s. No credit note was created because %(why)s.') % {
                    'amount': amount, 'note': (' (%s)' % refund['note']) if refund.get('note') else '',
                    'why': _('the order has no posted invoice') if not invoices else _('the refund has no product lines')})
                self._mark_refund_seen(order, rid)
                continue
            self._credit_note(store, order, invoices[0], rid, items, refund)

    def _mark_refund_seen(self, order, rid):
        # keep a tiny marker so the same refund is not announced again
        order.shopify_error = False
        self.env['axsync.shopify.log'].sudo().create({'store_id': order.shopify_store_id.id, 'operation': 'refund', 'level': 'info',
                                                      'message': _('Refund %s noted') % rid, 'reference': rid})

    def _credit_note(self, store, order, invoice, rid, items, refund):
        reversal = invoice._reverse_moves([{'ref': _('Shopify refund %s') % rid, 'invoice_date': fields.Date.context_today(self)}], cancel=False)
        commands = []
        for line in reversal.invoice_line_ids.filtered(lambda l: l.display_type == 'product' if 'display_type' in l._fields else True):
            sale_lines = line.sale_line_ids
            gid = sale_lines[:1].shopify_line_id
            qty = items.get(gid)
            if not qty:
                commands.append((2, line.id))
            else:
                commands.append((1, line.id, {'quantity': qty}))
        reversal.write({'invoice_line_ids': commands, 'shopify_refund_id': rid, 'shopify_store_id': store.id})
        if any(store.workflow_ids.mapped('validate_invoice')):
            reversal.action_post()
        order.message_post(body=_('Credit note %s created for the Shopify refund.') % reversal.display_name)

    # ------------------------------------------------------------------ cancel in Shopify
    def cancel_in_shopify(self, order):
        store = order.shopify_store_id
        store._ensure_writable('order cancel')
        data = store._query(gql.ORDER_CANCEL, {'orderId': order.shopify_order_id, 'reason': 'OTHER', 'refund': False,
                                               'restock': True, 'notifyCustomer': False})
        store._client().check_user_errors(data, 'orderCancel')
        order.shopify_cancelled = True
        order.message_post(body=_('The order was cancelled in Shopify as well.'))
        return True
