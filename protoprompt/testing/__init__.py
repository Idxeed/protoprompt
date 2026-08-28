"""Reusable contract checks for third-party protoprompt adapters.

The module has no test-runner dependency. Contract functions are async and
raise :class:`ContractViolation`, so they can be called from pytest, unittest,
or a standalone adapter smoke script.
"""

from protoprompt.testing.contracts import (
    ContractReport,
    ContractViolation,
    check_chat_client,
    check_embedding_client,
    check_profile_store,
    check_secret_store,
    check_vector_store,
)
from protoprompt.testing.long_dialog import (
    LongDialogResult,
    ScenarioEmbeddings,
    run_long_dialog_scenario,
)

__all__ = [
    "ContractReport",
    "ContractViolation",
    "check_chat_client",
    "check_embedding_client",
    "check_vector_store",
    "check_profile_store",
    "check_secret_store",
    "LongDialogResult",
    "ScenarioEmbeddings",
    "run_long_dialog_scenario",
]
