# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json
from datetime import timedelta

from odoo import _, api, fields, models


class ShopifyLocation(models.Model):
    _name = 'axsync.shopify.location'
    _description = 'Shopify location'
    _order = 'store_id, name'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade')
    name = fields.Char(required=True)
    shopify_id = fields.Char('Shopify ID', required=True, index=True)
    is_active = fields.Boolean(default=True)
    is_fulfillment_service = fields.Boolean()
    warehouse_id = fields.Many2one('stock.warehouse', string='Odoo warehouse',
                                   help='Stock of this warehouse is exported to this location and imported from it.')
    export_stock = fields.Boolean('Export stock', default=True)

    _sql_constraints = [('loc_uniq', 'unique(store_id, shopify_id)', 'Location already exists for the store.')]


class ShopifyGateway(models.Model):
    _name = 'axsync.shopify.gateway'
    _description = 'Shopify payment gateway'
    _order = 'store_id, name'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade')
    name = fields.Char('Gateway name', required=True, help='As Shopify names it, for example shopify_payments, paypal or manual.')
    journal_id = fields.Many2one('account.journal', domain="[('type', 'in', ('bank', 'cash'))]",
                                 help='Journal used to register the payment of orders paid with this gateway.')
    workflow_id = fields.Many2one('axsync.shopify.workflow', help='Overrides the workflow that matches the payment status.')

    _sql_constraints = [('gw_uniq', 'unique(store_id, name)', 'Gateway already exists for the store.')]


class ShopifyWorkflow(models.Model):
    _name = 'axsync.shopify.workflow'
    _description = 'Shopify order workflow'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade')
    financial_status = fields.Selection([
        ('any', 'Any'), ('pending', 'Pending'), ('authorized', 'Authorized'), ('paid', 'Paid'),
        ('partially_paid', 'Partially paid'), ('partially_refunded', 'Partially refunded'),
        ('refunded', 'Refunded'), ('voided', 'Voided')], default='paid', required=True)
    auto_confirm = fields.Boolean('Confirm order', default=True)
    auto_validate_delivery = fields.Boolean('Validate delivery', help='Reserve and validate the delivery right away. Use for digital goods or fully managed stock.')
    create_invoice = fields.Boolean('Create invoice')
    validate_invoice = fields.Boolean('Post invoice')
    register_payment = fields.Boolean('Register payment')
    payment_journal_id = fields.Many2one('account.journal', string='Default journal', domain="[('type', 'in', ('bank', 'cash'))]")


class ShopifyTaxMap(models.Model):
    _name = 'axsync.shopify.tax.map'
    _description = 'Shopify tax mapping'
    _order = 'store_id, title, rate'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade')
    title = fields.Char('Tax title', help='Title of the tax line in Shopify, for example VAT. Empty matches any title.')
    rate = fields.Float('Rate %', digits=(16, 4), help='Rate in percent. Matched with a tolerance of 0.01.')
    tax_id = fields.Many2one('account.tax', required=True, domain="[('type_tax_use', '=', 'sale')]")


class ShopifyMetafieldMap(models.Model):
    _name = 'axsync.shopify.metafield.map'
    _description = 'Shopify metafield mapping'
    _order = 'store_id, owner, namespace, key'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade')
    owner = fields.Selection([('order', 'Order'), ('product', 'Product'), ('customer', 'Customer')], required=True, default='order')
    namespace = fields.Char(required=True, default='custom')
    key = fields.Char(required=True)
    odoo_field = fields.Many2one('ir.model.fields', string='Odoo field', required=True, ondelete='cascade',
                                 domain="[('model', 'in', ('sale.order', 'product.template', 'res.partner')), ('ttype', 'in', ('char', 'text', 'integer', 'float', 'boolean')), ('store', '=', True)]")
    direction = fields.Selection([('in', 'Shopify to Odoo'), ('out', 'Odoo to Shopify')], default='in', required=True)


class ShopifyWebhook(models.Model):
    _name = 'axsync.shopify.webhook'
    _description = 'Shopify webhook subscription'
    _order = 'store_id, topic'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade')
    topic = fields.Char(required=True)
    shopify_id = fields.Char('Shopify ID')
    enabled = fields.Boolean(default=True, help='Switch off to ignore deliveries of this topic without touching Shopify.')
    registered_remote = fields.Boolean('Found in Shopify', readonly=True)
    received_count = fields.Integer(readonly=True)
    last_received = fields.Datetime(readonly=True)

    _sql_constraints = [('wh_uniq', 'unique(store_id, topic)', 'Topic already exists for the store.')]


