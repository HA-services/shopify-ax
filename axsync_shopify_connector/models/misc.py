# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from datetime import timedelta

from odoo import _, api, fields, models

from . import compat, gql
from .client import ShopifyError
from .order import parse_dt


class ShopifyCheckout(models.Model):
    _name = 'axsync.shopify.checkout'
    _description = 'Abandoned checkout'
    _order = 'created_at desc'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade', index=True)
    shopify_id = fields.Char('Shopify ID', index=True)
    email = fields.Char()
    partner_id = fields.Many2one('res.partner')
    customer_name = fields.Char()
    currency = fields.Char()
    total = fields.Float()
    items = fields.Text()
    url = fields.Char('Recovery link')
    created_at = fields.Datetime()
    recovered = fields.Boolean(help='The customer completed the purchase.')
    followed_up = fields.Boolean()

    _sql_constraints = [('sid_uniq', 'unique(store_id, shopify_id)', 'Checkout already imported.')]

    def action_followed_up(self):
        self.write({'followed_up': True})

    def action_create_activity(self):
        for rec in self:
            user = rec.store_id.user_id or self.env.user
            rec.store_id.activity_schedule('mail.mail_activity_data_todo', summary=_('Follow up abandoned checkout'),
                                           note=_('%(who)s left %(items)s (%(total).2f %(cur)s). Recovery link: %(url)s') % {
                                               'who': rec.email or rec.customer_name or _('A visitor'), 'items': (rec.items or '').replace('\n', ', '),
                                               'total': rec.total, 'cur': rec.currency or '', 'url': rec.url or '-'},
                                           user_id=user.id)
            rec.followed_up = True


class ShopifyPayout(models.Model):
    _name = 'axsync.shopify.payout'
    _description = 'Shopify Payments payout'
    _order = 'issued_at desc'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade', index=True)
    shopify_id = fields.Char('Shopify ID', index=True)
    issued_at = fields.Datetime()
    status = fields.Char()
    currency = fields.Char()
    net = fields.Float()
    gross = fields.Float()
    fees = fields.Float()
    refunds = fields.Float()
    statement_ref = fields.Char(compute='_compute_ref', store=True)

    _sql_constraints = [('sid_uniq', 'unique(store_id, shopify_id)', 'Payout already imported.')]

    @api.depends('shopify_id')
    def _compute_ref(self):
        for rec in self:
            rec.statement_ref = 'SHOPIFY-%s' % (rec.shopify_id or '').rsplit('/', 1)[-1]


class ShopifyFulfillmentService(models.AbstractModel):
    _name = 'axsync.shopify.svc.fulfillment'
    _description = 'Shopify tracking export'

    def export_tracking(self, store):
        pickings = self.env['stock.picking'].search([
            ('shopify_store_id', '=', store.id), ('state', '=', 'done'), ('shopify_sent', '=', False),
            ('picking_type_code', '=', 'outgoing')])
        count = 0
        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    if self.send_picking(picking):
                        count += 1
            except ShopifyError as exc:
                store._log('tracking export', '%s: %s' % (picking.name, exc), 'warning', picking.name)
        store.last_tracking_export = fields.Datetime.now()
        return count

    def _tracking(self, picking):
        number = picking.shopify_tracking_number
        company = picking.shopify_tracking_company
        url = picking.shopify_tracking_url
        if not number and 'carrier_tracking_ref' in picking._fields:
            number = picking.carrier_tracking_ref
        if not company and 'carrier_id' in picking._fields and picking.carrier_id:
            company = picking.carrier_id.name
        if not url and 'carrier_tracking_url' in picking._fields:
            url = picking.carrier_tracking_url
        info = {}
        if number:
            info['number'] = number
        if company:
            info['company'] = company
        if url:
            info['url'] = url
        return info

    def send_picking(self, picking):
        store, order = picking.shopify_store_id, picking.sale_id
        store._ensure_writable('tracking export')
        data = store._query(gql.ORDER_FULFILLMENT_ORDERS, {'id': order.shopify_order_id})
        fos = ((data.get('order') or {}).get('fulfillmentOrders') or {}).get('nodes') or []
        done = {}
        for move in picking.move_ids:
            qty = compat.move_done_qty(move)
            gid = move.sale_line_id.shopify_line_id
            if gid and qty:
                done[gid] = done.get(gid, 0) + int(qty)
        groups = []
        for fo in fos:
            if fo.get('status') not in ('OPEN', 'IN_PROGRESS'):
                continue
            items = []
            for li in (fo.get('lineItems') or {}).get('nodes') or []:
                gid = (li.get('lineItem') or {}).get('id')
                remaining = li.get('remainingQuantity') or 0
                want = min(done.get(gid, 0), remaining)
                if want > 0:
                    items.append({'id': li['id'], 'quantity': want})
                    done[gid] -= want
            if items:
                groups.append({'fulfillmentOrderId': fo['id'], 'fulfillmentOrderLineItems': items})
        if not groups:
            picking.write({'shopify_sent': True, 'shopify_sent_date': fields.Datetime.now()})
            picking.message_post(body=_('Nothing left to fulfill in Shopify for this delivery.'))
            return False
        fulfillment = {'notifyCustomer': True, 'lineItemsByFulfillmentOrder': groups}
        info = self._tracking(picking)
        if info:
            fulfillment['trackingInfo'] = info
        res = store._query(gql.FULFILLMENT_CREATE, {'fulfillment': fulfillment})
        store._client().check_user_errors(res, 'fulfillmentCreate')
        picking.write({'shopify_sent': True, 'shopify_sent_date': fields.Datetime.now()})
        picking.message_post(body=_('Sent to Shopify as fulfilled%s.') % (_(' with tracking %s') % info['number'] if info.get('number') else ''))
        return True


