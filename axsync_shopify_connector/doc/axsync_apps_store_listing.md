# Odoo Apps Store listing: copy-ready text (19.0)

## Product title
Shopify Odoo Connector
(Manifest name, 22 characters, the Apps Store limit is 25.)

## Short description (summary)
Two-way Shopify integration: products, stock, orders, customers, tracking and refunds, with webhooks and a retry queue.

## Full product description
Shopify Odoo Connector links one or more Shopify stores to Odoo through the GraphQL Admin API. Products and variants, stock per location, orders with automatic workflows, customers, tracking numbers with partial shipments, refunds as credit notes, cancellations in both directions, abandoned checkouts and payouts. Webhooks are verified with HMAC-SHA256, every event goes through a queue with retries, and unmatched products wait with an activity instead of failing the import.

Problem it solves: retyping orders, copying tracking numbers, stock that drifts and connectors that fail silently on the first unknown SKU or duplicate webhook.

Who should use it: companies that sell on Shopify and run Sales, Inventory and Accounting in Odoo, and the consultants who implement it.

How it works: install the module, create a Dev Dashboard app, paste the client ID and secret in Shopify > Dashboard, press Connect and test, map the locations to warehouses, import, register webhooks.

What makes it different: a queue with replay, safe mode for the first run, tax and total matching, workflows by payment status and gateway, per-store schedules and multi-company support.

## Feature list
- Multi-store and multi-company, GraphQL Admin API, Dev Dashboard client credentials or Admin API token
- Product and variant import and export with images, tags, SEO, metafields and pricelist prices
- Stock export per location with percent, buffer and cap, changed quantities only, stock import
- Order import by webhook or schedule, duplicate protection, taxes, discounts, shipping, currencies
- Workflows by payment status and gateway: confirm, deliver, invoice, post, register payment
- Tracking numbers and partial shipments, cancellations both ways, refunds as credit notes
- Customers both ways, abandoned checkouts with follow-ups, payouts
- Webhooks with signature check, queue with retries, log, dashboard, safe mode

## Keywords
Shopify, ecommerce, connector, integration, GraphQL, webhooks, inventory sync, order import, tracking, multi-store

## Technical specifications
- Technical name: axsync_shopify_connector
- Version: 19.0.1.0.0
- Category: Sales
- License: OPL-1
- Depends: sale_stock, account, mail
- Implementation: Odoo models and a controller for webhooks and signed image links, no changes to standard files, no external Python libraries
- Testing: 94 automated tests on Odoo 19.0 against a simulated Shopify, browser checks on a demo database. A verification checklist for a Shopify development store is in the documentation.

## Supported versions
Odoo 19.0.

## Pricing
Price: 120.00 USD (one-time, per Odoo Apps Store terms).

## Publisher information
Published by AXSync. Website: https://www.axsync.com. This module is not made or endorsed by Shopify Inc.

## Support information
Email hunain@axsync.com, or use Contact the Author on the Apps Store page. See SUPPORT.md.
