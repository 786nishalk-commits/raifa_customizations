# raifa_customizations/api/shop.py
#
# Whitelisted API methods that power the /shop storefront page.
# Called from the browser as:
#   /api/method/raifa_customizations.api.shop.<method_name>
#
# ============================================================================
# PLEASE CONFIRM THE FOUR CONSTANTS BELOW BEFORE GOING LIVE.
# I don't have direct access to your ERPNext instance, so these are my best
# assumptions based on what's in our chat history. Everything else in this
# file is standard Frappe/ERPNext framework behaviour I'm confident about,
# but these four are business-data specific to Raifa Centre and should be
# checked against your actual setup first. See SETUP_README.md.
# ============================================================================
STATIONERY_ROOT_ITEM_GROUP = "Stationery - Barwa"   # Parent Item Group for the products this shop sells
DEFAULT_PRICE_LIST = "Standard Selling"             # Selling > Price List name used to look up rates
DEFAULT_CUSTOMER_GROUP = "Individual"               # Customer Group assigned to new website customers
DEFAULT_TERRITORY = "Qatar"                         # Territory assigned to new website customers
# ============================================================================

import frappe
from frappe import _
from frappe.utils import flt, cint, nowdate, add_days

DELIVERY_LEAD_DAYS = 2


# ---------------------------------------------------------------------------
# Helpers (not whitelisted — internal use only)
# ---------------------------------------------------------------------------

def get_stationery_item_groups():
    """Root group + any direct child groups. If you don't use sub-groups at
    all, this just returns the root group on its own, which is fine."""
    groups = [STATIONERY_ROOT_ITEM_GROUP]
    children = frappe.get_all(
        "Item Group",
        filters={"parent_item_group": STATIONERY_ROOT_ITEM_GROUP},
        pluck="name",
    )
    groups.extend(children)
    return groups


def attach_rates(items, price_list=DEFAULT_PRICE_LIST):
    """Batch-fetches Item Price for a list of item dicts and adds a 'rate'
    key to each, in a single query (avoids N+1 queries on large catalogues)."""
    if not items:
        return items
    codes = [i["item_code"] for i in items]
    prices = frappe.get_all(
        "Item Price",
        filters={"item_code": ["in", codes], "price_list": price_list, "selling": 1},
        fields=["item_code", "price_list_rate"],
    )
    price_map = {}
    for p in prices:
        # first match wins if there happen to be duplicate Item Price rows
        price_map.setdefault(p["item_code"], p["price_list_rate"])
    for i in items:
        i["rate"] = flt(price_map.get(i["item_code"], 0))
    return items


def get_authoritative_rate(item_code, price_list=DEFAULT_PRICE_LIST):
    """Used ONLY at order-placement time. Never trust a price sent from the
    browser — always re-look-up the real price server-side, otherwise
    someone could edit the page and submit a fake price."""
    rate = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": price_list, "selling": 1},
        "price_list_rate",
    )
    return flt(rate or 0)


def get_or_create_customer(name, mobile, email=None):
    """Matches an existing Customer by mobile number (via Contact Phone),
    or creates a new Customer + Contact pair for a guest checkout.
    This uses standard Frappe Contact / Dynamic Link schema (Contact has a
    child table of phone numbers, and Dynamic Link rows connect a Contact to
    a Customer) — this part of the framework hasn't changed in a long time,
    so I'm confident in it, but it's still worth testing once on a
    non-production site before relying on it."""
    contact_name = frappe.db.get_value("Contact Phone", {"phone": mobile}, "parent")
    if contact_name:
        customer = frappe.db.get_value(
            "Dynamic Link",
            {"parenttype": "Contact", "parent": contact_name, "link_doctype": "Customer"},
            "link_name",
        )
        if customer and frappe.db.exists("Customer", customer):
            return customer

    customer_doc = frappe.new_doc("Customer")
    customer_doc.customer_name = name
    customer_doc.customer_type = "Individual"
    customer_doc.customer_group = DEFAULT_CUSTOMER_GROUP
    customer_doc.territory = DEFAULT_TERRITORY
    customer_doc.insert(ignore_permissions=True)

    contact_doc = frappe.new_doc("Contact")
    contact_doc.first_name = name or mobile
    contact_doc.append("phone_nos", {"phone": mobile, "is_primary_phone": 1})
    if email:
        contact_doc.append("email_ids", {"email_id": email, "is_primary": 1})
    contact_doc.append("links", {"link_doctype": "Customer", "link_name": customer_doc.name})
    contact_doc.insert(ignore_permissions=True)

    return customer_doc.name


