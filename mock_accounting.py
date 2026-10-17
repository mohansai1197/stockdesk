"""A separate, persistent fictional accounting service with authenticated APIs."""

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from integration_secrets import demo_api_key

ROOT = Path(__file__).resolve().parent
SOURCE_PATH = ROOT / ".data" / "mock-accounting.sqlite3"
MODES = Literal[
    "normal",
    "temporary_outage",
    "rate_limit",
    "page_two_outage",
    "slow_page",
    "invalid_data",
    "changed_invoice",
]


def prepare_demo_source(shop_path, source_path=SOURCE_PATH):
    """Provision fictional invoices once from a snapshot of available demo orders.

    The running accounting service reads only its own database, never StockDesk.
    Existing source records are retained unchanged on restart.
    """
    path = Path(source_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as source:
        source.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS invoices(position INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS suppliers(code TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS faults(id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT NOT NULL,remaining INTEGER NOT NULL);
            INSERT OR IGNORE INTO faults VALUES(1,'normal',0);
        """)
        if source.execute("SELECT 1 FROM invoices LIMIT 1").fetchone():
            return source.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        with sqlite3.connect(shop_path) as shop:
            shop.row_factory = sqlite3.Row
            vendors = shop.execute(
                "SELECT id,name FROM suppliers ORDER BY id"
            ).fetchall()
            orders = []
            for order in shop.execute("SELECT * FROM purchase_orders ORDER BY id"):
                lines = [
                    dict(row)
                    for row in shop.execute(
                        "SELECT pi.product_id,pi.quantity,pi.cost_paise,p.sku FROM purchase_items pi JOIN products p ON p.id=pi.product_id WHERE purchase_id=? ORDER BY pi.id",
                        (order["id"],),
                    )
                ]
                if lines:
                    orders.append({**dict(order), "lines": lines})
        if not orders:
            return 0
        dataset = uuid.uuid4().hex
        for vendor in vendors:
            source.execute(
                "INSERT OR REPLACE INTO suppliers VALUES (?,?)",
                (f"SUP-{vendor['id']:04d}", vendor["name"]),
            )
        position = 0
        while position < 250:
            made_progress = False
            for order in orders:
                available = next(
                    (line for line in order["lines"] if line["quantity"] > 0), None
                )
                if available is None:
                    continue
                position += 1
                available["quantity"] -= 1
                amount = f"{available['cost_paise'] / 100:.2f}"
                payload = {
                    "id": f"{dataset}-invoice-{position:04d}",
                    "invoice_no": f"DEMO-{dataset[:6].upper()}-{position:04d}",
                    "invoice_date": datetime.fromisoformat(order["created_at"])
                    .astimezone(ZoneInfo("Asia/Kolkata"))
                    .date()
                    .isoformat(),
                    "vendor_ref": f"SUP-{order['supplier_id']:04d}",
                    "purchase_ref": f"SD-PO-{order['id']:06d}",
                    "currency": "INR",
                    "lines": [
                        {
                            "item_code": available["sku"],
                            "units": 1,
                            "unit_cost": amount,
                            "line_total": amount,
                        }
                    ],
                    "amount_total": amount,
                }
                source.execute(
                    "INSERT INTO invoices VALUES (?,?)", (position, json.dumps(payload))
                )
                made_progress = True
                if position == 250:
                    break
            if not made_progress:
                break
        source.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('dataset',?)", (dataset,)
        )
        return position


class FaultInput(BaseModel):
    mode: MODES


def create_accounting_app(source_path=SOURCE_PATH, api_key=None):
    application = FastAPI(title="StockDesk Mock Accounting Service", version="1.0.0")
    path = Path(source_path)

    def authenticated(x_api_key: str = Header(default="")):
        expected = api_key if api_key is not None else demo_api_key()
        if not hmac.compare_digest(x_api_key.encode(), expected.encode()):
            raise HTTPException(401, "Invalid accounting API key")

    @application.get("/health")
    def health():
        return {"status": "ok", "simulated": True}

    @application.get("/suppliers", dependencies=[Depends(authenticated)])
    def suppliers():
        with sqlite3.connect(path) as db:
            return [
                {"code": row[0], "name": row[1]}
                for row in db.execute("SELECT code,name FROM suppliers ORDER BY code")
            ]

    @application.get("/demo/fault", dependencies=[Depends(authenticated)])
    def fault_status():
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT mode,remaining FROM faults WHERE id=1").fetchone()
        return {"mode": row[0], "remaining": row[1]}

    @application.post("/demo/fault", dependencies=[Depends(authenticated)])
    def set_fault(body: FaultInput):
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE faults SET mode=?,remaining=? WHERE id=1",
                (
                    body.mode,
                    1 if body.mode in ("temporary_outage", "rate_limit") else 0,
                ),
            )
        return {"mode": body.mode}

    @application.get("/invoices", dependencies=[Depends(authenticated)])
    async def invoices(
        cursor: str | None = Query(None, max_length=100),
        limit: int = Query(100, ge=1, le=100),
    ):
        with sqlite3.connect(path) as db:
            metadata = db.execute(
                "SELECT value FROM metadata WHERE key='dataset'"
            ).fetchone()
            if not metadata:
                raise HTTPException(
                    409,
                    "No demo purchase orders were available when the source was prepared",
                )
            dataset = metadata[0]
            offset = 0
            if cursor:
                prefix, separator, number = cursor.partition(":")
                if (
                    prefix != dataset
                    or separator != ":"
                    or not number.isascii()
                    or not number.isdigit()
                ):
                    raise HTTPException(400, "Invalid invoice cursor")
                offset = int(number)
            mode, remaining = db.execute(
                "SELECT mode,remaining FROM faults WHERE id=1"
            ).fetchone()
            if mode == "temporary_outage" and remaining:
                db.execute("UPDATE faults SET remaining=remaining-1 WHERE id=1")
                return JSONResponse(
                    {"error": "Simulated temporary outage"}, status_code=503
                )
            if mode == "rate_limit" and offset >= 100 and remaining:
                db.execute("UPDATE faults SET remaining=remaining-1 WHERE id=1")
                return JSONResponse(
                    {"error": "Simulated rate limit"},
                    status_code=429,
                    headers={"Retry-After": "1"},
                )
            if mode == "page_two_outage" and offset >= 100:
                return JSONResponse(
                    {"error": "Simulated page-two outage"}, status_code=503
                )
            rows = db.execute(
                "SELECT position,payload FROM invoices ORDER BY position LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        if mode == "slow_page" and offset >= 100:
            await asyncio.sleep(3)
        items = []
        for position, payload in rows:
            item = json.loads(payload)
            if mode == "invalid_data":
                if position == 1:
                    item["amount_total"] = "999999.99"
                if position == 2:
                    item["vendor_ref"] = "UNKNOWN-VENDOR"
                if position == 3:
                    item["invoice_date"] = "2026-02-30"
            if mode == "changed_invoice" and position == 1:
                from decimal import Decimal

                value = (
                    f"{Decimal(item['lines'][0]['unit_cost']) + Decimal('1.00'):.2f}"
                )
                item["lines"][0].update(unit_cost=value, line_total=value)
                item["amount_total"] = value
            items.append(item)
        data_mode = mode if mode in ("invalid_data", "changed_invoice") else "normal"
        snapshot = hashlib.sha256(f"{dataset}:{data_mode}".encode()).hexdigest()
        next_offset = offset + len(items)
        return {
            "items": items,
            "next_cursor": f"{dataset}:{next_offset}" if next_offset < total else None,
            "snapshot": snapshot,
            "total": total,
        }

    return application


app = create_accounting_app(os.environ.get("STOCKDESK_ACCOUNTING_DB", SOURCE_PATH))
