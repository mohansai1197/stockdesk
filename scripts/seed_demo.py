"""Replace business samples with an internally consistent, costed demo ledger.

Stop the local server first. Explicit replacement is required; staff and sessions
are retained, and the existing database is backed up before any deletion.
"""

import argparse
import csv
import json
import random
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database, adjust_stock, audit, blended_cost
from models import shop_today
from supplier_imports import STANDARD, SUPPLIER

CATALOG = {
    "Stationery": [
        ("A5 Ruled Notebook", 8500),
        ("A4 Spiral Notebook", 14900),
        ("Blue Gel Pen Pack", 9900),
        ("Black Rollerball Pen", 3500),
        ("HB Pencil Pack", 6000),
        ("Highlighter Set", 12900),
        ("Sticky Note Pad", 4900),
        ("Document Folder", 7500),
        ("Drawing Sketchbook", 19900),
        ("Desk Planner", 24900),
    ],
    "Home": [
        ("Ceramic Coffee Mug", 19900),
        ("Glass Storage Jar", 24900),
        ("Cotton Hand Towel", 14900),
        ("Coaster Set", 17900),
        ("Mini Desk Planter", 29900),
        ("LED Table Lamp", 89900),
        ("Storage Basket", 34900),
        ("Scented Candle", 22900),
        ("Photo Frame", 27900),
        ("Wooden Serving Tray", 54900),
    ],
    "Everyday": [
        ("Steel Water Bottle", 44900),
        ("Canvas Tote Bag", 24900),
        ("Lunch Box", 39900),
        ("Travel Umbrella", 49900),
        ("Reusable Shopping Bag", 7900),
        ("Travel Coffee Tumbler", 59900),
        ("Pocket Tissue Pack", 3500),
        ("Cotton Face Towel", 9900),
        ("Small Utility Pouch", 17900),
        ("Rain Poncho", 19900),
    ],
    "Office": [
        ("Desktop Organizer", 34900),
        ("Stapler", 14900),
        ("Staple Pin Box", 3900),
        ("Binder Clip Set", 6900),
        ("Paper Clip Box", 2900),
        ("Clear Tape Roll", 4500),
        ("Packing Tape Roll", 7900),
        ("Scissors", 12900),
        ("Desk Calculator", 49900),
        ("Whiteboard Marker Set", 15900),
    ],
    "Accessories": [
        ("USB-C Charging Cable", 29900),
        ("Phone Stand", 19900),
        ("Mouse Pad", 14900),
        ("Cable Organizer Set", 9900),
        ("Key Ring", 5900),
        ("Laptop Sleeve", 69900),
        ("Screen Cleaning Kit", 17900),
        ("Luggage Tag", 12900),
        ("Card Holder", 24900),
        ("Earphone Storage Case", 14900),
    ],
}
VENDORS = [
    ("Paperlane Stationery Supply", 4),
    ("Deskworks Wholesale", 5),
    ("Hearth Home Supply", 7),
    ("Nest Living Distributors", 9),
    ("Everyday Essentials Supply", 6),
    ("Urban Utility Wholesale", 8),
    ("Office Corner Distributors", 5),
    ("Brightlink Accessories", 10),
]
CATEGORY_VENDORS = {
    "Stationery": (1, 2),
    "Home": (3, 4),
    "Everyday": (5, 6),
    "Office": (2, 7),
    "Accessories": (6, 8),
}
BUSINESS_TABLES = (
    "accounting_sync_attempts",
    "accounting_sync_errors",
    "accounting_invoice_items",
    "accounting_invoices",
    "accounting_sync_runs",
    "accounting_supplier_mappings",
    "return_items",
    "returns",
    "purchase_items",
    "purchase_orders",
    "transfers",
    "stock_movements",
    "sale_items",
    "sales",
    "inventory",
    "import_batches",
    "mapping_profiles",
    "products",
    "suppliers",
    "locations",
)