# ---------------------------------------------------------------------------
# Whitelisted endpoints
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def get_products(item_group=None, search=None, sort="name_asc", page=1, page_size=24):
    """Paginated, searchable, sortable product listing. Guest-accessible
    (read-only) so anyone can browse without logging in."""
    page = cint(page) or 1
    page_size = min(cint(page_size) or 24, 60)

    valid_groups = get_stationery_item_groups()
    filters = {"disabled": 0, "item_group": ["in", valid_groups]}
    if item_group and item_group in valid_groups:
        filters["item_group"] = item_group

    or_filters = None
    if search:
        search = search.strip()
        or_filters = {
            "item_name": ["like", f"%{search}%"],
            "item_code": ["like", f"%{search}%"],
        }

    order_by = "item_name asc"
    if sort == "newest":
        order_by = "creation desc"

    total = frappe.db.count("Item", filters=filters)

    items = frappe.get_all(
        "Item",
        filters=filters,
        or_filters=or_filters,
        fields=["item_code", "item_name", "item_group", "image", "stock_uom", "description"],
        order_by=order_by,
        limit_start=(page - 1) * page_size,
        limit_page_length=page_size,
    )
    attach_rates(items)

    # Price sort happens after fetch since price lives in a separate doctype
    if sort == "price_asc":
        items.sort(key=lambda i: i["rate"])
    elif sort == "price_desc":
        items.sort(key=lambda i: i["rate"], reverse=True)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@frappe.whitelist(allow_guest=True)
def get_product(item_code):
    """Single product detail + up to 4 related items from the same group."""
    valid_groups = get_stationery_item_groups()
    item = frappe.db.get_value(
        "Item",
        {"item_code": item_code, "disabled": 0, "item_group": ["in", valid_groups]},
        ["item_code", "item_name", "item_group", "description", "image", "stock_uom"],
        as_dict=True,
    )
    if not item:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    attach_rates([item])

    related = frappe.get_all(
        "Item",
        filters={"item_group": item.item_group, "disabled": 0, "item_code": ["!=", item_code]},
        fields=["item_code", "item_name", "image", "stock_uom"],
        limit_page_length=4,
    )
    attach_rates(related)

    return {"item": item, "related": related}


