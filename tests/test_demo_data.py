"""Validate the sample ledger's real report results and non-business data preservation."""

import sqlite3

from fastapi.testclient import TestClient

from app import create_app
from models import shop_today
from scripts.seed_demo import replace_business_data


def test_replacement_preserves_accounts_sessions_and_backup_and_reports_profit(
    tmp_path,
):
    path = tmp_path / "demo.sqlite3"
    with TestClient(create_app(path)) as client:
        session = client.post(
            "/api/auth/setup",
            json={"username": "sampleowner", "password": "Sample-test-password-123!"},
        ).json()
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        old = client.post(
            "/api/products",
            json={
                "sku": "OLD-SAMPLE",
                "name": "Previous sample",
                "category": "Old",
                "price": "10.00",
                "stock": 5,
            },
        ).json()
        assert (
            client.post(
                "/api/sales", json={"items": [{"product_id": old["id"], "quantity": 1}]}
            ).status_code
            == 201
        )
        manifest, backup, _ = replace_business_data(path)
        assert client.get("/api/auth/me").json()["username"] == "sampleowner"
        assert manifest["counts"]["products"] == 50
        assert manifest["counts"]["sales"] == 240
        assert manifest["counts"]["returns"] == 24
        assert manifest["counts"]["transfers"] == 20
        report = client.get("/api/reports").json()
        for key, expected in manifest["current_month"].items():
            assert report[key] == expected, (key, report[key], expected)
        assert report["gross_profit_paise"] > 0 and report["uncosted_units"] == 0
        full = client.get(
            f"/api/reports?start={manifest['start']}&end={manifest['end']}"
        ).json()
        for key, expected in manifest["full_period"].items():
            assert full[key] == expected
        health = client.get("/api/operations").json()
        assert health["stock_mismatches"] == [] and health["sale_total_mismatches"] == 0
        with sqlite3.connect(backup) as original:
            assert original.execute("SELECT sku FROM products").fetchall() == [
                ("OLD-SAMPLE",)
            ]
            assert original.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
            assert original.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        with sqlite3.connect(path) as current:
            assert (
                current.execute(
                    "SELECT COUNT(*) FROM products WHERE sku='OLD-SAMPLE'"
                ).fetchone()[0]
                == 0
            )
            assert (
                current.execute(
                    "SELECT COUNT(*) FROM products WHERE cost_paise IS NULL"
                ).fetchone()[0]
                == 0
            )
            assert (
                current.execute(
                    "SELECT COUNT(*) FROM sale_items WHERE unit_cost_paise IS NULL"
                ).fetchone()[0]
                == 0
            )
            assert (
                current.execute(
                    "SELECT COUNT(DISTINCT sale_date) FROM sales"
                ).fetchone()[0]
                == 60
            )
            assert current.execute(
                "SELECT MIN(sale_date), MAX(sale_date) FROM sales"
            ).fetchone() == (manifest["start"], manifest["end"])
            assert not current.execute("PRAGMA foreign_key_check").fetchall()
            assert not current.execute(
                """SELECT i.product_id,i.location_id FROM inventory i LEFT JOIN stock_movements m ON m.product_id=i.product_id AND m.location_id=i.location_id GROUP BY i.product_id,i.location_id HAVING i.quantity!=COALESCE(SUM(m.change),0)"""
            ).fetchall()


def test_sample_replacement_is_repeatable_and_has_useful_screen_data(tmp_path):
    path = tmp_path / "demo.sqlite3"
    first, _, _ = replace_business_data(path)
    second, _, _ = replace_business_data(path)
    assert first == second
    assert first["received_purchases"] == 24 and first["pending_purchases"] == 6
    assert first["low_stock_products"] >= 3
    assert first["counts"]["suppliers"] == 8 and first["counts"]["locations"] == 3
    assert first["end"] == shop_today().isoformat()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM mapping_profiles").fetchone()[0] == 2
        assert (
            db.execute("SELECT COUNT(DISTINCT category) FROM products").fetchone()[0]
            == 5
        )
