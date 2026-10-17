"""Invoice integration tests with an independent source and disposable shop data."""

import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app
from database import Database
from mock_accounting import create_accounting_app, prepare_demo_source

KEY = "Fictional-accounting-test-key"
PASSWORD = "Test-password-123!"


@pytest.fixture
def integration(tmp_path):
    application = create_app(tmp_path / "shop.sqlite3")
    with TestClient(application) as client:
        setup = client.post(
            "/api/auth/setup", json={"username": "owner", "password": PASSWORD}
        )
        client.headers["X-CSRF-Token"] = setup.json()["csrf_token"]
        product = client.post(
            "/api/products",
            json={
                "sku": "NOTE",
                "name": "Notebook",
                "category": "Paper",
                "price": "85.00",
                "cost": "60.00",
                "stock": 10,
            },
        ).json()
        supplier = client.post(
            "/api/suppliers", json={"name": "Fictional stationery"}
        ).json()
        purchase = client.post(
            "/api/purchases",
            json={
                "supplier_id": supplier["id"],
                "items": [
                    {"product_id": product["id"], "quantity": 1000, "cost": "60.00"}
                ],
            },
        ).json()
        source_path = tmp_path / "accounting.sqlite3"
        assert prepare_demo_source(application.state.store.path, source_path) == 250
        source = create_accounting_app(source_path, KEY)
        service = application.state.accounting
        service.client_factory = lambda: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=source)
        )
        delays = []

        async def sleep(delay):
            delays.append(delay)

        service.sleep = sleep
        connection = client.post(
            "/api/accounting/connection", json={"use_demo_key": False, "api_key": KEY}
        )
        assert connection.status_code == 200, connection.text
        assert connection.json()["mappings"][0]["supplier_id"] == supplier["id"]
        yield client, source_path, service, delays, purchase


def sync(client):
    response = client.post("/api/accounting/sync")
    assert response.status_code == 200, response.text
    return response.json()


def scenario(client, mode):
    response = client.post("/api/accounting/demo/fault", json={"mode": mode})
    assert response.status_code == 200, response.text


def edit_source(path, position, change):
    with sqlite3.connect(path) as db:
        raw = json.loads(
            db.execute(
                "SELECT payload FROM invoices WHERE position=?", (position,)
            ).fetchone()[0]
        )
        change(raw)
        db.execute(
            "UPDATE invoices SET payload=? WHERE position=?",
            (json.dumps(raw), position),
        )


def test_three_pages_repeat_safely_without_changing_shop(integration):
    client, source_path, service, _delays, purchase = integration
    before = client.get("/api/products").json(), client.get("/api/summary").json()
    first = sync(client)
    assert (first["status"], first["pages"], first["imported"], first["errors"]) == (
        "completed",
        3,
        250,
        0,
    )
    assert first["attempt_count"] == 3
    again = sync(client)
    assert (again["imported"], again["duplicates"], again["errors"]) == (0, 250, 0)
    assert client.get("/api/accounting/invoices").json()["total"] == 250
    assert before == (
        client.get("/api/products").json(),
        client.get("/api/summary").json(),
    )
    invoice = client.get("/api/accounting/invoices/1").json()
    assert invoice["purchase_id"] == purchase["id"]
    assert invoice["total_paise"] == 6000 and invoice["items"][0]["quantity"] == 1
    assert prepare_demo_source(service.store.path, source_path) == 250
    assert sync(client)["duplicates"] == 250  # Source IDs survive a restart.


def test_page_two_failure_preserves_first_page_then_retry_continues(integration):
    client, _, _, delays, _ = integration
    scenario(client, "page_two_outage")
    failed = sync(client)
    assert (failed["status"], failed["pages"], failed["imported"]) == (
        "partial_failure",
        1,
        100,
    )
    assert failed["attempt_count"] == 4 and delays == [0.25, 0.5]
    scenario(client, "normal")
    retried = sync(client)
    assert (retried["status"], retried["imported"], retried["duplicates"]) == (
        "completed",
        150,
        100,
    )
    assert client.get("/api/accounting/invoices").json()["total"] == 250


@pytest.mark.parametrize(
    "mode,delay,status", [("temporary_outage", 0.25, 503), ("rate_limit", 1, 429)]
)
def test_transient_failures_retry_and_respect_requested_delay(
    integration, mode, delay, status
):
    client, _, _, delays, _ = integration
    scenario(client, mode)
    run = sync(client)
    assert run["imported"] == 250 and run["attempt_count"] == 4
    assert delays == [delay]
    detail = client.get(f"/api/accounting/runs/{run['id']}").json()
    retry = next(item for item in detail["attempts"] if item["status_code"] == status)
    assert retry["retry_delay"] == delay