def replace_business_data(path, today=None):
    today = today or shop_today()
    start = today - timedelta(days=59)
    rng = random.Random(20261017)
    store = Database(path)
    backup_name = store.backup()
    entries = []
    now = datetime.now(timezone.utc)

    def timestamp(day, hour=10, minute=0):
        value = datetime.combine(
            day, time(hour, minute), tzinfo=ZoneInfo("Asia/Kolkata")
        )
        value = value.astimezone(timezone.utc)
        if day == shop_today():
            value = min(value, now - timedelta(seconds=1))
        return value.isoformat().replace("+00:00", "Z")

    with store.transaction(write=True) as db:
        account_snapshot = [
            tuple(row) for row in db.execute("SELECT * FROM users ORDER BY id")
        ]
        session_snapshot = [
            tuple(row)
            for row in db.execute("SELECT * FROM sessions ORDER BY token_hash")
        ]
        db.execute(
            "UPDATE accounting_connections SET lease_token=NULL,lease_until=NULL"
        )
        for table in BUSINESS_TABLES:
            db.execute(f"DELETE FROM {table}")
        # Keep account/security/recovery events; old business events refer to removed IDs.
        db.execute(
            "DELETE FROM activity WHERE action NOT LIKE 'owner.%' AND action NOT LIKE 'staff.%' "
            "AND action NOT LIKE 'login.%' AND action NOT LIKE 'backup.%' AND action != 'logout'"
        )
        db.executemany(
            "INSERT INTO locations(id,name) VALUES (?,?)",
            [(1, "Main shop"), (2, "Warehouse"), (3, "City branch")],
        )
        db.executemany(
            "INSERT INTO suppliers(id,name,email,lead_days) VALUES (?,?,?,?)",
            [
                (i, name, f"supplier{i}@example.test", lead)
                for i, (name, lead) in enumerate(VENDORS, 1)
            ],
        )

        def log(action, identity, details, day, actor="Sample staff"):
            audit(db, actor, action, identity, details)
            db.execute(
                "UPDATE activity SET created_at=? WHERE id=last_insert_rowid()",
                (timestamp(day, 19),),
            )

        def move(
            identity,
            location,
            quantity,
            reason,
            day,
            actor="Sample staff",
            sale_id=None,
        ):
            adjust_stock(db, identity, location, quantity, reason, actor, sale_id)
            if quantity:
                db.execute(
                    "UPDATE stock_movements SET created_at=? WHERE id=last_insert_rowid()",
                    (timestamp(day, 11),),
                )

        products = []
        for category, items in CATALOG.items():
            for offset, (name, price) in enumerate(items):
                identity = len(products) + 1
                vendor = CATEGORY_VENDORS[category][offset % 2]
                cost = (price * rng.randint(54, 70) + 50) // 100
                sku = f"{category[:3].upper()}-{offset + 1:03d}"
                low = offset == 4
                opening = (
                    (12, 6, 8)
                    if low
                    else (
                        rng.randint(75, 130),
                        rng.randint(90, 160),
                        rng.randint(50, 85),
                    )
                )
                db.execute(
                    "INSERT INTO products(id,sku,name,category,price_paise,stock,low_stock_at,cost_paise,supplier_id) VALUES (?,?,?,?,?,0,?,?,?)",
                    (
                        identity,
                        sku,
                        name,
                        category,
                        price,
                        12 if low else 18,
                        cost,
                        vendor,
                    ),
                )
                for location, quantity in enumerate(opening, 1):
                    move(
                        identity,
                        location,
                        quantity,
                        "Sample opening stock",
                        start - timedelta(days=1),
                    )
                products.append(
                    {
                        "id": identity,
                        "sku": sku,
                        "name": name,
                        "category": category,
                        "price": price,
                        "opening_cost": cost,
                        "opening_stock": sum(opening),
                        "supplier": vendor,
                        "low": low,
                    }
                )

        pending_returns = []
        receipt_schedule = {}
        for day_index in range(60):
            day = start + timedelta(days=day_index)
            if day_index % 2 == 0:
                order_number = day_index // 2
                vendor = order_number % len(VENDORS) + 1
                options = [
                    item
                    for item in products
                    if item["supplier"] == vendor and not item["low"]
                ]
                chosen = rng.sample(options, min(len(options), rng.randint(2, 4)))
                lines = []
                for item in chosen:
                    cost = max(
                        1, (item["opening_cost"] * rng.randint(96, 104) + 50) // 100
                    )
                    lines.append((item["id"], rng.randint(15, 45), cost))
                location = (2, 1, 2, 3)[order_number % 4]
                total = sum(quantity * cost for _, quantity, cost in lines)
                cursor = db.execute(
                    "INSERT INTO purchase_orders(supplier_id,location_id,total_paise,actor,created_at) VALUES (?,?,?,?,?)",
                    (vendor, location, total, "Sample purchasing", timestamp(day, 8)),
                )
                purchase_id = cursor.lastrowid
                db.executemany(
                    "INSERT INTO purchase_items(purchase_id,product_id,quantity,cost_paise) VALUES (?,?,?,?)",
                    [(purchase_id, *line) for line in lines],
                )
                log(
                    "purchase.ordered",
                    purchase_id,
                    {"total_paise": total, "sample": True},
                    day,
                )
                if order_number < 24:
                    receipt_day = day + timedelta(days=VENDORS[vendor - 1][1])
                    receipt_schedule.setdefault(receipt_day, []).append(
                        (purchase_id, location, lines)
                    )

            for purchase_id, location, lines in receipt_schedule.get(day, []):
                for identity, quantity, unit_cost in lines:
                    item = db.execute(
                        "SELECT * FROM products WHERE id=?", (identity,)
                    ).fetchone()
                    cost = blended_cost(
                        item["stock"], item["cost_paise"], quantity, unit_cost
                    )
                    move(
                        identity,
                        location,
                        quantity,
                        f"Purchase #{purchase_id} received",
                        day,
                        "Sample purchasing",
                    )
                    db.execute(
                        "UPDATE products SET cost_paise=? WHERE id=?", (cost, identity)
                    )
                db.execute(
                    "UPDATE purchase_orders SET status='received',received_at=? WHERE id=?",
                    (timestamp(day, 9), purchase_id),
                )
                log("purchase.received", purchase_id, {"sample": True}, day)

            if day_index % 3 == 1:
                options = [
                    dict(row)
                    for row in db.execute(
                        "SELECT p.id,i.quantity FROM products p JOIN inventory i ON i.product_id=p.id WHERE i.location_id=2 AND i.quantity>=8 ORDER BY p.id"
                    )
                ]
                item = rng.choice(options)
                quantity = rng.randint(5, min(15, item["quantity"]))
                target = 1 if day_index % 2 else 3
                move(
                    item["id"],
                    2,
                    -quantity,
                    f"Transfer to {'Main shop' if target == 1 else 'City branch'}",
                    day,
                    "Sample stock manager",
                )
                move(
                    item["id"],
                    target,
                    quantity,
                    "Transfer from Warehouse",
                    day,
                    "Sample stock manager",
                )
                cursor = db.execute(
                    "INSERT INTO transfers(product_id,from_location,to_location,quantity,actor,created_at) VALUES (?,2,?,?,?,?)",
                    (
                        item["id"],
                        target,
                        quantity,
                        "Sample stock manager",
                        timestamp(day, 9),
                    ),
                )
                log(
                    "stock.transferred",
                    cursor.lastrowid,
                    {"quantity": quantity, "sample": True},
                    day,
                )

            for counter in range(4):
                location = 3 if (day_index + counter) % 3 == 0 else 1
                available = [
                    dict(row)
                    for row in db.execute(
                        "SELECT p.*,i.quantity AS available FROM products p JOIN inventory i ON i.product_id=p.id WHERE i.location_id=? AND i.quantity>0 ORDER BY p.id",
                        (location,),
                    )
                ]
                chosen = rng.sample(available, rng.randint(2, 4))
                lines = [
                    (item, rng.randint(1, min(4, item["available"]))) for item in chosen
                ]
                revenue = sum(
                    item["price_paise"] * quantity for item, quantity in lines
                )
                cogs = sum(item["cost_paise"] * quantity for item, quantity in lines)
                cursor = db.execute(
                    "INSERT INTO sales(sale_date,total_paise,location_id,actor,created_at) VALUES (?,?,?,?,?)",
                    (
                        day.isoformat(),
                        revenue,
                        location,
                        "Sample cashier",
                        timestamp(day, 10 + counter * 2),
                    ),
                )
                sale_id = cursor.lastrowid
                return_candidate = None
                for item, quantity in lines:
                    cursor = db.execute(
                        "INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_paise,unit_cost_paise) VALUES (?,?,?,?,?)",
                        (
                            sale_id,
                            item["id"],
                            quantity,
                            item["price_paise"],
                            item["cost_paise"],
                        ),
                    )
                    if return_candidate is None:
                        return_candidate = (
                            cursor.lastrowid,
                            item["id"],
                            min(quantity, 2),
                            item["price_paise"],
                            item["cost_paise"],
                        )
                    move(
                        item["id"],
                        location,
                        -quantity,
                        "Sale",
                        day,
                        "Sample cashier",
                        sale_id,
                    )
                entries.append(
                    {
                        "date": day.isoformat(),
                        "revenue_paise": revenue,
                        "refund_paise": 0,
                        "cost_paise": cogs,
                    }
                )
                log(
                    "sale.recorded",
                    sale_id,
                    {"total_paise": revenue, "sample": True},
                    day,
                    "Sample cashier",
                )
                if sale_id % 9 == 0 and len(pending_returns) < 24:
                    pending_returns.append(
                        (
                            day + timedelta(days=3),
                            sale_id,
                            location,
                            return_candidate,
                            len(pending_returns),
                        )
                    )

            for return_day, sale_id, location, line, number in pending_returns:
                if return_day != day:
                    continue
                line_id, identity, quantity, price, cost = line
                sellable = number % 4 != 0
                reason = (
                    "Customer changed mind (sample)"
                    if sellable
                    else "Damaged item (sample)"
                )
                refund = quantity * price
                cursor = db.execute(
                    "INSERT INTO returns(sale_id,return_date,total_paise,reason,actor,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        sale_id,
                        day.isoformat(),
                        refund,
                        reason,
                        "Sample manager",
                        timestamp(day, 19),
                    ),
                )
                return_id = cursor.lastrowid
                db.execute(
                    "INSERT INTO return_items(return_id,sale_item_id,quantity,restock) VALUES (?,?,?,?)",
                    (return_id, line_id, quantity, int(sellable)),
                )
                if sellable:
                    item = db.execute(
                        "SELECT * FROM products WHERE id=?", (identity,)
                    ).fetchone()
                    average = blended_cost(
                        item["stock"], item["cost_paise"], quantity, cost
                    )
                    move(
                        identity,
                        location,
                        quantity,
                        f"Return #{return_id}",
                        day,
                        "Sample manager",
                        sale_id,
                    )
                    db.execute(
                        "UPDATE products SET cost_paise=? WHERE id=?",
                        (average, identity),
                    )
                entries.append(
                    {
                        "date": day.isoformat(),
                        "revenue_paise": 0,
                        "refund_paise": refund,
                        "cost_paise": -quantity * cost if sellable else 0,
                    }
                )
                log(
                    "sale.returned",
                    return_id,
                    {"sale_id": sale_id, "refund_paise": refund, "sample": True},
                    day,
                    "Sample manager",
                )

        for name, mapping in (
            ("Sample standard catalog", STANDARD),
            ("Sample supplier headings", SUPPLIER),
        ):
            db.execute(
                "INSERT INTO mapping_profiles(name,version,mapping_json,actor) VALUES (?,1,?,'Sample dataset')",
                (name, json.dumps(mapping)),
            )
        for source, status, imported, errors in (
            ("Generated sample catalog.csv", "imported", 50, []),
            (
                "sample-invalid.csv (demo)",
                "rejected",
                0,
                [
                    {
                        "row": 3,
                        "sku": "DEMO-BAD-01",
                        "error": "price: enter a valid amount",
                    },
                    {
                        "row": 8,
                        "sku": "DEMO-BAD-02",
                        "error": "stock: must be a nonnegative whole number",
                    },
                ],
            ),
        ):
            db.execute(
                "INSERT INTO import_batches(source,location_id,mapping_json,status,imported,errors_json,actor,created_at) VALUES (?,1,?,?,?,?,?,?)",
                (
                    source,
                    json.dumps(STANDARD),
                    status,
                    imported,
                    json.dumps(errors),
                    "Sample dataset",
                    timestamp(start, 8),
                ),
            )

        def totals(first, last):
            selected = [entry for entry in entries if first <= entry["date"] <= last]
            gross = sum(entry["revenue_paise"] for entry in selected)
            refunds = sum(entry["refund_paise"] for entry in selected)
            cost = sum(entry["cost_paise"] for entry in selected)
            return {
                "gross_revenue_paise": gross,
                "refund_paise": refunds,
                "revenue_paise": gross - refunds,
                "cost_of_goods_paise": cost,
                "gross_profit_paise": gross - refunds - cost,
            }

        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "products",
                "suppliers",
                "locations",
                "sales",
                "sale_items",
                "purchase_orders",
                "returns",
                "transfers",
            )
        }
        month_start = today.replace(day=1).isoformat()
        manifest = {
            "synthetic": True,
            "seed": 20261017,
            "start": start.isoformat(),
            "end": today.isoformat(),
            "counts": counts,
            "current_month": totals(month_start, today.isoformat()),
            "full_period": totals(start.isoformat(), today.isoformat()),
            "received_purchases": db.execute(
                "SELECT COUNT(*) FROM purchase_orders WHERE status='received'"
            ).fetchone()[0],
            "pending_purchases": db.execute(
                "SELECT COUNT(*) FROM purchase_orders WHERE status='ordered'"
            ).fetchone()[0],
            "low_stock_products": db.execute(
                "SELECT COUNT(*) FROM products WHERE stock<=low_stock_at"
            ).fetchone()[0],
        }
        assert (
            counts["products"] == 50
            and counts["sales"] == 240
            and counts["returns"] == 24
        )
        assert (
            manifest["received_purchases"] == 24 and manifest["pending_purchases"] == 6
        )
        assert not db.execute(
            "SELECT 1 FROM sale_items WHERE unit_cost_paise IS NULL LIMIT 1"
        ).fetchone()
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert not db.execute(
            "SELECT p.id FROM products p LEFT JOIN inventory i ON i.product_id=p.id GROUP BY p.id HAVING p.stock!=COALESCE(SUM(i.quantity),0)"
        ).fetchall()
        assert not db.execute(
            "SELECT p.id FROM products p LEFT JOIN stock_movements m ON m.product_id=p.id GROUP BY p.id HAVING p.stock!=COALESCE(SUM(m.change),0)"
        ).fetchall()
        assert [
            tuple(row) for row in db.execute("SELECT * FROM users ORDER BY id")
        ] == account_snapshot
        assert [
            tuple(row)
            for row in db.execute("SELECT * FROM sessions ORDER BY token_hash")
        ] == session_snapshot
        audit(
            db,
            "Sample dataset",
            "demo.replaced",
            "60-day demo",
            {"backup": backup_name, "synthetic": True, **counts},
        )
    return manifest, store.backups / backup_name, products


def write_samples(manifest, products):
    (ROOT / "samples" / "expanded-demo-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    with (ROOT / "samples" / "expanded-products.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["sku", "name", "category", "price", "cost", "stock", "low_stock_at"]
        )
        for item in products:
            writer.writerow(
                [
                    item["sku"],
                    item["name"],
                    item["category"],
                    f"{item['price'] / 100:.2f}",
                    f"{item['opening_cost'] / 100:.2f}",
                    item["opening_stock"],
                    12 if item["low"] else 18,
                ]
            )

    (ROOT / "samples" / "products.csv").write_bytes(
        (ROOT / "samples" / "expanded-products.csv").read_bytes()
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / ".data" / "stockdesk.sqlite3")
    parser.add_argument(
        "--replace-business-data",
        action="store_true",
        help="Explicitly replace products and transactions; preserve staff and sessions",
    )
    args = parser.parse_args()
    if not args.replace_business_data:
        parser.error(
            "Stop the server and pass --replace-business-data to replace the shop's business records"
        )
    manifest, backup, products = replace_business_data(args.db)
    write_samples(manifest, products)
    print(json.dumps({"backup": str(backup), **manifest}, indent=2))
