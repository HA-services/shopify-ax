# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
"""GraphQL documents used by the connector. Kept in one place so that they can be reviewed
and adapted in a single file when Shopify changes its Admin API."""

MONEY = 'shopMoney { amount currencyCode } presentmentMoney { amount currencyCode }'

SHOP = '''query { shop { name myshopifyDomain currencyCode ianaTimezone taxesIncluded taxShipping email } }'''

SCOPES = '''query { currentAppInstallation { accessScopes { handle } } }'''

LOCATIONS = '''query ($first: Int!, $after: String) {
  locations(first: $first, after: $after) {
    nodes { id name isActive isFulfillmentService address { countryCode } }
    pageInfo { hasNextPage endCursor }
  }
}'''

PRODUCT_FIELDS = '''
  id title handle descriptionHtml vendor productType tags status createdAt updatedAt
  featuredMedia { ... on MediaImage { image { url } } }
  options { name values }
  variants(first: 100) { nodes {
    id sku barcode price compareAtPrice taxable position
    selectedOptions { name value }
    image { url }
    inventoryItem { id tracked measurement { weight { value unit } } }
  } }
  metafields(first: 50) { nodes { namespace key value type } }
'''

PRODUCTS = '''query ($first: Int!, $after: String, $query: String) {
  products(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}''' % PRODUCT_FIELDS

PRODUCT = '''query ($id: ID!) { product(id: $id) { %s } }''' % PRODUCT_FIELDS

PRODUCT_SET = '''mutation ($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(synchronous: true, input: $input, identifier: $identifier) {
    product { id handle status variants(first: 100) { nodes { id sku inventoryItem { id } selectedOptions { name value } } } }
    userErrors { field message code }
  }
}'''

PRODUCT_STATUS = '''mutation ($input: ProductUpdateInput!) {
  productUpdate(product: $input) { product { id status } userErrors { field message } }
}'''

PRODUCT_DELETE = '''mutation ($input: ProductDeleteInput!) {
  productDelete(input: $input) { deletedProductId userErrors { field message } }
}'''

INVENTORY_SET = '''mutation ($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { id }
    userErrors { field message code }
  }
}'''

INVENTORY_ACTIVATE = '''mutation ($inventoryItemId: ID!, $locationId: ID!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) {
    inventoryLevel { id }
    userErrors { field message }
  }
}'''

INVENTORY_LEVELS = '''query ($id: ID!) {
  inventoryItem(id: $id) {
    id
    inventoryLevels(first: 50) { nodes { location { id } quantities(names: ["available"]) { name quantity } } }
  }
}'''

CUSTOMER_FIELDS = '''
  id email firstName lastName phone tags note state
  defaultAddress { company address1 address2 city province provinceCode zip countryCodeV2 phone name }
  addresses { company address1 address2 city province provinceCode zip countryCodeV2 phone name }
  metafields(first: 50) { nodes { namespace key value type } }
'''

CUSTOMERS = '''query ($first: Int!, $after: String, $query: String) {
  customers(first: $first, after: $after, query: $query) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}''' % CUSTOMER_FIELDS

CUSTOMER = '''query ($id: ID!) { customer(id: $id) { %s } }''' % CUSTOMER_FIELDS

CUSTOMER_CREATE = '''mutation ($input: CustomerInput!) {
  customerCreate(input: $input) { customer { id email } userErrors { field message } }
}'''

CUSTOMER_UPDATE = '''mutation ($input: CustomerInput!) {
  customerUpdate(input: $input) { customer { id email } userErrors { field message } }
}'''

ADDRESS = 'name firstName lastName company address1 address2 city province provinceCode zip countryCodeV2 phone'

