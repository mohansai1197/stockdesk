"""Database migration, transactions, stock bookkeeping, and local backup recovery."""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent
MAX_STOCK = 1_000_000
SCHEMA_VERSION = 3


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backups = self.path.parent / "backups"
        self.backups.mkdir(exist_ok=True)
        self.condition = threading.Condition()
        self.active = 0
        self.maintenance = False
        self.request_active = 0
        self.request_maintenance = False
        connection = self.connect()
        old = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='products'"
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if old and version < SCHEMA_VERSION:
            self._copy(connection, f"pre-upgrade-{uuid.uuid4().hex}.sqlite3")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript((ROOT / "schema.sql").read_text())
        connection.execute("BEGIN IMMEDIATE")
        additions = {
            "products": {
                "cost_paise": "INTEGER",
                "supplier_id": "INTEGER REFERENCES suppliers(id)",
            },
            "sales": {
                "location_id": "INTEGER NOT NULL DEFAULT 1",
                "actor": "TEXT NOT NULL DEFAULT 'Legacy'",
            },
            "sale_items": {"unit_cost_paise": "INTEGER"},
            "stock_movements": {
                "location_id": "INTEGER NOT NULL DEFAULT 1",
                "actor": "TEXT NOT NULL DEFAULT 'Legacy'",
            },
        }
        for table, columns in additions.items():
            existing = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for name, declaration in columns.items():
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )
        connection.execute(
            "INSERT OR IGNORE INTO locations(id, name) VALUES (1, 'Main shop'), (2, 'Warehouse')"
        )
        if version < 2:
            connection.execute(
                "INSERT OR IGNORE INTO inventory(product_id, location_id, quantity) SELECT id, 1, stock FROM products"
            )
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
        connection.close()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def lease(self, exempt=False):
        if exempt:
            yield
            return
        with self.condition:
            self.condition.wait_for(lambda: not self.request_maintenance)
            self.request_active += 1
        try:
            yield
        finally:
            with self.condition:
                self.request_active -= 1
                self.condition.notify_all()

    @contextmanager
    def transaction(self, write=False):
        with self.condition:
            self.condition.wait_for(lambda: not self.maintenance)
            self.active += 1
        db = None
        try:
            db = self.connect()
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            if db:
                db.rollback()
            raise
        finally:
            if db:
                db.close()
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def _copy(self, source, name):
        target = sqlite3.connect(self.backups / name)
        try:
            source.backup(target)
        finally:
            target.close()
        return name

    def backup(self):
        with self.transaction() as db:
            name = f"backup-{uuid.uuid4().hex}.sqlite3"
            self._copy(db, name)
        return name

    def backup_path(self, name):
        import re

        if not re.fullmatch(
            r"(?:backup|safety|pre-upgrade)-[a-f0-9]{32}\.sqlite3", name
        ):
            raise HTTPException(404, "Backup not found")
        path = self.backups / name
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Backup not found")
        return path

    def backup_version(self, name):
        path = self.backup_path(name)
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                return db.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError:
            return None

    def restore(self, name, actor, session_hash):
        path = self.backup_path(name)
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise HTTPException(422, "Backup failed its integrity check")
            version = source.execute("PRAGMA user_version").fetchone()[0]
            if version not in (2, SCHEMA_VERSION):
                raise HTTPException(
                    422,
                    "This backup predates staff and location support. Keep it as an archive; restore a version 2 or 3 backup here.",
                )
            with self.condition:
                self.condition.wait_for(lambda: not self.request_maintenance)
                self.request_maintenance = True
                self.condition.notify_all()
                self.condition.wait_for(lambda: self.request_active == 0)
                self.maintenance = True
                self.condition.wait_for(lambda: self.active == 0)
            try:
                live = self.connect()
                try:
                    import time

                    current = live.execute(
                        "SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1 AND u.role='owner'",
                        (session_hash, time.time()),
                    ).fetchone()
                    if not current:
                        raise HTTPException(401, "Sign in again before restoring")
                    safety = self._copy(live, f"safety-{uuid.uuid4().hex}.sqlite3")
                    source.backup(live)
                    if version == 2:
                        live.executescript((ROOT / "schema.sql").read_text())
                        live.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    # Restoring old sessions must never resurrect logged-out sessions.
                    live.execute("DELETE FROM sessions")
                    audit(
                        live, actor, "backup.restored", name, {"safety_backup": safety}
                    )
                    live.commit()
                finally:
                    live.close()
            finally:
                with self.condition:
                    self.maintenance = False
                    self.request_maintenance = False
                    self.condition.notify_all()
        except sqlite3.DatabaseError:
            raise HTTPException(422, "This is not a valid StockDesk backup") from None
        finally:
            source.close()
        return safety


def audit(db, actor, action, entity="", details=None):
    db.execute(
        "INSERT INTO activity(actor, action, entity, details) VALUES (?, ?, ?, ?)",
        (actor, action, str(entity), json.dumps(details or {}, ensure_ascii=False)),
    )


def adjust_stock(db, product_id, location_id, change, reason, actor, sale_id=None):
    product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        raise HTTPException(404, "Product not found")
    if not db.execute("SELECT 1 FROM locations WHERE id=?", (location_id,)).fetchone():
        raise HTTPException(404, "Stock location not found")
    row = db.execute(
        "SELECT quantity FROM inventory WHERE product_id=? AND location_id=?",
        (product_id, location_id),
    ).fetchone()
    available = row[0] if row else 0
    if available + change < 0:
        raise HTTPException(
            409, f"{product['name']}: only {available} units available at this location"
        )
    if product["stock"] + change > MAX_STOCK:
        raise HTTPException(422, "Total stock cannot exceed 1,000,000 units")
    db.execute(
        "INSERT INTO inventory(product_id,location_id,quantity) VALUES (?,?,?) "
        "ON CONFLICT(product_id,location_id) DO UPDATE SET quantity=quantity+excluded.quantity",
        (product_id, location_id, change),
    ) if change >= 0 else db.execute(
        "UPDATE inventory SET quantity=quantity+? WHERE product_id=? AND location_id=?",
        (change, product_id, location_id),
    )
    db.execute("UPDATE products SET stock=stock+? WHERE id=?", (change, product_id))
    if change:
        db.execute(
            "INSERT INTO stock_movements(product_id,location_id,change,reason,actor,sale_id) VALUES (?,?,?,?,?,?)",
            (product_id, location_id, change, reason, actor, sale_id),
        )


def blended_cost(old_quantity, old_cost, added_quantity, added_cost):
    if old_quantity == 0:
        return added_cost
    if old_cost is None or added_cost is None:
        return None
    total = old_quantity + added_quantity
    return (old_quantity * old_cost + added_quantity * added_cost + total // 2) // total


def utc_now():
    return datetime.now(timezone.utc).isoformat()
