"""Public non-I/O capability receipt regression for the SQLite Ledger."""

from __future__ import annotations

from protoprompt.ledger import SqliteMemoryLedger
from protoprompt.ledger.storage_conformance import sqlite_v7_storage_capabilities


def test_sqlite_capabilities_are_available_without_an_instance_or_database_open():
    """The class-level receipt must not require a path, setup, or connection."""

    capabilities = SqliteMemoryLedger.storage_capabilities()

    assert capabilities == sqlite_v7_storage_capabilities()
    assert capabilities.explain()["backend_id"] == "sqlite_v7"