def test_bad_key_stops_without_retry_or_leaking_secret(integration):
    client, _, service, delays, _ = integration
    wrong = "Wrong-fictional-accounting-key"
    configured = client.post(
        "/api/accounting/connection", json={"use_demo_key": False, "api_key": wrong}
    )
    assert configured.status_code == 200 and "source_error" in configured.json()
    run = sync(client)
    assert run["status"] == "failed" and "AUTH_FAILED" in run["message"]
    assert run["attempt_count"] == 1 and delays == []
    assert client.get("/api/auth/me").status_code == 200
    assert (
        wrong
        not in configured.text + json.dumps(run) + client.get("/api/activity").text
    )
    backup = service.store.backup()
    assert wrong.encode() not in (service.store.backups / backup).read_bytes()
    key_file = next((service.store.path.parent / "accounting-secrets").glob("*.key"))
    assert key_file.stat().st_mode & 0o077 == 0


def test_invalid_rows_saved_as_errors_and_can_be_corrected(integration):
    client, _, _, _, _ = integration
    scenario(client, "invalid_data")
    run = sync(client)
    assert (run["status"], run["imported"], run["errors"]) == (
        "completed_with_errors",
        247,
        3,
    )
    detail = client.get(f"/api/accounting/runs/{run['id']}").json()
    assert {row["code"] for row in detail["error_details"]} == {
        "INVOICE_TOTAL_MISMATCH",
        "UNKNOWN_SUPPLIER",
        "INVALID_DATE",
    }
    assert (
        "INVOICE_TOTAL_MISMATCH"
        in client.get(f"/api/accounting/runs/{run['id']}/errors.csv").text
    )
    scenario(client, "normal")
    again = sync(client)
    assert (again["imported"], again["duplicates"], again["errors"]) == (3, 247, 0)


def test_changed_source_is_flagged_and_original_preserved(integration):
    client, _, _, _, _ = integration
    sync(client)
    original = client.get("/api/accounting/invoices/1").json()
    scenario(client, "changed_invoice")
    run = sync(client)
    assert (run["imported"], run["duplicates"], run["errors"]) == (0, 249, 1)
    assert (
        client.get(f"/api/accounting/runs/{run['id']}").json()["error_details"][0][
            "code"
        ]
        == "SOURCE_CHANGED"
    )
    assert original == client.get("/api/accounting/invoices/1").json()


def test_missing_mapping_can_be_fixed_then_retried(integration):
    client, _, _, _, _ = integration
    client.post(
        "/api/accounting/suppliers/map",
        json={"source_code": "SUP-0001", "supplier_id": None},
    )
    assert sync(client)["errors"] == 250
    client.post(
        "/api/accounting/suppliers/map",
        json={"source_code": "SUP-0001", "supplier_id": 1},
    )
    assert sync(client)["imported"] == 250


def test_purchase_line_and_amount_validation(integration):
    client, path, _, _, _ = integration
    edit_source(path, 1, lambda row: row.update(purchase_ref="SD-PO-999999"))
    edit_source(path, 2, lambda row: row["lines"][0].update(item_code="OTHER-SKU"))
    edit_source(path, 3, lambda row: row["lines"][0].update(units=2))

    def wrong_cost(row):
        row["lines"][0].update(unit_cost="61.00", line_total="61.00")
        row["amount_total"] = "61.00"

    edit_source(path, 4, wrong_cost)
    run = sync(client)
    assert run["imported"] == 246 and run["errors"] == 4
    errors = client.get(f"/api/accounting/runs/{run['id']}").json()["error_details"]
    assert [row["code"] for row in errors] == [
        "UNKNOWN_PURCHASE",
        "PO_LINE_MISMATCH",
        "LINE_TOTAL_MISMATCH",
        "PO_LINE_MISMATCH",
    ]


def test_cumulative_invoices_cannot_exceed_purchase_quantity(integration):
    client, _, service, _, _ = integration
    with service.store.transaction(write=True) as db:
        db.execute("UPDATE purchase_items SET quantity=2")
    run = sync(client)
    assert run["imported"] == 2 and run["errors"] == 248
    assert (
        client.get(f"/api/accounting/runs/{run['id']}").json()["error_details"][0][
            "code"
        ]
        == "OVER_BILLED"
    )


