-- Run against a copy of the SQLite database. These are read-only examples.

-- 1. JOIN + GROUP BY: revenue and saved costs by product category.
SELECT p.category,
       SUM(si.quantity * si.unit_price_paise) AS gross_sales_paise,
       SUM(si.quantity * si.unit_cost_paise) AS known_cost_paise,
       SUM(CASE WHEN si.unit_cost_paise IS NULL THEN si.quantity ELSE 0 END) AS uncosted_units
FROM sale_items si
JOIN products p ON p.id = si.product_id
JOIN sales s ON s.id = si.sale_id
GROUP BY p.category;

-- 2. CTE + window function: rank products within each category by units sold.
-- This ranks gross units; the application separately reports net revenue after returns.
WITH product_sales AS (
    SELECT p.id, p.name, p.category, COALESCE(SUM(si.quantity), 0) AS units_sold
    FROM products p LEFT JOIN sale_items si ON si.product_id = p.id
    GROUP BY p.id
)
SELECT name, category, units_sold,
       DENSE_RANK() OVER (PARTITION BY category ORDER BY units_sold DESC) AS category_rank
FROM product_sales
ORDER BY category, category_rank;

-- 3. Reconciliation: these rows should be empty when stock totals are consistent.
SELECT p.sku, p.stock AS recorded_total, COALESCE(SUM(i.quantity), 0) AS location_total
FROM products p LEFT JOIN inventory i ON i.product_id = p.id
GROUP BY p.id
HAVING p.stock != COALESCE(SUM(i.quantity), 0);

-- 4. Debug supplier data problems with their original field mapping.
SELECT id, source, status, mapping_json, errors_json, actor, created_at
FROM import_batches WHERE status = 'rejected'
ORDER BY id DESC;

-- 5. Trace one stock adjustment back to its staff member and location.
SELECT p.sku, l.name AS location, m.change, m.reason, m.actor, m.created_at
FROM stock_movements m JOIN products p ON p.id = m.product_id
JOIN locations l ON l.id = m.location_id
ORDER BY m.id DESC;
