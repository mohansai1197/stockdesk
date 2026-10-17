# A short StockDesk interview demo

Use a fresh local database or your own demo records; do not restore or clear another person's shop data.

1. **Owner and product:** Create the owner account. Add an A5 Notebook: SKU `DEMO-NB`, Stationery, selling price ₹85, purchase cost ₹60, opening stock 10 in Main shop.
2. **Supplier and purchase:** Add a supplier with a 5-day lead time. Order 10 notebooks at ₹80 into Warehouse. Show that stock stays 10 until receipt. Receive the order; total becomes 20 and average cost ₹70.
3. **Transfer:** Move 4 notebooks from Warehouse to Main shop. Show Main shop 14, Warehouse 6, total 20. Open History to see both transfer entries.
4. **Sale:** Sell 2 from Main shop. Revenue ₹170, saved cost ₹140, gross profit ₹30. Total stock becomes 18.
5. **Return:** Return 1 sellable notebook from that sale. Refund due ₹85, total stock 19, net revenue ₹85, gross profit ₹15. Explain that no actual payment is processed.
6. **Supplier mapping:** In Integrations, choose `samples/supplier-catalog.csv`, starting mapping Supplier column names, Preview & map fields. Point out Item Code → sku. Save a profile name, then import. Repeating the import shows row errors without adding duplicates.
7. **HTTP API:** Connect demo supplier, selecting the supplier. Two different products arrive through the separate local service. Explain that it is a fictional API used to demonstrate real HTTP handling.
8. **Roles and audit:** Create a cashier account, sign in as that account, and show its smaller menu. Sign back in as owner. Activity records the actions and permission failures.
9. **Recovery:** Create and download a backup. Explain the password confirmation, automatic safety snapshot, and session invalidation on restore. Only restore your disposable demo database if you want to demonstrate it.

Keep the live explanation focused: data comes in, is checked, is stored, is used in transactions, and can be investigated or recovered.

## Invoice integration demonstration

Use the existing prepared dataset and start with `python run.py`. In a fresh shop, create suppliers and purchase orders before starting the mock source; it prepares invoices only once from the available orders.

1. **Connect:** Integrations → Connection settings → Use the locally configured demo key → Save connection. Review Supplier mappings; matching names are selected automatically.
2. **Failure after progress:** Before the first sync, Demo lab → Page two outage → Set scenario. Sync invoices → Start sync. The first 100 invoices save; page two fails after three attempts. Open Details to show the requests.
3. **Recovery:** Demo lab → Normal service. Sync again. The prepared 250-record source gives 150 imported, 100 already existed, 0 errors. The ledger contains 250, not 350.
4. **Idempotency:** Sync again. It skips all 250 unchanged invoices. Open View on an invoice to show its supplier, purchase order, original source ID, and line amounts.
5. **Retry behavior:** Demo lab → Page two rate limit. Sync and open Details: HTTP 429, one-second wait, then success. Temporary outage demonstrates a 503 retry; Slow page two demonstrates bounded timeouts.
6. **Changed records:** Demo lab → Changed first invoice. Sync: the original invoice stays intact and a SOURCE_CHANGED error explains that review is needed. Download the error CSV.
7. **Validation:** Three invalid invoices demonstrates total, supplier, and calendar-date errors. If you already imported the normal source, a changed supplier is reported as a changed original. Use a disposable fresh ledger to demonstrate three initial row-validation errors.
8. Finish by setting **Normal service**. Explain that invoices record billing; receiving purchases remains the action that moves stock. The mock is fictional, with real HTTP requests between local services.
