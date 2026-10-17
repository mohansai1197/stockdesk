from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import create_app, shop_today


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "shop.sqlite3")) as test_client:
        session = test_client.post(
            "/api/auth/setup",
            json={"username": "testowner", "password": "Test-password-123!"},
        ).json()
        test_client.headers["X-CSRF-Token"] = session["csrf_token"]
        yield test_client


def product(client, sku="NB-A5", stock=10, price="85.00"):
    response = client.post(
        "/api/products",
        json={
            "sku": sku,
            "name": "A5 Notebook",
            "category": "Stationery",
            "price": price,
            "stock": stock,
            "low_stock_at": 5,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def sell(client, items, **extra):
    return client.post("/api/sales", json={"items": items, **extra})


def test_sale_reduces_stock_and_preserves_exact_money(client):
    item = product(client, stock=3, price="19.99")
    sale = sell(client, [{"product_id": item["id"], "quantity": 3}])
    assert sale.status_code == 201
    assert sale.json()["total_paise"] == 5997
    assert client.get("/api/products").json()[0]["stock"] == 0
    history = client.get(f"/api/products/{item['id']}/movements").json()
    assert [row["change"] for row in history] == [-3, 3]
    stored = client.get("/api/sales").json()[0]
    assert stored["items"][0]["unit_price_paise"] == 1999


def test_failed_multi_product_sale_changes_nothing(client):
    first = product(client, stock=10)
    second = product(client, sku="PEN-BL", stock=1)
    result = sell(
        client,
        [
            {"product_id": first["id"], "quantity": 2},
            {"product_id": second["id"], "quantity": 2},
        ],
    )
    assert result.status_code == 409
    assert [row["stock"] for row in client.get("/api/products").json()] == [10, 1]
    assert client.get("/api/sales").json() == []
    assert len(client.get(f"/api/products/{first['id']}/movements").json()) == 1


def test_concurrent_sales_cannot_oversell(client):
    item = product(client, stock=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: sell(client, [{"product_id": item["id"], "quantity": 1}]),
                range(2),
            )
        )
    assert sorted(response.status_code for response in results) == [201, 409]
    assert client.get("/api/products").json()[0]["stock"] == 0
    assert len(client.get("/api/sales").json()) == 1


def test_restock_is_saved_in_history(client):
    item = product(client, stock=0)
    response = client.post(f"/api/products/{item['id']}/restock", json={"quantity": 7})
    assert response.json()["stock"] == 7
    history = client.get(f"/api/products/{item['id']}/movements").json()
    assert [(row["change"], row["reason"]) for row in history] == [(7, "Restock")]
    assert (
        client.post(
            f"/api/products/{item['id']}/restock", json={"quantity": 1000000}
        ).status_code
        == 422
    )
    assert client.get("/api/products").json()[0]["stock"] == 7


def test_csv_invalid_row_does_not_partially_import(client):
    content = "sku,name,category,price,stock,low_stock_at\nNB,Notebook,Paper,85.00,5,2\nPEN,Pen,Paper,nope,10,2\n"
    response = client.post(
        "/api/products/import", files={"file": ("products.csv", content, "text/csv")}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["row"] == 3
    assert client.get("/api/products").json() == []


def test_csv_existing_sku_rolls_back_earlier_rows(client):
    product(client, sku="OLD")
    content = "sku,name,category,price,stock,low_stock_at\nNEW,Notebook,Paper,85.00,5,2\nOLD,Pen,Paper,20.00,10,2\n"
    response = client.post(
        "/api/products/import", files={"file": ("products.csv", content, "text/csv")}
    )
    assert response.status_code == 422
    assert [row["sku"] for row in client.get("/api/products").json()] == ["OLD"]


def test_sample_csv_import_and_repeat(client):
    sample = client.get("/api/sample.csv")
    response = client.post(
        "/api/products/import",
        files={"file": ("products.csv", sample.content, "text/csv")},
    )
    assert response.json()["imported"] == 50
    repeat = client.post(
        "/api/products/import",
        files={"file": ("products.csv", sample.content, "text/csv")},
    )
    assert repeat.status_code == 422
    report = client.get("/api/reports").json()
    assert report["product_count"] == 50
    assert report["units_in_stock"] == 13537
    assert report["low_stock_count"] == 0
    assert all(
        row["cost_paise"] is not None for row in client.get("/api/products").json()
    )


@pytest.mark.parametrize(
    "price", ["NaN", "Infinity", "0", "-2", "1.999", "1000000.01", "invalid"]
)
def test_invalid_prices_are_rejected(client, price):
    response = client.post(
        "/api/products",
        json={
            "sku": "NB",
            "name": "Notebook",
            "category": "Paper",
            "price": price,
            "stock": 1,
        },
    )
    assert response.status_code == 422
    assert client.get("/api/products").json() == []


def test_duplicate_product_lines_and_future_sales_are_rejected(client):
    item = product(client)
    line = {"product_id": item["id"], "quantity": 1}
    assert sell(client, [line, line]).status_code == 422
    tomorrow = (shop_today() + timedelta(days=1)).isoformat()
    assert sell(client, [line], sale_date=tomorrow).status_code == 422
    assert (
        sell(client, [{"product_id": item["id"], "quantity": 1.5}]).status_code == 422
    )


def test_reports_include_date_bounds_categories_and_totals(client):
    item = product(client)
    for sale_date, quantity in [("2026-01-01", 2), ("2026-02-01", 3)]:
        assert (
            sell(
                client,
                [{"product_id": item["id"], "quantity": quantity}],
                sale_date=sale_date,
            ).status_code
            == 201
        )
    report = client.get("/api/reports?start=2026-01-01&end=2026-01-31").json()
    assert report["revenue_paise"] == 17000
    assert report["sales_count"] == 1
    assert report["units_in_stock"] == 5
    assert report["categories"] == [
        {"category": "Stationery", "units": 2, "revenue_paise": 17000}
    ]
    assert report["daily"] == [{"sale_date": "2026-01-01", "revenue_paise": 17000}]
    assert client.get("/api/reports?start=2026-02-01&end=2026-01-01").status_code == 422


def test_data_survives_another_app_instance(tmp_path):
    path = tmp_path / "shop.sqlite3"
    with TestClient(create_app(path)) as first:
        session = first.post(
            "/api/auth/setup",
            json={"username": "testowner", "password": "Test-password-123!"},
        ).json()
        first.headers["X-CSRF-Token"] = session["csrf_token"]
        item = product(first, stock=2)
        assert (
            sell(first, [{"product_id": item["id"], "quantity": 1}]).status_code == 201
        )
    with TestClient(create_app(path)) as reopened:
        session = reopened.post(
            "/api/auth/login",
            json={"username": "testowner", "password": "Test-password-123!"},
        ).json()
        reopened.headers["X-CSRF-Token"] = session["csrf_token"]
        assert reopened.get("/api/products").json()[0]["stock"] == 1
        assert len(reopened.get("/api/sales").json()) == 1
