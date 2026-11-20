# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
"""An in-memory stand-in for the Shopify Admin GraphQL API.

It is written from the public documentation and answers exactly the operations the connector sends.
It proves the Odoo side (queue, mapping, workflows, security). It cannot prove that field names match
a live shop, that is what the verification checklist in the documentation is for."""
import json
import re
from urllib.parse import parse_qs

GID = 'gid://shopify/%s/%s'


def money(amount, cur='USD'):
    node = {'amount': '%.2f' % amount, 'currencyCode': cur}
    return {'shopMoney': dict(node), 'presentmentMoney': dict(node)}


class MockShopify:

    def __init__(self, domain='demo-shop.myshopify.com', currency='USD'):
        self.domain = domain
        self.currency = currency
        self.calls = []
        self.token_calls = 0
        self.token = 'shpat_mock_1'
        self.token_serial = 1
        self.valid_tokens = {self.token}
        self.client_id, self.client_secret = 'cid', 'csecret'
        self.scopes = ['read_products', 'write_products', 'read_inventory', 'write_inventory', 'read_locations',
                       'read_orders', 'write_orders', 'read_customers', 'write_customers',
                       'read_merchant_managed_fulfillment_orders', 'write_merchant_managed_fulfillment_orders']
        self.locations = [
            {'id': GID % ('Location', 1), 'name': 'Main warehouse', 'isActive': True, 'isFulfillmentService': False, 'address': {'countryCode': 'US'}},
            {'id': GID % ('Location', 2), 'name': 'Pop-up shop', 'isActive': True, 'isFulfillmentService': False, 'address': {'countryCode': 'US'}},
        ]
        self.products, self.customers, self.orders = {}, {}, {}
        self.inventory = {}
        self.webhooks = {}
        self.fulfillments, self.cancelled, self.checkouts, self.payouts = [], [], [], []
        self.fail_next = []
        self.throttle_next = 0
        self.product_set_errors = []
        self.inventory_errors_once = False
        self._seq = 1000

    # -------------------------------------------------------------- helpers
    def nid(self):
        self._seq += 1
        return self._seq

    def ops(self, name):
        return [c for c in self.calls if c[0] == name]

    def rotate_token(self):
        """Simulate an expired token: the old one stops working."""
        self.valid_tokens = set()
        self.token_serial += 1
        self.token = 'shpat_mock_%s' % self.token_serial

    # -------------------------------------------------------------- transport
    def __call__(self, method, url, headers, body, timeout):
        if self.fail_next:
            status, text = self.fail_next.pop(0)
            return status, {'Retry-After': '0'}, text
        if url.endswith('/admin/oauth/access_token'):
            return self._token(body)
        if '/graphql.json' not in url:
            return 404, {}, 'not found'
        if headers.get('X-Shopify-Access-Token') not in self.valid_tokens:
            return 401, {}, '{"errors":"[API] Invalid API key or access token"}'
        payload = json.loads(body)
        if self.throttle_next:
            self.throttle_next -= 1
            return 200, {}, json.dumps({'errors': [{'message': 'Throttled', 'extensions': {'code': 'THROTTLED'}}],
                                        'extensions': {'cost': {'requestedQueryCost': 100, 'throttleStatus': {'currentlyAvailable': 0, 'restoreRate': 50}}}})
        data = self.dispatch(payload['query'], payload.get('variables') or {})
        return 200, {}, json.dumps({'data': data, 'extensions': {'cost': {'requestedQueryCost': 10, 'throttleStatus': {'currentlyAvailable': 990, 'restoreRate': 50}}}})

    def _token(self, body):
        self.token_calls += 1
        form = {k: v[0] for k, v in parse_qs(body).items()}
        if form.get('grant_type') != 'client_credentials' or form.get('client_id') != self.client_id or form.get('client_secret') != self.client_secret:
            return 400, {}, json.dumps({'error': 'shop_not_permitted', 'error_description': 'Client credentials rejected'})
        if 'scope' in form:
            return 400, {}, '{"error":"invalid_request"}'
        self.valid_tokens = {self.token}
        return 200, {}, json.dumps({'access_token': self.token, 'scope': ','.join(self.scopes), 'expires_in': 86399})

    # -------------------------------------------------------------- dispatcher
    def dispatch(self, query, v):
        q = ' '.join(query.split())
        def has(s):
            return s in q
        if has('currentAppInstallation'):
            return self._rec('scopes', v, {'currentAppInstallation': {'accessScopes': [{'handle': s} for s in self.scopes]}})
        if has('shop { name'):
            return self._rec('shop', v, {'shop': {'name': 'Demo Shop', 'myshopifyDomain': self.domain, 'currencyCode': self.currency,
                                                  'ianaTimezone': 'UTC', 'taxesIncluded': False, 'taxShipping': False, 'email': 'owner@example.test'}})
        if has('locations(first'):
            return self._rec('locations', v, {'locations': {'nodes': self.locations, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}})
        if has('productSet('):
            return self._product_set(v)
        if has('productUpdate('):
            return self._product_update(v)
        if has('productDelete('):
            self.products.pop(v['input']['id'], None)
            return self._rec('product_delete', v, {'productDelete': {'deletedProductId': v['input']['id'], 'userErrors': []}})
        if has('products(first'):
            return self._rec('products', v, {'products': self._page(list(self.products.values()), v)})
        if has('product(id'):
            return self._rec('product', v, {'product': self.products.get(v['id'])})
        if has('inventorySetQuantities('):
            return self._inventory_set(v)
        if has('inventoryActivate('):
            self.inventory_errors_once = False
            return self._rec('inventory_activate', v, {'inventoryActivate': {'inventoryLevel': {'id': 'x'}, 'userErrors': []}})
        if has('inventoryItem(id'):
            return self._inventory_levels(v)
        if has('customerCreate('):
            return self._customer_write(v, create=True)
        if has('customerUpdate('):
            return self._customer_write(v, create=False)
        if has('customers(first'):
            return self._rec('customers', v, {'customers': self._page(list(self.customers.values()), v)})
        if has('customer(id'):
            return self._rec('customer', v, {'customer': self.customers.get(v['id'])})
        if has('orderCancel('):
            self.cancelled.append(v['orderId'])
            if v['orderId'] in self.orders:
                self.orders[v['orderId']]['cancelledAt'] = '2026-11-02T10:00:00Z'
            return self._rec('order_cancel', v, {'orderCancel': {'job': {'id': 'j', 'done': True}, 'orderCancelUserErrors': []}})
        if has('fulfillmentCreate('):
            return self._fulfillment_create(v)
        if has('orders(first'):
            return self._rec('orders', v, {'orders': self._page(self._filter_orders(v.get('query')), v)})
        if has('order(id') and has('transactions'):
            return self._rec('order', v, {'order': self.orders.get(v['id'])})
        if has('order(id'):
            order = self.orders.get(v['id'])
            return self._rec('order_fulfillment_orders', v, {'order': {'id': v['id'], 'fulfillmentOrders': order['fulfillmentOrders']} if order else None})
        if has('webhookSubscriptionCreate('):
            gid = GID % ('WebhookSubscription', self.nid())
            self.webhooks[gid] = {'id': gid, 'topic': v['topic'], 'url': v['webhookSubscription']['callbackUrl']}
            return self._rec('webhook_create', v, {'webhookSubscriptionCreate': {'webhookSubscription': {'id': gid, 'topic': v['topic']}, 'userErrors': []}})
        if has('webhookSubscriptionDelete('):
            self.webhooks.pop(v['id'], None)
            return self._rec('webhook_delete', v, {'webhookSubscriptionDelete': {'deletedWebhookSubscriptionId': v['id'], 'userErrors': []}})
        if has('webhookSubscriptions('):
            return self._rec('webhooks', v, {'webhookSubscriptions': {'nodes': list(self.webhooks.values())}})
        if has('abandonedCheckouts('):
            return self._rec('checkouts', v, {'abandonedCheckouts': self._page(self.checkouts, v)})
        if has('shopifyPaymentsAccount'):
            return self._rec('payouts', v, {'shopifyPaymentsAccount': {'payouts': self._page(self.payouts, v)}})
        raise AssertionError('The mock does not know this query: %s' % q[:120])

    def _rec(self, name, variables, data):
        self.calls.append((name, variables))
        return data

    def _page(self, nodes, v):
        first = v.get('first') or 50
        start = int(v.get('after') or 0)
        chunk = nodes[start:start + first]
        more = start + first < len(nodes)
        return {'nodes': chunk, 'pageInfo': {'hasNextPage': more, 'endCursor': str(start + first) if more else None}}

    def _filter_orders(self, query):
        nodes = list(self.orders.values())
        if query:
            m = re.search(r'name:(#?\S+)', query)
            if m:
                nodes = [o for o in nodes if o['name'] == m.group(1)]
        return nodes

    # -------------------------------------------------------------- products
    def _product_set(self, v):
        inp, ident = v['input'], v.get('identifier')
        if self.product_set_errors:
            errs = self.product_set_errors.pop(0)
            return self._rec('product_set', v, {'productSet': {'product': None, 'userErrors': [{'field': ['input'], 'message': errs, 'code': 'INVALID'}]}})
        gid = (ident or {}).get('id') or GID % ('Product', self.nid())
        old = self.products.get(gid)
        variants = []
        for i, var in enumerate(inp.get('variants') or []):
            old_var = None
            if old:
                for ov in old['variants']['nodes']:
                    if [o['value'] for o in ov['selectedOptions']] == [o['name'] for o in var['optionValues']]:
                        old_var = ov
            vid = old_var['id'] if old_var else GID % ('ProductVariant', self.nid())
            iid = old_var['inventoryItem']['id'] if old_var else GID % ('InventoryItem', self.nid())
            variants.append({
                'id': vid, 'sku': (var.get('sku') or ''), 'barcode': var.get('barcode'), 'price': var.get('price'),
                'compareAtPrice': var.get('compareAtPrice'), 'taxable': var.get('taxable', True), 'position': i + 1,
                'selectedOptions': [{'name': o['optionName'], 'value': o['name']} for o in var['optionValues']],
                'image': None, 'inventoryItem': {'id': iid, 'tracked': True, 'measurement': None}})
        node = {'id': gid, 'title': inp['title'], 'handle': re.sub(r'[^a-z0-9]+', '-', inp['title'].lower()).strip('-'),
                'descriptionHtml': inp.get('descriptionHtml', ''), 'vendor': inp.get('vendor', ''), 'productType': inp.get('productType', ''),
                'tags': inp.get('tags') or [], 'status': inp.get('status', 'DRAFT'), 'createdAt': '2026-11-01T00:00:00Z',
                'updatedAt': '2026-11-01T00:00:00Z', 'featuredMedia': None,
                'options': [{'name': o['name'], 'values': [x['name'] for x in o['values']]} for o in inp.get('productOptions') or []],
                'variants': {'nodes': variants}, 'metafields': {'nodes': [
                    {'namespace': m['namespace'], 'key': m['key'], 'value': m['value'], 'type': m['type']} for m in inp.get('metafields') or []]},
                '_files': inp.get('files'), '_seo': inp.get('seo')}
        self.products[gid] = node
        return self._rec('product_set', v, {'productSet': {'product': node, 'userErrors': []}})

    def _product_update(self, v):
        inp = v['input']
        if inp['id'] in self.products:
            self.products[inp['id']]['status'] = inp['status']
        return self._rec('product_update', v, {'productUpdate': {'product': {'id': inp['id'], 'status': inp['status']}, 'userErrors': []}})

    def add_product(self, title, variants, options=None, status='ACTIVE', product_type='', metafields=None):
        """variants: list of dict(sku, price, barcode, options=[values])."""
        gid = GID % ('Product', self.nid())
        options = options or [{'name': 'Title', 'values': ['Default Title']}]
        nodes = []
        for var in variants:
            vals = var.get('options') or ['Default Title']
            nodes.append({'id': GID % ('ProductVariant', self.nid()), 'sku': var.get('sku', ''), 'barcode': var.get('barcode'),
                          'price': '%.2f' % var['price'], 'compareAtPrice': None, 'taxable': True, 'position': 1,
                          'selectedOptions': [{'name': o['name'], 'value': val} for o, val in zip(options, vals)],
                          'image': None, 'inventoryItem': {'id': GID % ('InventoryItem', self.nid()), 'tracked': True, 'measurement': None}})
        node = {'id': gid, 'title': title, 'handle': re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-'), 'descriptionHtml': '',
                'vendor': '', 'productType': product_type, 'tags': [], 'status': status, 'createdAt': '2026-11-01T00:00:00Z',
                'updatedAt': '2026-11-01T00:00:00Z', 'featuredMedia': None, 'options': options, 'variants': {'nodes': nodes},
                'metafields': {'nodes': metafields or []}}
        self.products[gid] = node
        return node

    # -------------------------------------------------------------- inventory
    def _inventory_set(self, v):
        if self.inventory_errors_once:
            self.inventory_errors_once = False
            return self._rec('inventory_set_error', v, {'inventorySetQuantities': {'inventoryAdjustmentGroup': None,
                             'userErrors': [{'field': ['input'], 'message': 'The inventory item is not stocked at the location', 'code': 'ITEM_NOT_STOCKED_AT_LOCATION'}]}})
        assert len(v['input']['quantities']) <= 250
        for q in v['input']['quantities']:
            self.inventory[(q['inventoryItemId'], q['locationId'])] = q['quantity']
        return self._rec('inventory_set', v, {'inventorySetQuantities': {'inventoryAdjustmentGroup': {'id': 'g'}, 'userErrors': []}})

    def _inventory_levels(self, v):
        item = v['id']
        nodes = [{'location': {'id': loc}, 'quantities': [{'name': 'available', 'quantity': qty}]}
                 for (i, loc), qty in self.inventory.items() if i == item]
        return self._rec('inventory_levels', v, {'inventoryItem': {'id': item, 'inventoryLevels': {'nodes': nodes}}})

    # -------------------------------------------------------------- customers
    def _customer_write(self, v, create):
        inp = v['input']
        gid = GID % ('Customer', self.nid()) if create else inp['id']
        node = self.customers.get(gid, {'id': gid, 'tags': [], 'note': None, 'state': 'ENABLED', 'metafields': {'nodes': []},
                                        'defaultAddress': None, 'addresses': []})
        node.update({'email': inp.get('email'), 'firstName': inp.get('firstName'), 'lastName': inp.get('lastName'), 'phone': inp.get('phone')})
        if inp.get('tags'):
            node['tags'] = inp['tags']
        self.customers[gid] = node
        key = 'customerCreate' if create else 'customerUpdate'
        return self._rec('customer_create' if create else 'customer_update', v, {key: {'customer': {'id': gid, 'email': node['email']}, 'userErrors': []}})

    def add_customer(self, email, first, last, tags=None, company=None, phone=None):
        gid = GID % ('Customer', self.nid())
        addr = {'company': company, 'address1': '12 Harbor Road', 'address2': None, 'city': 'Portland', 'province': 'Oregon',
                'provinceCode': 'OR', 'zip': '97201', 'countryCodeV2': 'US', 'phone': phone, 'name': '%s %s' % (first, last)}
        node = {'id': gid, 'email': email, 'firstName': first, 'lastName': last, 'phone': phone, 'tags': tags or [], 'note': None,
                'state': 'ENABLED', 'defaultAddress': addr, 'addresses': [addr], 'metafields': {'nodes': []}}
        self.customers[gid] = node
        return node

    # -------------------------------------------------------------- orders
    def add_order(self, name, lines, customer=None, financial='PAID', fulfillment='UNFULFILLED', gateways=('shopify_payments',),
                  shipping=5.0, tax_rate=0.0, taxes_included=False, discount=0.0, note=None, tags=None, email=None, currency=None,
                  presentment=None, refunds=None, cancelled=False):
        """lines: list of dict(sku, title, qty, price, variant=<variant gid>)."""
        cur = currency or self.currency
        oid = GID % ('Order', self.nid())
        li_nodes, fo_items = [], []
        subtotal = tax_total = 0.0
        for ln in lines:
            lid = GID % ('LineItem', self.nid())
            qty, price = ln['qty'], ln['price']
            disc = ln.get('discount', 0.0)
            line_total = price * qty - disc
            tax = round(line_total * tax_rate / (1 + tax_rate) if taxes_included else line_total * tax_rate, 2)
            subtotal += line_total
            tax_total += tax
            li_nodes.append({'id': lid, 'title': ln['title'], 'name': ln['title'], 'sku': ln.get('sku'), 'quantity': qty, 'currentQuantity': qty,
                             'taxable': tax_rate > 0, 'requiresShipping': True,
                             'variant': {'id': ln.get('variant'), 'sku': ln.get('sku')} if ln.get('variant') else None, 'product': None,
                             'originalUnitPriceSet': money(price, cur), 'discountedUnitPriceSet': money(price - disc / qty, cur),
                             'totalDiscountSet': money(disc, cur),
                             'taxLines': [{'title': 'VAT', 'rate': tax_rate, 'priceSet': money(tax, cur)}] if tax_rate else []})
            fo_items.append({'id': GID % ('FulfillmentOrderLineItem', self.nid()), 'remainingQuantity': qty, 'totalQuantity': qty, 'lineItem': {'id': lid}})
        ship_tax = round(shipping * tax_rate, 2) if tax_rate and not taxes_included else 0.0
        total = subtotal + (0 if taxes_included else tax_total) + shipping + ship_tax
        cust = None
        if customer:
            cust = {k: customer[k] for k in ('id', 'email', 'firstName', 'lastName', 'phone', 'tags')}
            cust['defaultAddress'] = {'company': (customer.get('defaultAddress') or {}).get('company')}
        addr = {'name': 'Ana Buyer', 'firstName': 'Ana', 'lastName': 'Buyer', 'company': None, 'address1': '88 Lake Street', 'address2': None,
                'city': 'Seattle', 'province': 'Washington', 'provinceCode': 'WA', 'zip': '98101', 'countryCodeV2': 'US', 'phone': None}
        order = {
            'id': oid, 'name': name, 'createdAt': '2026-11-01T09:30:00Z', 'updatedAt': '2026-11-01T09:31:00Z', 'processedAt': '2026-11-01T09:30:00Z',
            'cancelledAt': '2026-11-02T10:00:00Z' if cancelled else None, 'cancelReason': None, 'closedAt': None, 'test': False,
            'email': email or (customer or {}).get('email'), 'phone': None, 'note': note, 'tags': tags or [], 'sourceName': 'web',
            'currencyCode': cur, 'presentmentCurrencyCode': presentment or cur, 'taxesIncluded': taxes_included,
            'displayFinancialStatus': financial, 'displayFulfillmentStatus': fulfillment, 'paymentGatewayNames': list(gateways),
            'customer': cust, 'shippingAddress': addr, 'billingAddress': dict(addr),
            'currentTotalPriceSet': money(total, cur), 'totalDiscountsSet': money(discount, cur), 'totalShippingPriceSet': money(shipping, cur),
            'totalTaxSet': money(tax_total + ship_tax, cur),
            'lineItems': {'nodes': li_nodes},
            'shippingLines': {'nodes': [{'id': GID % ('ShippingLine', self.nid()), 'title': 'Standard shipping', 'code': 'std',
                                         'originalPriceSet': money(shipping, cur), 'discountedPriceSet': money(shipping, cur),
                                         'taxLines': [{'title': 'VAT', 'rate': tax_rate, 'priceSet': money(ship_tax, cur)}] if ship_tax else []}] if shipping else []},
            'transactions': [{'id': GID % ('OrderTransaction', self.nid()), 'kind': 'SALE', 'status': 'SUCCESS', 'gateway': gateways[0],
                              'amountSet': money(total, cur)}],
            'fulfillmentOrders': {'nodes': [{'id': GID % ('FulfillmentOrder', self.nid()), 'status': 'OPEN',
                                             'assignedLocation': {'location': {'id': self.locations[0]['id']}}, 'lineItems': {'nodes': fo_items}}]},
            'refunds': refunds or [], 'metafields': {'nodes': []},
        }
        self.orders[oid] = order
        return order

    def add_refund(self, order, line_index, qty, amount):
        li = order['lineItems']['nodes'][line_index]
        order['refunds'].append({
            'id': GID % ('Refund', self.nid()), 'createdAt': '2026-11-03T08:00:00Z', 'note': 'Damaged in transit',
            'totalRefundedSet': money(amount, order['currencyCode']),
            'refundLineItems': {'nodes': [{'id': GID % ('RefundLineItem', self.nid()), 'quantity': qty, 'restockType': 'RETURN',
                                           'subtotalSet': money(amount, order['currencyCode']), 'totalTaxSet': money(0, order['currencyCode']),
                                           'lineItem': {'id': li['id']}}]}})
        order['displayFinancialStatus'] = 'PARTIALLY_REFUNDED'
        return order['refunds'][-1]

    def _fulfillment_create(self, v):
        ful = v['fulfillment']
        for group in ful['lineItemsByFulfillmentOrder']:
            for order in self.orders.values():
                for fo in order['fulfillmentOrders']['nodes']:
                    if fo['id'] != group['fulfillmentOrderId']:
                        continue
                    for item in group['fulfillmentOrderLineItems']:
                        for li in fo['lineItems']['nodes']:
                            if li['id'] == item['id']:
                                li['remainingQuantity'] -= item['quantity']
                    if all(li['remainingQuantity'] == 0 for li in fo['lineItems']['nodes']):
                        fo['status'] = 'CLOSED'
                        order['displayFulfillmentStatus'] = 'FULFILLED'
        self.fulfillments.append(ful)
        info = ful.get('trackingInfo') or {}
        return self._rec('fulfillment_create', v, {'fulfillmentCreate': {'fulfillment': {'id': 'f1', 'status': 'SUCCESS', 'trackingInfo': [info] if info else []},
                                                                         'userErrors': []}})

    def add_checkout(self, email, total, items):
        gid = GID % ('Checkout', self.nid())
        self.checkouts.append({'id': gid, 'createdAt': '2026-11-01T12:00:00Z', 'updatedAt': '2026-11-01T12:30:00Z', 'completedAt': None,
                               'abandonedCheckoutUrl': 'https://%s/checkouts/recover/%s' % (self.domain, gid[-4:]),
                               'customer': {'id': GID % ('Customer', 1), 'email': email, 'firstName': 'Lena', 'lastName': 'Shopper'},
                               'totalPriceSet': money(total), 'lineItems': {'nodes': [{'title': t, 'quantity': q, 'sku': s} for t, q, s in items]}})

    def add_payout(self, net, gross, fee):
        self.payouts.append({'id': GID % ('ShopifyPaymentsPayout', self.nid()), 'issuedAt': '2026-11-04T00:00:00Z', 'status': 'PAID',
                             'net': {'amount': '%.2f' % net, 'currencyCode': self.currency},
                             'summary': {'chargesGross': {'amount': '%.2f' % gross}, 'chargesFee': {'amount': '%.2f' % fee},
                                         'refundsFeeGross': {'amount': '0.00'}}})
