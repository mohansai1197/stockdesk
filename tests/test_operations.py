"""Business-flow and permission tests use disposable databases only."""

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import create_app
from supplier_imports import SUPPLIER

PASSWORD = "Test-password-123!"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "shop.sqlite3")) as browser:
        login = browser.post(
            "/api/auth/setup", json={"username": "owner", "password": PASSWORD}
        )
        assert login.status_code == 201
        browser.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        yield browser


def add_product(client, **values):
    body = {
        "sku": "NB",
        "name": "Notebook",
        "category": "Paper",
        "price": "85.00",
        "cost": "60.00",
        "stock": 10,
        "low_stock_at": 5,
    }
    body.update(values)
    response = client.post("/api/products", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def supplier(client):
    response = client.post(
        "/api/suppliers", json={"name": "Demo stationery supplier", "lead_days": 4}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def sell(client, product, quantity=2, location=1):
    response = client.post(
        "/api/sales",
        json={
            "items": [{"product_id": product["id"], "quantity": quantity}],
            "location_id": location,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_authentication_and_csrf_are_required(client):
    with TestClient(client.app) as anonymous:
        assert anonymous.get("/api/products").status_code == 401
        assert anonymous.get("/api/backups").status_code == 401
        assert anonymous.post("/api/products", json={}).status_code == 401
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/suppliers", json={"name": "No access"}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert (
        client.post(
            "/api/auth/setup", json={"username": "newowner", "password": PASSWORD}
        ).status_code
        == 409
    )


def test_cashier_permissions_are_enforced_by_server(client):
    product = add_product(client)
    created = client.post(
        "/api/staff",
        json={
            "username": "cashier",
            "display_name": "Counter Staff",
            "password": PASSWORD,
            "role": "cashier",
        },
    )
    assert created.status_code == 201
    with TestClient(client.app) as cashier:
        result = cashier.post(
            "/api/auth/login", json={"username": "cashier", "password": PASSWORD}
        ).json()
        cashier.headers["X-CSRF-Token"] = result["csrf_token"]
        assert "cost_paise" not in cashier.get("/api/products").json()[0]
        assert "gross_profit_paise" not in cashier.get("/api/summary").json()
        for path in (
            "/api/staff",
            "/api/backups",
            "/api/reports",
            "/api/suppliers",
            "/api/activity",
            "/api/purchases",
        ):
            assert cashier.get(path).status_code == 403, path
        assert (
            cashier.post(
                "/api/products",
                json={
                    "sku": "NO",
                    "name": "No",
                    "category": "No",
                    "price": "1.00",
                    "stock": 1,
                },
            ).status_code
            == 403
        )
        assert (
            cashier.post(
                f"/api/products/{product['id']}/update", json={"price": "2.00"}
            ).status_code
            == 403
        )
        sell(cashier, product, 1)
    actions = [row["action"] for row in client.get("/api/activity").json()]
    assert "access.denied" in actions


def test_staff_deactivation_revokes_sessions(client):
    staff_id = client.post(
        "/api/staff",
        json={
            "username": "manager",
            "display_name": "Manager",
            "password": PASSWORD,
            "role": "manager",
        },
    ).json()["id"]
    with TestClient(client.app) as manager:
        result = manager.post(
            "/api/auth/login", json={"username": "manager", "password": PASSWORD}
        ).json()
        manager.headers["X-CSRF-Token"] = result["csrf_token"]
        assert manager.get("/api/reports").status_code == 200
        assert manager.get("/api/backups").status_code == 403
        assert client.post(f"/api/staff/{staff_id}/deactivate").status_code == 200
        assert manager.get("/api/products").status_code == 401
    assert client.post("/api/staff/1/deactivate").status_code == 409


def test_session_expiration_and_logout(client):
    with client.app.state.store.transaction(write=True) as db:
        db.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert client.get("/api/auth/me").status_code == 401
    login = client.post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD}
    ).json()
    client.headers["X-CSRF-Token"] = login["csrf_token"]
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/products").status_code == 401


def test_purchase_receipt_updates_weighted_cost_once(client):
    item = add_product(client, stock=10, cost="60.00")
    vendor = supplier(client)
    order = client.post(
        "/api/purchases",
        json={
            "supplier_id": vendor,
            "location_id": 2,
            "items": [{"product_id": item["id"], "quantity": 10, "cost": "80.00"}],
        },
    )
    assert order.status_code == 201
    identity = order.json()["id"]
    assert client.get("/api/products").json()[0]["stock"] == 10
    assert client.post(f"/api/purchases/{identity}/receive").status_code == 200
    current = client.get("/api/products").json()[0]
    assert current["stock"] == 20 and current["cost_paise"] == 7000
    assert current["locations"][1]["quantity"] == 10
    assert client.post(f"/api/purchases/{identity}/receive").status_code == 409
    assert client.get("/api/products").json()[0]["stock"] == 20


def test_receipt_failure_rolls_back_all_lines(client):
    first = add_product(client, stock=0)
    second = add_product(client, sku="UNKNOWN", cost=None)
    vendor = supplier(client)
    order = client.post(
        "/api/purchases",
        json={
            "supplier_id": vendor,
            "items": [
                {"product_id": first["id"], "quantity": 2, "cost": "50.00"},
                {"product_id": second["id"], "quantity": 2, "cost": "50.00"},
            ],
        },
    ).json()
    assert client.post(f"/api/purchases/{order['id']}/receive").status_code == 409
    records = client.get("/api/products").json()
    assert [row["stock"] for row in records] == [0, 10]
    assert client.get("/api/purchases").json()[0]["status"] == "ordered"


def test_transfer_conserves_total_and_sale_uses_location_stock(client):
    item = add_product(client)
    transfer = {
        "product_id": item["id"],
        "quantity": 4,
        "from_location": 1,
        "to_location": 2,
    }
    assert client.post("/api/transfers", json=transfer).status_code == 201
    current = client.get("/api/products").json()[0]
    assert current["stock"] == 10
    assert [row["quantity"] for row in current["locations"]] == [6, 4]
    failed = client.post(
        "/api/sales",
        json={"location_id": 2, "items": [{"product_id": item["id"], "quantity": 5}]},
    )
    assert failed.status_code == 409
    transfer["quantity"] = 7
    assert client.post("/api/transfers", json=transfer).status_code == 409
    assert client.get("/api/products").json()[0]["stock"] == 10
    sell(client, item, 4, 2)
    assert client.get("/api/products").json()[0]["locations"][1]["quantity"] == 0


def test_partial_return_uses_original_sale_price_and_cost(client):
    item = add_product(client)
    sale = sell(client, item, 2)
    line = client.get("/api/sales").json()[0]["items"][0]
    assert (
        client.post(
            f"/api/products/{item['id']}/update",
            json={"price": "100.00", "cost": "70.00"},
        ).status_code
        == 200
    )
    body = {
        "reason": "Customer changed mind",
        "items": [{"sale_item_id": line["id"], "quantity": 1, "restock": True}],
    }
    result = client.post(f"/api/sales/{sale['id']}/returns", json=body)
    assert result.status_code == 201
    assert result.json()["refund_paise"] == 8500
    assert result.json()["payment_processed"] is False
    assert client.get("/api/products").json()[0]["stock"] == 9
    report = client.get("/api/reports").json()
    assert report["revenue_paise"] == 8500
    assert report["cost_of_goods_paise"] == 6000
    assert report["gross_profit_paise"] == 2500
    body["items"][0]["quantity"] = 2
    assert client.post(f"/api/sales/{sale['id']}/returns", json=body).status_code == 409


def test_damaged_return_does_not_restore_stock_or_reverse_cost(client):
    item = add_product(client)
    sale = sell(client, item, 1)
    line = client.get("/api/sales").json()[0]["items"][0]
    body = {
        "reason": "Product damaged",
        "items": [{"sale_item_id": line["id"], "quantity": 1, "restock": False}],
    }
    assert client.post(f"/api/sales/{sale['id']}/returns", json=body).status_code == 201
    assert client.get("/api/products").json()[0]["stock"] == 9
    report = client.get("/api/reports").json()
    assert report["revenue_paise"] == 0 and report["gross_profit_paise"] == -6000


def test_two_returns_cannot_return_the_same_last_unit(client):
    item = add_product(client)
    sale = sell(client, item, 1)
    line = client.get("/api/sales").json()[0]["items"][0]
    body = {
        "reason": "Returned item",
        "items": [{"sale_item_id": line["id"], "quantity": 1, "restock": True}],
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: client.post(f"/api/sales/{sale['id']}/returns", json=body),
                range(2),
            )
        )
    assert sorted(result.status_code for result in results) == [201, 409]
    assert client.get("/api/products").json()[0]["stock"] == 10


