# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
{
    'name': 'Shopify Odoo Connector',
    'summary': 'Two-way Shopify integration: products, stock, orders, customers, tracking, refunds, with webhooks and a retry queue',
    'description': 'AXSync Shopify Connector. Connect one or more Shopify stores to Odoo with the GraphQL Admin API: '
                   'products and variants, stock per location, orders with automatic workflows, customers, tracking '
                   'numbers, refunds, cancellations, abandoned checkouts and payouts. Real-time webhooks with signature '
                   'verification, a retry queue and a clear error log.',
    'version': '17.0.1.0.0',
    'category': 'Sales/Sales',
    'author': 'AXSync',
    'website': 'https://www.axsync.com',
    'support': 'hunain@axsync.com',
    'license': 'OPL-1',
    'price': 120.0,
    'currency': 'USD',
    'depends': ['sale_stock', 'account', 'mail'],
    'data': [
        'security/axsync_shopify_groups.xml',
        'security/ir.model.access.csv',
        'security/axsync_shopify_rules.xml',
        'data/axsync_shopify_data.xml',
        'data/axsync_shopify_cron.xml',
        'views/store_views.xml',
        'views/data_views.xml',
        'views/inherit_views.xml',
        'views/menus.xml',
    ],
    'images': [
        'static/description/axsync_banner.png',
        'static/description/axsync_card.png',
    ],
    'application': True,
    'installable': True,
    'auto_install': False,
}
