"""StockDesk: inventory, purchasing, sales, integrations, and staff operations."""

import csv
import io
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, Response as RawResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from database import (
    Database,
    adjust_stock,
    audit,
    blended_cost,
    utc_now,
)
from accounting_sync import install_accounting
from models import (
    MAX_STOCK,
    ProductInput,
    RestockInput,
    SaleInput,
    shop_today,
    to_paise,
)
from security import install_auth, verify_password, token_hash
from supplier_imports import (
    FIELDS,
    STANDARD,
    SUPPLIER,
    convert_records,
    read_records,
    validate_mapping,
)

ROOT = Path(__file__).resolve().parent


class SupplierInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(default="", max_length=120)
    lead_days: int = Field(default=7, ge=1, le=90, strict=True)


class LocationInput(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class ProductUpdate(BaseModel):
    price: str | None = Field(default=None, max_length=24)
    cost: str | None = Field(default=None, max_length=24)
    low_stock_at: int | None = Field(default=None, ge=0, le=MAX_STOCK, strict=True)
    supplier_id: int | None = Field(default=None, gt=0, strict=True)


class PurchaseLine(BaseModel):
    product_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, le=MAX_STOCK, strict=True)
    cost: str = Field(max_length=24)

    @field_validator("cost")
    @classmethod
    def valid_cost(cls, value):
        to_paise(value)
        return value


class PurchaseInput(BaseModel):
    supplier_id: int = Field(gt=0, strict=True)
    location_id: int = Field(default=1, gt=0, strict=True)
    items: list[PurchaseLine] = Field(min_length=1, max_length=20)


class ReturnLine(BaseModel):
    sale_item_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, le=MAX_STOCK, strict=True)
    restock: bool = Field(default=True, strict=True)


class ReturnInput(BaseModel):
    items: list[ReturnLine] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=3, max_length=200)


class TransferInput(BaseModel):
    product_id: int = Field(gt=0, strict=True)
    from_location: int = Field(gt=0, strict=True)
    to_location: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, le=MAX_STOCK, strict=True)


class ProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    supplier_id: int | None = Field(default=None, gt=0, strict=True)
    mapping: dict[str, str]


class RestoreInput(BaseModel):
    confirmation: str
    password: str = Field(max_length=128)