def test_unknown_cost_is_not_reported_as_profit(client):
    item = add_product(client, cost=None)
    sell(client, item)
    report = client.get("/api/reports").json()
    assert report["gross_profit_paise"] is None and report["uncosted_units"] == 2
    client.post(f"/api/products/{item['id']}/update", json={"cost": "60.00"})
    assert client.get("/api/reports").json()["gross_profit_paise"] is None


def test_supplier_json_mapping_profile_and_repeated_import(client):
    vendor = supplier(client)
    mapping = {"name": "Vendor catalog", "supplier_id": vendor, "mapping": SUPPLIER}
    assert client.post("/api/mapping-profiles", json=mapping).json()["version"] == 1
    assert client.post("/api/mapping-profiles", json=mapping).json()["version"] == 2
    data = [
        {
            "Item Code": "EXT-01",
            "Product Name": "Supplier Pen",
            "Department": "Paper",
            "Sell Price": "19.99",
            "Buy Price": "10.00",
            "Available": 5,
            "Reorder At": 2,
        }
    ]
    files = {"file": ("vendor.json", json.dumps(data), "application/json")}
    preview = client.post("/api/imports/preview", data={"kind": "json"}, files=files)
    assert preview.status_code == 200 and "Item Code" in preview.json()["headers"]
    params = {
        "kind": "json",
        "mapping": json.dumps(SUPPLIER),
        "supplier_id": vendor,
        "location_id": 2,
    }
    result = client.post("/api/products/import", data=params, files=files)
    assert result.status_code == 201 and result.json()["imported"] == 1
    product = client.get("/api/products").json()[0]
    assert (
        product["sku"] == "EXT-01"
        and product["price_paise"] == 1999
        and product["locations"][1]["quantity"] == 5
    )
    assert (
        client.post("/api/products/import", data=params, files=files).status_code == 422
    )
    history = client.get("/api/imports").json()
    assert [row["status"] for row in history] == ["rejected", "imported"]


