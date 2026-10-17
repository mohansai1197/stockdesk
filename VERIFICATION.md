# Verification

The sanitized standalone source export passed **60 tests** on 17 October 2026 using Python 3.12 and the project dependencies.

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

## Coverage

- Products, sales, purchases, returns, location stock, transactional updates, and weighted average costs.
- Owner/manager/cashier permissions, server-side authorization, and CSRF restrictions.
- Supplier CSV/JSON/XML parsing, mappings, validation, and error reporting.
- 250 mock accounting invoices across three pages; unchanged repeats and changed-original handling.
- Supplier, purchase-order, SKU, cost, quantity, date, and amount validation; cumulative overbilling prevention.
- Transient HTTP 503/429 failures, bounded retries, Retry-After handling, timeouts, and malformed pagination.
- Durable partial-failure reporting, connection claims, private key storage, and key exclusion from backups.
- Database migration, integrity checks, backup restoration, and session revocation.

Tests use disposable databases and fictional data. No runtime databases, staff credentials, API keys, or backups are distributed with this repository.

The existing environment emitted a Starlette/httpx adapter deprecation warning; the test suite passed. A separate fresh dependency installation, production deployment, load test, and comprehensive security audit were not performed as part of this export.
