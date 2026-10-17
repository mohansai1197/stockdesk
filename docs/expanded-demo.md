# Larger sample: what to explore

This is fictional shop data for **19 August–17 October 2026**. Earlier business records were backed up and replaced; staff accounts were kept.

| Screen | Sample records |
| --- | --- |
| Inventory | 50 products across Stationery, Home, Everyday, Office, and Accessories; 5 low-stock products |
| Sales | 240 purchases across 60 days, containing 716 sale lines; every line has a saved purchase cost |
| Purchasing | 8 suppliers and 30 orders: 24 received, 6 pending |
| Locations | Main shop, Warehouse, and City branch, with 20 stock transfers |
| Returns | 24 returns, including both sellable and damaged items |
| Integrations | Two mapping profiles and imported/rejected example batches |
| Reports | Known costs, category totals, daily sales, recorded refunds, and gross profit |

After signing in, open **Reports**. The initial October totals are:

- Gross sales: **₹93,434.00** from 68 sales
- Recorded refunds: **₹2,532.00**
- Net revenue: **₹90,902.00**
- Cost of goods sold: **₹57,320.90**
- Gross profit: **₹33,581.10**

Choose **19 August–17 October 2026** for the full sample: net revenue **₹3,84,231.00**, gross profit **₹1,41,030.58**. These are sample business results, not performance claims. Gross profit excludes rent, salaries, fees, and taxes.

The transaction history intentionally uses labels such as Sample cashier and Sample manager. They describe generated records; no extra staff login accounts were created. Actual transactions added later use the signed-in staff member.

The sample CSV download includes the larger catalog with known costs. Its stock values are opening balances before the generated transaction history; importing it alone does not reproduce the ledger. Use `scripts/seed_demo.py --replace-business-data` only when intentionally replacing the business data, with the server stopped first.
