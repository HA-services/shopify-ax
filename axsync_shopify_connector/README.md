# Shopify Odoo Connector

Odoo 16.0 | Category: Sales | License: OPL-1 | Publisher: AXSync (www.axsync.com)

Connect one or more Shopify stores to Odoo. Products, stock, orders, customers, tracking numbers, refunds and cancellations move between the two systems with real-time webhooks, a retry queue and a clear error log.

## Requirements

- Odoo 16.0 with Sales, Inventory (`sale_stock`), Invoicing (`account`) and Discuss (`mail`). No Python library is added.
- A Shopify store and an app that can reach it (see Shopify setup).
- For webhooks: an Odoo that Shopify can reach over public HTTPS. Without it the connector still works by schedule.
- For product images: the same, because Shopify downloads the images from your Odoo.

## Installation

1. Copy the `axsync_shopify_connector` folder into a directory on your Odoo `addons_path` (or add the Git branch `16.0` on Odoo.sh).
2. Restart Odoo and update the Apps list (developer mode, Apps, Update Apps List).
3. Install **Shopify Odoo Connector**.
4. Open Shopify > Dashboard and create a store.

## Shopify setup (Dev Dashboard app)

1. In the Shopify Dev Dashboard create an app and give it these access scopes. Install it on your store (same Shopify organization).
2. Copy the client ID and the client secret.
3. In Odoo, Shopify > Dashboard > New. Enter the store address, the client ID and the client secret. Press **Connect and test**.
4. Check the granted scopes, set the warehouse for each Shopify location (Inventory tab), choose the pricelist (Orders tab).
5. Press **Register webhooks** (needs a public HTTPS base URL in Odoo).

| Scope | Used for |
|---|---|
| `read_products` | Read products and variants |
| `write_products` | Create and update products |
| `read_inventory` | Read stock levels |
| `write_inventory` | Update stock levels |
| `read_locations` | Read locations |
| `read_orders` | Read orders, refunds and abandoned checkouts |
| `write_orders` | Cancel orders |
| `read_customers` | Read customers |
| `write_customers` | Create and update customers |
| `read_merchant_managed_fulfillment_orders` | Read fulfillment orders |
| `write_merchant_managed_fulfillment_orders` | Send tracking numbers (create fulfillments) |
| `read_all_orders` | Orders older than 60 days. Needed for a historical import. |
| `read_shopify_payments_payouts` | Payouts. Needs Shopify Payments on the store. |

Stores that already have a custom app with an Admin API access token can choose "Admin API access token" and paste it together with the app secret key (used to verify webhooks). New legacy tokens can no longer be created in Shopify.

## What moves

| Data | Direction |
|---|---|
| Products and variants | Both ways |
| Prices and compare-at prices | Odoo to Shopify |
| Stock | Odoo to Shopify, and one-click import |
| Orders | Shopify to Odoo |
| Cancellations | Both ways |
| Tracking numbers and partial shipments | Odoo to Shopify |
| Customers | Both ways |
| Refunds | Shopify to Odoo (credit notes) |
| Abandoned checkouts, payouts | Shopify to Odoo |

## Scheduled actions

| Action | Interval |
|---|---|
| Process queue | every 5 minutes |
| Import orders | every 15 minutes |
| Export stock | every 30 minutes |
| Send tracking numbers | every 15 minutes |
| Import products | every hour, off by default |
| Import abandoned checkouts | every hour, off by default |
| Import payouts | every day, off by default |
| Clean old logs | every day |

## Groups

- **Shopify User**: sees stores, orders, products, queue and logs.
- **Shopify Manager**: connects stores, changes settings, runs imports and exports, manages webhooks. Administration Settings includes it.

## Safe mode

Tick **Safe mode** on a store to import without ever writing to Shopify. Use it for the first run against a live store.

## Honest notes

- The automated tests (94 of them) run against a simulated Shopify written from the public API documentation. They prove the Odoo side: mapping, workflows, queue, webhooks, rights. A short checklist for a Shopify development store is in the documentation, please run it before you go live.
- Not included: Shopify Markets price lists, bundles, multi-language product content, customer segments, automatic bank reconciliation of payouts, creating refunds in Shopify from Odoo, editing orders in Shopify.
- Interactive OAuth install for public apps is not implemented. Use a Dev Dashboard app in your own organization or an existing access token.
- Images are exported from the main product image only, as a link that Shopify fetches.
- A product exports up to 100 variants.