def create_app(db_path=None):
    store = Database(
        db_path or os.environ.get("STOCKDESK_DB", ROOT / ".data" / "stockdesk.sqlite3")
    )
    application = FastAPI(title="StockDesk API", version="2.1.0")
    application.state.store = store
    counters = {
        "started": time.monotonic(),
        "requests": 0,
        "rejected": 0,
        "server_errors": 0,
    }
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    require = install_auth(application, store)
    install_accounting(application, store, require)
    staff = Depends(require())
    manager = Depends(require("owner", "manager"))
    owner = Depends(require("owner"))

    @application.middleware("http")
    async def response_policy(request: Request, call_next):
        counters["requests"] += 1
        try:
            response = await call_next(request)
        except Exception as exc:
            counters["server_errors"] += 1
            with store.transaction(write=True) as db:
                audit(
                    db,
                    "Server",
                    "server.error",
                    request.url.path,
                    {"type": type(exc).__name__},
                )
            raise
        if response.status_code >= 400:
            counters["rejected"] += 1
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        if (
            request.method == "POST"
            and response.status_code >= 400
            and not request.url.path.startswith("/api/auth")
        ):
            with store.transaction(write=True) as db:
                audit(
                    db,
                    "Request",
                    "request.rejected",
                    request.url.path,
                    {"status": response.status_code},
                )
        return response

    def exists(db, table, identity, label):
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if not row:
            raise HTTPException(404, f"{label} not found")
        return row

    def supplier_exists(db, supplier_id):
        if supplier_id is not None:
            exists(db, "suppliers", supplier_id, "Supplier")

    def insert_product(db, product, actor):
        if db.execute("SELECT 1 FROM products WHERE sku=?", (product.sku,)).fetchone():
            raise HTTPException(
                409,
                f"SKU {product.sku} already exists. Use a purchase or Restock to add units.",
            )
        supplier_exists(db, product.supplier_id)
        exists(db, "locations", product.location_id, "Location")
        cursor = db.execute(
            "INSERT INTO products(sku,name,category,price_paise,stock,low_stock_at,cost_paise,supplier_id) VALUES (?,?,?,?,0,?,?,?)",
            (
                product.sku,
                product.name,
                product.category,
                to_paise(product.price),
                product.low_stock_at,
                to_paise(product.cost) if product.cost is not None else None,
                product.supplier_id,
            ),
        )
        identity = cursor.lastrowid
        adjust_stock(
            db, identity, product.location_id, product.stock, "Opening stock", actor
        )
        return identity

    def product_row(db, identity, user):
        result = dict(exists(db, "products", identity, "Product"))
        result["locations"] = [
            dict(row)
            for row in db.execute(
                "SELECT l.id,l.name,COALESCE(i.quantity,0) AS quantity "
                "FROM locations l LEFT JOIN inventory i ON i.location_id=l.id AND i.product_id=? ORDER BY l.id",
                (identity,),
            )
        ]
        if user["role"] == "cashier":
            result.pop("cost_paise", None)
        return result

    @application.get("/")
    def home():
        return FileResponse(ROOT / "static" / "index.html")

    @application.get("/api/health")
    def health():
        with store.transaction() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok", "version": "2.1.0"}

    @application.get("/api/locations")
    def locations(user=staff):
        with store.transaction() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM locations ORDER BY id")
            ]

    @application.post("/api/locations", status_code=201)
    def add_location(body: LocationInput, user=manager):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Enter a location name")
        with store.transaction(write=True) as db:
            if db.execute(
                "SELECT 1 FROM locations WHERE name=? COLLATE NOCASE", (name,)
            ).fetchone():
                raise HTTPException(409, "A location with that name already exists")
            cursor = db.execute("INSERT INTO locations(name) VALUES (?)", (name,))
            audit(
                db,
                user["username"],
                "location.created",
                cursor.lastrowid,
                {"name": name},
            )
            return {"id": cursor.lastrowid, "name": name}

    @application.get("/api/products")
    def products(
        response: Response,
        search: str = Query("", max_length=100),
        category: str = Query("", max_length=50),
        location_id: int | None = Query(None, gt=0),
        low: bool = False,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        user=staff,
    ):
        clauses, values = [], []
        if search:
            clauses.append(
                "(instr(lower(p.name),lower(?))>0 OR instr(lower(p.sku),lower(?))>0)"
            )
            values.extend([search, search])
        if category:
            clauses.append("p.category=?")
            values.append(category)
        stock = "p.stock"
        if location_id:
            stock = "COALESCE((SELECT quantity FROM inventory WHERE product_id=p.id AND location_id=?),0)"
            if low:
                clauses.append(stock + "<=p.low_stock_at")
                values.append(location_id)
        elif low:
            clauses.append("p.stock<=p.low_stock_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with store.transaction() as db:
            response.headers["X-Total-Count"] = str(
                db.execute(
                    "SELECT COUNT(*) FROM products p" + where, values
                ).fetchone()[0]
            )
            ids = db.execute(
                "SELECT p.id FROM products p"
                + where
                + " ORDER BY p.name COLLATE NOCASE,p.id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            return [product_row(db, row[0], user) for row in ids]

    @application.post("/api/products", status_code=201)
    def add_product(body: ProductInput, user=manager):
        with store.transaction(write=True) as db:
            identity = insert_product(db, body, user["username"])
            audit(db, user["username"], "product.created", identity, {"sku": body.sku})
            return product_row(db, identity, user)

    @application.post("/api/products/{product_id}/update")
    def update_product(product_id: int, body: ProductUpdate, user=manager):
        with store.transaction(write=True) as db:
            product = exists(db, "products", product_id, "Product")
            changes = {}
            for source, target in (("price", "price_paise"), ("cost", "cost_paise")):
                value = getattr(body, source)
                if value is not None:
                    try:
                        parsed = to_paise(value)
                    except ValueError as exc:
                        raise HTTPException(422, str(exc)) from None
                    changes[target] = {"from": product[target], "to": parsed}
                    db.execute(
                        f"UPDATE products SET {target}=? WHERE id=?",
                        (parsed, product_id),
                    )
            for field in ("low_stock_at", "supplier_id"):
                value = getattr(body, field)
                if value is not None:
                    if field == "supplier_id":
                        supplier_exists(db, value)
                    changes[field] = {"from": product[field], "to": value}
                    db.execute(
                        f"UPDATE products SET {field}=? WHERE id=?", (value, product_id)
                    )
            if not changes:
                raise HTTPException(422, "Choose at least one value to update")
            audit(db, user["username"], "product.updated", product_id, changes)
            return product_row(db, product_id, user)

    @application.post("/api/products/{product_id}/restock")
    def restock(product_id: int, body: RestockInput, user=manager):
        with store.transaction(write=True) as db:
            product = exists(db, "products", product_id, "Product")
            try:
                cost = (
                    to_paise(body.cost)
                    if body.cost is not None
                    else product["cost_paise"]
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            cost = blended_cost(
                product["stock"], product["cost_paise"], body.quantity, cost
            )
            adjust_stock(
                db,
                product_id,
                body.location_id,
                body.quantity,
                "Restock",
                user["username"],
            )
            db.execute(
                "UPDATE products SET cost_paise=? WHERE id=?", (cost, product_id)
            )
            audit(
                db,
                user["username"],
                "stock.restocked",
                product_id,
                {"quantity": body.quantity, "location": body.location_id},
            )
            return product_row(db, product_id, user)

    @application.get("/api/products/{product_id}/movements")
    def movements(product_id: int, offset: int = Query(0, ge=0), user=staff):
        with store.transaction() as db:
            exists(db, "products", product_id, "Product")
            return [
                dict(row)
                for row in db.execute(
                    "SELECT m.*,l.name AS location FROM stock_movements m JOIN locations l ON l.id=m.location_id WHERE product_id=? ORDER BY m.id DESC LIMIT 100 OFFSET ?",
                    (product_id, offset),
                )
            ]

    @application.post("/api/transfers", status_code=201)
    def transfer(body: TransferInput, user=manager):
        if body.from_location == body.to_location:
            raise HTTPException(422, "Choose two different locations")
        with store.transaction(write=True) as db:
            source = exists(db, "locations", body.from_location, "Source location")
            target = exists(db, "locations", body.to_location, "Destination location")
            adjust_stock(
                db,
                body.product_id,
                body.from_location,
                -body.quantity,
                f"Transfer to {target['name']}",
                user["username"],
            )
            adjust_stock(
                db,
                body.product_id,
                body.to_location,
                body.quantity,
                f"Transfer from {source['name']}",
                user["username"],
            )
            cursor = db.execute(
                "INSERT INTO transfers(product_id,from_location,to_location,quantity,actor) VALUES (?,?,?,?,?)",
                (
                    body.product_id,
                    body.from_location,
                    body.to_location,
                    body.quantity,
                    user["username"],
                ),
            )
            audit(
                db,
                user["username"],
                "stock.transferred",
                cursor.lastrowid,
                body.model_dump(),
            )
            return {"id": cursor.lastrowid}

    @application.get("/api/suppliers")
    def suppliers(user=manager):
        with store.transaction() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM suppliers ORDER BY name")
            ]

    @application.post("/api/suppliers", status_code=201)
    def add_supplier(body: SupplierInput, user=manager):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "Enter a supplier name")
        with store.transaction(write=True) as db:
            if db.execute(
                "SELECT 1 FROM suppliers WHERE name=? COLLATE NOCASE", (name,)
            ).fetchone():
                raise HTTPException(409, "Supplier already exists")
            cursor = db.execute(
                "INSERT INTO suppliers(name,email,lead_days) VALUES (?,?,?)",
                (name, body.email.strip(), body.lead_days),
            )
            audit(
                db,
                user["username"],
                "supplier.created",
                cursor.lastrowid,
                {"name": name},
            )
            return {"id": cursor.lastrowid}

    @application.get("/api/purchases")
    def purchases(
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), user=manager
    ):
        with store.transaction() as db:
            records = [
                dict(row)
                for row in db.execute(
                    "SELECT po.*,s.name AS supplier,l.name AS location FROM purchase_orders po JOIN suppliers s ON s.id=po.supplier_id JOIN locations l ON l.id=po.location_id ORDER BY po.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]
            for item in records:
                item["items"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT pi.*,p.name,p.sku FROM purchase_items pi JOIN products p ON p.id=pi.product_id WHERE purchase_id=?",
                        (item["id"],),
                    )
                ]
            return records

    @application.post("/api/purchases", status_code=201)
    def order(body: PurchaseInput, user=manager):
        if len({line.product_id for line in body.items}) != len(body.items):
            raise HTTPException(422, "Combine repeated products into one purchase line")
        with store.transaction(write=True) as db:
            supplier_exists(db, body.supplier_id)
            exists(db, "locations", body.location_id, "Location")
            for line in body.items:
                exists(db, "products", line.product_id, "Product")
            total = sum(line.quantity * to_paise(line.cost) for line in body.items)
            cursor = db.execute(
                "INSERT INTO purchase_orders(supplier_id,location_id,total_paise,actor) VALUES (?,?,?,?)",
                (body.supplier_id, body.location_id, total, user["username"]),
            )
            identity = cursor.lastrowid
            for line in body.items:
                db.execute(
                    "INSERT INTO purchase_items(purchase_id,product_id,quantity,cost_paise) VALUES (?,?,?,?)",
                    (identity, line.product_id, line.quantity, to_paise(line.cost)),
                )
            audit(
                db,
                user["username"],
                "purchase.ordered",
                identity,
                {"total_paise": total},
            )
            return {"id": identity, "status": "ordered", "total_paise": total}

    @application.post("/api/purchases/{purchase_id}/receive")
    def receive(purchase_id: int, user=manager):
        with store.transaction(write=True) as db:
            purchase = exists(db, "purchase_orders", purchase_id, "Purchase order")
            if purchase["status"] != "ordered":
                raise HTTPException(409, "This purchase has already been received")
            for line in db.execute(
                "SELECT * FROM purchase_items WHERE purchase_id=?", (purchase_id,)
            ).fetchall():
                product = exists(db, "products", line["product_id"], "Product")
                if product["stock"] and product["cost_paise"] is None:
                    raise HTTPException(
                        409,
                        f"Set the opening purchase cost for {product['name']} before receiving more stock",
                    )
                cost = blended_cost(
                    product["stock"],
                    product["cost_paise"],
                    line["quantity"],
                    line["cost_paise"],
                )
                adjust_stock(
                    db,
                    product["id"],
                    purchase["location_id"],
                    line["quantity"],
                    f"Purchase #{purchase_id} received",
                    user["username"],
                )
                db.execute(
                    "UPDATE products SET cost_paise=?,supplier_id=? WHERE id=?",
                    (cost, purchase["supplier_id"], product["id"]),
                )
            db.execute(
                "UPDATE purchase_orders SET status='received',received_at=? WHERE id=?",
                (utc_now(), purchase_id),
            )
            audit(db, user["username"], "purchase.received", purchase_id)
            return {"id": purchase_id, "status": "received"}

    @application.post("/api/sales", status_code=201)
    def record_sale(body: SaleInput, user=staff):
        with store.transaction(write=True) as db:
            exists(db, "locations", body.location_id, "Location")
            lines, total = [], 0
            for item in body.items:
                product = exists(db, "products", item.product_id, "Product")
                available = db.execute(
                    "SELECT quantity FROM inventory WHERE product_id=? AND location_id=?",
                    (item.product_id, body.location_id),
                ).fetchone()
                quantity = available[0] if available else 0
                if item.quantity > quantity:
                    raise HTTPException(
                        409,
                        f"{product['name']}: only {quantity} units available at this location",
                    )
                lines.append((item, product))
                total += item.quantity * product["price_paise"]
            cursor = db.execute(
                "INSERT INTO sales(sale_date,total_paise,location_id,actor) VALUES (?,?,?,?)",
                (body.sale_date.isoformat(), total, body.location_id, user["username"]),
            )
            identity = cursor.lastrowid
            for item, product in lines:
                db.execute(
                    "INSERT INTO sale_items(sale_id,product_id,quantity,unit_price_paise,unit_cost_paise) VALUES (?,?,?,?,?)",
                    (
                        identity,
                        item.product_id,
                        item.quantity,
                        product["price_paise"],
                        product["cost_paise"],
                    ),
                )
                adjust_stock(
                    db,
                    item.product_id,
                    body.location_id,
                    -item.quantity,
                    "Sale",
                    user["username"],
                    identity,
                )
            audit(
                db,
                user["username"],
                "sale.recorded",
                identity,
                {"total_paise": total, "location": body.location_id},
            )
            return {
                "id": identity,
                "total_paise": total,
                "sale_date": body.sale_date.isoformat(),
            }

    @application.get("/api/sales")
    def sales(
        response: Response,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        user=staff,
    ):
        with store.transaction() as db:
            response.headers["X-Total-Count"] = str(
                db.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
            )
            records = [
                dict(row)
                for row in db.execute(
                    "SELECT s.*,l.name AS location FROM sales s JOIN locations l ON l.id=s.location_id ORDER BY s.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]
            for sale in records:
                sale["items"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT si.id,si.product_id,p.name,p.sku,si.quantity,si.unit_price_paise,COALESCE((SELECT SUM(quantity) FROM return_items WHERE sale_item_id=si.id),0) AS returned FROM sale_items si JOIN products p ON p.id=si.product_id WHERE si.sale_id=?",
                        (sale["id"],),
                    )
                ]
            return records

    @application.post("/api/sales/{sale_id}/returns", status_code=201)
    def return_sale(sale_id: int, body: ReturnInput, user=manager):
        if len({line.sale_item_id for line in body.items}) != len(body.items):
            raise HTTPException(422, "Combine repeated return lines")
        if not body.reason.strip():
            raise HTTPException(422, "Enter a return reason")
        with store.transaction(write=True) as db:
            sale = exists(db, "sales", sale_id, "Sale")
            lines, total = [], 0
            for item in body.items:
                line = exists(db, "sale_items", item.sale_item_id, "Sale line")
                if line["sale_id"] != sale_id:
                    raise HTTPException(422, "That product is not part of this sale")
                returned = db.execute(
                    "SELECT COALESCE(SUM(quantity),0) FROM return_items WHERE sale_item_id=?",
                    (item.sale_item_id,),
                ).fetchone()[0]
                if returned + item.quantity > line["quantity"]:
                    raise HTTPException(
                        409,
                        "Returned quantity cannot exceed the quantity originally sold",
                    )
                lines.append((item, line))
                total += item.quantity * line["unit_price_paise"]
            cursor = db.execute(
                "INSERT INTO returns(sale_id,return_date,total_paise,reason,actor) VALUES (?,?,?,?,?)",
                (
                    sale_id,
                    shop_today().isoformat(),
                    total,
                    body.reason.strip(),
                    user["username"],
                ),
            )
            identity = cursor.lastrowid
            for item, line in lines:
                db.execute(
                    "INSERT INTO return_items(return_id,sale_item_id,quantity,restock) VALUES (?,?,?,?)",
                    (identity, item.sale_item_id, item.quantity, int(item.restock)),
                )
                if item.restock:
                    product = exists(db, "products", line["product_id"], "Product")
                    cost = blended_cost(
                        product["stock"],
                        product["cost_paise"],
                        item.quantity,
                        line["unit_cost_paise"],
                    )
                    adjust_stock(
                        db,
                        line["product_id"],
                        sale["location_id"],
                        item.quantity,
                        f"Return #{identity}",
                        user["username"],
                        sale_id,
                    )
                    db.execute(
                        "UPDATE products SET cost_paise=? WHERE id=?",
                        (cost, line["product_id"]),
                    )
            audit(
                db,
                user["username"],
                "sale.returned",
                identity,
                {"sale_id": sale_id, "refund_paise": total},
            )
            return {"id": identity, "refund_paise": total, "payment_processed": False}

    @application.get("/api/returns")
    def returns(offset: int = Query(0, ge=0), user=manager):
        with store.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM returns ORDER BY id DESC LIMIT 50 OFFSET ?",
                    (offset,),
                )
            ]

    def build_report(db, start, end):
        bounds = (start.isoformat(), end.isoformat())
        totals = dict(
            db.execute(
                "SELECT COUNT(*) AS sales_count,COALESCE(SUM(total_paise),0) AS gross_revenue_paise FROM sales WHERE sale_date BETWEEN ? AND ?",
                bounds,
            ).fetchone()
        )
        refunds = db.execute(
            "SELECT COALESCE(SUM(total_paise),0) FROM returns WHERE return_date BETWEEN ? AND ?",
            bounds,
        ).fetchone()[0]
        cost = db.execute(
            "SELECT COALESCE(SUM(si.quantity*si.unit_cost_paise),0),COALESCE(SUM(CASE WHEN si.unit_cost_paise IS NULL THEN si.quantity ELSE 0 END),0) FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.sale_date BETWEEN ? AND ?",
            bounds,
        ).fetchone()
        reversals = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN ri.restock=1 THEN ri.quantity*si.unit_cost_paise ELSE 0 END),0),COALESCE(SUM(CASE WHEN si.unit_cost_paise IS NULL THEN ri.quantity ELSE 0 END),0) FROM return_items ri JOIN returns r ON r.id=ri.return_id JOIN sale_items si ON si.id=ri.sale_item_id WHERE r.return_date BETWEEN ? AND ?",
            bounds,
        ).fetchone()
        inventory = dict(
            db.execute(
                "SELECT COUNT(*) AS product_count,COALESCE(SUM(stock),0) AS units_in_stock,COALESCE(SUM(CASE WHEN stock<=low_stock_at THEN 1 ELSE 0 END),0) AS low_stock_count FROM products"
            ).fetchone()
        )
        categories = [
            dict(row)
            for row in db.execute(
                "SELECT category,SUM(units) AS units,SUM(revenue_paise) AS revenue_paise FROM (SELECT p.category,SUM(si.quantity) AS units,SUM(si.quantity*si.unit_price_paise) AS revenue_paise FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN products p ON p.id=si.product_id WHERE s.sale_date BETWEEN ? AND ? GROUP BY p.category UNION ALL SELECT p.category,-SUM(ri.quantity),-SUM(ri.quantity*si.unit_price_paise) FROM return_items ri JOIN returns r ON r.id=ri.return_id JOIN sale_items si ON si.id=ri.sale_item_id JOIN products p ON p.id=si.product_id WHERE r.return_date BETWEEN ? AND ? GROUP BY p.category) GROUP BY category ORDER BY revenue_paise DESC",
                [*bounds, *bounds],
            )
        ]
        daily = [
            dict(row)
            for row in db.execute(
                "SELECT sale_date,SUM(amount) AS revenue_paise FROM (SELECT sale_date,total_paise AS amount FROM sales WHERE sale_date BETWEEN ? AND ? UNION ALL SELECT return_date,-total_paise FROM returns WHERE return_date BETWEEN ? AND ?) GROUP BY sale_date ORDER BY sale_date",
                [*bounds, *bounds],
            )
        ]
        top = [
            dict(row)
            for row in db.execute(
                "SELECT p.sku,p.name,SUM(si.quantity) AS units,SUM(si.quantity*si.unit_price_paise) AS revenue_paise FROM sale_items si JOIN products p ON p.id=si.product_id JOIN sales s ON s.id=si.sale_id WHERE s.sale_date BETWEEN ? AND ? GROUP BY p.id ORDER BY units DESC LIMIT 10",
                bounds,
            )
        ]
        return {
            **totals,
            **inventory,
            "refund_paise": refunds,
            "revenue_paise": totals["gross_revenue_paise"] - refunds,
            "cost_of_goods_paise": cost[0] - reversals[0],
            "uncosted_units": cost[1] + reversals[1],
            "gross_profit_paise": None
            if cost[1] + reversals[1]
            else totals["gross_revenue_paise"] - refunds - cost[0] + reversals[0],
            "start": bounds[0],
            "end": bounds[1],
            "today": shop_today().isoformat(),
            "categories": categories,
            "daily": daily,
            "top_products": top,
        }

    @application.get("/api/reports")
    def reports(start: date | None = None, end: date | None = None, user=manager):
        today = shop_today()
        start, end = start or today.replace(day=1), end or today
        if start > end:
            raise HTTPException(422, "Start date must be on or before end date")
        with store.transaction() as db:
            return build_report(db, start, end)

    @application.get("/api/summary")
    def summary(user=staff):
        with store.transaction() as db:
            if user["role"] != "cashier":
                return build_report(db, shop_today().replace(day=1), shop_today())
            data = dict(
                db.execute(
                    "SELECT COUNT(*) AS product_count,COALESCE(SUM(stock),0) AS units_in_stock,COALESCE(SUM(CASE WHEN stock<=low_stock_at THEN 1 ELSE 0 END),0) AS low_stock_count FROM products"
                ).fetchone()
            )
            return {**data, "today": shop_today().isoformat()}

    @application.get("/api/reports/export")
    def export_report(start: date | None = None, end: date | None = None, user=manager):
        today = shop_today()
        start, end = start or today.replace(day=1), end or today
        if start > end:
            raise HTTPException(422, "Start date must be on or before end date")
        with store.transaction() as db:
            data = build_report(db, start, end)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Metric", "Value (paise for money)"])
        for key in (
            "gross_revenue_paise",
            "refund_paise",
            "revenue_paise",
            "cost_of_goods_paise",
            "gross_profit_paise",
            "uncosted_units",
            "sales_count",
        ):
            writer.writerow(
                [
                    key,
                    data[key]
                    if data[key] is not None
                    else "Unknown: missing historical cost",
                ]
            )
        writer.writerow([])
        writer.writerow(["Category", "Net units", "Net revenue paise"])
        for row in data["categories"]:
            name = row["category"]
            if name.lstrip().startswith(("=", "+", "-", "@")):
                name = "'" + name
            writer.writerow([name, row["units"], row["revenue_paise"]])
        return RawResponse(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=stockdesk-report.csv"
            },
        )

    @application.get("/api/reorders")
    def reorders(user=manager):
        import math

        start = (shop_today() - timedelta(days=29)).isoformat()
        with store.transaction() as db:
            rows = db.execute(
                "SELECT p.*,s.name AS supplier,COALESCE(s.lead_days,7) AS lead_days,COALESCE((SELECT SUM(si.quantity) FROM sale_items si JOIN sales sa ON sa.id=si.sale_id WHERE si.product_id=p.id AND sa.sale_date>=?),0) AS sold_30,COALESCE((SELECT SUM(pi.quantity) FROM purchase_items pi JOIN purchase_orders po ON po.id=pi.purchase_id WHERE pi.product_id=p.id AND po.status='ordered'),0) AS on_order FROM products p LEFT JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.stock",
                (start,),
            ).fetchall()
            result = []
            for record in rows:
                row = dict(record)
                target = max(
                    row["low_stock_at"] + 1,
                    math.ceil(row["sold_30"] / 30 * (row["lead_days"] + 7)),
                )
                row["target_stock"] = target
                row["suggested_quantity"] = max(
                    0, target - row["stock"] - row["on_order"]
                )
                if row["suggested_quantity"]:
                    result.append(row)
            return result

    @application.get("/api/operations")
    def operations(user=manager):
        with store.transaction() as db:
            mismatches = [
                dict(row)
                for row in db.execute(
                    "SELECT p.id,p.sku,p.stock,COALESCE(SUM(i.quantity),0) AS counted FROM products p LEFT JOIN inventory i ON i.product_id=p.id GROUP BY p.id HAVING p.stock!=COALESCE(SUM(i.quantity),0)"
                )
            ]
            sale_errors = db.execute(
                "SELECT COUNT(*) FROM (SELECT s.id FROM sales s JOIN sale_items si ON si.sale_id=s.id GROUP BY s.id HAVING s.total_paise!=SUM(si.quantity*si.unit_price_paise))"
            ).fetchone()[0]
            rejected = db.execute(
                "SELECT COUNT(*) FROM import_batches WHERE status='rejected'"
            ).fetchone()[0]
        return {
            "uptime_seconds": int(time.monotonic() - counters["started"]),
            "requests": counters["requests"],
            "rejected_requests": counters["rejected"],
            "server_errors": counters["server_errors"],
            "rejected_imports": rejected,
            "stock_mismatches": mismatches,
            "sale_total_mismatches": sale_errors,
        }

    @application.get("/api/activity")
    def activity(
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), user=manager
    ):
        with store.transaction() as db:
            return [
                {**dict(row), "details": json.loads(row["details"])}
                for row in db.execute(
                    "SELECT * FROM activity ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]

    @application.get("/api/mapping-profiles")
    def profiles(user=manager):
        with store.transaction() as db:
            return [
                {**dict(row), "mapping": json.loads(row["mapping_json"])}
                for row in db.execute("SELECT * FROM mapping_profiles ORDER BY id DESC")
            ]

    @application.post("/api/mapping-profiles", status_code=201)
    def save_profile(body: ProfileInput, user=manager):
        try:
            validate_mapping(body.mapping)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        with store.transaction(write=True) as db:
            supplier_exists(db, body.supplier_id)
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM mapping_profiles WHERE name=?",
                (body.name,),
            ).fetchone()[0]
            cursor = db.execute(
                "INSERT INTO mapping_profiles(name,supplier_id,version,mapping_json,actor) VALUES (?,?,?,?,?)",
                (
                    body.name,
                    body.supplier_id,
                    version,
                    json.dumps(body.mapping),
                    user["username"],
                ),
            )
            audit(
                db,
                user["username"],
                "mapping.saved",
                cursor.lastrowid,
                {"version": version},
            )
            return {"id": cursor.lastrowid, "version": version}

    def run_import(raw, kind, mapping, source, supplier_id, location_id, user):
        errors = []
        parsed = []
        try:
            rows = read_records(raw, kind)
            parsed, errors = convert_records(
                rows, mapping, supplier_id, location_id, kind
            )
        except ValueError as exc:
            errors = [{"row": 0, "error": str(exc)}]
        with store.transaction(write=True) as db:
            supplier_exists(db, supplier_id)
            exists(db, "locations", location_id, "Location")
            if not errors:
                errors = [
                    {
                        "row": index,
                        "sku": product.sku,
                        "error": "SKU already exists; use purchases or restock",
                    }
                    for index, product in enumerate(
                        parsed, start=2 if kind == "csv" else 1
                    )
                    if db.execute(
                        "SELECT 1 FROM products WHERE sku=?", (product.sku,)
                    ).fetchone()
                ]
            if not errors:
                for product in parsed:
                    insert_product(db, product, user["username"])
            cursor = db.execute(
                "INSERT INTO import_batches(source,supplier_id,location_id,mapping_json,status,imported,errors_json,actor) VALUES (?,?,?,?,?,?,?,?)",
                (
                    source[:120],
                    supplier_id,
                    location_id,
                    json.dumps(mapping),
                    "rejected" if errors else "imported",
                    0 if errors else len(parsed),
                    json.dumps(errors),
                    user["username"],
                ),
            )
            identity = cursor.lastrowid
            audit(
                db,
                user["username"],
                "import.rejected" if errors else "import.completed",
                identity,
                {
                    "source": source[:120],
                    "rows": len(parsed),
                    "error_count": len(errors),
                },
            )
        if errors:
            raise HTTPException(
                422,
                {
                    "message": "Nothing imported. Correct the errors and try again.",
                    "batch_id": identity,
                    "errors": errors,
                },
            )
        return {"imported": len(parsed), "batch_id": identity}

    @application.post("/api/imports/preview")
    async def preview(
        file: Annotated[UploadFile, File()], kind: str = Form("csv"), user=manager
    ):
        raw = await file.read(256 * 1024 + 1)
        await file.close()
        try:
            rows = read_records(raw, kind)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        headers = list(dict.fromkeys(column for row in rows for column in row))
        return {
            "headers": headers,
            "rows": [
                {key: str(value) for key, value in row.items()} for row in rows[:5]
            ],
            "total": len(rows),
            "fields": FIELDS,
        }

    @application.post("/api/products/import", status_code=201)
    async def import_products(
        file: Annotated[UploadFile, File()],
        kind: str = Form("csv"),
        mapping: str = Form(""),
        supplier_id: int | None = Form(None),
        location_id: int = Form(1),
        user=manager,
    ):
        source = file.filename or "Upload"
        raw = await file.read(256 * 1024 + 1)
        await file.close()
        if len(mapping) > 8192:
            raise HTTPException(422, "Mapping is too large")
        try:
            chosen = json.loads(mapping) if mapping else STANDARD
        except ValueError:
            raise HTTPException(422, "Mapping must be valid JSON") from None
        if not isinstance(chosen, dict):
            raise HTTPException(422, "Mapping must be an object")
        return run_import(raw, kind, chosen, source, supplier_id, location_id, user)

    @application.post("/api/imports/supplier-api", status_code=201)
    async def supplier_api(supplier_id: int, location_id: int = 1, user=manager):
        # Fixed local demo endpoint: no arbitrary URL fetching or third-party credentials.
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                async with client.stream(
                    "GET", "http://127.0.0.1:8701/catalog"
                ) as response:
                    response.raise_for_status()
                    raw = b""
                    async for chunk in response.aiter_bytes():
                        raw += chunk
                        if len(raw) > 256 * 1024:
                            raise ValueError("Supplier response is too large")
        except (httpx.HTTPError, ValueError):
            with store.transaction(write=True) as db:
                audit(
                    db,
                    user["username"],
                    "supplier_api.failed",
                    supplier_id,
                    {"message": "Demo supplier unavailable"},
                )
            raise HTTPException(
                502,
                "The demo supplier API is unavailable. Start it with python run.py and try again.",
            ) from None
        return run_import(
            raw,
            "json",
            SUPPLIER,
            "Demo supplier HTTP API",
            supplier_id,
            location_id,
            user,
        )

    @application.get("/api/imports")
    def imports(offset: int = Query(0, ge=0), user=manager):
        with store.transaction() as db:
            return [
                {
                    **dict(row),
                    "errors": json.loads(row["errors_json"]),
                    "mapping": json.loads(row["mapping_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM import_batches ORDER BY id DESC LIMIT 50 OFFSET ?",
                    (offset,),
                )
            ]

    @application.get("/api/sample.csv")
    def sample_csv(user=staff):
        return FileResponse(
            ROOT / "samples" / "products.csv", filename="stockdesk-products.csv"
        )

    @application.get("/api/backups")
    def backups(user=owner):
        return [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "created_at": path.stat().st_mtime,
                "schema_version": store.backup_version(path.name),
            }
            for path in sorted(
                store.backups.glob("*.sqlite3"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:50]
        ]

    @application.post("/api/backups", status_code=201)
    def backup(user=owner):
        name = store.backup()
        with store.transaction(write=True) as db:
            audit(db, user["username"], "backup.created", name)
        return {"name": name}

    @application.get("/api/backups/{name}")
    def download_backup(name: str, user=owner):
        return FileResponse(store.backup_path(name), filename=name)

    @application.post("/api/backups/{name}/restore")
    def restore(
        name: str, body: RestoreInput, response: Response, request: Request, user=owner
    ):
        if body.confirmation != "RESTORE":
            raise HTTPException(
                422, "Type RESTORE to confirm replacing current shop data"
            )
        with store.transaction() as db:
            saved = exists(db, "users", user["id"], "Owner")
            if not verify_password(body.password, saved["password_hash"]):
                raise HTTPException(403, "Your password is incorrect")
        safety = store.restore(
            name,
            user["username"],
            token_hash(request.cookies.get("stockdesk_session", "")),
        )
        response.delete_cookie("stockdesk_session")
        return {"restored": name, "safety_backup": safety, "sign_in_required": True}

    return application


app = create_app()
