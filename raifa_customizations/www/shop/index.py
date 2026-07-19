# raifa_customizations/www/shop/index.py
#
# get_context() runs once, server-side, when someone visits /shop.
# It server-renders the FIRST batch of categories + featured products
# (so the page has real content immediately, even before JS finishes
# loading), and hands the page its login state + CSRF token.
#
# Everything a visitor does after that first paint - browsing categories,
# searching, viewing a product, adding to cart, checking out - happens via
# the whitelisted API methods in raifa_customizations/api/shop.py, called
# from index.html's JavaScript.

import frappe
from raifa_customizations.api.shop import get_stationery_item_groups, attach_rates, STATIONERY_ROOT_ITEM_GROUP


def get_context(context):
    context.no_cache = 1
    context.categories = get_categories()
    context.featured_items = get_featured_items()
    context.banners = get_banners()
    context.is_logged_in = 1 if frappe.session.user != "Guest" else 0
    context.user_email = frappe.session.user if frappe.session.user != "Guest" else ""
    context.user_fullname = frappe.utils.get_fullname(frappe.session.user) if frappe.session.user != "Guest" else ""
    return context


def get_banners():
    """Homepage promotional banners. Reads from a "Shop Banner" doctype
    that you create yourself via the ERPNext UI (Setup > DocType > New) -
    no code change or deploy needed on your end. Add fields: title (Data),
    image (Attach Image), link_url (Data, optional), sort_order (Int),
    is_active (Check). Returns an empty list gracefully if you haven't
    created it yet, so nothing breaks in the meantime."""
    if not frappe.db.exists("DocType", "Shop Banner"):
        return []
    return frappe.get_all(
        "Shop Banner",
        filters={"is_active": 1},
        fields=["title", "image", "link_url"],
        order_by="sort_order asc",
        limit_page_length=10,
    )


def get_categories():
    groups = frappe.get_all(
        "Item Group",
        filters={"parent_item_group": STATIONERY_ROOT_ITEM_GROUP},
        fields=["name", "item_group_name"],
        order_by="item_group_name asc",
        limit_page_length=200,  # categories are admin-controlled, safe to fetch all rather than cap at a small number
    )
    if not groups:
        # No sub-groups configured under the root group - fall back to
        # showing the root group itself as a single category so the page
        # still has something to display.
        groups = [{"name": STATIONERY_ROOT_ITEM_GROUP, "item_group_name": STATIONERY_ROOT_ITEM_GROUP}]

    for g in groups:
        # Best-effort category thumbnail: first item image found in that group
        g["image"] = frappe.db.get_value(
            "Item", {"item_group": g["name"], "image": ["is", "set"], "disabled": 0}, "image"
        )
    return groups


FEATURED_ITEM_COUNT = 32


def get_featured_items():
    """Homepage "Popular right now" items. If you add a Check field called
    custom_show_on_homepage to the Item doctype (Customize Form), items you
    tick there are shown first - tick/untick any time, no code or deploy
    needed. Until you've flagged anything (or if you never add the field at
    all), this just falls back to your most recently modified items, so it
    always has something sensible to show."""
    filters = {"item_group": ["in", get_stationery_item_groups()], "disabled": 0}
    fields = ["item_code", "item_name", "item_group", "image", "stock_uom"]

    items = []
    meta = frappe.get_meta("Item")
    if meta.has_field("custom_show_on_homepage"):
        featured_filters = dict(filters)
        featured_filters["custom_show_on_homepage"] = 1
        items = frappe.get_all(
            "Item", filters=featured_filters, fields=fields,
            order_by="modified desc", limit_page_length=FEATURED_ITEM_COUNT,
        )

    if len(items) < FEATURED_ITEM_COUNT:
        existing_codes = {i["item_code"] for i in items}
        fallback = frappe.get_all(
            "Item", filters=filters, fields=fields,
            order_by="modified desc", limit_page_length=FEATURED_ITEM_COUNT + len(items),
        )
        for f in fallback:
            if f["item_code"] not in existing_codes:
                items.append(f)
            if len(items) >= FEATURED_ITEM_COUNT:
                break

    attach_rates(items)
    return items