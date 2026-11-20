# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
from odoo import _, api, fields, models

from . import gql, helpers
from .client import ShopifyError


class ShopifyCustomer(models.Model):
    _name = 'axsync.shopify.customer'
    _description = 'Shopify customer'
    _order = 'store_id, email'
    _rec_name = 'email'

    store_id = fields.Many2one('axsync.shopify.store', required=True, ondelete='cascade', index=True)
    partner_id = fields.Many2one('res.partner', required=True, ondelete='cascade', index=True)
    shopify_id = fields.Char('Shopify ID', index=True)
    email = fields.Char(index=True)
    last_sync = fields.Datetime()

    _sid_uniq = models.Constraint('UNIQUE(store_id, shopify_id)', 'This Shopify customer is already mapped.')


class ShopifyCustomerService(models.AbstractModel):
    _name = 'axsync.shopify.svc.customer'
    _description = 'Shopify customer synchronisation'

    # ------------------------------------------------------------------ addresses
    def _country(self, code):
        return self.env['res.country'].search([('code', '=', (code or '').upper())], limit=1) if code else self.env['res.country']

    def _state(self, country, code, name):
        if not country:
            return self.env['res.country.state']
        State = self.env['res.country.state']
        rec = State.search([('country_id', '=', country.id), ('code', '=', code)], limit=1) if code else State
        if not rec and name:
            rec = State.search([('country_id', '=', country.id), ('name', '=ilike', name)], limit=1)
        return rec

    def address_vals(self, addr):
        addr = addr or {}
        country = self._country(addr.get('countryCodeV2') or addr.get('countryCode'))
        state = self._state(country, addr.get('provinceCode'), addr.get('province'))
        full = addr.get('name') or ' '.join(p for p in (addr.get('firstName'), addr.get('lastName')) if p)
        return {
            'name': full or addr.get('company') or False,
            'street': addr.get('address1') or False, 'street2': addr.get('address2') or False,
            'city': addr.get('city') or False, 'zip': addr.get('zip') or False,
            'country_id': country.id or False, 'state_id': state.id or False,
            'phone': addr.get('phone') or False,
        }

    def address_partner(self, store, parent, addr, kind):
        """A child contact of parent for one address, reused when the same address was seen before."""
        vals = self.address_vals(addr)
        if not (vals.get('street') or vals.get('city')):
            return parent
        key = [('parent_id', '=', parent.id), ('type', '=', kind), ('street', '=', vals['street']),
               ('zip', '=', vals['zip']), ('city', '=', vals['city']), ('name', '=', vals['name'])]
        found = self.env['res.partner'].with_context(active_test=False).search(key, limit=1)
        if found:
            return found
        vals.update({'parent_id': parent.id, 'type': kind, 'company_id': store.company_id.id})
        if not vals.get('name'):
            vals['name'] = parent.name
        return self.env['res.partner'].with_context(no_vat_validation=True).create(vals)

    # ------------------------------------------------------------------ customers
    def _find_partner(self, store, email, phone=None):
        Partner = self.env['res.partner'].with_context(active_test=False)
        domain = [('company_id', 'in', (False, store.company_id.id))]
        if email:
            rec = Partner.search(domain + [('email', '=ilike', email.strip())], order='parent_id desc, id', limit=1)
            if rec:
                return rec.commercial_partner_id if rec.parent_id and rec.type == 'contact' and rec.commercial_partner_id.is_company is False else rec
        if phone:
            rec = Partner.search(domain + [('phone', '=', phone)], limit=1)
            if rec:
                return rec
        return Partner.browse()

    def partner_from_customer(self, store, node, order_email=None, default_address=None):
        """Find or create the partner for a Shopify customer node and map it."""
        Map = self.env['axsync.shopify.customer']
        gid = (node or {}).get('id')
        mapped = Map.search([('store_id', '=', store.id), ('shopify_id', '=', gid)], limit=1) if gid else Map
        if mapped:
            return mapped.partner_id
        email = ((node or {}).get('email') or order_email or '').strip()
        phone = (node or {}).get('phone')
        partner = self._find_partner(store, email, phone)
        if not partner:
            name = ' '.join(p for p in ((node or {}).get('firstName'), (node or {}).get('lastName')) if p) or email or _('Shopify customer')
            vals = {'name': name, 'email': email or False, 'phone': phone or False, 'customer_rank': 1,
                    'company_id': store.company_id.id}
            addr = default_address or (node or {}).get('defaultAddress') or {}
            company = addr.get('company') or ((node or {}).get('defaultAddress') or {}).get('company')
            Partner = self.env['res.partner'].with_context(no_vat_validation=True)
            if company:
                parent = self.env['res.partner'].search([('name', '=ilike', company), ('parent_id', '=', False)], limit=1)
                parent = parent or Partner.create({'name': company, 'is_company': True, 'customer_rank': 1, 'company_id': store.company_id.id})
                vals['parent_id'] = parent.id
            partner = Partner.create(vals)
        if gid:
            Map.create({'store_id': store.id, 'partner_id': partner.id, 'shopify_id': gid, 'email': email, 'last_sync': fields.Datetime.now()})
        return partner

    def _apply_tags(self, store, partner, node):
        if not store.import_tags_as_categories:
            return
        tags = (node or {}).get('tags') or []
        if isinstance(tags, str):
            tags = helpers.split_tags(tags)
        Category = self.env['res.partner.category']
        ids = []
        for tag in tags:
            cat = Category.search([('name', '=', tag)], limit=1) or Category.create({'name': tag})
            ids.append(cat.id)
        if ids:
            partner.category_id = [(4, i) for i in ids]

    def _upsert(self, store, node):
        partner = self.partner_from_customer(store, node)
        vals = {}
        if node.get('phone') and not partner.phone:
            vals['phone'] = node['phone']
        addr = node.get('defaultAddress')
        if addr and not partner.street:
            av = self.address_vals(addr)
            av.pop('name', None)
            vals.update({k: v for k, v in av.items() if v})
        if vals:
            partner.write(vals)
        self._apply_tags(store, partner, node)
        helpers.apply_metafields(store, 'customer', partner, (node.get('metafields') or {}).get('nodes'))
        self.env['axsync.shopify.customer'].search([('store_id', '=', store.id), ('shopify_id', '=', node['id'])]).write({'last_sync': fields.Datetime.now()})
        return partner

    def import_customers(self, store):
        started = fields.Datetime.now()
        variables = {}
        if store.last_customer_sync:
            variables['query'] = 'updated_at:>%s' % store.last_customer_sync.strftime('%Y-%m-%dT%H:%M:%SZ')
        count = 0
        for node in store._client().paginate(store, gql.CUSTOMERS, ['customers'], variables, page_size=50):
            try:
                with self.env.cr.savepoint():
                    self._upsert(store, node)
                count += 1
            except Exception as exc:  # noqa: BLE001
                store._log('customer import', '%s: %s' % (node.get('email'), exc), 'error', node.get('id'))
        store.last_customer_sync = started
        store._log('customer import', _('%s customers imported or updated') % count)
        return count

    def handle_job(self, store, job, payload):
        data = store._query(gql.CUSTOMER, {'id': job.shopify_id})
        node = data.get('customer')
        if node:
            self._upsert(store, node)

    # ------------------------------------------------------------------ export
    def _split_name(self, name):
        parts = (name or '').split(None, 1)
        return (parts[0] if parts else ''), (parts[1] if len(parts) > 1 else '')

    def _customer_input(self, partner):
        first, last = self._split_name(partner.name)
        inp = {'email': partner.email, 'firstName': first, 'lastName': last}
        if partner.phone and partner.phone.startswith('+'):
            inp['phone'] = partner.phone.replace(' ', '')
        if partner.street and partner.country_id:
            inp['addresses'] = [{'address1': partner.street, 'address2': partner.street2 or '', 'city': partner.city or '',
                                 'zip': partner.zip or '', 'countryCode': partner.country_id.code,
                                 'provinceCode': partner.state_id.code or '', 'company': partner.parent_id.name or '',
                                 'firstName': first, 'lastName': last}]
        tags = partner.category_id.mapped('name')
        if tags:
            inp['tags'] = tags
        meta = helpers.outbound_metafields(partner.env['axsync.shopify.store'].browse(), 'customer', partner) if False else []
        if meta:
            inp['metafields'] = meta
        return inp

    def export_customers(self, store):
        Map = self.env['axsync.shopify.customer']
        partners = self.env['res.partner'].search([('customer_rank', '>', 0), ('email', '!=', False), ('is_company', '=', False),
                                                   ('company_id', 'in', (False, store.company_id.id))])
        count = 0
        for partner in partners:
            mapped = Map.search([('store_id', '=', store.id), ('partner_id', '=', partner.id)], limit=1)
            try:
                with self.env.cr.savepoint():
                    inp = self._customer_input(partner)
                    if mapped and mapped.shopify_id:
                        inp['id'] = mapped.shopify_id
                        data = store._query(gql.CUSTOMER_UPDATE, {'input': inp})
                        store._client().check_user_errors(data, 'customerUpdate')
                        mapped.last_sync = fields.Datetime.now()
                    else:
                        data = store._query(gql.CUSTOMER_CREATE, {'input': inp})
                        store._client().check_user_errors(data, 'customerCreate')
                        cust = (data.get('customerCreate') or {}).get('customer') or {}
                        Map.create({'store_id': store.id, 'partner_id': partner.id, 'shopify_id': cust.get('id'),
                                    'email': partner.email, 'last_sync': fields.Datetime.now()})
                count += 1
            except ShopifyError as exc:
                store._log('customer export', '%s: %s' % (partner.email, exc), 'warning', partner.email)
        return count