@frappe.whitelist(allow_guest=True)
def place_order(cart_items, customer_details):
    """Creates a draft Sales Order from a cart. Left as a draft (not
    auto-submitted) so your team can review address/stock before confirming
    - see SETUP_README.md if you'd rather auto-submit instead.

    cart_items:       JSON list of {"item_code": str, "qty": number}
    customer_details: JSON dict of {"name","mobile","email","address","notes"}
    """
    cart_items = frappe.parse_json(cart_items)
    customer_details = frappe.parse_json(customer_details)

    if not cart_items:
        frappe.throw(_("Your cart is empty."))

    name = (customer_details.get("name") or "").strip()
    mobile = (customer_details.get("mobile") or "").strip()
    address_text = (customer_details.get("address") or "").strip()

    if not name or not mobile or not address_text:
        frappe.throw(_("Name, mobile number, and delivery address are required."))

    valid_groups = get_stationery_item_groups()
    customer = get_or_create_customer(name, mobile, customer_details.get("email"))

    so = frappe.new_doc("Sales Order")
    so.customer = customer
    so.delivery_date = add_days(nowdate(), DELIVERY_LEAD_DAYS)
    so.order_type = "Sales"

    # These are custom fields you'll need to add via Customize Form first
    # (Sales Order). If a field doesn't exist yet, it's silently skipped
    # rather than throwing an error, so this still works before you've set
    # them up - see SETUP_README.md for the exact fields to add.
    meta = frappe.get_meta("Sales Order")
    if meta.has_field("custom_payment_method"):
        so.custom_payment_method = "Cash on Delivery"
    if meta.has_field("custom_order_source"):
        so.custom_order_source = "Website"
    if meta.has_field("custom_delivery_address"):
        so.custom_delivery_address = address_text
    if meta.has_field("custom_customer_mobile"):
        so.custom_customer_mobile = mobile
    if meta.has_field("custom_order_notes") and customer_details.get("notes"):
        so.custom_order_notes = customer_details.get("notes")

    added_any = False
    for line in cart_items:
        item_code = line.get("item_code")
        qty = flt(line.get("qty") or 0)
        if not item_code or qty <= 0:
            continue
        if not frappe.db.exists(
            "Item", {"item_code": item_code, "disabled": 0, "item_group": ["in", valid_groups]}
        ):
            continue
        # Server-side price lookup - the rate shown in the browser is never
        # trusted directly, so editing the page can't change what's charged.
        rate = get_authoritative_rate(item_code)
        so.append("items", {
            "item_code": item_code,
            "qty": qty,
            "rate": rate,
            "delivery_date": so.delivery_date,
        })
        added_any = True

    if not added_any:
        frappe.throw(_("None of the items in your cart are currently available."))

    so.insert(ignore_permissions=True)

    return {"order_id": so.name, "grand_total": so.grand_total, "currency": so.currency}


@frappe.whitelist()
def get_my_orders():
    """Orders for the logged-in portal user, matched via their linked
    Contact -> Customer. Requires an active Frappe login."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Please log in to view your orders."), frappe.PermissionError)

    contact_name = frappe.db.get_value("Contact", {"user": frappe.session.user}, "name")
    if not contact_name:
        # Fallback for instances where Contact isn't linked via the `user` field
        contact_name = frappe.db.get_value("Contact Email", {"email_id": frappe.session.user}, "parent")
    if not contact_name:
        return []

    customer = frappe.db.get_value(
        "Dynamic Link",
        {"parenttype": "Contact", "parent": contact_name, "link_doctype": "Customer"},
        "link_name",
    )
    if not customer:
        return []

    return frappe.get_all(
        "Sales Order",
        filters={"customer": customer},
        fields=["name", "transaction_date", "grand_total", "currency", "status", "docstatus"],
        order_by="creation desc",
        limit_page_length=50,
    )


@frappe.whitelist(allow_guest=True)
def get_orders_by_mobile(mobile):
    """Guest order lookup by mobile number, for customers who checked out
    without an account. NOTE: this has no OTP/verification step, so anyone
    who knows a mobile number can see that customer's order history and
    totals. That's a fair trade-off for a low-friction COD stationery order,
    but if you want it locked down, add an SMS OTP step before calling this
    - see the security note in SETUP_README.md."""
    mobile = (mobile or "").strip()
    if not mobile:
        return []

    contact_name = frappe.db.get_value("Contact Phone", {"phone": mobile}, "parent")
    if not contact_name:
        return []

    customer = frappe.db.get_value(
        "Dynamic Link",
        {"parenttype": "Contact", "parent": contact_name, "link_doctype": "Customer"},
        "link_name",
    )
    if not customer:
        return []

    return frappe.get_all(
        "Sales Order",
        filters={"customer": customer},
        fields=["name", "transaction_date", "grand_total", "currency", "status"],
        order_by="creation desc",
        limit_page_length=20,
    )
