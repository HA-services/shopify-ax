# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import base64
import hashlib
import hmac
import json
import logging

from odoo import fields, http
from odoo.http import request, Response

from ..models import helpers

_logger = logging.getLogger(__name__)

TOPIC_KIND = {
    'ORDERS_': 'order', 'REFUNDS_': 'refund', 'FULFILLMENTS_': 'fulfillment', 'PRODUCTS_': 'product',
    'CUSTOMERS_': 'customer', 'INVENTORY_LEVELS_': 'inventory', 'CHECKOUTS_': 'checkout', 'APP_': 'app',
}
GDPR_TOPICS = ('CUSTOMERS_DATA_REQUEST', 'CUSTOMERS_REDACT', 'SHOP_REDACT')


def verify_signature(secret, body, header):
    if not secret or not header:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, header.strip())


def job_target(topic, payload):
    """Map a webhook delivery to (queue kind, Shopify id) or None."""
    kind = next((k for prefix, k in TOPIC_KIND.items() if topic.startswith(prefix)), None)
    if not kind:
        return None
    if kind in ('refund', 'fulfillment'):
        order_id = payload.get('order_id')
        return kind, ('gid://shopify/Order/%s' % order_id) if order_id else None
    if kind == 'inventory':
        return kind, str(payload.get('inventory_item_id') or '')
    if kind == 'app':
        return kind, 'app'
    gid = payload.get('admin_graphql_api_id')
    if not gid and payload.get('id'):
        label = {'order': 'Order', 'product': 'Product', 'customer': 'Customer', 'checkout': 'Checkout'}[kind]
        gid = 'gid://shopify/%s/%s' % (label, payload['id'])
    if not gid and kind == 'checkout':
        gid = payload.get('token')
    return kind, gid


class ShopifyController(http.Controller):

    def _reply(self, code, text=''):
        return Response(text, status=code, content_type='text/plain')

    @http.route('/axsync_shopify/webhook/<int:store_id>', type='http', auth='public', methods=['POST'], csrf=False, save_session=False, readonly=False)
    def webhook(self, store_id, **kw):
        store = request.env['axsync.shopify.store'].sudo().browse(store_id).exists()
        if not store:
            return self._reply(404, 'unknown store')
        body = request.httprequest.get_data()
        header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256') or request.httprequest.headers.get('X-Shopify-Hmac-SHA256')
        if not verify_signature(store._webhook_secret_value(), body, header):
            _logger.warning('Shopify webhook with a bad signature for store %s', store_id)
            return self._reply(401, 'bad signature')
        topic = (request.httprequest.headers.get('X-Shopify-Topic') or '').upper().replace('/', '_')
        webhook_id = request.httprequest.headers.get('X-Shopify-Webhook-Id') or request.httprequest.headers.get('X-Shopify-Event-Id')
        try:
            payload = json.loads(body.decode('utf-8') or '{}')
        except ValueError:
            return self._reply(400, 'bad json')
        store.write({'last_webhook_at': fields.Datetime.now()})
        if topic in GDPR_TOPICS:
            store._log('gdpr', 'Received %s for %s' % (topic.lower(), payload.get('shop_domain') or store.shop_domain), 'warning')
            return self._reply(200, 'logged')
        hook = store.webhook_ids.filtered(lambda w: w.topic == topic)[:1]
        if hook:
            hook.sudo().write({'received_count': hook.received_count + 1, 'last_received': fields.Datetime.now()})
            if not hook.enabled:
                return self._reply(200, 'topic disabled')
        if not store.use_webhooks:
            return self._reply(200, 'webhooks disabled')
        target = job_target(topic, payload)
        if not target or not target[1]:
            return self._reply(200, 'ignored')
        kind, gid = target
        keep = payload if kind in ('inventory', 'app') else None
        request.env['axsync.shopify.queue'].sudo().enqueue(store, kind, topic, gid, payload=keep, webhook_id=webhook_id)
        try:
            cron = request.env.ref('axsync_shopify_connector.cron_axsync_shopify_queue', raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()
        except Exception:  # noqa: BLE001
            pass
        return self._reply(200, 'queued')

    @http.route('/axsync_shopify/img/<int:tid>/<string:token>/<string:stamp>.png', type='http', auth='public', methods=['GET'], csrf=False)
    def image(self, tid, token, stamp, **kw):
        env = request.env
        if not hmac.compare_digest(token, helpers.sign_image(env, tid)):
            return self._reply(403, 'forbidden')
        tmpl = env['product.template'].sudo().browse(tid).exists()
        if not tmpl or not tmpl.image_1920:
            return self._reply(404, 'no image')
        raw = tmpl.image_1920
        from odoo.tools.image import image_process
        if hasattr(raw, 'content'):
            # Odoo 20: binary fields return a value object holding the bytes
            data = raw.content
            try:
                data = image_process(data, size=(2048, 2048), output_format='PNG') or data
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                raw = image_process(raw, size=(2048, 2048), output_format='PNG')
            except Exception:  # noqa: BLE001
                pass
            data = base64.b64decode(raw)
        return Response(data, content_type='image/png', headers=[('Cache-Control', 'public, max-age=86400')])