def test_xml_import_and_unsafe_xml_rejection(client):
    safe = "<products><product><sku>XML-01</sku><name>XML Notebook</name><category>Paper</category><price>40.00</price><stock>3</stock></product></products>"
    assert (
        client.post(
            "/api/products/import",
            data={"kind": "xml"},
            files={"file": ("products.xml", safe)},
        ).status_code
        == 201
    )
    unsafe = '<!DOCTYPE products [<!ENTITY x "bad">]><products><product><sku>&x;</sku></product></products>'
    assert (
        client.post(
            "/api/products/import",
            data={"kind": "xml"},
            files={"file": ("bad.xml", unsafe)},
        ).status_code
        == 422
    )
    assert len(client.get("/api/products").json()) == 1


def test_csv_mapping_missing_fields_has_saved_errors(client):
    data = "Item Code,Product Name,Department,Sell Price,Buy Price,Available,Reorder At\nS-01,Notebook,Paper,40.00,20.00,3,1\nS-02,Pen,Paper,wrong,10.00,2,1\n"
    result = client.post(
        "/api/products/import",
        data={"mapping": json.dumps(SUPPLIER)},
        files={"file": ("vendor.csv", data)},
    )
    assert result.status_code == 422
    assert result.json()["detail"]["errors"][0]["row"] == 3
    assert client.get("/api/products").json() == []
    assert client.get("/api/imports").json()[0]["status"] == "rejected"


