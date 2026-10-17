# Understand StockDesk without learning everything at once

The project is one shop workflow with supporting features. Start with a notebook, then follow what happens to its data.

## 1. Your one-sentence explanation

“I built an app that helps a shop buy stock, record sales, handle returns, and understand its inventory. It also imports supplier data and controls what different staff members can do.”

## 2. Learn the core in this order

### Products and locations

A **product** describes what an item is: SKU, name, category, selling price, and purchase cost. **Inventory** describes how many units of that product are at each location.

Example: one notebook product, 24 units in Main shop, 10 in Warehouse. There are 34 units in total. A transfer changes where units are; a sale changes how many units the business has.

Read: `ProductInput` in `models.py`, `products` and `inventory` in `schema.sql`, `adjust_stock()` in `database.py`.

### Purchases

A **supplier** is the company you buy from. A **purchase order** is a list of what you intend to buy. Creating the order does not add stock; receiving it does.

Each order has several `purchase_items`. Receipt adds their quantities to the chosen location and records the unit cost. An order can be received only once. This version receives the complete order rather than partial deliveries.

Read: `order()` and `receive()` in `app.py`.

### Sales

A customer buys two notebooks. The browser sends the product ID, quantity, and location to Python. The server reads the stored price and location stock, checks availability, and saves the sale and stock deduction together.

The sale's selling price **and purchase cost** are copied into `sale_items`. That is why changing today's price cannot change yesterday's profit.

Read: `SaleInput` → `record_sale()` → `sales` and `sale_items` tables.

### Returns

A return references a particular line from the original sale. Python checks the quantity sold minus quantities already returned. You cannot return the same unit twice.

A sellable return goes back into the original sale location. A damaged return records a refund but does not increase stock. Refund amounts use the original selling price, not today's price. The app records a refund due; it does not move real money.

Read: `return_sale()` → `returns` and `return_items` tables.

## 3. Why transactions matter

A **transaction** means several database changes succeed together. Saving a sale without reducing stock would leave incorrect records. If a later line fails, the entire sale is rolled back.

`BEGIN IMMEDIATE` acquires SQLite's write lock before checking stock. If two staff members try to sell the final unit, the second request waits and then sees the updated stock. One succeeds; the other gets an insufficient-stock message.

The same idea protects receipts, transfers, returns, and imports. Tests cover these rules, including two simultaneous returns of the same last unit.

## 4. How costs and gross profit work

Money is stored in **paise**: ₹19.99 becomes `1999`, avoiding floating-point rounding errors.

Suppose you have 10 units costing ₹60 and receive another 10 costing ₹80:

```text
Average unit cost = (10 × 60 + 10 × 80) ÷ 20 = ₹70
```

Selling one at ₹85 gives a gross margin of ₹15. The app stores that ₹70 cost with the sale. It rounds weighted unit costs to the nearest paise; it is not a full accounting or tax system.

Reports show:

```text
Net revenue = gross sales − recorded refunds
Gross profit = net revenue − cost of goods sold
```

Sellable returns reverse the original item's cost. Damaged returns do not. Reports use the date each sale or return happened. Rent, salaries, and taxes are not included.

Older sales without purchase costs show **Unknown** profit. Entering a cost today does not invent a historical cost for an old sale.

## 5. The integration feature and why it matters for the JD

Integration means getting data from another system into yours accurately.

One supplier calls a field **Item Code**; StockDesk calls it **sku**. A mapping tells Python these mean the same thing. You can preview and map CSV, JSON, or XML records. All formats end up as the same `ProductInput` objects and pass the same validation.

If any row is invalid or already exists, nothing is imported. The attempt still saves its field mapping and row errors so you can investigate it. Saving a mapping under the same profile name creates another version.

The demo supplier runs as a **separate HTTP service** on port 8701. StockDesk requests `/catalog`, checks the response, maps its alternate headings, validates its products, and saves them. An outage is reported as an integration failure. This supplier is fictional.

Read: `supplier_imports.py` → `run_import()` → `supplier_api()` in `app.py` → `mock_supplier.py`.

### Pulling invoices from another application

The accounting example adds **fetch → convert → validate → save → report**. The separate mock service holds 250 fictional invoices. StockDesk sends an API key in a header and asks for 100 at a time. The response includes a cursor telling StockDesk how to request the next page. That is pagination.

