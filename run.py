"""Run with: python run.py. Stop the server with Ctrl+C."""

import os
import threading
from pathlib import Path

import uvicorn

from database import Database
from integration_secrets import demo_api_key
from mock_accounting import SOURCE_PATH, prepare_demo_source

if __name__ == "__main__":
    shop_path = Path(
        os.environ.get(
            "STOCKDESK_DB",
            Path(__file__).resolve().parent / ".data" / "stockdesk.sqlite3",
        )
    )
    Database(shop_path)
    source_path = Path(os.environ.get("STOCKDESK_ACCOUNTING_DB", SOURCE_PATH))
    count = prepare_demo_source(shop_path, source_path)
    demo_api_key()  # Initialize the shared key before either service starts.
    print(f"Mock accounting source: {count} fictional invoices on port 8703")
    accounting = uvicorn.Server(
        uvicorn.Config(
            "mock_accounting:app", host="127.0.0.1", port=8703, log_level="warning"
        )
    )
    accounting_thread = threading.Thread(target=accounting.run, daemon=True)
    accounting_thread.start()
    print("Open StockDesk at http://127.0.0.1:8700")
    supplier = uvicorn.Server(
        uvicorn.Config(
            "mock_supplier:app", host="127.0.0.1", port=8701, log_level="warning"
        )
    )
    thread = threading.Thread(target=supplier.run, daemon=True)
    thread.start()
    print("Demo supplier API: http://127.0.0.1:8701/catalog (fictional data)")
    try:
        uvicorn.run("app:app", host="127.0.0.1", port=8700)
    finally:
        accounting.should_exit = True
        accounting_thread.join(timeout=5)
        supplier.should_exit = True
        thread.join(timeout=5)
