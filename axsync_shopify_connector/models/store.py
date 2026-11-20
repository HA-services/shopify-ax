# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import gql
from .client import ShopifyAuthError, ShopifyError
from .helpers import get_param

REQUIRED_SCOPES = [
    ('read_products', 'Read products'), ('write_products', 'Create and update products'),
    ('read_inventory', 'Read stock levels'), ('write_inventory', 'Update stock levels'),
    ('read_locations', 'Read locations'),
    ('read_orders', 'Read orders'), ('write_orders', 'Cancel orders'),
    ('read_customers', 'Read customers'), ('write_customers', 'Create and update customers'),
    ('read_merchant_managed_fulfillment_orders', 'Read fulfillment orders'),
    ('write_merchant_managed_fulfillment_orders', 'Send tracking numbers'),
]
OPTIONAL_SCOPES = [
    ('read_all_orders', 'Orders older than 60 days'), ('read_shopify_payments_payouts', 'Payout import'),
    ('read_assigned_fulfillment_orders', 'Fulfillment service orders'),
]

WEBHOOK_TOPICS = [
    ('ORDERS_CREATE', 'orders/create'), ('ORDERS_UPDATED', 'orders/updated'), ('ORDERS_CANCELLED', 'orders/cancelled'),
    ('REFUNDS_CREATE', 'refunds/create'), ('FULFILLMENTS_CREATE', 'fulfillments/create'),
    ('PRODUCTS_CREATE', 'products/create'), ('PRODUCTS_UPDATE', 'products/update'), ('PRODUCTS_DELETE', 'products/delete'),
    ('CUSTOMERS_CREATE', 'customers/create'), ('CUSTOMERS_UPDATE', 'customers/update'),
    ('INVENTORY_LEVELS_UPDATE', 'inventory_levels/update'),
    ('CHECKOUTS_CREATE', 'checkouts/create'), ('CHECKOUTS_UPDATE', 'checkouts/update'),
    ('APP_UNINSTALLED', 'app/uninstalled'),
]