The source calls an invoice number `invoice_no`; StockDesk calls it `invoice_number`. Python converts those fields, checks the supplier mapping, finds the purchase order, and verifies dates, quantities, unit costs, and totals. A supplier invoice is evidence of billing; it does not mean goods have arrived. That is why syncing invoices does not receive stock or change sales profit.

The important failure example: page one saves 100 invoices, then page two is unavailable. The run shows a partial failure. Restore **Normal service** in **Demo lab** and retry: 100 are recognized, 150 are newly imported, and the ledger has 250. The source invoice ID is unique within its connection; a stable hash checks whether the contents changed. A changed invoice is flagged rather than overwriting the original. This safe-repeat behavior is called **idempotency**.

Temporary outages retry at most twice after the initial attempt. Rate limits use the API's requested wait. Slow requests time out. Invalid invoice rows get specific errors while other valid rows save. **Run Details** shows what happened, including requests and mapping snapshots. This gives you concrete authentication, pagination, validation, error handling, and SQL constraints to explain in an integration-engineer interview.

Read: `mock_accounting.py` → `InvoiceSync.fetch_page()` → `canonical_invoice()` → `save_invoice()` and `save_page()` in `accounting_sync.py` → the `accounting_*` tables in `schema.sql`. Start with the successful path, then follow the page-two outage.

## 6. Roles and authentication

- **Owner:** every feature, plus staff accounts and backups.
- **Manager:** shop operations, imports, and reports.
- **Cashier:** inventory, stock history, sales history, and recording sales.

Authentication answers “Who are you?” Authorization answers “Are you allowed to do this?” Hiding a button is helpful, but the server must still reject a forbidden API request.

Passwords are stored as salted `scrypt` hashes. A hash is a one-way representation used to check a password. Logging in creates a random session cookie with an eight-hour expiry. The database stores a hash of that session token. Requests that change data also need a session-specific CSRF token.

Read: `install_auth()` in `security.py`, then a route's `user=manager` or `user=owner` dependency in `app.py`.

## 7. Reorder suggestions

The rule is intentionally explainable:

```text
Daily demand = units sold during the last 30 calendar days ÷ 30
Target = larger of (alert threshold + 1) and ceiling(daily demand × (lead days + 7))
Suggested order = max(0, target − stock on hand − stock already ordered)
```

These are suggestions, not automatic purchases or advanced forecasting. Review them before creating an order.

## 8. Activity, monitoring, and recovery

**Activity** records who changed something, what changed, and when. Import errors and supplier outages help explain failures. Basic counters show requests/errors since the server restarted; SQL checks compare stored stock totals with location stock and check sale arithmetic.

**Backups** use SQLite's backup API to make a consistent snapshot, including when the database uses its WAL journal. Owners can download a copy. Restoring checks the file, pauses other authorized requests, makes a safety copy, restores the selected database, and invalidates all sessions. The owner must enter their password and type RESTORE.

The earliest pre-upgrade backups are archives. The in-app restore feature accepts version 2 and 3 backups, migrating version 2 after restoration. Invoice tables use version 3. API keys are private local files and are excluded from database backups; re-enter a key when moving a backup to another computer. Keep downloaded backups secure: they contain shop records and password hashes.

Read: `Database.backup()` and `Database.restore()` in `database.py`.

## 9. SQL you can explain

Find `build_report()` in `app.py` and `sql/operations.sql`:

- `JOIN` connects products, sale items, sales, and locations.
- `SUM` and `COUNT` calculate totals.
- `GROUP BY` summarizes categories and days.
- `CASE` identifies missing costs and low stock.
- `UNION ALL` combines sale income and negative return amounts.
- `HAVING` finds mismatched totals.
- A CTE and `DENSE_RANK()` example ranks products within each category.

## 10. Interview explanation

“StockDesk manages a shop's inventory, purchases, sales, and returns. I used FastAPI and SQLite with transactions to keep stock updates consistent. It supports staff roles and multiple locations, and stores historical sale prices and costs for gross-profit reporting. For integration work, it maps supplier CSV/JSON/XML fields into one schema and calls a separate mock supplier API over HTTP. Rejected imports keep row-level errors. I tested permissions, concurrent stock and return updates, costs, migration, and backup recovery.”

Start with the notebook workflow, then explain the integration example. You do not need to recite every endpoint. Be candid about AI assistance and demonstrate that you understand the behavior, tradeoffs, and tests.
