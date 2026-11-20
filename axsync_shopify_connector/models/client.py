# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import json
import logging
import time

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Tests replace this with a function (method, url, headers, body, timeout) -> (status, headers, text)
TRANSPORT = None
SLEEP = time.sleep


class ShopifyError(UserError):
    """Raised for every failure talking to Shopify."""


class ShopifyAuthError(ShopifyError):
    pass


class ShopifyMismatch(ShopifyError):
    """A record on the Shopify side that cannot be matched in Odoo (for example an unknown SKU).

    ``summary`` and ``note`` describe the activity to schedule. The caller creates it after the
    failed work was rolled back, otherwise the rollback would remove it too."""

    def __init__(self, message, summary=None, note=None):
        super().__init__(message)
        self.summary = summary
        self.note = note


def _default_transport(method, url, headers, body, timeout):
    resp = requests.request(method, url, headers=headers, data=body, timeout=timeout)
    return resp.status_code, dict(resp.headers), resp.text


class ShopifyClient(models.AbstractModel):
    _name = 'axsync.shopify.client'
    _description = 'Shopify GraphQL client'

    # ------------------------------------------------------------------ low level
    @api.model
    def _send(self, method, url, headers, body, timeout=60):
        transport = TRANSPORT or _default_transport
        try:
            return transport(method, url, headers, body, timeout)
        except requests.RequestException as exc:
            raise ShopifyError(_('Could not reach Shopify: %s') % exc)

    @api.model
    def fetch_token(self, store):
        """Client credentials grant. Returns (token, expires_in, scope)."""
        url = 'https://%s/admin/oauth/access_token' % store.shop_domain
        body = 'grant_type=client_credentials&client_id=%s&client_secret=%s' % (
            requests.utils.quote(store.client_id or '', safe=''), requests.utils.quote(store.client_secret or '', safe=''))
        status, _headers, text = self._send('POST', url, {'Content-Type': 'application/x-www-form-urlencoded'}, body)
        try:
            data = json.loads(text or '{}')
        except ValueError:
            data = {}
        if status != 200 or not data.get('access_token'):
            msg = data.get('error_description') or data.get('error') or text[:200]
            raise ShopifyAuthError(_('Shopify refused the client credentials (%(status)s): %(msg)s') % {'status': status, 'msg': msg})
        return data['access_token'], int(data.get('expires_in') or 86399), data.get('scope') or ''

    @api.model
    def request(self, store, query, variables=None, retries=4):
        """Run one GraphQL document and return its ``data``. Handles throttling and token refresh."""
        token = store._get_token()
        url = 'https://%s/admin/api/%s/graphql.json' % (store.shop_domain, store.api_version or '2026-10')
        payload = json.dumps({'query': query, 'variables': variables or {}})
        attempt = 0
        while True:
            attempt += 1
            headers = {'Content-Type': 'application/json', 'X-Shopify-Access-Token': token}
            status, resp_headers, text = self._send('POST', url, headers, payload)
            if status == 401:
                if store.auth_mode == 'client' and attempt == 1:
                    store._invalidate_token()
                    token = store._get_token()
                    continue
                raise ShopifyAuthError(_('Shopify rejected the access token (401). Check the credentials and the granted scopes.'))
            if status == 403:
                raise ShopifyAuthError(_('Shopify refused the request (403). The app is missing an access scope or the store is not permitted.'))
            if status in (429, 502, 503, 504):
                if attempt > retries:
                    raise ShopifyError(_('Shopify is busy (HTTP %s). Try again later.') % status)
                try:
                    wait = float((resp_headers or {}).get('Retry-After') or 2)
                except ValueError:
                    wait = 2.0
                SLEEP(min(wait, 10))
                continue
            if status != 200:
                raise ShopifyError(_('Unexpected answer from Shopify (HTTP %(s)s): %(t)s') % {'s': status, 't': (text or '')[:300]})
            try:
                data = json.loads(text)
            except ValueError:
                raise ShopifyError(_('Shopify returned something that is not JSON.'))
            errors = data.get('errors')
            if errors:
                throttled = any((e.get('extensions') or {}).get('code') == 'THROTTLED' for e in errors if isinstance(e, dict))
                if throttled and attempt <= retries:
                    cost = ((data.get('extensions') or {}).get('cost') or {})
                    ts = cost.get('throttleStatus') or {}
                    rate = float(ts.get('restoreRate') or 50)
                    need = float(cost.get('requestedQueryCost') or 100) - float(ts.get('currentlyAvailable') or 0)
                    SLEEP(min(max(need / rate, 0.5), 10))
                    continue
                msgs = '; '.join(str(e.get('message', e)) if isinstance(e, dict) else str(e) for e in errors)
                raise ShopifyError(_('Shopify GraphQL error: %s') % msgs)
            # be gentle when the bucket is nearly empty
            ts = (((data.get('extensions') or {}).get('cost') or {}).get('throttleStatus') or {})
            if ts and float(ts.get('currentlyAvailable', 1000)) < 100:
                SLEEP(min(1.0, 100.0 / float(ts.get('restoreRate') or 50)))
            return data.get('data') or {}

    # ------------------------------------------------------------------ helpers
    @api.model
    def paginate(self, store, query, path, variables=None, page_size=50, limit=None):
        """Yield every node of a connection, following the cursors."""
        variables = dict(variables or {})
        variables['first'] = page_size
        after = None
        count = 0
        while True:
            variables['after'] = after
            data = self.request(store, query, variables)
            conn = data
            for key in path:
                conn = (conn or {}).get(key) or {}
            for node in conn.get('nodes') or []:
                yield node
                count += 1
                if limit and count >= limit:
                    return
            info = conn.get('pageInfo') or {}
            if not info.get('hasNextPage'):
                return
            after = info.get('endCursor')

    @api.model
    def check_user_errors(self, data, root, keys=('userErrors', 'orderCancelUserErrors')):
        part = (data or {}).get(root) or {}
        for key in keys:
            errs = part.get(key) or []
            if errs:
                raise ShopifyError('; '.join('%s%s' % ((', '.join(e.get('field') or []) + ': ') if e.get('field') else '', e.get('message')) for e in errs))
        return part

    @staticmethod
    def gid_to_id(gid):
        return (gid or '').rsplit('/', 1)[-1]

    @staticmethod
    def make_gid(kind, number):
        return 'gid://shopify/%s/%s' % (kind, number)
