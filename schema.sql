PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price_paise INTEGER NOT NULL CHECK (price_paise > 0),
    stock INTEGER NOT NULL CHECK (stock BETWEEN 0 AND 1000000),
    low_stock_at INTEGER NOT NULL CHECK (low_stock_at >= 0)
);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY,
    sale_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    total_paise INTEGER NOT NULL CHECK (total_paise > 0)
);

CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES sales(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise > 0),
    UNIQUE (sale_id, product_id)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    sale_id INTEGER REFERENCES sales(id),
    change INTEGER NOT NULL CHECK (change != 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS sales_by_date ON sales(sale_date);
CREATE INDEX IF NOT EXISTS movements_by_product ON stock_movements(product_id, id);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS inventory (
    product_id INTEGER NOT NULL REFERENCES products(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    quantity INTEGER NOT NULL CHECK(quantity BETWEEN 0 AND 1000000),
    PRIMARY KEY(product_id, location_id)
);
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, email TEXT NOT NULL DEFAULT '',
    lead_days INTEGER NOT NULL DEFAULT 7 CHECK(lead_days BETWEEN 1 AND 90)
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','manager','cashier')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    csrf_token TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    status TEXT NOT NULL DEFAULT 'ordered' CHECK(status IN ('ordered','received')),
    total_paise INTEGER NOT NULL CHECK(total_paise>0), actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), received_at TEXT
);
CREATE TABLE IF NOT EXISTS purchase_items (
    id INTEGER PRIMARY KEY, purchase_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL CHECK(quantity>0),
    cost_paise INTEGER NOT NULL CHECK(cost_paise>0), UNIQUE(purchase_id,product_id)
);
CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL REFERENCES sales(id), return_date TEXT NOT NULL,
    total_paise INTEGER NOT NULL CHECK(total_paise>0), reason TEXT NOT NULL, actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS return_items (
    id INTEGER PRIMARY KEY, return_id INTEGER NOT NULL REFERENCES returns(id),
    sale_item_id INTEGER NOT NULL REFERENCES sale_items(id), quantity INTEGER NOT NULL CHECK(quantity>0),
    restock INTEGER NOT NULL CHECK(restock IN (0,1)), UNIQUE(return_id,sale_item_id)
);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
    from_location INTEGER NOT NULL REFERENCES locations(id), to_location INTEGER NOT NULL REFERENCES locations(id),
    quantity INTEGER NOT NULL CHECK(quantity>0), actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK(from_location != to_location)
);
CREATE TABLE IF NOT EXISTS mapping_profiles (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, supplier_id INTEGER REFERENCES suppliers(id),
    version INTEGER NOT NULL, mapping_json TEXT NOT NULL, actor TEXT NOT NULL,
    UNIQUE(name,version)
);
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, supplier_id INTEGER REFERENCES suppliers(id),
    location_id INTEGER NOT NULL REFERENCES locations(id), mapping_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('imported','rejected')), imported INTEGER NOT NULL,
    errors_json TEXT NOT NULL, actor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS returns_by_date ON returns(return_date);
CREATE INDEX IF NOT EXISTS activity_by_time ON activity(id DESC);
CREATE INDEX IF NOT EXISTS sale_items_by_product ON sale_items(product_id);
CREATE INDEX IF NOT EXISTS return_items_by_sale_item ON return_items(sale_item_id);

CREATE TABLE IF NOT EXISTS accounting_connections (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
    secret_ref TEXT NOT NULL, lease_token TEXT, lease_until REAL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS accounting_supplier_mappings (
    connection_id INTEGER NOT NULL REFERENCES accounting_connections(id),
    source_code TEXT NOT NULL, source_name TEXT NOT NULL,
    supplier_id INTEGER REFERENCES suppliers(id), PRIMARY KEY(connection_id,source_code)
);
CREATE TABLE IF NOT EXISTS accounting_sync_runs (
    id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES accounting_connections(id),
    status TEXT NOT NULL, actor TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
    seen INTEGER NOT NULL DEFAULT 0, imported INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '', mapping_json TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), finished_at TEXT
);
CREATE TABLE IF NOT EXISTS accounting_invoices (
    id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES accounting_connections(id),
    source_id TEXT NOT NULL, invoice_number TEXT NOT NULL, invoice_date TEXT NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id), purchase_id INTEGER NOT NULL REFERENCES purchase_orders(id),
    currency TEXT NOT NULL CHECK(currency='INR'), total_paise INTEGER NOT NULL CHECK(total_paise>0),
    payload_hash TEXT NOT NULL, source_json TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES accounting_sync_runs(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(connection_id,source_id), UNIQUE(supplier_id,invoice_number)
);
CREATE TABLE IF NOT EXISTS accounting_invoice_items (
    id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES accounting_invoices(id),
    product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL CHECK(quantity>0),
    unit_cost_paise INTEGER NOT NULL CHECK(unit_cost_paise>0), line_total_paise INTEGER NOT NULL CHECK(line_total_paise>0),
    UNIQUE(invoice_id,product_id)
);
CREATE TABLE IF NOT EXISTS accounting_sync_errors (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES accounting_sync_runs(id),
    page INTEGER NOT NULL, source_id TEXT NOT NULL, code TEXT NOT NULL,
    message TEXT NOT NULL, source_preview TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounting_sync_attempts (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES accounting_sync_runs(id),
    page INTEGER NOT NULL, attempt INTEGER NOT NULL, status_code INTEGER,
    code TEXT NOT NULL, message TEXT NOT NULL, retry_delay REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS accounting_errors_by_run ON accounting_sync_errors(run_id,id);
CREATE INDEX IF NOT EXISTS accounting_attempts_by_run ON accounting_sync_attempts(run_id,id);
