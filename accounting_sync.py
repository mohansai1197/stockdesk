"""Paginated invoice ingestion with durable run history and connection-scoped identity."""

import asyncio
import csv
import hashlib
import io
import json
import re
import sqlite3
import time
import uuid
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

import httpx
from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError, field_validator

from database import audit, utc_now
from integration_secrets import connection_key, demo_api_key, save_connection_key
from models import SHOP_TIMEZONE, shop_today, to_paise

BASE_URL = "http://127.0.0.1:8703"
FIELD_MAPPING = {
    "id": "source_id",
    "invoice_no": "invoice_number",
    "invoice_date": "invoice_date",
    "vendor_ref": "supplier_code",
    "purchase_ref": "purchase_reference",
    "amount_total": "total_paise",
    "currency": "currency",
    "lines[].item_code": "lines[].sku",
    "lines[].units": "lines[].quantity",
    "lines[].unit_cost": "lines[].unit_cost_paise",
    "lines[].line_total": "lines[].line_total_paise",
}


class SourceLine(BaseModel):
    item_code: str = Field(min_length=1, max_length=32)
    units: int = Field(gt=0, le=1000000, strict=True)
    unit_cost: str = Field(max_length=24, strict=True)
    line_total: str = Field(max_length=24, strict=True)


class SourceInvoice(BaseModel):
    id: str = Field(min_length=1, max_length=100, strict=True)
    invoice_no: str = Field(min_length=1, max_length=100, strict=True)
    invoice_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    vendor_ref: str = Field(min_length=1, max_length=80)
    purchase_ref: str = Field(min_length=1, max_length=80)
    currency: str = Field(pattern=r"^INR$")
    lines: list[SourceLine] = Field(min_length=1, max_length=50)
    amount_total: str = Field(max_length=24, strict=True)

    @field_validator("id", "invoice_no", "vendor_ref", "purchase_ref")
    @classmethod
    def trim_nonempty(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Cannot be blank")
        return value


def canonical_invoice(raw):
    item = SourceInvoice.model_validate(raw)
    try:
        invoice_date = date.fromisoformat(item.invoice_date)
    except ValueError:
        raise InvoiceProblem(
            "INVALID_DATE", "invoice_date is not a real calendar date"
        ) from None
    if invoice_date > shop_today():
        raise InvoiceProblem("INVALID_DATE", "invoice_date cannot be in the future")
    lines = []
    codes = set()
    for number, line in enumerate(item.lines, 1):
        sku = line.item_code.strip().upper()
        if sku in codes:
            raise InvoiceProblem(
                "DUPLICATE_LINE", f"Line {number}: combine repeated product {sku}"
            )
        codes.add(sku)
        try:
            cost, total = to_paise(line.unit_cost), to_paise(line.line_total)
        except ValueError as exc:
            raise InvoiceProblem("INVALID_AMOUNT", f"Line {number}: {exc}") from None
        if cost * line.units != total:
            raise InvoiceProblem(
                "LINE_TOTAL_MISMATCH",
                f"Line {number}: quantity × unit cost does not equal line total",
            )
        lines.append(
            {
                "sku": sku,
                "quantity": line.units,
                "unit_cost_paise": cost,
                "line_total_paise": total,
            }
        )
    try:
        total = to_paise(item.amount_total)
    except ValueError as exc:
        raise InvoiceProblem("INVALID_AMOUNT", str(exc)) from None
    if sum(line["line_total_paise"] for line in lines) != total:
        raise InvoiceProblem(
            "INVOICE_TOTAL_MISMATCH", "Sum of line amounts does not equal amount_total"
        )
    return {
        "source_id": item.id,
        "invoice_number": item.invoice_no,
        "invoice_date": invoice_date.isoformat(),
        "supplier_code": item.vendor_ref,
        "purchase_reference": item.purchase_ref,
        "currency": item.currency,
        "lines": sorted(lines, key=lambda line: line["sku"]),
        "total_paise": total,
    }


class InvoiceProblem(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class SyncFailure(InvoiceProblem):
    pass


class ConnectionInput(BaseModel):
    name: str = Field(default="Demo accounting", min_length=1, max_length=80)
    use_demo_key: bool = True
    api_key: str | None = Field(default=None, min_length=8, max_length=256)


class MappingInput(BaseModel):
    source_code: str = Field(min_length=1, max_length=80)
    supplier_id: int | None = Field(default=None, gt=0, strict=True)


class FaultInput(BaseModel):
    mode: str = Field(
        pattern=r"^(normal|temporary_outage|rate_limit|page_two_outage|slow_page|invalid_data|changed_invoice)$"
    )


def retry_delay(header, attempt):
    fallback = 0.25 * (2 ** (attempt - 1))
    if not header:
        return fallback
    try:
        return max(0, float(header))
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0, when.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return fallback


class InvoiceSync:
    def __init__(self, store):
        self.store = store
        self.sleep = asyncio.sleep
        self.client_factory = lambda: httpx.AsyncClient(
            timeout=httpx.Timeout(1.5, connect=1.0),
            trust_env=False,
            follow_redirects=False,
        )
        self.budget_seconds = 30

    def configuration(self):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM accounting_connections WHERE id=1"
            ).fetchone()
            if not row:
                return None
            result = {key: row[key] for key in ("id", "name", "base_url", "created_at")}
            result["key_configured"] = bool(
                connection_key(self.store, row["secret_ref"])
            )
            result["syncing"] = bool(
                row["lease_until"] and row["lease_until"] > time.time()
            )
            result["mappings"] = [
                dict(item)
                for item in db.execute(
                    "SELECT m.source_code,m.source_name,m.supplier_id,s.name AS supplier FROM accounting_supplier_mappings m LEFT JOIN suppliers s ON s.id=m.supplier_id WHERE m.connection_id=1 ORDER BY source_code"
                )
            ]
            return result

    def credentials(self):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM accounting_connections WHERE id=1"
            ).fetchone()
        if not row:
            raise HTTPException(409, "Configure the accounting connection first")
        key = connection_key(self.store, row["secret_ref"])
        if not key:
            raise HTTPException(
                409,
                "The connection key is missing. Re-enter it in connection settings.",
            )
        return dict(row), key

    def busy_guard(self, db):
        row = db.execute(
            "SELECT lease_until FROM accounting_connections WHERE id=1"
        ).fetchone()
        if row and row[0] and row[0] > time.time():
            raise HTTPException(
                409,
                "An invoice sync is running. Wait before changing the connection or mappings.",
            )

    async def small_request(self, path, method="GET", body=None):
        connection, key = self.credentials()
        try:
            async with (
                self.client_factory() as client,
                asyncio.timeout(1.5),
                client.stream(
                    method,
                    connection["base_url"] + path,
                    headers={"X-API-Key": key},
                    json=body,
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise HTTPException(
                        422,
                        "The accounting API key was rejected. Fix connection settings or select Use demo key.",
                    )
                if response.status_code != 200:
                    raise HTTPException(
                        502,
                        "The mock accounting service is unavailable or has no prepared demo data.",
                    )
                content = b""
                async for chunk in response.aiter_bytes():
                    content += chunk
                    if len(content) > 128 * 1024:
                        raise HTTPException(
                            502, "Accounting response exceeded its size limit"
                        )
                return json.loads(content)
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise HTTPException(
                502,
                "Cannot reach the mock accounting service. Start StockDesk with python run.py and try again.",
            ) from None

    async def refresh_suppliers(self, actor):
        vendors = await self.small_request("/suppliers")
        if not isinstance(vendors, list) or len(vendors) > 100:
            raise HTTPException(502, "Accounting supplier list is invalid")
        with self.store.transaction(write=True) as db:
            self.busy_guard(db)
            for vendor in vendors:
                if (
                    not isinstance(vendor, dict)
                    or not isinstance(vendor.get("code"), str)
                    or not isinstance(vendor.get("name"), str)
                ):
                    raise HTTPException(502, "Accounting supplier list is invalid")
                code, name = vendor["code"][:80], vendor["name"][:100]
                matches = db.execute(
                    "SELECT id FROM suppliers WHERE name=? COLLATE NOCASE", (name,)
                ).fetchall()
                matched = matches[0][0] if len(matches) == 1 else None
                db.execute(
                    "INSERT INTO accounting_supplier_mappings(connection_id,source_code,source_name,supplier_id) VALUES (1,?,?,?) ON CONFLICT(connection_id,source_code) DO UPDATE SET source_name=excluded.source_name",
                    (code, name, matched),
                )
            audit(
                db, actor, "accounting.suppliers_refreshed", 1, {"count": len(vendors)}
            )
        return self.configuration()

    async def save_configuration(self, body, actor):
        with self.store.transaction(write=True) as db:
            self.busy_guard(db)
            previous = db.execute(
                "SELECT * FROM accounting_connections WHERE id=1"
            ).fetchone()
            key = demo_api_key() if body.use_demo_key else body.api_key
            if key is None:
                key = (
                    connection_key(self.store, previous["secret_ref"])
                    if previous
                    else None
                )
            if not key:
                raise HTTPException(422, "Enter an API key or select Use demo key")
            reference = uuid.uuid4().hex
            save_connection_key(self.store, reference, key)
            db.execute(
                "INSERT INTO accounting_connections(id,name,base_url,secret_ref) VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,secret_ref=excluded.secret_ref",
                (body.name.strip(), BASE_URL, reference),
            )
            audit(
                db, actor, "accounting.connection_saved", 1, {"name": body.name.strip()}
            )
        result = self.configuration()
        try:
            return await self.refresh_suppliers(actor)
        except HTTPException as exc:
            result["source_error"] = exc.detail
            return result

    def renew(self, db, token):
        cursor = db.execute(
            "UPDATE accounting_connections SET lease_until=? WHERE id=1 AND lease_token=?",
            (time.time() + 60, token),
        )
        if cursor.rowcount != 1:
            raise SyncFailure(
                "LEASE_LOST", "This sync lost its connection claim. Start a new sync."
            )

    def attempt(self, run_id, token, page, attempt, status, code, message, delay=0):
        with self.store.transaction(write=True) as db:
            self.renew(db, token)
            db.execute(
                "INSERT INTO accounting_sync_attempts(run_id,page,attempt,status_code,code,message,retry_delay) VALUES (?,?,?,?,?,?,?)",
                (run_id, page, attempt, status, code, message, delay),
            )

    async def fetch_page(
        self, client, connection, key, run_id, token, page, cursor, deadline
    ):
        for attempt in range(1, 4):
            if time.monotonic() >= deadline:
                raise SyncFailure(
                    "TIME_BUDGET", "Sync time limit reached. Retry to safely continue."
                )
            with self.store.transaction(write=True) as db:
                self.renew(db, token)
            status = None
            retry_after = None
            retryable = False
            try:
                params = {"limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                async with (
                    asyncio.timeout(min(1.5, max(0, deadline - time.monotonic()))),
                    client.stream(
                        "GET",
                        connection["base_url"] + "/invoices",
                        params=params,
                        headers={"X-API-Key": key},
                    ) as response,
                ):
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    if status == 200:
                        content = b""
                        async for chunk in response.aiter_bytes():
                            content += chunk
                            if len(content) > 1024 * 1024:
                                raise SyncFailure(
                                    "BAD_RESPONSE", "An invoice page exceeded 1 MiB"
                                )
                            if time.monotonic() > deadline:
                                raise SyncFailure(
                                    "TIME_BUDGET",
                                    "Sync time limit reached. Retry to safely continue.",
                                )
                        try:
                            payload = json.loads(content)
                        except ValueError:
                            raise SyncFailure(
                                "BAD_RESPONSE", "Accounting returned invalid JSON"
                            ) from None
                        self.attempt(
                            run_id, token, page, attempt, status, "OK", "Page fetched"
                        )
                        return payload
                    if status in (401, 403):
                        code, message = (
                            "AUTH_FAILED",
                            "Accounting API key rejected. Fix connection settings before retrying.",
                        )
                    elif status == 429:
                        code, message, retryable = (
                            "RATE_LIMITED",
                            "Accounting asked this connection to slow down.",
                            True,
                        )
                    elif status == 408 or status >= 500:
                        code, message, retryable = (
                            "UNAVAILABLE",
                            "Accounting service temporarily unavailable.",
                            True,
                        )
                    else:
                        code, message = (
                            "SOURCE_REJECTED",
                            f"Accounting rejected the page request (HTTP {status}).",
                        )
            except (httpx.TimeoutException, TimeoutError):
                code, message, retryable = (
                    "TIMEOUT",
                    "Accounting request timed out.",
                    True,
                )
            except httpx.HTTPError:
                code, message, retryable = (
                    "NETWORK_ERROR",
                    "Could not reach accounting service.",
                    True,
                )
            except SyncFailure as exc:
                self.attempt(
                    run_id, token, page, attempt, status, exc.code, exc.message
                )
                raise
            delay = (
                retry_delay(retry_after, attempt) if retryable and attempt < 3 else 0
            )
            should_retry = (
                retryable
                and attempt < 3
                and delay <= 5
                and time.monotonic() + delay < deadline
            )
            self.attempt(
                run_id,
                token,
                page,
                attempt,
                status,
                code,
                message,
                delay if should_retry else 0,
            )
            if not should_retry:
                if retryable and delay > 5:
                    message += " Retry later; its requested wait exceeds this short sync budget."
                raise SyncFailure(code, message)
            await self.sleep(delay)
        raise SyncFailure("UNAVAILABLE", "Retry limit reached")

    def save_invoice(self, db, raw, run_id, mappings):
        item = canonical_invoice(raw)
        text = json.dumps(item, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(text.encode()).hexdigest()
        original = db.execute(
            "SELECT payload_hash FROM accounting_invoices WHERE connection_id=1 AND source_id=?",
            (item["source_id"],),
        ).fetchone()
        if original:
            if original[0] == payload_hash:
                return "duplicates"
            raise InvoiceProblem(
                "SOURCE_CHANGED",
                "This source invoice was already imported with different data. The original was preserved; review the change explicitly.",
            )
        supplier_id = mappings.get(item["supplier_code"])
        if supplier_id is None:
            raise InvoiceProblem(
                "UNKNOWN_SUPPLIER",
                f"Map accounting supplier {item['supplier_code']} to a StockDesk supplier",
            )
        match = re.fullmatch(r"SD-PO-(\d{1,9})", item["purchase_reference"])
        if not match:
            raise InvoiceProblem(
                "UNKNOWN_PURCHASE",
                "purchase_ref must identify a StockDesk order, for example SD-PO-000001",
            )
        purchase_id = int(match[1])
        purchase = db.execute(
            "SELECT * FROM purchase_orders WHERE id=?", (purchase_id,)
        ).fetchone()
        if not purchase:
            raise InvoiceProblem(
                "UNKNOWN_PURCHASE", f"Purchase order #{purchase_id} was not found"
            )
        if purchase["supplier_id"] != supplier_id:
            raise InvoiceProblem(
                "SUPPLIER_MISMATCH",
                "The mapped supplier does not match the purchase order supplier",
            )
        ordered = (
            datetime.fromisoformat(purchase["created_at"])
            .astimezone(SHOP_TIMEZONE)
            .date()
        )
        if date.fromisoformat(item["invoice_date"]) < ordered:
            raise InvoiceProblem(
                "INVALID_DATE", "Invoice date is before its purchase order date"
            )
        if db.execute(
            "SELECT 1 FROM accounting_invoices WHERE supplier_id=? AND invoice_number=?",
            (supplier_id, item["invoice_number"]),
        ).fetchone():
            raise InvoiceProblem(
                "DUPLICATE_NUMBER",
                "This supplier invoice number already belongs to another source invoice",
            )
        linked = []
        for line in item["lines"]:
            order_line = db.execute(
                "SELECT pi.*,p.id AS product_id FROM purchase_items pi JOIN products p ON p.id=pi.product_id WHERE purchase_id=? AND p.sku=?",
                (purchase_id, line["sku"]),
            ).fetchone()
            if not order_line:
                raise InvoiceProblem(
                    "PO_LINE_MISMATCH", f"{line['sku']} is not on this purchase order"
                )
            if order_line["cost_paise"] != line["unit_cost_paise"]:
                raise InvoiceProblem(
                    "PO_LINE_MISMATCH",
                    f"{line['sku']}: unit cost differs from the purchase order",
                )
            billed = db.execute(
                "SELECT COALESCE(SUM(ii.quantity),0) FROM accounting_invoice_items ii JOIN accounting_invoices i ON i.id=ii.invoice_id WHERE i.purchase_id=? AND ii.product_id=?",
                (purchase_id, order_line["product_id"]),
            ).fetchone()[0]
            if billed + line["quantity"] > order_line["quantity"]:
                raise InvoiceProblem(
                    "OVER_BILLED",
                    f"{line['sku']}: cumulative invoiced units exceed the ordered quantity",
                )
            linked.append((order_line["product_id"], line))
        cursor = db.execute(
            "INSERT INTO accounting_invoices(connection_id,source_id,invoice_number,invoice_date,supplier_id,purchase_id,currency,total_paise,payload_hash,source_json,run_id) VALUES (1,?,?,?,?,?,?,?,?,?,?)",
            (
                item["source_id"],
                item["invoice_number"],
                item["invoice_date"],
                supplier_id,
                purchase_id,
                item["currency"],
                item["total_paise"],
                payload_hash,
                text,
                run_id,
            ),
        )
        identity = cursor.lastrowid
        for product_id, line in linked:
            db.execute(
                "INSERT INTO accounting_invoice_items(invoice_id,product_id,quantity,unit_cost_paise,line_total_paise) VALUES (?,?,?,?,?)",
                (
                    identity,
                    product_id,
                    line["quantity"],
                    line["unit_cost_paise"],
                    line["line_total_paise"],
                ),
            )
        return "imported"

    def save_page(self, payload, run_id, token, page, mappings):
        counts = {"imported": 0, "duplicates": 0, "errors": 0}
        with self.store.transaction(write=True) as db:
            self.renew(db, token)
            for row_number, raw in enumerate(payload, 1):
                db.execute("SAVEPOINT invoice_record")
                try:
                    result = self.save_invoice(db, raw, run_id, mappings)
                    counts[result] += 1
                    db.execute("RELEASE invoice_record")
                except (InvoiceProblem, ValidationError, sqlite3.IntegrityError) as exc:
                    db.execute("ROLLBACK TO invoice_record")
                    db.execute("RELEASE invoice_record")
                    counts["errors"] += 1
                    if isinstance(exc, ValidationError):
                        code = "INVALID_RECORD"
                        message = "; ".join(
                            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                            for e in exc.errors()
                        )
                    elif isinstance(exc, InvoiceProblem):
                        code, message = exc.code, exc.message
                    else:
                        code, message = (
                            "SAVE_CONFLICT",
                            "A related record changed or an invoice identity is already in use. Review before retrying.",
                        )
                    source_id = (
                        str(raw.get("id", f"page-{page}-row-{row_number}"))[:100]
                        if isinstance(raw, dict)
                        else f"page-{page}-row-{row_number}"
                    )
                    db.execute(
                        "INSERT INTO accounting_sync_errors(run_id,page,source_id,code,message,source_preview) VALUES (?,?,?,?,?,?)",
                        (
                            run_id,
                            page,
                            source_id,
                            code,
                            message[:1500],
                            json.dumps(raw, default=str)[:32000],
                        ),
                    )
            db.execute(
                "UPDATE accounting_sync_runs SET pages=pages+1,seen=seen+?,imported=imported+?,duplicates=duplicates+?,errors=errors+? WHERE id=?",
                (
                    len(payload),
                    counts["imported"],
                    counts["duplicates"],
                    counts["errors"],
                    run_id,
                ),
            )

    async def sync(self, actor):
        connection, key = self.credentials()
        token = uuid.uuid4().hex
        with self.store.transaction(write=True) as db:
            self.busy_guard(db)
            db.execute(
                "UPDATE accounting_sync_runs SET status='interrupted',message='Previous process stopped; a new sync will safely repeat its pages.',finished_at=? WHERE status='running' AND connection_id=1",
                (utc_now(),),
            )
            mappings = {
                row["source_code"]: row["supplier_id"]
                for row in db.execute(
                    "SELECT * FROM accounting_supplier_mappings WHERE connection_id=1"
                )
            }
            cursor = db.execute(
                "INSERT INTO accounting_sync_runs(connection_id,status,actor,mapping_json) VALUES (1,'running',?,?)",
                (actor, json.dumps({"fields": FIELD_MAPPING, "suppliers": mappings})),
            )
            run_id = cursor.lastrowid
            db.execute(
                "UPDATE accounting_connections SET lease_token=?,lease_until=? WHERE id=1",
                (token, time.time() + 60),
            )
            audit(db, actor, "invoice_sync.started", run_id)
        deadline = time.monotonic() + self.budget_seconds
        failure = None
        cursor = None
        visited = set()
        snapshot = None
        expected = None
        seen = 0
        try:
            async with self.client_factory() as client:
                for page in range(1, 51):
                    payload = await self.fetch_page(
                        client, connection, key, run_id, token, page, cursor, deadline
                    )
                    if (
                        not isinstance(payload, dict)
                        or not isinstance(payload.get("items"), list)
                        or len(payload["items"]) > 100
                    ):
                        raise SyncFailure(
                            "BAD_RESPONSE",
                            "Accounting returned an invalid invoice page",
                        )
                    total = payload.get("total")
                    stamp = payload.get("snapshot")
                    next_cursor = payload.get("next_cursor")
                    if (
                        type(total) is not int
                        or not 0 <= total <= 5000
                        or not isinstance(stamp, str)
                        or not stamp
                        or len(stamp) > 100
                    ):
                        raise SyncFailure(
                            "BAD_RESPONSE",
                            "Accounting page is missing a valid total or snapshot",
                        )
                    if page == 1:
                        snapshot, expected = stamp, total
                    if stamp != snapshot or total != expected:
                        raise SyncFailure(
                            "SOURCE_CHANGED_DURING_SYNC",
                            "Accounting data changed between pages. Retry the sync safely.",
                        )
                    if next_cursor is not None and (
                        not isinstance(next_cursor, str)
                        or not next_cursor
                        or len(next_cursor) > 100
                        or next_cursor in visited
                        or next_cursor == cursor
                    ):
                        raise SyncFailure(
                            "BAD_PAGINATION",
                            "Accounting returned a repeated or invalid page cursor",
                        )
                    if not payload["items"] and next_cursor is not None:
                        raise SyncFailure(
                            "BAD_PAGINATION",
                            "Accounting returned an empty page with another cursor",
                        )
                    seen += len(payload["items"])
                    if seen > expected:
                        raise SyncFailure(
                            "BAD_PAGINATION",
                            "Accounting returned more invoices than its declared total",
                        )
                    self.save_page(payload["items"], run_id, token, page, mappings)
                    if next_cursor is None:
                        if seen != expected:
                            raise SyncFailure(
                                "BAD_PAGINATION",
                                "Accounting ended before all declared invoices were received",
                            )
                        break
                    visited.add(next_cursor)
                    cursor = next_cursor
                else:
                    raise SyncFailure(
                        "PAGE_LIMIT",
                        "Invoice page limit reached. Review the source pagination.",
                    )
        except SyncFailure as exc:
            failure = exc
        except (httpx.HTTPError, sqlite3.Error):
            failure = SyncFailure(
                "SYNC_FAILED",
                "The sync could not continue. Saved pages are preserved; retry safely.",
            )
        except asyncio.CancelledError:
            failure = SyncFailure(
                "INTERRUPTED", "The sync was interrupted. Retry safely."
            )
            raise
        except Exception:  # noqa: BLE001 — finalize durable run history on unexpected failures.
            failure = SyncFailure(
                "INTERNAL_ERROR",
                "An unexpected sync error occurred. Saved pages are preserved; review and retry.",
            )
        finally:
            with self.store.transaction(write=True) as db:
                result = db.execute(
                    "SELECT * FROM accounting_sync_runs WHERE id=?", (run_id,)
                ).fetchone()
                status = (
                    ("partial_failure" if result["pages"] else "failed")
                    if failure
                    else ("completed_with_errors" if result["errors"] else "completed")
                )
                message = (
                    f"{failure.code}: {failure.message}"
                    if failure
                    else (
                        "Some invoices need correction. Valid invoices were saved."
                        if result["errors"]
                        else "All pages processed. Repeated invoices were skipped safely."
                    )
                )
                db.execute(
                    "UPDATE accounting_sync_runs SET status=?,message=?,finished_at=? WHERE id=?",
                    (status, message, utc_now(), run_id),
                )
                db.execute(
                    "UPDATE accounting_connections SET lease_token=NULL,lease_until=NULL WHERE id=1 AND lease_token=?",
                    (token,),
                )
                audit(
                    db,
                    actor,
                    "invoice_sync." + status,
                    run_id,
                    {
                        "imported": result["imported"],
                        "duplicates": result["duplicates"],
                        "errors": result["errors"],
                        "message": message,
                    },
                )
        return self.run(run_id)

    def run(self, run_id):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM accounting_sync_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Invoice sync run not found")
            return {
                **dict(row),
                "attempt_count": db.execute(
                    "SELECT COUNT(*) FROM accounting_sync_attempts WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0],
            }


def install_accounting(application, store, require):
    service = InvoiceSync(store)
    application.state.accounting = service
    manager = Depends(require("owner", "manager"))
    owner = Depends(require("owner"))

    @application.get("/api/accounting/connection")
    def connection(user=manager):
        return service.configuration()

    @application.post("/api/accounting/connection")
    async def save_connection(body: ConnectionInput, user=owner):
        return await service.save_configuration(body, user["username"])

    @application.post("/api/accounting/suppliers/refresh")
    async def refresh_suppliers(user=manager):
        return await service.refresh_suppliers(user["username"])

    @application.post("/api/accounting/suppliers/map")
    def map_supplier(body: MappingInput, user=manager):
        with store.transaction(write=True) as db:
            service.busy_guard(db)
            if (
                body.supplier_id is not None
                and not db.execute(
                    "SELECT 1 FROM suppliers WHERE id=?", (body.supplier_id,)
                ).fetchone()
            ):
                raise HTTPException(404, "Supplier not found")
            cursor = db.execute(
                "UPDATE accounting_supplier_mappings SET supplier_id=? WHERE connection_id=1 AND source_code=?",
                (body.supplier_id, body.source_code),
            )
            if cursor.rowcount != 1:
                raise HTTPException(
                    404, "Accounting supplier code not found; refresh the supplier list"
                )
            audit(
                db,
                user["username"],
                "accounting.supplier_mapped",
                body.source_code,
                {"supplier_id": body.supplier_id},
            )
        return service.configuration()

    @application.post("/api/accounting/sync")
    async def sync(user=manager):
        return await service.sync(user["username"])

    @application.get("/api/accounting/runs")
    def runs(offset: int = Query(0, ge=0), user=manager):
        with store.transaction() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM accounting_sync_runs ORDER BY id DESC LIMIT 20 OFFSET ?",
                    (offset,),
                )
            ]
        return [service.run(identity) for identity in ids]

    @application.get("/api/accounting/runs/{run_id}")
    def run_detail(run_id: int, user=manager):
        result = service.run(run_id)
        with store.transaction() as db:
            result["error_details"] = [
                dict(row)
                for row in db.execute(
                    "SELECT page,source_id,code,message FROM accounting_sync_errors WHERE run_id=? ORDER BY id LIMIT 100",
                    (run_id,),
                )
            ]
            result["attempts"] = [
                dict(row)
                for row in db.execute(
                    "SELECT page,attempt,status_code,code,message,retry_delay FROM accounting_sync_attempts WHERE run_id=? ORDER BY id LIMIT 200",
                    (run_id,),
                )
            ]
        return result

    @application.get("/api/accounting/runs/{run_id}/errors.csv")
    def error_csv(run_id: int, user=manager):
        service.run(run_id)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Source invoice ID", "Page", "Error code", "Message"])
        with store.transaction() as db:
            for row in db.execute(
                "SELECT source_id,page,code,message FROM accounting_sync_errors WHERE run_id=? ORDER BY id",
                (run_id,),
            ):
                writer.writerow(
                    [
                        "'" + value
                        if isinstance(value, str)
                        and value.lstrip().startswith(("=", "+", "-", "@"))
                        else value
                        for value in row
                    ]
                )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=invoice-sync-{run_id}-errors.csv"
            },
        )

    @application.get("/api/accounting/invoices")
    def invoices(
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), user=manager
    ):
        with store.transaction() as db:
            records = [
                dict(row)
                for row in db.execute(
                    "SELECT i.id,i.source_id,i.invoice_number,i.invoice_date,i.purchase_id,i.total_paise,s.name AS supplier FROM accounting_invoices i JOIN suppliers s ON s.id=i.supplier_id ORDER BY i.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]
            total = db.execute("SELECT COUNT(*) FROM accounting_invoices").fetchone()[0]
        return {"items": records, "total": total}

    @application.get("/api/accounting/invoices/{invoice_id}")
    def invoice_detail(invoice_id: int, user=manager):
        with store.transaction() as db:
            row = db.execute(
                "SELECT i.*,s.name AS supplier FROM accounting_invoices i JOIN suppliers s ON s.id=i.supplier_id WHERE i.id=?",
                (invoice_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Invoice not found")
            return {
                **dict(row),
                "items": [
                    dict(item)
                    for item in db.execute(
                        "SELECT ii.quantity,ii.unit_cost_paise,ii.line_total_paise,p.name,p.sku FROM accounting_invoice_items ii JOIN products p ON p.id=ii.product_id WHERE invoice_id=?",
                        (invoice_id,),
                    )
                ],
            }

    @application.get("/api/accounting/demo/fault")
    async def fault(user=manager):
        return await service.small_request("/demo/fault")

    @application.post("/api/accounting/demo/fault")
    async def set_fault(body: FaultInput, user=manager):
        with store.transaction() as db:
            service.busy_guard(db)
        result = await service.small_request("/demo/fault", "POST", body.model_dump())
        with store.transaction(write=True) as db:
            audit(db, user["username"], "accounting.demo_fault", 1, body.model_dump())
        return result

    return service