def test_reorder_subtracts_pending_purchases(client):
    item = add_product(client, stock=1, low_stock_at=5)
    vendor = supplier(client)
    initial = client.get("/api/reorders").json()[0]
    assert initial["suggested_quantity"] == 5
    result = client.post(
        "/api/purchases",
        json={
            "supplier_id": vendor,
            "items": [{"product_id": item["id"], "quantity": 5, "cost": "60.00"}],
        },
    )
    assert result.status_code == 201
    assert client.get("/api/reorders").json() == []


def test_backup_restore_safety_copy_and_session_revocation(client):
    item = add_product(client)
    backup = client.post("/api/backups").json()["name"]
    assert client.get(f"/api/backups/{backup}").content.startswith(b"SQLite format 3")
    sell(client, item, 2)
    result = client.post(
        f"/api/backups/{backup}/restore",
        json={"confirmation": "RESTORE", "password": PASSWORD},
    )
    assert result.status_code == 200, result.text
    assert result.json()["safety_backup"].startswith("safety-")
    assert client.get("/api/auth/me").status_code == 401
    login = client.post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD}
    ).json()
    client.headers["X-CSRF-Token"] = login["csrf_token"]
    assert client.get("/api/products").json()[0]["stock"] == 10
    assert client.get("/api/sales").json() == []
    assert len(client.get("/api/backups").json()) == 2