class ShopifyLog(models.Model):
    _name = 'axsync.shopify.log'
    _description = 'Shopify log'
    _order = 'id desc'

    store_id = fields.Many2one('axsync.shopify.store', ondelete='cascade', index=True)
    operation = fields.Char()
    level = fields.Selection([('info', 'Info'), ('warning', 'Warning'), ('error', 'Error')], default='info', index=True)
    message = fields.Text()
    reference = fields.Char()


class ShopifyQueue(models.Model):
    _name = 'axsync.shopify.queue'
    _description = 'Shopify job'
    _order = 'id'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade', index=True)
    kind = fields.Selection([
        ('order', 'Order'), ('product', 'Product'), ('customer', 'Customer'), ('refund', 'Refund'),
        ('fulfillment', 'Fulfillment'), ('inventory', 'Inventory'), ('checkout', 'Checkout'), ('app', 'App')], required=True, index=True)
    topic = fields.Char()
    shopify_id = fields.Char('Shopify ID', index=True)
    payload = fields.Text()
    webhook_id = fields.Char('Webhook ID', index=True)
    state = fields.Selection([('pending', 'Pending'), ('failed', 'Failed, will retry'), ('dead', 'Gave up'), ('done', 'Done')],
                             default='pending', index=True)
    attempts = fields.Integer(readonly=True)
    next_try = fields.Datetime(default=fields.Datetime.now, index=True)
    error = fields.Text(readonly=True)
    done_date = fields.Datetime(readonly=True)

    _sql_constraints = [('wh_uniq', 'unique(store_id, webhook_id)', 'This webhook delivery was already received.')]

    MAX_ATTEMPTS = 6

    @api.model
    def enqueue(self, store, kind, topic, shopify_id, payload=None, webhook_id=None):
        if webhook_id and self.sudo().search_count([('store_id', '=', store.id), ('webhook_id', '=', webhook_id)]):
            return False
        # one open job per object is enough, the job fetches fresh data anyway
        open_job = self.sudo().search([('store_id', '=', store.id), ('kind', '=', kind), ('shopify_id', '=', shopify_id),
                                       ('state', 'in', ('pending', 'failed')), ('topic', '=', topic)], limit=1) if shopify_id else False
        if open_job:
            if webhook_id and not open_job.webhook_id:
                open_job.webhook_id = webhook_id
            return open_job
        return self.sudo().create({
            'store_id': store.id, 'kind': kind, 'topic': topic, 'shopify_id': shopify_id,
            'payload': json.dumps(payload) if payload is not None else False, 'webhook_id': webhook_id})

    def _handler(self):
        self.ensure_one()
        return {
            'order': ('axsync.shopify.svc.order', 'handle_job'),
            'product': ('axsync.shopify.svc.product', 'handle_job'),
            'customer': ('axsync.shopify.svc.customer', 'handle_job'),
            'refund': ('axsync.shopify.svc.order', 'handle_refund_job'),
            'fulfillment': ('axsync.shopify.svc.order', 'handle_fulfillment_job'),
            'inventory': ('axsync.shopify.svc.stock', 'handle_job'),
            'checkout': ('axsync.shopify.svc.misc', 'handle_checkout_job'),
            'app': ('axsync.shopify.svc.misc', 'handle_app_job'),
        }[self.kind]

    def run_job(self):
        self.ensure_one()
        model, method = self._handler()
        payload = json.loads(self.payload) if self.payload else {}
        try:
            with self.env.cr.savepoint():
                getattr(self.env[model], method)(self.store_id, self, payload)
            self.write({'state': 'done', 'done_date': fields.Datetime.now(), 'error': False, 'attempts': self.attempts + 1})
            return True
        except Exception as exc:  # noqa: BLE001
            attempts = self.attempts + 1
            dead = attempts >= self.MAX_ATTEMPTS
            delay = min(2 ** attempts, 120)
            self.write({'state': 'dead' if dead else 'failed', 'attempts': attempts, 'error': str(exc),
                        'next_try': fields.Datetime.now() + timedelta(minutes=delay)})
            self.store_id._log('queue', '%s %s: %s' % (self.kind, self.shopify_id or '', exc), 'error' if dead else 'warning', self.shopify_id or '')
            if getattr(exc, 'summary', None):
                self.store_id._mismatch_activity(exc.summary, exc.note)
            return False

    @api.model
    def process_store(self, store, limit=100):
        jobs = self.search([('store_id', '=', store.id), ('state', 'in', ('pending', 'failed')),
                            ('next_try', '<=', fields.Datetime.now())], order='id', limit=limit)
        done = 0
        for job in jobs:
            if job.run_job():
                done += 1
        return done

    def action_retry(self):
        self.write({'state': 'pending', 'attempts': 0, 'next_try': fields.Datetime.now()})
        return True

    def action_run_now(self):
        for job in self:
            job.run_job()
        return True
