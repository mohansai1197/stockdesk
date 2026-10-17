"""Local demo secrets are kept out of JSON responses, logs, and DB backups."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, value.encode())
    finally:
        os.close(descriptor)


def demo_api_key():
    configured = os.environ.get("STOCKDESK_ACCOUNTING_API_KEY")
    if configured:
        return configured
    path = ROOT / ".data" / "accounting-demo.key"
    if not path.exists():
        private_write(path, secrets.token_urlsafe(32))
    return path.read_text().strip()


def save_connection_key(store, reference, key):
    private_write(store.path.parent / "accounting-secrets" / f"{reference}.key", key)


def connection_key(store, reference):
    if not reference:
        return None
    path = store.path.parent / "accounting-secrets" / f"{reference}.key"
    return path.read_text().strip() if path.is_file() else None
