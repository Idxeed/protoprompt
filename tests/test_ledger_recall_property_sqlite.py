"""Bounded property gate for strict SQLite Ledger recall."""

from __future__ import annotations

from hypothesis import given

from ledger_conformance.property import (
    PROPERTY_SETTINGS,
    RECALL_BYTE_SLACK,
    RECALL_PAYLOADS,
    RECALL_TOKEN_SLACK,
    assert_recall_budget_packing_property,
)
from protoprompt.ledger import SqliteMemoryLedger


@given(
    payloads=RECALL_PAYLOADS,
    token_slack=RECALL_TOKEN_SLACK,
    byte_slack=RECALL_BYTE_SLACK,
)
@PROPERTY_SETTINGS
def test_sqlite_recall_budget_packing_property(
    payloads: list[str],
    token_slack: int,
    byte_slack: int,
) -> None:
    assert_recall_budget_packing_property(
        SqliteMemoryLedger,
        payloads=payloads,
        token_slack=token_slack,
        byte_slack=byte_slack,
    )
