# -*- coding: utf-8 -*-
# Part of AXSync. See LICENSE file for full copyright and licensing details.
import hashlib
import hmac
import re

META_TYPES = {
    'char': 'single_line_text_field', 'text': 'multi_line_text_field', 'integer': 'number_integer',
    'float': 'number_decimal', 'boolean': 'boolean',
}


def sign_image(env, template_id):
    secret = get_param(env, 'database.secret') or 'axsync'
    return hmac.new(secret.encode(), ('shopify-img-%s' % template_id).encode(), hashlib.sha256).hexdigest()[:32]


def image_url(env, store, template):
    base = (store.image_base_url or get_param(env, 'web.base.url', '')).rstrip('/')
    stamp = int(template.write_date.timestamp()) if template.write_date else 0
    return '%s/axsync_shopify/img/%s/%s/%s.png' % (base, template.id, sign_image(env, template.id), stamp)


def cast_meta(field, value):
    """Convert the string Shopify gives to the python type of the Odoo field."""
    if value is None:
        return False
    if field.ttype == 'integer':
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
    if field.ttype == 'float':
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if field.ttype == 'boolean':
        return str(value).lower() in ('true', '1', 'yes')
    return str(value)


def apply_metafields(store, owner, record, nodes):
    """Write inbound metafields on record according to the store mapping."""
    maps = store.metafield_map_ids.filtered(lambda m: m.owner == owner and m.direction == 'in')
    if not maps or not nodes:
        return
    vals = {}
    for m in maps:
        for node in nodes:
            if node.get('namespace') == m.namespace and node.get('key') == m.key:
                vals[m.odoo_field.name] = cast_meta(m.odoo_field, node.get('value'))
    if vals:
        record.write(vals)


def outbound_metafields(store, owner, record):
    out = []
    for m in store.metafield_map_ids.filtered(lambda m: m.owner == owner and m.direction == 'out'):
        value = record[m.odoo_field.name]
        if value in (False, None, ''):
            continue
        out.append({'namespace': m.namespace, 'key': m.key, 'type': META_TYPES.get(m.odoo_field.ttype, 'single_line_text_field'),
                    'value': str(value).lower() if m.odoo_field.ttype == 'boolean' else str(value)})
    return out


def split_tags(value):
    return [t.strip() for t in re.split(r'[,;]', value or '') if t.strip()]


def get_param(env, key, default=''):
    """Read a system parameter (get_str on Odoo 20, get_param before)."""
    icp = env['ir.config_parameter'].sudo()
    if hasattr(icp, 'get_str'):
        return icp.get_str(key, default)
    return icp.get_param(key, default)


def set_param(env, key, value):
    icp = env['ir.config_parameter'].sudo()
    if hasattr(icp, 'set_str'):
        return icp.set_str(key, value)
    return icp.set_param(key, value)
