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

# Business rules — keep these three numbers in sync with the matching
# MIN_ORDER_VALUE / FREE_DELIVERY_THRESHOLD / FLAT_DELIVERY_FEE constants
# in www/shop/index.html (there's no shared config between the two files).
MIN_ORDER_VALUE = 100
FREE_DELIVERY_THRESHOLD = 300
DELIVERY_FEE_FLAT = 20
DELIVERY_FEE_ITEM_CODE = "DELIVERY-CHARGE"  # create this as a non-stock service Item if you want
                                             # the delivery fee to automatically appear as a line on
                                             # the Sales Order total. Until it exists, orders still go
                                             # through fine — the fee is just noted, not line-totalled.

import frappe
from frappe import _
from frappe.utils import flt, cint, nowdate, add_days

DELIVERY_LEAD_DAYS = 2


# ---------------------------------------------------------------------------
# Helpers (not whitelisted — internal use only)
# ---------------------------------------------------------------------------

def base_item_filters():
    """Standard filters applied everywhere the storefront queries Item:
    not disabled, and - if you've added the field - only items you've
    actually ticked to show on the website. Add a Check field called
    custom_show_in_webshop to Item (default: 1, so all your existing items
    keep showing exactly as they do now). Untick it on items you only
    create for internal quoting, and they'll stop appearing on the shop
    without needing any code change."""
    filters = {"disabled": 0}
    if frappe.get_meta("Item").has_field("custom_show_in_webshop"):
        filters["custom_show_in_webshop"] = 1
    return filters


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
def get_products(item_group=None, search=None, sort="name_asc", page=1, page_size=24, brand=None, price_min=None, price_max=None):
    """Paginated, searchable, sortable, filterable product listing.
    Guest-accessible (read-only) so anyone can browse without logging in."""
    page = cint(page) or 1
    page_size = min(cint(page_size) or 24, 60)

    valid_groups = get_stationery_item_groups()
    filters = base_item_filters()
    filters["item_group"] = ["in", valid_groups]
    if item_group and item_group in valid_groups:
        filters["item_group"] = item_group

    brand_list = [b.strip() for b in (brand or "").split(",") if b.strip()]
    if brand_list:
        filters["brand"] = ["in", brand_list]

    search = (search or "").strip()
    or_filters = None
    if search:
        # Match items containing ANY word from the search phrase (not just
        # the exact phrase as one block) - e.g. searching "a4 paper" also
        # finds "Copier Paper A4 80gsm", not only items with that exact
        # substring. Exact-phrase matches are still ranked first below.
        words = [w for w in search.split() if w]
        or_filters = []
        for w in words:
            or_filters.append(["item_name", "like", f"%{w}%"])
            or_filters.append(["item_code", "like", f"%{w}%"])

    order_by = "item_name asc"
    if sort == "newest":
        order_by = "creation desc"

    # Price filtering and search ranking both need to happen in Python
    # (price lives in a separate doctype; ranking needs to see the whole
    # matching set, not just one page of it) - so whenever either is
    # active, fetch a larger candidate batch, process in Python, then
    # paginate the processed result. Fine at this catalogue size; would be
    # worth pushing into SQL if the catalogue grows into the tens of thousands.
    needs_python_processing = bool(search) or bool(price_min) or bool(price_max)

    if needs_python_processing:
        candidates = frappe.get_all(
            "Item",
            filters=filters,
            or_filters=or_filters,
            fields=["item_code", "item_name", "item_group", "image", "stock_uom", "description", "brand"],
            order_by=order_by,
            limit_page_length=1000,
        )
        attach_rates(candidates)

        pmin = flt(price_min) if price_min else None
        pmax = flt(price_max) if price_max else None
        if pmin is not None:
            candidates = [i for i in candidates if i["rate"] >= pmin]
        if pmax is not None:
            candidates = [i for i in candidates if i["rate"] <= pmax]

        if sort == "price_asc":
            candidates.sort(key=lambda i: i["rate"])
        elif sort == "price_desc":
            candidates.sort(key=lambda i: i["rate"], reverse=True)

        if search:
            # Stable sort on top of whatever ordering is already there:
            # items containing the FULL search phrase float to the top,
            # partial word matches follow, in their existing order.
            search_lower = search.lower()
            candidates.sort(key=lambda i: 0 if search_lower in (i["item_name"] or "").lower() else 1)

        total = len(candidates)
        start = (page - 1) * page_size
        items = candidates[start:start + page_size]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    total = frappe.db.count("Item", filters=filters)

    items = frappe.get_all(
        "Item",
        filters=filters,
        or_filters=or_filters,
        fields=["item_code", "item_name", "item_group", "image", "stock_uom", "description", "brand"],
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
def get_brands():
    """Distinct brand names among sellable items, for the sidebar Brand
    filter. Returns an empty list if the Brand field isn't populated in
    your catalogue - the frontend hides the filter automatically in that
    case rather than showing an empty section."""
    valid_groups = get_stationery_item_groups()
    brand_filters = base_item_filters()
    brand_filters["item_group"] = ["in", valid_groups]
    brand_filters["brand"] = ["is", "set"]
    brands = frappe.get_all(
        "Item",
        filters=brand_filters,
        pluck="brand",
        distinct=True,
        order_by="brand asc",
        limit_page_length=300,
    )
    return sorted(set(b for b in brands if b))


@frappe.whitelist(allow_guest=True)
def get_product(item_code):
    """Single product detail + up to 4 related items from the same group."""
    valid_groups = get_stationery_item_groups()
    item_filters = base_item_filters()
    item_filters["item_code"] = item_code
    item_filters["item_group"] = ["in", valid_groups]
    item = frappe.db.get_value(
        "Item",
        item_filters,
        ["item_code", "item_name", "item_group", "description", "image", "stock_uom"],
        as_dict=True,
    )
    if not item:
        frappe.throw(_("Product not found"), frappe.DoesNotExistError)

    attach_rates([item])

    related_filters = base_item_filters()
    related_filters["item_group"] = item.item_group
    related_filters["item_code"] = ["!=", item_code]
    related = frappe.get_all(
        "Item",
        filters=related_filters,
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
    customer_details: JSON dict of {"name","mobile","email","address","billing_address","notes"}
    """
    cart_items = frappe.parse_json(cart_items)
    customer_details = frappe.parse_json(customer_details)

    if not cart_items:
        frappe.throw(_("Your cart is empty."))

    name = (customer_details.get("name") or "").strip()
    mobile = (customer_details.get("mobile") or "").strip()
    address_text = (customer_details.get("address") or "").strip()
    billing_address_text = (customer_details.get("billing_address") or "").strip()

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
    if meta.has_field("custom_billing_address") and billing_address_text:
        so.custom_billing_address = billing_address_text
    if meta.has_field("custom_customer_mobile"):
        so.custom_customer_mobile = mobile
    if meta.has_field("custom_order_notes") and customer_details.get("notes"):
        so.custom_order_notes = customer_details.get("notes")

    added_any = False
    items_subtotal = 0.0
    for line in cart_items:
        item_code = line.get("item_code")
        qty = flt(line.get("qty") or 0)
        if not item_code or qty <= 0:
            continue
        order_check = base_item_filters()
        order_check["item_code"] = item_code
        order_check["item_group"] = ["in", valid_groups]
        if not frappe.db.exists("Item", order_check):
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
        items_subtotal += rate * qty
        added_any = True

    if not added_any:
        frappe.throw(_("None of the items in your cart are currently available."))

    if items_subtotal < MIN_ORDER_VALUE:
        frappe.throw(_("Minimum order value is QAR {0}.").format(MIN_ORDER_VALUE))

    # Delivery fee: added as a real line item so it's reflected in the
    # order's actual total, IF you've created the DELIVERY_FEE_ITEM_CODE
    # item. If not, the order still goes through fine - the fee is just
    # noted in Order Notes instead, for staff to add manually.
    delivery_fee = 0 if items_subtotal >= FREE_DELIVERY_THRESHOLD else DELIVERY_FEE_FLAT
    if delivery_fee > 0:
        if frappe.db.exists("Item", DELIVERY_FEE_ITEM_CODE):
            so.append("items", {
                "item_code": DELIVERY_FEE_ITEM_CODE,
                "qty": 1,
                "rate": delivery_fee,
                "delivery_date": so.delivery_date,
            })
        elif meta.has_field("custom_order_notes"):
            note = f"Delivery fee QAR {delivery_fee} applies but Item '{DELIVERY_FEE_ITEM_CODE}' doesn't exist yet - add it to the order total manually."
            so.custom_order_notes = (f"{so.custom_order_notes}\n{note}" if so.custom_order_notes else note)

    so.insert(ignore_permissions=True)

    notify_new_order(so, name, mobile, address_text)

    return {"order_id": so.name, "grand_total": so.grand_total, "currency": so.currency}


def notify_new_order(so, customer_name, mobile, address_text):
    """Emails sales@raifacentre.qa when a website order comes in. Uses
    whatever your default outgoing Email Account already is (you mentioned
    notification@raifacentre.com is set as default) - nothing new to
    configure. Wrapped in try/except deliberately: if email sending ever
    fails (SMTP hiccup, etc.) it must never stop the order itself from
    being saved - the order already exists in the database by this point,
    the email is just a heads-up on top of it."""
    try:
        item_lines = "".join(
            f"<tr><td style='padding:4px 10px 4px 0;'>{d.item_code} - {d.item_name}</td>"
            f"<td style='padding:4px 10px;text-align:right;'>{d.qty} x QAR {d.rate}</td></tr>"
            for d in so.items
        )
        frappe.sendmail(
            recipients=["sales@raifacentre.qa"],
            subject=f"New website order {so.name} - QAR {so.grand_total}",
            message=f"""
                <p>New Cash on Delivery order placed on the website.</p>
                <p><b>Order:</b> {so.name}<br>
                <b>Customer:</b> {frappe.utils.escape_html(customer_name)} ({frappe.utils.escape_html(mobile)})<br>
                <b>Delivery Address:</b> {frappe.utils.escape_html(address_text)}<br>
                <b>Total:</b> QAR {so.grand_total}</p>
                <table style="border-collapse:collapse;">{item_lines}</table>
                <p>Review and submit the order in ERPNext under Selling &gt; Sales Order.</p>
            """,
        )
    except Exception:
        frappe.log_error(title="Shop: order notification email failed")


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