def test_backup_restore_requires_password_and_confirmation(client):
    add_product(client)
    backup = client.post("/api/backups").json()["name"]
    assert (
        client.post(
            f"/api/backups/{backup}/restore",
            json={"confirmation": "RESTORE", "password": "wrong"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/backups/{backup}/restore",
            json={"confirmation": "no", "password": PASSWORD},
        ).status_code
        == 422
    )
    assert client.get("/api/products").status_code == 200


def test_pagination_and_csv_formula_escaping(client):
    for index in range(3):
        add_product(
            client, sku=f"NB-{index}", name=f"Notebook {index}", category="=SUM(A1)"
        )
    response = client.get("/api/products?limit=1&offset=1")
    assert response.headers["X-Total-Count"] == "3"
    assert response.json()[0]["sku"] == "NB-1"
    sell(client, response.json()[0], 1)
    exported = client.get("/api/reports/export")
    assert "'=SUM(A1)" in exported.text
    assert client.get("/api/products?search=NB-2").json()[0]["sku"] == "NB-2"


def test_legacy_database_migration_preserves_records(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE products(id INTEGER PRIMARY KEY,sku TEXT NOT NULL UNIQUE,name TEXT NOT NULL,category TEXT NOT NULL,price_paise INTEGER NOT NULL,stock INTEGER NOT NULL,low_stock_at INTEGER NOT NULL);
    CREATE TABLE sales(id INTEGER PRIMARY KEY,sale_date TEXT NOT NULL,created_at TEXT,total_paise INTEGER NOT NULL);
    CREATE TABLE sale_items(id INTEGER PRIMARY KEY,sale_id INTEGER NOT NULL,product_id INTEGER NOT NULL,quantity INTEGER NOT NULL,unit_price_paise INTEGER NOT NULL);
    CREATE TABLE stock_movements(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL,sale_id INTEGER,change INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT);
    INSERT INTO products VALUES(1,'OLD','Original Notebook','Paper',8500,12,5);
    INSERT INTO sales VALUES(1,'2026-01-01','2026-01-01T00:00:00Z',8500);
    INSERT INTO sale_items VALUES(1,1,1,1,8500);""")
    db.close()
    with TestClient(create_app(path)) as browser:
        login = browser.post(
            "/api/auth/setup", json={"username": "owner", "password": PASSWORD}
        ).json()
        browser.headers["X-CSRF-Token"] = login["csrf_token"]
        item = browser.get("/api/products").json()[0]
        assert item["stock"] == 12 and item["locations"][0]["quantity"] == 12
        assert browser.get("/api/sales").json()[0]["total_paise"] == 8500
        assert list((tmp_path / "backups").glob("pre-upgrade-*.sqlite3"))
        assert (
            browser.get("/api/reports?start=2026-01-01&end=2026-01-31").json()[
                "gross_profit_paise"
            ]
            is None
        )


def test_restore_waits_for_existing_request_without_blocking_its_db_access(client):
    from security import token_hash

    store = client.app.state.store
    backup = client.post("/api/backups").json()["name"]
    session = token_hash(client.cookies.get("stockdesk_session"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.lease():
            pending = pool.submit(store.restore, backup, "owner", session)
            with store.condition:
                assert store.condition.wait_for(
                    lambda: store.request_maintenance, timeout=2
                )
            # An already-authorized request must still finish its database work.
            with store.transaction(write=True) as db:
                db.execute("INSERT INTO suppliers(name) VALUES ('During request')")
        assert pending.result(timeout=5).startswith("safety-")
    assert client.get("/api/auth/me").status_code == 401


def test_supplier_api_success_and_outage_are_visible(client, monkeypatch):
    import app as app_module
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import Response
    from mock_supplier import app as mock_supplier

    vendor = supplier(client)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        app_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.ASGITransport(app=mock_supplier)
        ),
    )
    response = client.post(f"/api/imports/supplier-api?supplier_id={vendor}")
    assert response.status_code == 201 and response.json()["imported"] == 2
    unavailable = FastAPI()

    @unavailable.get("/catalog")
    def fail():
        return Response(status_code=503)

    monkeypatch.setattr(
        app_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.ASGITransport(app=unavailable)
        ),
    )
    response = client.post(f"/api/imports/supplier-api?supplier_id={vendor}")
    assert response.status_code == 502
    assert len(client.get("/api/products").json()) == 2
    assert any(
        row["action"] == "supplier_api.failed"
        for row in client.get("/api/activity").json()
    )


def test_operational_checks_detect_inconsistent_stock(client):
    product = add_product(client)
    healthy = client.get("/api/operations").json()
    assert healthy["stock_mismatches"] == [] and healthy["sale_total_mismatches"] == 0
    with client.app.state.store.transaction(write=True) as db:
        db.execute("UPDATE products SET stock=stock+1 WHERE id=?", (product["id"],))
    result = client.get("/api/operations").json()
    assert result["stock_mismatches"][0]["stock"] == 11
    assert result["stock_mismatches"][0]["counted"] == 10


def test_oversized_import_and_repeated_mapping_are_rejected(client):
    result = client.post(
        "/api/products/import", files={"file": ("large.csv", b"x" * (256 * 1024 + 1))}
    )
    assert result.status_code == 422 and client.get("/api/products").json() == []
    bad = {"sku": "same", "name": "same", "category": "c", "price": "p", "stock": "s"}
    assert (
        client.post(
            "/api/mapping-profiles", json={"name": "Bad", "mapping": bad}
        ).status_code
        == 422
    )


def test_login_throttling(client):
    with TestClient(client.app) as browser:
        for _ in range(10):
            assert (
                browser.post(
                    "/api/auth/login",
                    json={"username": "missing", "password": PASSWORD},
                ).status_code
                == 401
            )
        assert (
            browser.post(
                "/api/auth/login", json={"username": "missing", "password": PASSWORD}
            ).status_code
            == 429
        )
