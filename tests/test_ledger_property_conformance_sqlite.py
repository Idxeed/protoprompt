"""Property/state-machine conformance gate for the SQLite Ledger backend."""

from __future__ import annotations

from hypothesis import given

from ledger_conformance.property import (
    OPAQUE_TEXT,
    PROPERTY_SETTINGS,
    SCOPE_FIELD,
    assert_scoped_deletion_property,
    run_lifecycle_state_machine,
)
from protoprompt.ledger import SqliteMemoryLedger


@given(scope_seed=OPAQUE_TEXT, differing_field=SCOPE_FIELD, content=OPAQUE_TEXT)
@PROPERTY_SETTINGS
def test_sqlite_scoped_deletion_property(
    scope_seed: str,
    differing_field: str,
    content: str,
) -> None:
    assert_scoped_deletion_property(
        SqliteMemoryLedger,
        scope_seed=scope_seed,
        differing_field=differing_field,
        content=content,
    )


def test_sqlite_lifecycle_state_machine() -> None:
    run_lifecycle_state_machine(SqliteMemoryLedger)
