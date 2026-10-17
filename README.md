# StockDesk

A local inventory, sales, purchasing, and supplier-integration application for a small shop. Built with Python, FastAPI, SQLite, SQL, and JavaScript.

StockDesk connects a complete business workflow: buy stock, receive it, sell it, handle returns, and review results. Supplier imports and a separate mock accounting API demonstrate field mapping, data validation, paginated ingestion, retries, and troubleshooting.

## Features

- Owner, manager, and cashier permissions; hashed passwords, expiring sessions, and CSRF protection.
- Product inventory, location-level stock, purchase orders, weighted average costs, transfers, and partial returns.
- Transactional stock updates and SQL revenue, cost, gross-profit, and reorder reports.
- CSV, JSON, and XML supplier imports with preview, saved mapping profiles, row validation, and error history.
- A separate mock supplier HTTP service and an API-key-protected mock accounting service.
- Purchase-invoice synchronization with pagination, supplier/PO matching, exact monetary validation, bounded retries, duplicate protection, and durable run history.
- Database backup, integrity checks, password-confirmed restoration, and pre-restore safety copies.

## Requirements and setup

Use Python 3.12 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:8700 and create your owner account. There are no default accounts or shared passwords. All services bind only to this computer.

| Service | Address |
| --- | --- |
| StockDesk | http://127.0.0.1:8700 |
| Interactive API documentation | http://127.0.0.1:8700/docs |
| Fictional supplier catalog | http://127.0.0.1:8701/catalog |
| Mock accounting API documentation | http://127.0.0.1:8703/docs |

Ctrl+C stops the launcher and its services. Do not start a second copy while they are running.

## Optional synthetic demo

A clean clone contains source and synthetic sample files, but no existing shop database, users, API keys, backups, or transactions. The application creates its local database under `.data/`.

After first setup, stop the server and run the following only against a disposable demo shop. **This replaces its business records**, preserves staff accounts, and creates a backup first.

```sh
python scripts/seed_demo.py --replace-business-data
python run.py
```

The reproducible dataset includes 50 products, 240 sales, 8 suppliers, 3 locations, 30 purchase orders, 24 returns, and 20 transfers. Dates are generated relative to the shop date. On first launch against this dataset, the separate mock accounting service provisions up to 250 fictional invoices. Source IDs are preserved on subsequent launches.

For a smaller demonstration, use the files under `samples/` through the in-app supplier importer. File imports create products; the seed script creates the full transaction ledger.

## Invoice synchronization

1. Sign in as owner and open **Integrations → Connection settings**.
2. Select the locally configured demo key and review supplier mappings.
3. Start invoice synchronization and inspect its run details and errors.
4. Repeat the synchronization to demonstrate duplicate protection.
5. Use the Demo lab to simulate outages, rate limiting, timeouts, invalid data, or changed source invoices.

The service validates supplier and purchase-order identity, invoice dates, SKUs, unit costs, line arithmetic, totals, and cumulative billed quantities. Unchanged repeats are skipped. Changed source invoices are flagged for review while the originals are preserved. Each page saves its accepted records and errors atomically; completed pages survive a later page failure.

HTTP requests have bounded response sizes, timeouts, retry attempts, and an overall sync budget. HTTP 429 honors bounded `Retry-After` waits. Private local files hold API keys; keys are excluded from database backups and application responses.

These are fictional supplier and accounting integrations. The project does not connect to a real ERP, accounting provider, or ClearTax API. Importing billing records does not process payments or receive inventory.

## Architecture

```text
Browser → FastAPI → SQLite
             ├── HTTP → mock supplier API
             └── authenticated HTTP → mock accounting API → separate SQLite
```

- `app.py`, `database.py`, `models.py`: routes, validation, transactions, inventory, and reports.
- `supplier_imports.py`: file adapters, field mappings, and validation.
- `accounting_sync.py`, `mock_accounting.py`: invoice ingestion and the independent mock source.
- `security.py`, `integration_secrets.py`: permissions, sessions, and local key storage.
- `schema.sql`, `sql/operations.sql`: database structure and readable SQL analysis examples.
- `static/`: browser interface.
- `tests/`: isolated business, permission, import, sync, and recovery tests.

## Verification

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The export passed **60 tests** on 17 October 2026. See [VERIFICATION.md](VERIFICATION.md) for coverage and limitations.

## Documentation

- [Learning guide](LEARN.md)
- [Interview demo](docs/demo-script.md)
- [Synthetic dataset guide](docs/expanded-demo.md)
- [SQL examples](sql/operations.sql)

## Scope

This is a functional local portfolio project. Public multi-worker deployment, payment processing, legal tax validation, partial purchase receipt, and real-provider credentials are outside this version. Keep the default loopback binding. Downloaded database backups contain business records and staff password hashes and should be protected.
