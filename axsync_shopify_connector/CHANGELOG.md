# Changelog

## 19.0.1.0.0 (initial release)

- Multiple Shopify stores and companies, GraphQL Admin API (default version 2026-10, set per store).
- Connection with the Dev Dashboard client credentials grant (token renewed automatically) or a pasted Admin API access token. Scope check and location import.
- Products and variants: import by SKU or barcode, optional creation with options, export as draft or active with images, tags, vendor, SEO, metafields, compare-at price, pricelist prices with markup and rounding, publish, unpublish and archive.
- Stock: free, on hand or forecast, percent, safety stock and cap, per location and warehouse, only changed quantities, batches of 100, import from Shopify.
- Orders: webhook and scheduled import, payment status filters, duplicate protection, customers, addresses, discounts, shipping lines, tax mapping, currencies, workflows by payment status and gateway (confirm, validate delivery, invoice, post, register payment).
- Tracking numbers and partial shipments sent through fulfillment orders.
- Cancellations both ways, refunds as credit notes, abandoned checkouts with follow-up activities, payouts.
- Webhooks: registration, HMAC-SHA256 check, duplicate check, per-topic switch, privacy topics logged.
- Queue with retries and growing delay, replay, activities for unmatched products, log, dashboard with counters.
- Safe mode, per-store schedules, import one order wizard, metafield mapping, sales pivot and graph.
