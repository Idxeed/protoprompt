"""Executable behavioural contracts for protoprompt adapter authors."""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Any, Callable
import uuid

from protoprompt.profile.types import UserProfile
from protoprompt.store.protocol import await_if_needed


class ContractViolation(AssertionError):
    """Raised when an adapter violates an observable protocol guarantee."""


@dataclass(frozen=True, slots=True)
class ContractReport:
    """Successful checks performed for one adapter contract."""

    contract: str
    checks: tuple[str, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractViolation(message)


async def _call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await await_if_needed(method(*args, **kwargs))


async def check_chat_client(
    client: Any,
    *,
    model: str = "contract-chat",
    messages: list[dict] | None = None,
) -> ContractReport:
    """Verify the result shape of a chat-capable client.

    The supplied client is expected to be an offline fake, a recorded
    transport, or an explicitly authorized live test client.
    """
    probe = messages or [{"role": "user", "content": "contract probe"}]
    reply = await _call(client.chat, probe, model=model)
    _require(isinstance(reply, str), "chat() must return str")
    return ContractReport("chat_client", ("awaitable", "string_result"))


async def check_embedding_client(
    client: Any,
    *,
    model: str = "contract-embedding",
    texts: list[str] | None = None,
) -> ContractReport:
    """Verify cardinality, dimensions, and finite numeric embeddings."""
    probe = texts or ["first contract probe", "second contract probe"]
    vectors = await _call(client.embed, probe, model=model)
    _require(isinstance(vectors, list), "embed() must return list[list[float]]")
    _require(
        len(vectors) == len(probe),
        "embed() must preserve input cardinality and ordering",
    )
    dimensions: set[int] = set()
    for index, vector in enumerate(vectors):
        _require(isinstance(vector, list), f"embedding {index} must be a list")
        _require(bool(vector), f"embedding {index} must not be empty")
        dimensions.add(len(vector))
        for value in vector:
            _require(
                isinstance(value, Real)
                and not isinstance(value, bool)
                and math.isfinite(float(value)),
                f"embedding {index} contains a non-finite numeric value",
            )
    _require(len(dimensions) == 1, "all embeddings must have the same dimension")
    return ContractReport(
        "embedding_client",
        ("awaitable", "cardinality", "stable_dimension", "finite_values"),
    )


async def check_vector_store(
    store: Any,
    *,
    embedding_a: list[float] | None = None,
    embedding_b: list[float] | None = None,
    id_prefix: str | None = None,
) -> ContractReport:
    """Verify add/query/filter/threshold/delete semantics on an isolated key.

    The check writes two temporary documents and removes them in ``finally``.
    Pass vectors matching the configured dimension of an existing backend.
    """
    vector_a = embedding_a or [1.0, 0.0]
    vector_b = embedding_b or [0.0, 1.0]
    _require(len(vector_a) == len(vector_b), "contract vectors must have equal size")
    prefix = id_prefix or f"contract-{uuid.uuid4().hex}"
    doc_a, doc_b = f"{prefix}-a", f"{prefix}-b"
    try:
        await _call(
            store.add,
            doc_a,
            ["contract alpha"],
            [vector_a],
            {"contract_scope": "alpha", "contract_group": "shared"},
        )
        await _call(
            store.add,
            doc_b,
            ["contract beta"],
            [vector_b],
            {"contract_scope": "beta", "contract_group": "shared"},
        )

        exact = await _call(
            store.query,
            vector_a,
            top_k=10,
            where={"contract_scope": "alpha"},
        )
        _require(len(exact) == 1, "equality metadata filter must isolate one hit")
        _check_vector_hit(exact[0], expected_document="contract alpha")
        _require(
            exact[0]["metadata"].get("doc_id") == doc_a,
            "store metadata must expose the source doc_id",
        )

        grouped = await _call(
            store.query,
            vector_a,
            top_k=10,
            where={"contract_scope": {"$in": ["alpha", "beta"]}},
        )
        _require(
            {hit.get("document") for hit in grouped}
            == {"contract alpha", "contract beta"},
            "$in metadata filter must include every matching value",
        )

        thresholded = await _call(
            store.query,
            vector_a,
            top_k=10,
            where={"contract_group": "shared"},
            score_threshold=0.9,
        )
        _require(
            [hit.get("document") for hit in thresholded] == ["contract alpha"],
            "score_threshold must exclude lower-similarity hits",
        )

        await _call(store.delete, doc_a)
        deleted = await _call(
            store.query,
            vector_a,
            top_k=10,
            where={"doc_id": doc_a},
        )
        _require(not deleted, "delete(doc_id) must remove all of that document's chunks")
    finally:
        await _call(store.delete, doc_a)
        await _call(store.delete, doc_b)

    return ContractReport(
        "vector_store",
        ("add", "hit_shape", "equality_filter", "in_filter", "threshold", "delete"),
    )


def _check_vector_hit(hit: Any, *, expected_document: str) -> None:
    _require(isinstance(hit, dict), "query() hits must be dictionaries")
    _require(hit.get("document") == expected_document, "query() changed document text")
    _require(isinstance(hit.get("metadata"), dict), "query() hit needs metadata")
    score = hit.get("score")
    if score is None and hit.get("distance") is not None:
        score = 1.0 - float(hit["distance"])
    _require(
        isinstance(score, Real)
        and not isinstance(score, bool)
        and math.isfinite(float(score)),
        "query() hit needs a finite numeric similarity score",
    )


async def check_profile_store(
    store: Any,
    *,
    user_id: str | None = None,
) -> ContractReport:
    """Verify profile roundtrip, optimistic locking, and deletion."""
    identity = user_id or f"contract-{uuid.uuid4().hex}"
    initial = UserProfile(user_id=identity, version=1, summary="contract profile")
    updated = UserProfile(user_id=identity, version=2, summary="updated profile")
    try:
        await _call(store.delete, identity)
        created = await _call(
            store.compare_and_put,
            initial,
            expected_version=None,
        )
        _require(created is True, "compare_and_put(None) must create a missing profile")
        _require(await _call(store.get, identity) == initial, "profile roundtrip failed")
        stale = await _call(store.compare_and_put, updated, expected_version=0)
        _require(stale is False, "compare_and_put must reject a stale version")
        accepted = await _call(store.compare_and_put, updated, expected_version=1)
        _require(accepted is True, "compare_and_put must accept the current version")
        _require(await _call(store.get, identity) == updated, "profile update failed")
        await _call(store.delete, identity)
        _require(await _call(store.get, identity) is None, "profile delete failed")
    finally:
        await _call(store.delete, identity)
    return ContractReport(
        "profile_store",
        ("create", "roundtrip", "stale_rejection", "compare_and_put", "delete"),
    )


async def check_secret_store(
    store: Any,
    *,
    key: str | None = None,
) -> ContractReport:
    """Verify secret roundtrip, overwrite, scope isolation, listing, deletion."""
    name = key or f"contract-{uuid.uuid4().hex}"
    scope_a = f"{name}:scope-a"
    scope_b = f"{name}:scope-b"
    try:
        await _call(store.delete, name, scope=scope_a)
        await _call(store.delete, name, scope=scope_b)
        await _call(store.put, name, "first", scope=scope_a)
        _require(await _call(store.get, name, scope=scope_a) == "first", "secret roundtrip failed")
        _require(await _call(store.get, name, scope=scope_b) is None, "secret scope isolation failed")
        await _call(store.put, name, "second", scope=scope_a)
        _require(await _call(store.get, name, scope=scope_a) == "second", "secret overwrite failed")
        keys = await _call(store.list_keys, scope=scope_a)
        _require(name in keys, "list_keys() omitted a stored secret")
        await _call(store.delete, name, scope=scope_a)
        _require(await _call(store.get, name, scope=scope_a) is None, "secret delete failed")
    finally:
        await _call(store.delete, name, scope=scope_a)
        await _call(store.delete, name, scope=scope_b)
    return ContractReport(
        "secret_store",
        ("roundtrip", "overwrite", "scope_isolation", "list_keys", "delete"),
    )
