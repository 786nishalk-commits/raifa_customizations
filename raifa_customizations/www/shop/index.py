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
    context.is_logged_in = 1 if frappe.session.user != "Guest" else 0
    context.user_email = frappe.session.user if frappe.session.user != "Guest" else ""
    context.user_fullname = frappe.utils.get_fullname(frappe.session.user) if frappe.session.user != "Guest" else ""
    return context


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


def get_featured_items():
    items = frappe.get_all(
        "Item",
        filters={"item_group": ["in", get_stationery_item_groups()], "disabled": 0},
        fields=["item_code", "item_name", "item_group", "image", "stock_uom"],
        order_by="modified desc",
        limit_page_length=8,
    )
    attach_rates(items)
    return items