ORDER_FIELDS = '''
  id name createdAt updatedAt processedAt cancelledAt cancelReason closedAt test
  email phone note tags sourceName currencyCode presentmentCurrencyCode taxesIncluded
  displayFinancialStatus displayFulfillmentStatus paymentGatewayNames
  customer { id email firstName lastName phone tags defaultAddress { company } }
  shippingAddress { %(addr)s }
  billingAddress { %(addr)s }
  currentTotalPriceSet { %(money)s }
  totalDiscountsSet { %(money)s }
  totalShippingPriceSet { %(money)s }
  totalTaxSet { %(money)s }
  lineItems(first: 100) { nodes {
    id title name sku quantity currentQuantity taxable requiresShipping
    variant { id sku }
    product { id }
    originalUnitPriceSet { %(money)s }
    discountedUnitPriceSet { %(money)s }
    totalDiscountSet { %(money)s }
    taxLines { title rate priceSet { %(money)s } }
  } }
  shippingLines(first: 10) { nodes {
    id title code
    originalPriceSet { %(money)s }
    discountedPriceSet { %(money)s }
    taxLines { title rate priceSet { %(money)s } }
  } }
  transactions(first: 20) { id kind status gateway amountSet { %(money)s } }
  fulfillmentOrders(first: 20) { nodes {
    id status assignedLocation { location { id } }
    lineItems(first: 100) { nodes { id remainingQuantity totalQuantity lineItem { id } } }
  } }
  refunds { id createdAt note
    totalRefundedSet { %(money)s }
    refundLineItems(first: 100) { nodes { id quantity restockType subtotalSet { %(money)s } totalTaxSet { %(money)s } lineItem { id } } }
  }
  metafields(first: 50) { nodes { namespace key value type } }
''' % {'addr': ADDRESS, 'money': MONEY}

ORDERS = '''query ($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {
    nodes { %s }
    pageInfo { hasNextPage endCursor }
  }
}''' % ORDER_FIELDS

ORDER = '''query ($id: ID!) { order(id: $id) { %s } }''' % ORDER_FIELDS

ORDER_FULFILLMENT_ORDERS = '''query ($id: ID!) {
  order(id: $id) {
    id
    fulfillmentOrders(first: 20) { nodes {
      id status
      lineItems(first: 100) { nodes { id remainingQuantity lineItem { id } } }
    } }
  }
}'''

FULFILLMENT_CREATE = '''mutation ($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status trackingInfo { number company url } }
    userErrors { field message }
  }
}'''

ORDER_CANCEL = '''mutation ($orderId: ID!, $reason: OrderCancelReason!, $refund: Boolean!, $restock: Boolean!, $notifyCustomer: Boolean) {
  orderCancel(orderId: $orderId, reason: $reason, refund: $refund, restock: $restock, notifyCustomer: $notifyCustomer) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}'''

WEBHOOKS = '''query { webhookSubscriptions(first: 100) { nodes { id topic endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } } }'''

WEBHOOK_CREATE = '''mutation ($topic: WebhookSubscriptionTopic!, $webhookSubscription: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
    webhookSubscription { id topic }
    userErrors { field message }
  }
}'''

WEBHOOK_DELETE = '''mutation ($id: ID!) {
  webhookSubscriptionDelete(id: $id) { deletedWebhookSubscriptionId userErrors { field message } }
}'''

CHECKOUTS = '''query ($first: Int!, $after: String, $query: String) {
  abandonedCheckouts(first: $first, after: $after, query: $query) {
    nodes {
      id createdAt updatedAt completedAt abandonedCheckoutUrl
      customer { id email firstName lastName }
      totalPriceSet { %s }
      lineItems(first: 20) { nodes { title quantity sku } }
    }
    pageInfo { hasNextPage endCursor }
  }
}''' % MONEY

PAYOUTS = '''query ($first: Int!, $after: String) {
  shopifyPaymentsAccount {
    payouts(first: $first, after: $after, reverse: true) {
      nodes {
        id issuedAt status
        net { amount currencyCode }
        summary { chargesGross { amount } chargesFee { amount } refundsFeeGross { amount } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}'''