def test_timeout_retries_are_bounded_and_prior_page_survives(integration):
    client, _, service, delays, _ = integration
    original = service.client_factory
    transport = original()._transport

    class TimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.params.get("cursor"):
                raise httpx.ReadTimeout("fictional timeout", request=request)
            return await transport.handle_async_request(request)

    service.client_factory = lambda: httpx.AsyncClient(transport=TimeoutTransport())
    run = sync(client)
    assert run["status"] == "partial_failure" and run["imported"] == 100
    assert run["attempt_count"] == 4 and delays == [0.25, 0.5]
    assert "TIMEOUT" in run["message"]


def test_long_rate_limit_is_not_retried_early(integration):
    client, _, service, delays, _ = integration
    service.client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "120"})
        )
    )
    run = sync(client)
    assert run["status"] == "failed" and run["attempt_count"] == 1
    assert delays == [] and "Retry later" in run["message"]


def test_invalid_pagination_and_unexpected_error_never_report_success(integration):
    client, _, service, _, _ = integration
    service.client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "items": [],
                    "total": 250,
                    "snapshot": "s",
                    "next_cursor": "again",
                },
            )
        )
    )
    run = sync(client)
    assert run["status"] == "failed" and "BAD_PAGINATION" in run["message"]

    def broken():
        raise RuntimeError("Internal fake failure")

    service.client_factory = broken
    run = sync(client)
    assert run["status"] == "failed" and "INTERNAL_ERROR" in run["message"]


def test_active_connection_claim_blocks_sync_and_setting_changes(integration):
    client, _, service, _, _ = integration
    with service.store.transaction(write=True) as db:
        db.execute(
            "UPDATE accounting_connections SET lease_token='active',lease_until=?",
            (time.time() + 60,),
        )
    assert client.post("/api/accounting/sync").status_code == 409
    assert client.post("/api/accounting/connection", json={}).status_code == 409
    assert (
        client.post(
            "/api/accounting/suppliers/map",
            json={"source_code": "SUP-0001", "supplier_id": None},
        ).status_code
        == 409
    )


def test_invoice_endpoints_enforce_roles_and_csrf(integration):
    client, _, _, _, _ = integration
    for role in ("cashier", "manager"):
        assert (
            client.post(
                "/api/staff",
                json={
                    "username": role,
                    "display_name": role,
                    "password": PASSWORD,
                    "role": role,
                },
            ).status_code
            == 201
        )
        with TestClient(client.app) as staff:
            login = staff.post(
                "/api/auth/login", json={"username": role, "password": PASSWORD}
            ).json()
            assert staff.post("/api/accounting/sync").status_code == 403
            staff.headers["X-CSRF-Token"] = login["csrf_token"]
            assert staff.post("/api/accounting/connection", json={}).status_code == 403
            assert staff.get("/api/accounting/invoices").status_code == (
                200 if role == "manager" else 403
            )
            assert staff.post("/api/accounting/sync").status_code == (
                200 if role == "manager" else 403
            )


def test_version_two_upgrade_keeps_stock_and_creates_archive(tmp_path):
    path = tmp_path / "shop.sqlite3"
    store = Database(path)
    with store.transaction(write=True) as db:
        db.execute(
            "INSERT INTO products(sku,name,category,price_paise,stock,low_stock_at) VALUES ('KEEP','Keep','Paper',100,12,2)"
        )
        db.execute(
            "INSERT INTO inventory(product_id,location_id,quantity) VALUES (1,1,7),(1,2,5)"
        )
        db.execute("PRAGMA user_version=2")
    upgraded = Database(path)
    with upgraded.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert [
            row[0]
            for row in db.execute("SELECT quantity FROM inventory ORDER BY location_id")
        ] == [7, 5]
        assert db.execute("SELECT stock FROM products").fetchone()[0] == 12
    assert len(list(upgraded.backups.glob("pre-upgrade-*.sqlite3"))) == 1


def test_version_two_backup_restores_and_migrates_without_losing_stock(integration):
    client, _, service, _, _ = integration
    backup = service.store.backup()
    with sqlite3.connect(service.store.backups / backup) as db:
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'accounting_%'"
        ).fetchall()
        for (name,) in tables:
            db.execute(f'DROP TABLE "{name}"')
        db.execute("PRAGMA user_version=2")
    assert client.get("/api/backups").json()[0]["schema_version"] == 2
    sync(client)
    restored = client.post(
        f"/api/backups/{backup}/restore",
        json={"confirmation": "RESTORE", "password": PASSWORD},
    )
    assert restored.status_code == 200, restored.text
    assert client.get("/api/auth/me").status_code == 401
    login = client.post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD}
    ).json()
    client.headers["X-CSRF-Token"] = login["csrf_token"]
    assert client.get("/api/products").json()[0]["stock"] == 10
    assert client.get("/api/accounting/invoices").json()["total"] == 0
    with service.store.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
