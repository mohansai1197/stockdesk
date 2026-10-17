"""CSV / JSON / XML adapters and explicit supplier field mappings."""

import csv
import io
import json
from decimal import Decimal

from defusedxml import ElementTree

from models import ProductInput

FIELDS = ("sku", "name", "category", "price", "cost", "stock", "low_stock_at")
STANDARD = {field: field for field in FIELDS}
SUPPLIER = {
    "sku": "Item Code",
    "name": "Product Name",
    "category": "Department",
    "price": "Sell Price",
    "cost": "Buy Price",
    "stock": "Available",
    "low_stock_at": "Reorder At",
}


def read_records(raw, kind):
    if len(raw) > 256 * 1024:
        raise ValueError("Files must be no larger than 256 KiB")
    try:
        text = raw.decode("utf-8-sig")
        if kind == "csv":
            reader = csv.DictReader(io.StringIO(text), strict=True)
            headers = reader.fieldnames
            if (
                not headers
                or len(headers) != len(set(headers))
                or any(not value for value in headers)
            ):
                raise ValueError("CSV needs unique, nonempty column headings")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(
                        "CSV contains a row with the wrong number of columns"
                    )
                rows.append(row)
                if len(rows) > 500:
                    raise ValueError("Import at most 500 products at a time")
        elif kind == "json":
            rows = json.loads(text, parse_float=Decimal)
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) for row in rows
            ):
                raise ValueError("JSON must be an array of flat product objects")
        elif kind == "xml":
            root = ElementTree.fromstring(text)
            if root.tag != "products" or any(node.tag != "product" for node in root):
                raise ValueError("XML must use products > product > field elements")
            rows = []
            for node in root:
                if any(len(child) > 0 for child in node) or len(
                    {child.tag for child in node}
                ) != len(node):
                    raise ValueError("XML product fields must be flat and unique")
                rows.append({child.tag: child.text or "" for child in node})
        else:
            raise ValueError("Choose CSV, JSON, or XML")
        if not rows or len(rows) > 500:
            raise ValueError("Include between 1 and 500 products")
        if any(
            any(
                isinstance(value, (dict, list, bool)) or value is None
                for value in row.values()
            )
            for row in rows
        ):
            raise ValueError(
                "Each product must contain only non-null text or number fields"
            )
        return rows
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            "Could not parse the file. Check its encoding and format."
        ) from exc


def validate_mapping(mapping):
    if (
        not isinstance(mapping, dict)
        or len(mapping) > 7
        or any(key not in FIELDS for key in mapping)
    ):
        raise ValueError("Mapping keys must use the seven supported product fields")
    if any(
        not isinstance(value, str) or len(value) > 100 for value in mapping.values()
    ):
        raise ValueError("Mapping values must be column names")
    if any(
        not mapping.get(key) for key in ("sku", "name", "category", "price", "stock")
    ):
        raise ValueError("Map SKU, name, category, price, and stock")
    chosen = [value for value in mapping.values() if value]
    if len(chosen) != len(set(chosen)):
        raise ValueError("Each source column can be used only once")
    return mapping


def convert_records(rows, mapping, supplier_id=None, location_id=1, kind="csv"):
    validate_mapping(mapping)
    products, errors, seen = [], [], set()
    for index, row in enumerate(rows, start=2 if kind == "csv" else 1):
        try:
            values = {
                field: row[column]
                for field, column in mapping.items()
                if column and column in row
            }
            missing = [
                column
                for field, column in mapping.items()
                if column
                and column not in row
                and field not in ("cost", "low_stock_at")
            ]
            if missing:
                raise ValueError("Missing columns: " + ", ".join(missing))
            for field in ("stock", "low_stock_at"):
                original = str(
                    values.get(field, "5" if field == "low_stock_at" else "")
                ).strip()
                if not original.isascii() or not original.isdigit():
                    raise ValueError(f"{field} must be a nonnegative whole number")
                values[field] = int(original)
            for field in ("price", "cost"):
                if field in values:
                    values[field] = str(values[field]).strip() or None
            values.update(supplier_id=supplier_id, location_id=location_id)
            product = ProductInput(**values)
            if product.sku in seen:
                raise ValueError(f"Repeated SKU {product.sku}")
            seen.add(product.sku)
            products.append(product)
        except (ValueError, TypeError) as exc:
            reason = (
                "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())
                if hasattr(exc, "errors")
                else str(exc)
            )
            errors.append(
                {
                    "row": index,
                    "sku": str(row.get(mapping.get("sku"), ""))[:32],
                    "error": reason,
                }
            )
    return products, errors