class ShopifyStore(models.Model):
    _name = 'axsync.shopify.store'
    _description = 'Shopify store'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company, tracking=True)
    shop_domain = fields.Char('Store domain', required=True, tracking=True,
                              help='The permanent address of the store, for example my-shop.myshopify.com.')
    api_version = fields.Char('API version', default='2026-10', required=True,
                              help='Shopify Admin API version used for every request, for example 2026-10.')
    auth_mode = fields.Selection([
        ('client', 'Client credentials (Dev Dashboard app)'),
        ('token', 'Admin API access token'),
    ], string='Authentication', default='client', required=True)
    client_id = fields.Char('Client ID')
    client_secret = fields.Char('Client secret', groups='axsync_shopify_connector.group_axsync_shopify_manager')
    access_token = fields.Char('Access token', groups='axsync_shopify_connector.group_axsync_shopify_manager')
    token_expires = fields.Datetime(readonly=True, copy=False)
    webhook_secret = fields.Char('Webhook secret', groups='axsync_shopify_connector.group_axsync_shopify_manager',
                                 help='Used to verify webhook signatures. For client credentials this is the client secret and can stay empty. '
                                      'With an access token, enter the app secret key.')
    state = fields.Selection([('draft', 'Not connected'), ('connected', 'Connected'), ('error', 'Error')],
                             default='draft', readonly=True, tracking=True)
    last_error = fields.Text(readonly=True)
    shop_name = fields.Char(readonly=True)
    shop_currency_id = fields.Many2one('res.currency', readonly=True)
    taxes_included = fields.Boolean('Prices include tax', readonly=True)
    granted_scopes = fields.Text(readonly=True)
    missing_scopes = fields.Text(readonly=True)
    read_only = fields.Boolean('Safe mode (never write to Shopify)', tracking=True,
                               help='When on, nothing is sent to Shopify. Imports keep working. Useful for the first test with a live store.')

    # defaults for imported orders
    warehouse_id = fields.Many2one('stock.warehouse', domain="[('company_id', '=', company_id)]")
    pricelist_id = fields.Many2one('product.pricelist', string='Pricelist',
                                   help='Used for orders in the store currency and for the prices exported to Shopify.')
    payment_term_id = fields.Many2one('account.payment.term')
    team_id = fields.Many2one('crm.team', string='Sales team')
    user_id = fields.Many2one('res.users', string='Salesperson')
    activity_user_id = fields.Many2one('res.users', string='Responsible for mismatches',
                                       help='Receives an activity when something cannot be matched, for example an unknown SKU.')
    shipping_product_id = fields.Many2one('product.product', string='Shipping product')
    discount_product_id = fields.Many2one('product.product', string='Discount product')
    default_tax_id = fields.Many2one('account.tax', string='Fallback tax', domain="[('type_tax_use', '=', 'sale')]",
                                     help='Used when an order line carries tax but no tax mapping matches. Leave empty to import such lines without tax.')
    order_origin_prefix = fields.Char('Order reference prefix', default='Shopify ')
    auto_create_products = fields.Boolean('Create unknown products on order import', default=False)
    use_presentment_currency = fields.Boolean('Use the customer currency', help='Import orders in the currency the customer paid in when a pricelist with that currency exists.')
    imp_pending = fields.Boolean('Pending', default=True)
    imp_authorized = fields.Boolean('Authorized', default=True)
    imp_paid = fields.Boolean('Paid', default=True)
    imp_partially_paid = fields.Boolean('Partially paid', default=True)
    imp_partially_refunded = fields.Boolean('Partially refunded', default=True)
    imp_refunded = fields.Boolean('Refunded', default=False)
    imp_voided = fields.Boolean('Voided', default=False)
    import_orders_after = fields.Datetime('Import orders created after')
    last_order_sync = fields.Datetime(readonly=True, copy=False)
    last_product_sync = fields.Datetime(readonly=True, copy=False)
    last_customer_sync = fields.Datetime(readonly=True, copy=False)
    last_stock_export = fields.Datetime(readonly=True, copy=False)
    last_tracking_export = fields.Datetime(readonly=True, copy=False)
    last_payout_sync = fields.Datetime(readonly=True, copy=False)
    last_webhook_at = fields.Datetime(readonly=True, copy=False)
    sync_cancellations = fields.Boolean('Cancel Odoo orders cancelled in Shopify', default=True)
    export_cancellations = fields.Boolean('Cancel Shopify orders cancelled in Odoo', default=True)
    import_tags_as_categories = fields.Boolean('Customer tags become partner tags', default=True)
    order_tags_to_note = fields.Boolean('Copy Shopify order tags and note to the order', default=True)

    # product export
    export_as_draft = fields.Boolean('Export new products as draft', default=True)
    vendor_name = fields.Char('Vendor name', help='Shown as the vendor of exported products.')
    price_markup_percent = fields.Float('Price markup %', help='Added on top of the pricelist price when prices are exported.')
    price_rounding = fields.Float('Price rounding', default=0.0, help='For example 0.05 rounds to five cents. 0 means no rounding.')
    compare_at_mode = fields.Selection([('none', 'Do not export'), ('list_price', 'Sales price of the product')],
                                       string='Compare-at price', default='none',
                                       help='When the pricelist price is lower than the sales price, the sales price is sent as the compare-at price.')
    export_images = fields.Boolean('Export product images', default=True)
    image_base_url = fields.Char('Public base URL for images', help='Shopify downloads exported images from this address. Defaults to the system base URL.')
    export_description = fields.Boolean('Export product description', default=True)
    create_missing_categories = fields.Boolean('Create product categories on import', default=True)

    # stock export
    stock_source = fields.Selection([('free', 'Free quantity'), ('onhand', 'On hand quantity'), ('forecast', 'Forecasted quantity')],
                                    string='Quantity to export', default='free', required=True)
    stock_percent = fields.Float('Export percentage', default=100.0)
    stock_buffer = fields.Float('Safety stock', help='Subtracted from every exported quantity.')
    stock_cap = fields.Float('Maximum quantity', help='Never export more than this. 0 means no limit.')
    stock_changed_only = fields.Boolean('Export only changed quantities', default=True)

    # automation
    auto_import_orders = fields.Boolean(default=True)
    auto_export_stock = fields.Boolean(default=True)
    auto_export_tracking = fields.Boolean(default=True)
    auto_import_products = fields.Boolean(default=False)
    auto_import_payouts = fields.Boolean(default=False)
    auto_import_checkouts = fields.Boolean(default=False)
    use_webhooks = fields.Boolean('Real-time webhooks', default=True)
    webhook_url = fields.Char(compute='_compute_webhook_url')

    location_ids = fields.One2many('axsync.shopify.location', 'store_id')
    gateway_ids = fields.One2many('axsync.shopify.gateway', 'store_id')
    workflow_ids = fields.One2many('axsync.shopify.workflow', 'store_id')
    tax_map_ids = fields.One2many('axsync.shopify.tax.map', 'store_id')
    metafield_map_ids = fields.One2many('axsync.shopify.metafield.map', 'store_id')
    webhook_ids = fields.One2many('axsync.shopify.webhook', 'store_id')

    # dashboard
    order_count = fields.Integer(compute='_compute_counts')
    product_count = fields.Integer(compute='_compute_counts')
    customer_count = fields.Integer(compute='_compute_counts')
    queue_pending = fields.Integer(compute='_compute_counts')
    queue_failed = fields.Integer(compute='_compute_counts')
    error_count = fields.Integer(compute='_compute_counts')
    sales_total = fields.Monetary(compute='_compute_counts', currency_field='currency_id')
    currency_id = fields.Many2one('res.currency', compute='_compute_currency')
    token_status = fields.Char(compute='_compute_token_status')

    _domain_uniq = models.Constraint('UNIQUE(shop_domain, company_id)', 'This Shopify store is already connected for the company.')

    # ------------------------------------------------------------------ computes
    @api.depends('shop_currency_id', 'company_id')
    def _compute_currency(self):
        for rec in self:
            rec.currency_id = rec.shop_currency_id or rec.company_id.currency_id

    def _compute_webhook_url(self):
        base = get_param(self.env, 'web.base.url', '')
        for rec in self:
            rec.webhook_url = '%s/axsync_shopify/webhook/%s' % (base.rstrip('/'), rec.id) if rec.id else ''

    def _compute_counts(self):
        Order, Queue, Log = self.env['sale.order'], self.env['axsync.shopify.queue'], self.env['axsync.shopify.log']
        for rec in self:
            orders = Order.search([('shopify_store_id', '=', rec.id)])
            rec.order_count = len(orders)
            rec.sales_total = sum(orders.filtered(lambda o: o.state in ('sale', 'done')).mapped('amount_total'))
            rec.product_count = self.env['axsync.shopify.product'].search_count([('store_id', '=', rec.id)])
            rec.customer_count = self.env['axsync.shopify.customer'].search_count([('store_id', '=', rec.id)])
            rec.queue_pending = Queue.search_count([('store_id', '=', rec.id), ('state', 'in', ('pending', 'failed'))])
            rec.queue_failed = Queue.search_count([('store_id', '=', rec.id), ('state', '=', 'dead')])
            rec.error_count = Log.search_count([('store_id', '=', rec.id), ('level', '=', 'error')])

    @api.depends('token_expires', 'auth_mode', 'state')
    def _compute_token_status(self):
        for rec in self:
            if rec.auth_mode == 'token':
                rec.token_status = _('Static access token')
            elif rec.token_expires:
                rec.token_status = _('Renews automatically, current token valid until %s UTC') % fields.Datetime.to_string(rec.token_expires)
            else:
                rec.token_status = _('No token requested yet')

    # ------------------------------------------------------------------ constraints
    @api.onchange('shop_domain')
    def _onchange_shop_domain(self):
        if self.shop_domain:
            self.shop_domain = self._clean_domain(self.shop_domain)

    @staticmethod
    def _clean_domain(value):
        value = (value or '').strip().lower()
        value = re.sub(r'^https?://', '', value).split('/')[0]
        if value and '.' not in value:
            value += '.myshopify.com'
        return value

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('shop_domain'):
                vals['shop_domain'] = self._clean_domain(vals['shop_domain'])
        stores = super().create(vals_list)
        for store in stores:
            store._set_defaults()
        return stores

    def write(self, vals):
        if vals.get('shop_domain'):
            vals['shop_domain'] = self._clean_domain(vals['shop_domain'])
        creds = {'client_id', 'client_secret', 'access_token', 'shop_domain', 'auth_mode'} & set(vals)
        res = super().write(vals)
        if creds:
            self.sudo().write({'token_expires': False}) if 'access_token' not in vals else None
        return res

    def _set_defaults(self):
        self.ensure_one()
        vals = {}
        if not self.shipping_product_id:
            prod = self.env.ref('axsync_shopify_connector.product_shopify_shipping', raise_if_not_found=False)
            if prod:
                vals['shipping_product_id'] = prod.id
        if not self.discount_product_id:
            prod = self.env.ref('axsync_shopify_connector.product_shopify_discount', raise_if_not_found=False)
            if prod:
                vals['discount_product_id'] = prod.id
        if not self.warehouse_id:
            vals['warehouse_id'] = self.env['stock.warehouse'].search([('company_id', '=', self.company_id.id)], limit=1).id
        if not self.pricelist_id:
            vals['pricelist_id'] = self.env['product.pricelist'].search(
                [('company_id', 'in', (False, self.company_id.id)), ('currency_id', '=', self.company_id.currency_id.id)], limit=1).id
        if not self.workflow_ids:
            self.env['axsync.shopify.workflow'].create([
                {'store_id': self.id, 'name': 'Paid orders: confirm, invoice and register payment', 'financial_status': 'paid',
                 'auto_confirm': True, 'create_invoice': True, 'validate_invoice': True, 'register_payment': True},
                {'store_id': self.id, 'name': 'Pending and authorized: confirm only', 'financial_status': 'pending',
                 'auto_confirm': True},
                {'store_id': self.id, 'name': 'Authorized: confirm only', 'financial_status': 'authorized',
                 'auto_confirm': True},
            ])
        if vals:
            self.write(vals)

    # ------------------------------------------------------------------ authentication
    def _get_token(self):
        self.ensure_one()
        store = self.sudo()
        if store.auth_mode == 'token':
            if not store.access_token:
                raise ShopifyError(_('The store has no access token.'))
            return store.access_token
        if not (store.client_id and store.client_secret):
            raise ShopifyError(_('Enter the client ID and the client secret of the Shopify app.'))
        now = fields.Datetime.now()
        if store.access_token and store.token_expires and store.token_expires > now + timedelta(seconds=120):
            return store.access_token
        token, expires_in, scope = self.env['axsync.shopify.client'].fetch_token(store)
        store.write({'access_token': token, 'token_expires': now + timedelta(seconds=expires_in)})
        if scope:
            store.granted_scopes = scope
        return token

    def _invalidate_token(self):
        self.sudo().write({'access_token': False, 'token_expires': False})

    def _client(self):
        return self.env['axsync.shopify.client']

    def _query(self, query, variables=None):
        self.ensure_one()
        return self._client().request(self, query, variables)

    def _ensure_writable(self, what=''):
        self.ensure_one()
        if self.read_only:
            raise ShopifyError(_('Safe mode is on for %(store)s, so nothing is sent to Shopify%(what)s.') % {
                'store': self.name, 'what': (' (%s)' % what) if what else ''})

    # ------------------------------------------------------------------ connection
    def action_connect(self):
        self.ensure_one()
        try:
            self._get_token()
            data = self._query(gql.SHOP).get('shop') or {}
            vals = {'shop_name': data.get('name'), 'taxes_included': bool(data.get('taxesIncluded')), 'state': 'connected', 'last_error': False}
            cur = self.env['res.currency'].with_context(active_test=False).search([('name', '=', data.get('currencyCode'))], limit=1)
            if cur:
                vals['shop_currency_id'] = cur.id
            if self.auth_mode == 'token' or not self.granted_scopes:
                inst = (self._query(gql.SCOPES).get('currentAppInstallation') or {})
                vals['granted_scopes'] = ', '.join(s['handle'] for s in inst.get('accessScopes') or [])
            self.write(vals)
            granted = set(re.split(r'[,\s]+', self.granted_scopes or ''))
            missing = [h for h, _t in REQUIRED_SCOPES if h not in granted and not (h.startswith('read_') and ('write_' + h[5:]) in granted and False)]
            self.missing_scopes = ', '.join(missing)
            self.action_import_locations()
        except ShopifyError as exc:
            self.write({'state': 'error', 'last_error': str(exc)})
            self._log('connect', str(exc), 'error')
            # no exception: the error state and the log must be saved, so the user sees the reason on the form
            return self._notify(str(exc), 'danger')
        self._log('connect', _('Connected to %s') % (self.shop_name or self.shop_domain))
        return self._notify(_('Connected to %s') % (self.shop_name or self.shop_domain), 'success')

    def _notify(self, message, kind='info'):
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Shopify'), 'message': message, 'type': kind, 'sticky': False}}

    def action_import_locations(self):
        self.ensure_one()
        Location = self.env['axsync.shopify.location']
        seen = set()
        for node in self._client().paginate(self, gql.LOCATIONS, ['locations']):
            seen.add(node['id'])
            loc = Location.search([('store_id', '=', self.id), ('shopify_id', '=', node['id'])], limit=1)
            vals = {'name': node.get('name'), 'is_active': bool(node.get('isActive', True)), 'is_fulfillment_service': bool(node.get('isFulfillmentService'))}
            if loc:
                loc.write(vals)
            else:
                vals.update({'store_id': self.id, 'shopify_id': node['id'], 'warehouse_id': self.warehouse_id.id if len(self.location_ids) == 0 else False,
                             'export_stock': not node.get('isFulfillmentService')})
                Location.create(vals)
        return self._notify(_('%s locations imported') % len(seen), 'success')

    def action_test_connection(self):
        return self.action_connect()

    # ------------------------------------------------------------------ logging
    def _log(self, operation, message, level='info', ref=''):
        for store in self:
            self.env['axsync.shopify.log'].sudo().create({
                'store_id': store.id, 'operation': operation, 'message': message, 'level': level, 'reference': ref})

    def _mismatch_activity(self, summary, note):
        self.ensure_one()
        user = self.activity_user_id or self.user_id or self.env.ref('base.user_admin', raise_if_not_found=False)
        if not user:
            return
        existing = self.activity_ids.filtered(lambda a: a.summary == summary and a.note and note in a.note)
        if existing:
            return
        self.activity_schedule('mail.mail_activity_data_todo', summary=summary, note=note, user_id=user.id)

    # ------------------------------------------------------------------ webhooks
    def action_register_webhooks(self):
        self.ensure_one()
        self._ensure_writable('webhooks')
        url = self.webhook_url
        if not url.startswith('https://'):
            raise UserError(_('Shopify only delivers webhooks to public HTTPS addresses. The system base URL is %s.') % url)
        existing = {w.topic: w for w in self.webhook_ids}
        for topic, _label in WEBHOOK_TOPICS:
            if topic in existing and existing[topic].enabled and existing[topic].shopify_id:
                continue
            if topic.startswith('CHECKOUTS_') and not self.auto_import_checkouts:
                continue
            data = self._query(gql.WEBHOOK_CREATE, {'topic': topic, 'webhookSubscription': {'callbackUrl': url, 'format': 'JSON'}})
            part = (data.get('webhookSubscriptionCreate') or {})
            errs = part.get('userErrors') or []
            sub = part.get('webhookSubscription') or {}
            if errs and not sub:
                msg = '; '.join(e.get('message', '') for e in errs)
                if 'already been taken' in msg.lower():
                    continue
                self._log('webhooks', '%s: %s' % (topic, msg), 'warning')
                continue
            vals = {'store_id': self.id, 'topic': topic, 'shopify_id': sub.get('id'), 'enabled': True}
            if topic in existing:
                existing[topic].write(vals)
            else:
                self.env['axsync.shopify.webhook'].create(vals)
        self._log('webhooks', _('Webhooks registered'))
        return self._notify(_('Webhooks registered'), 'success')

    def action_remove_webhooks(self):
        self.ensure_one()
        self._ensure_writable('webhooks')
        for hook in self.webhook_ids.filtered('shopify_id'):
            try:
                self._query(gql.WEBHOOK_DELETE, {'id': hook.shopify_id})
            except ShopifyError as exc:
                self._log('webhooks', str(exc), 'warning')
            hook.write({'shopify_id': False, 'enabled': False})
        return self._notify(_('Webhooks removed'), 'info')

    def action_sync_webhooks(self):
        """Compare the subscriptions in Shopify with the ones stored here."""
        self.ensure_one()
        data = self._query(gql.WEBHOOKS).get('webhookSubscriptions') or {}
        remote = {n['id']: n for n in data.get('nodes') or []}
        for hook in self.webhook_ids:
            hook.registered_remote = hook.shopify_id in remote
        return self._notify(_('%s subscriptions found in Shopify') % len(remote))

    # ------------------------------------------------------------------ buttons that start work
    def action_import_orders(self):
        self.ensure_one()
        count = self.env['axsync.shopify.svc.order'].import_orders(self)
        return self._notify(_('%s orders queued or imported') % count, 'success')

    def action_import_products(self):
        self.ensure_one()
        count = self.env['axsync.shopify.svc.product'].import_products(self)
        return self._notify(_('%s products imported or updated') % count, 'success')

    def action_export_products(self):
        self.ensure_one()
        self._ensure_writable('product export')
        count = self.env['axsync.shopify.svc.product'].export_products(self)
        return self._notify(_('%s products exported') % count, 'success')

    def action_export_stock(self):
        self.ensure_one()
        self._ensure_writable('stock export')
        count = self.env['axsync.shopify.svc.stock'].export_stock(self, full=True)
        return self._notify(_('%s quantities exported') % count, 'success')

    def action_import_stock(self):
        self.ensure_one()
        count = self.env['axsync.shopify.svc.stock'].import_stock(self)
        return self._notify(_('%s quantities imported') % count, 'success')

    def action_import_customers(self):
        self.ensure_one()
        count = self.env['axsync.shopify.svc.customer'].import_customers(self)
        return self._notify(_('%s customers imported or updated') % count, 'success')

    def action_export_customers(self):
        self.ensure_one()
        self._ensure_writable('customer export')
        count = self.env['axsync.shopify.svc.customer'].export_customers(self)
        return self._notify(_('%s customers exported') % count, 'success')

    def action_export_tracking(self):
        self.ensure_one()
        self._ensure_writable('tracking export')
        count = self.env['axsync.shopify.svc.fulfillment'].export_tracking(self)
        return self._notify(_('%s deliveries sent to Shopify') % count, 'success')

    def action_import_checkouts(self):
        self.ensure_one()
        count = self.env['axsync.shopify.svc.misc'].import_checkouts(self)
        return self._notify(_('%s abandoned checkouts imported') % count, 'success')

    def action_import_payouts(self):
        self.ensure_one()
        count = self.env['axsync.shopify.svc.misc'].import_payouts(self)
        return self._notify(_('%s payouts imported') % count, 'success')

    def action_process_queue(self):
        self.ensure_one()
        done = self.env['axsync.shopify.queue'].process_store(self, limit=200)
        return self._notify(_('%s queue items processed') % done, 'success')

    def action_view_orders(self):
        self.ensure_one()
        return self._action('sale.order', _('Shopify orders'), [('shopify_store_id', '=', self.id)])

    def action_view_queue(self):
        self.ensure_one()
        return self._action('axsync.shopify.queue', _('Queue'), [('store_id', '=', self.id), ('state', 'in', ('pending', 'failed', 'dead'))])

    def action_view_logs(self):
        self.ensure_one()
        return self._action('axsync.shopify.log', _('Logs'), [('store_id', '=', self.id)])

    def action_view_products(self):
        self.ensure_one()
        return self._action('axsync.shopify.product', _('Shopify products'), [('store_id', '=', self.id)])

    def action_view_customers(self):
        self.ensure_one()
        return self._action('axsync.shopify.customer', _('Shopify customers'), [('store_id', '=', self.id)])

    def _action(self, model, name, domain):
        return {'type': 'ir.actions.act_window', 'name': name, 'res_model': model, 'view_mode': 'list,form' if self.env['ir.ui.view'].search_count([('model', '=', model), ('type', '=', 'list')]) else 'tree,form',
                'domain': domain, 'context': {'default_store_id': self.id, 'search_default_store_id': self.id}}

    # ------------------------------------------------------------------ scheduled actions
    @api.model
    def _active_stores(self, flag=None):
        domain = [('state', '=', 'connected')]
        if flag:
            domain.append((flag, '=', True))
        return self.search(domain)

    @api.model
    def _guarded(self, store, operation, func):
        """Run one scheduled step for one store, never letting its failure stop the others."""
        try:
            with self.env.cr.savepoint():
                return func()
        except Exception as exc:  # noqa: BLE001
            store._log(operation, str(exc), 'error')
            if isinstance(exc, ShopifyAuthError):
                store.write({'state': 'error', 'last_error': str(exc)})
            return 0

    @api.model
    def _cron_process_queue(self):
        for store in self._active_stores():
            self.env['axsync.shopify.queue'].process_store(store, limit=100)

    @api.model
    def _cron_import_orders(self):
        for store in self._active_stores('auto_import_orders'):
            self._guarded(store, 'order import', lambda s=store: self.env['axsync.shopify.svc.order'].import_orders(s))

    @api.model
    def _cron_export_stock(self):
        for store in self._active_stores('auto_export_stock'):
            if store.read_only:
                continue
            self._guarded(store, 'stock export', lambda s=store: self.env['axsync.shopify.svc.stock'].export_stock(s))

    @api.model
    def _cron_export_tracking(self):
        for store in self._active_stores('auto_export_tracking'):
            if store.read_only:
                continue
            self._guarded(store, 'tracking export', lambda s=store: self.env['axsync.shopify.svc.fulfillment'].export_tracking(s))

    @api.model
    def _cron_import_products(self):
        for store in self._active_stores('auto_import_products'):
            self._guarded(store, 'product import', lambda s=store: self.env['axsync.shopify.svc.product'].import_products(s, incremental=True))

    @api.model
    def _cron_import_payouts(self):
        for store in self._active_stores('auto_import_payouts'):
            self._guarded(store, 'payout import', lambda s=store: self.env['axsync.shopify.svc.misc'].import_payouts(s))

    @api.model
    def _cron_import_checkouts(self):
        for store in self._active_stores('auto_import_checkouts'):
            self._guarded(store, 'checkout import', lambda s=store: self.env['axsync.shopify.svc.misc'].import_checkouts(s))

    @api.model
    def _cron_cleanup(self):
        limit = fields.Datetime.now() - timedelta(days=60)
        self.env['axsync.shopify.log'].search([('create_date', '<', limit), ('level', '!=', 'error')]).unlink()
        self.env['axsync.shopify.queue'].search([('create_date', '<', limit), ('state', '=', 'done')]).unlink()

    # ------------------------------------------------------------------ helpers shared by services
    def _money(self, node, presentment=False):
        """Amount from a MoneyBag node: (amount, currency code)."""
        if not node:
            return 0.0, self.shop_currency_id.name
        part = node.get('presentmentMoney') if presentment else node.get('shopMoney')
        part = part or node.get('shopMoney') or {}
        return float(part.get('amount') or 0.0), part.get('currencyCode')

    def _webhook_secret_value(self):
        self.ensure_one()
        store = self.sudo()
        return store.webhook_secret or store.client_secret or ''