class ShopifyMiscService(models.AbstractModel):
    _name = 'axsync.shopify.svc.misc'
    _description = 'Shopify checkouts, payouts and app events'

    def import_checkouts(self, store):
        Checkout = self.env['axsync.shopify.checkout']
        variables = {}
        since = fields.Datetime.now() - timedelta(days=30)
        variables['query'] = 'updated_at:>=%s' % since.strftime('%Y-%m-%dT%H:%M:%SZ')
        count = 0
        for node in store._client().paginate(store, gql.CHECKOUTS, ['abandonedCheckouts'], variables, page_size=50):
            cust = node.get('customer') or {}
            email = cust.get('email')
            partner = self.env['axsync.shopify.svc.customer']._find_partner(store, email) if email else self.env['res.partner']
            amount, cur = store._money(node.get('totalPriceSet'))
            items = '\n'.join('%s x %s' % (li.get('quantity'), li.get('title')) for li in (node.get('lineItems') or {}).get('nodes') or [])
            vals = {'store_id': store.id, 'shopify_id': node['id'], 'email': email, 'partner_id': partner.id or False,
                    'customer_name': ' '.join(p for p in (cust.get('firstName'), cust.get('lastName')) if p) or False,
                    'currency': cur, 'total': amount, 'items': items, 'url': node.get('abandonedCheckoutUrl'),
                    'created_at': parse_dt(node.get('createdAt')), 'recovered': bool(node.get('completedAt'))}
            rec = Checkout.search([('store_id', '=', store.id), ('shopify_id', '=', node['id'])], limit=1)
            if rec:
                rec.write(vals)
            else:
                Checkout.create(vals)
            count += 1
        return count

    def handle_checkout_job(self, store, job, payload):
        self.import_checkouts(store)

    def import_payouts(self, store):
        Payout = self.env['axsync.shopify.payout']
        count = 0
        for node in store._client().paginate(store, gql.PAYOUTS, ['shopifyPaymentsAccount', 'payouts'], page_size=50, limit=200):
            summary = node.get('summary') or {}
            net = node.get('net') or {}
            vals = {'store_id': store.id, 'shopify_id': node['id'], 'issued_at': parse_dt(node.get('issuedAt')),
                    'status': (node.get('status') or '').lower(), 'currency': net.get('currencyCode'),
                    'net': float(net.get('amount') or 0), 'gross': float((summary.get('chargesGross') or {}).get('amount') or 0),
                    'fees': float((summary.get('chargesFee') or {}).get('amount') or 0),
                    'refunds': float((summary.get('refundsFeeGross') or {}).get('amount') or 0)}
            rec = Payout.search([('store_id', '=', store.id), ('shopify_id', '=', node['id'])], limit=1)
            if rec:
                rec.write(vals)
            else:
                Payout.create(vals)
                count += 1
        store.last_payout_sync = fields.Datetime.now()
        return count

    def handle_app_job(self, store, job, payload):
        if job.topic == 'APP_UNINSTALLED':
            store.write({'state': 'error', 'last_error': _('The app was uninstalled from the Shopify store.')})
            store._log('app', _('The app was uninstalled from Shopify'), 'error')
