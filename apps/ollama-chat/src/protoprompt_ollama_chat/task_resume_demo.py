"""Trusted-host bridge for the optional Ollama chat task-resume demo.

This module is deliberately not connected to FastAPI, the browser, document
ingestion, chat transcripts, or model tools.  An embedding application may
hold one :class:`TaskResumeDemoHost` privately and invoke it only from trusted
host code.  It keeps the Ledger in a database separate from the application
state mapping, and it never exposes a writer, review gate, binding, checkpoint
identifier, task identifier, descriptor, source reference, or secret to a
caller-facing receipt.

The bridge is intentionally narrow:

* ``seed`` accepts a host-authored episode and creates exactly one admitted
  ``host_assertion`` record for an existing conversation;
* ``compose_active`` restores only an already active signed mapping and uses
  the model-safe ``TaskResumePlanner.compose_checkpoint`` projection; and
* ``close`` removes a mapping from the resume path before it asks the Ledger
  to forget the one host-minted source.

Nothing in this module promotes PDF text, chat turns, or model output into a
Ledger record.  Hosts that want a different admission workflow must implement
one explicitly outside this demonstration bridge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
import threading
from typing import Callable, TypeAlias

from protoprompt import ContextInput
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
    TaskEpisode,
    TaskOutcome,
    TaskResumePlanner,
    TaskResumeReferenceRequest,
    task_resume_scope,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy
from protoprompt.ledger.types import validate_identifier
from protoprompt.scope import MemoryScope

from protoprompt_ollama_chat.task_resume_state import (
    TASK_RESUME_BINDING_CONTRACT_ID,
    TaskResumeBinding,
    TaskResumeStateRepository,
)


TASK_RESUME_DEMO_SCHEMA_VERSION = 1
"""The versioned, host-only demonstration bridge contract."""

_HOST_ACTOR = "ollama-chat-task-resume-host"
_ADMISSION_POLICY_ID = "ollama-chat-task-resume-demo-episode-v1"
_CLOSE_REASON_CODE = "task_resume_demo_closed"
_SEED_ROLLBACK_REASON_CODE = "task_resume_demo_seed_failed"
_DEFAULT_CHECKPOINT_TOKEN_BUDGET = 1_024
_DEFAULT_CHECKPOINT_BYTE_BUDGET = 32_768
_MAX_TASK_DESCRIPTOR_CHARS = 16_000
_SEED_VALIDATION_TASK_REF = "task-resume-demo-seed-validation"


class TaskResumeDemoError(RuntimeError):
    """Base error for the trusted-host task-resume demonstration bridge."""


class TaskResumeDemoConfigurationError(TaskResumeDemoError):
    """Raised when a host factory or storage configuration is unsafe."""


TaskResumeBuilderFactory: TypeAlias = Callable[[MemoryScope], TokenBudgetedContextBuilder]
"""Trusted callback that returns one builder pinned to the supplied scope."""


@dataclass(frozen=True, slots=True)
class TaskResumeDemoSeed:
    """Host-authored data for one non-executable task episode.

    This type deliberately has no PDF, chat-transcript, model-response, or
    browser-derived fields.  ``goal``, ``next_action`` and ``lesson`` become
    provider-safe reference data only after the strict Ledger admission and
    the one-way ``TaskEpisode`` projection.
    """

    conversation_id: str
    task_descriptor: str
    goal: str
    completed_action_refs: tuple[str, ...]
    outcome: TaskOutcome | str
    next_action: str | None = None
    lesson: str | None = None

    def __post_init__(self) -> None:
        """Normalize every host seed before it can reach a Ledger writer.

        The CLI parser is intentionally strict too, but this dataclass is a
        public host-side construction point.  Validating here keeps an
        embedding host from accidentally turning malformed JSON-like values
        into a partially-created demo record.  Constructing a ``TaskEpisode``
        is validation only: it neither writes to the Ledger nor exposes the
        sentinel task reference to the provider.
        """

        conversation_id = validate_identifier(
            self.conversation_id,
            field="conversation_id",
        )
        if not isinstance(self.task_descriptor, str):
            raise TypeError("task_descriptor must be a string")
        task_descriptor = self.task_descriptor.strip()
        if not task_descriptor:
            raise ValueError("task_descriptor must not be empty")
        if len(task_descriptor) > _MAX_TASK_DESCRIPTOR_CHARS:
            raise ValueError(
                "task_descriptor must be at most "
                f"{_MAX_TASK_DESCRIPTOR_CHARS} characters"
            )
        episode = TaskEpisode(
            task_ref=_SEED_VALIDATION_TASK_REF,
            goal=self.goal,
            completed_action_refs=self.completed_action_refs,
            outcome=self.outcome,
            next_action=self.next_action,
            lesson=self.lesson,
        )
        object.__setattr__(self, "conversation_id", conversation_id)
        object.__setattr__(self, "task_descriptor", task_descriptor)
        object.__setattr__(self, "goal", episode.goal)
        object.__setattr__(self, "completed_action_refs", episode.completed_action_refs)
        object.__setattr__(self, "outcome", episode.outcome)
        object.__setattr__(self, "next_action", episode.next_action)
        object.__setattr__(self, "lesson", episode.lesson)


@dataclass(frozen=True, slots=True)
class TaskResumeDemoStatus:
    """Content-free status for a host that already knows a conversation id."""

    schema_version: int
    contract_id: str
    active: bool

    def explain(self) -> dict[str, object]:
        """Return a serializable status with no mapping or payload values."""

        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class TaskResumeDemoReceipt:
    """Content-free result of a successful trusted-host lifecycle action."""

    schema_version: int
    contract_id: str
    operation: str
    active: bool
    selected_episode_count: int

    def explain(self) -> dict[str, object]:
        """Return the intentionally identifier- and content-free receipt."""

        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "operation": self.operation,
            "active": self.active,
            "selected_episode_count": self.selected_episode_count,
        }


def _strict_episode_admission_policy() -> MemoryAdmissionPolicy:
    """Return the sole admission policy accepted by this demo bridge."""

    return MemoryAdmissionPolicy(
        policy_id=_ADMISSION_POLICY_ID,
        policy_version="1",
        allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
        allowed_kinds=(MemoryKind.EPISODE,),
        minimum_confidence=0.75,
    )


def _nonnegative_budget(value: object, *, field: str) -> int:
    """Reject a malformed seed budget before it can create a Ledger record."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


class TaskResumeDemoHost:
    """Own a local Ledger and signed state binding for one trusted host.

    ``checkpoint_secret`` is supplied by the embedding host on every process
    start.  The raw value stays only in this object and ephemeral recall
    planners; neither the app-state repository nor the Ledger serializes it.
    ``state_path`` and ``ledger_path`` must be distinct SQLite files so an
    application mapping cannot be mistaken for Ledger operational data.
    """

    def __init__(
        self,
        *,
        state_path: Path | str,
        ledger_path: Path | str,
        checkpoint_secret: bytes,
    ) -> None:
        self._state_path = Path(state_path)
        self._ledger_path = Path(ledger_path)
        if self._state_path.resolve(strict=False) == self._ledger_path.resolve(
            strict=False
        ):
            raise TaskResumeDemoConfigurationError(
                "state_path and ledger_path must be separate files"
            )
        if not isinstance(checkpoint_secret, bytes):
            raise TypeError("checkpoint_secret must be bytes")

        # Both owned components independently enforce the durable checkpoint
        # secret size bounds.  Retain a private byte copy only for ephemeral
        # recall planners created after a restart.
        self._checkpoint_secret = bytes(checkpoint_secret)
        self._state = TaskResumeStateRepository(
            self._state_path,
            checkpoint_secret=self._checkpoint_secret,
        )
        self._ledger = SqliteMemoryLedger(str(self._ledger_path))
        self._closed = False
        self._lock = threading.RLock()
        try:
            self._ledger.setup()
        except BaseException:
            self._ledger.close()
            self._state.close()
            raise

    def close(self) -> None:
        """Close host-owned SQLite resources without exposing their contents."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._ledger.close()
            finally:
                self._state.close()

    def __enter__(self) -> "TaskResumeDemoHost":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def explain(self) -> dict[str, object]:
        """Return the public contract shape without paths, IDs, or secrets."""

        return {
            "schema_version": TASK_RESUME_DEMO_SCHEMA_VERSION,
            "contract_id": TASK_RESUME_BINDING_CONTRACT_ID,
            "host_only": True,
            "browser_api": False,
            "auto_admission": False,
            "record_kinds": [MemoryKind.EPISODE.value],
            "record_origins": [MemoryOrigin.HOST_ASSERTION.value],
            "provider_projection": "task-episode-reference-v1",
        }

    def status(self, conversation_id: str) -> TaskResumeDemoStatus:
        """Report whether an authenticated mapping is currently resumable.

        A closing mapping intentionally looks inactive.  This prevents callers
        from treating deletion-in-progress as a recoverable active task.
        """

        self._ensure_open()
        normalized_conversation = validate_identifier(
            conversation_id,
            field="conversation_id",
        )
        with self._lock:
            binding = self._state.load_active(normalized_conversation)
        return TaskResumeDemoStatus(
            schema_version=TASK_RESUME_DEMO_SCHEMA_VERSION,
            contract_id=TASK_RESUME_BINDING_CONTRACT_ID,
            active=binding is not None,
        )

    def seed(
        self,
        seed: TaskResumeDemoSeed,
        *,
        builder_factory: TaskResumeBuilderFactory,
        checkpoint_token_budget: int = _DEFAULT_CHECKPOINT_TOKEN_BUDGET,
        checkpoint_byte_budget: int = _DEFAULT_CHECKPOINT_BYTE_BUDGET,
    ) -> TaskResumeDemoReceipt:
        """Admit and bind exactly one host-authored episode for a conversation.

        The state binding is written only after strict review/confirmation and
        durable checkpoint sealing both succeed.  The caller receives only a
        content-free receipt; task and checkpoint identifiers remain inside
        the host's signed state and Ledger files.
        """

        self._ensure_open()
        if not isinstance(seed, TaskResumeDemoSeed):
            raise TypeError("seed must be a TaskResumeDemoSeed")
        if not callable(builder_factory):
            raise TypeError("builder_factory must be callable")

        conversation_id = validate_identifier(
            seed.conversation_id,
            field="conversation_id",
        )
        token_budget = _nonnegative_budget(
            checkpoint_token_budget,
            field="checkpoint_token_budget",
        )
        byte_budget = _nonnegative_budget(
            checkpoint_byte_budget,
            field="checkpoint_byte_budget",
        )
        with self._lock:
            # Do an inexpensive preflight for the common active case.  The
            # repository still enforces uniqueness at the durable write edge.
            if self._state.load_active(conversation_id) is not None:
                raise TaskResumeDemoError(
                    "an active task-resume demonstration already exists"
                )

            task_ref = self._opaque_identifier("task")
            checkpoint_id = self._opaque_identifier("checkpoint")
            parent_scope = self._state.parent_scope_for(conversation_id)
            scope = task_resume_scope(parent_scope, task_ref=task_ref)
            episode = TaskEpisode(
                task_ref=task_ref,
                goal=seed.goal,
                completed_action_refs=seed.completed_action_refs,
                outcome=seed.outcome,
                next_action=seed.next_action,
                lesson=seed.lesson,
            )
            writer = self._writer(scope)
            # Validate the factory and frozen descriptor before the first
            # admission write.  A malformed host seed must not leave a record
            # merely because its later checkpoint could not be constructed.
            adapter = self._adapter(
                parent_scope=parent_scope,
                task_ref=task_ref,
                task_descriptor=seed.task_descriptor,
                builder_factory=builder_factory,
            )
            self._admit_one_episode(writer, task_ref=task_ref, episode=episode)
            try:
                checkpoint = adapter.seal_checkpoint(
                    checkpoint_id=checkpoint_id,
                    token_budget=token_budget,
                    byte_budget=byte_budget,
                )
                if checkpoint.selected_count != 1:
                    # The derived scope is fresh, so a non-unit selection
                    # indicates an unexpected host/storage condition.  Do not
                    # bind it.
                    raise TaskResumeDemoError(
                        "demo seed did not produce exactly one selected episode"
                    )
                self._state.create(
                    conversation_id=conversation_id,
                    task_ref=task_ref,
                    task_descriptor=seed.task_descriptor,
                    checkpoint_id=checkpoint_id,
                )
            except BaseException:
                # The record is intentionally unreachable without the signed
                # mapping, but remove it anyway so a failed seed cannot leave
                # an active orphan in the separate Ledger.  If cleanup itself
                # fails, its error is surfaced instead of pretending that the
                # rollback completed.
                try:
                    writer.forget_by_source(
                        self._source_ref(task_ref),
                        reason_code=_SEED_ROLLBACK_REASON_CODE,
                    )
                except BaseException as cleanup_error:
                    raise TaskResumeDemoError(
                        "failed to roll back an unbound task-resume episode"
                    ) from cleanup_error
                raise
        return TaskResumeDemoReceipt(
            schema_version=TASK_RESUME_DEMO_SCHEMA_VERSION,
            contract_id=TASK_RESUME_BINDING_CONTRACT_ID,
            operation="seeded",
            active=True,
            selected_episode_count=1,
        )

    async def compose_active(
        self,
        conversation_id: str,
        *,
        inp: ContextInput,
        builder_factory: TaskResumeBuilderFactory,
        history: list[dict[str, object]] | None = None,
        user_message: str | None = None,
        final_messages: list[dict[str, object]] | None = None,
        output_reserve: int | None = None,
    ) -> TaskResumeReferenceRequest | None:
        """Compose only an active binding into model-safe provider messages.

        ``inp`` is passed through unchanged: its query remains the current
        request/RAG query rather than being replaced by the frozen host task
        descriptor.  ``None`` means no active binding (including a closing
        binding), so callers can retain their ordinary non-resume path.
        """

        self._ensure_open()
        if not isinstance(inp, ContextInput):
            raise TypeError("inp must be a ContextInput")
        if not callable(builder_factory):
            raise TypeError("builder_factory must be callable")
        conversation = validate_identifier(conversation_id, field="conversation_id")
        with self._lock:
            binding = self._state.load_active(conversation)
            if binding is None:
                return None
            adapter = self._adapter_for_binding(binding, builder_factory=builder_factory)
        # Do not retain a thread lock while the builder awaits embedding or
        # retrieval.  A close that races this composition flips the signed
        # binding out of ``active``; the post-await check below then prevents
        # the already-planned messages from reaching a provider.
        # TaskResumePlanner snapshots and validates the input before it
        # awaits; do not alter query, document filters, or live RAG data.
        request = await adapter.compose_checkpoint(
            checkpoint_id=binding.checkpoint_id,
            inp=inp,
            history=history,
            user_message=user_message,
            final_messages=final_messages,
            output_reserve=output_reserve,
        )
        with self._lock:
            self._ensure_open()
            current = self._state.load_active(conversation)
            if current != binding:
                raise TaskResumeDemoError(
                    "task-resume binding changed during provider composition"
                )
        return request

    def close_binding(self, conversation_id: str) -> TaskResumeDemoReceipt:
        """Fail closed, forget the host source, then remove its state binding.

        ``begin_close`` happens before Ledger cleanup.  If the cleanup or
        finishing write fails, the durable mapping remains closing and can no
        longer be composed; a trusted host may retry this method after restart.
        """

        self._ensure_open()
        conversation = validate_identifier(conversation_id, field="conversation_id")
        with self._lock:
            binding = self._state.begin_close(conversation)
            if binding is None:
                return TaskResumeDemoReceipt(
                    schema_version=TASK_RESUME_DEMO_SCHEMA_VERSION,
                    contract_id=TASK_RESUME_BINDING_CONTRACT_ID,
                    operation="not_active",
                    active=False,
                    selected_episode_count=0,
                )

            writer = self._writer(task_resume_scope(
                binding.parent_scope,
                task_ref=binding.task_ref,
            ))
            # This call is intentionally before finish_close.  A failure leaves
            # the authenticated row in the closing state and therefore blocks
            # all future resume composition until cleanup is retried.
            writer.forget_by_source(
                self._source_ref(binding.task_ref),
                reason_code=_CLOSE_REASON_CODE,
            )
            self._state.finish_close(
                conversation,
                expected_generation=binding.generation,
            )
        return TaskResumeDemoReceipt(
            schema_version=TASK_RESUME_DEMO_SCHEMA_VERSION,
            contract_id=TASK_RESUME_BINDING_CONTRACT_ID,
            operation="closed",
            active=False,
            selected_episode_count=0,
        )

    def _adapter_for_binding(
        self,
        binding: TaskResumeBinding,
        *,
        builder_factory: TaskResumeBuilderFactory,
    ) -> TaskResumePlanner:
        return self._adapter(
            parent_scope=binding.parent_scope,
            task_ref=binding.task_ref,
            task_descriptor=binding.task_descriptor,
            builder_factory=builder_factory,
        )

    def _adapter(
        self,
        *,
        parent_scope: MemoryScope,
        task_ref: str,
        task_descriptor: str,
        builder_factory: TaskResumeBuilderFactory,
    ) -> TaskResumePlanner:
        scope = task_resume_scope(parent_scope, task_ref=task_ref)
        builder = builder_factory(scope)
        if not isinstance(builder, TokenBudgetedContextBuilder):
            raise TaskResumeDemoConfigurationError(
                "builder_factory must return a TokenBudgetedContextBuilder"
            )
        writer = self._writer(scope)
        recall = LedgerRecallPlanner(
            writer,
            policy=LedgerRecallPolicy.task_resume_safe_default(),
            # Selection/checkpoint accounting and the final provider request
            # must use one exact counter instance. The trusted factory owns
            # that counter as part of its pinned builder configuration.
            counter=builder.counter,
            checkpoint_secret=self._checkpoint_secret,
        )
        return TaskResumePlanner(
            builder,
            recall,
            parent_scope=parent_scope,
            task_ref=task_ref,
            task_descriptor=task_descriptor,
        )

    def _writer(self, scope: MemoryScope) -> MemoryWriter:
        return MemoryWriter(self._ledger, scope=scope, actor=_HOST_ACTOR)

    def _admit_one_episode(
        self,
        writer: MemoryWriter,
        *,
        task_ref: str,
        episode: TaskEpisode,
    ) -> None:
        """Use exactly one asserted ingress/review/confirmation sequence."""

        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.HOST_ASSERTION,
            policy=_strict_episode_admission_policy(),
        )
        candidate = gate.ingress(
            kind=MemoryKind.EPISODE,
            source_ref=self._source_ref(task_ref),
            confidence=0.9,
            asserted=True,
        ).submit(episode.to_json())
        gate.confirm(
            gate.review(candidate.record_id),
            event_id=self._opaque_identifier("admission"),
        )

    @staticmethod
    def _opaque_identifier(prefix: str) -> str:
        """Mint one validation-safe random identifier without task text."""

        return f"{prefix}:{secrets.token_urlsafe(24)}"

    @staticmethod
    def _source_ref(task_ref: str) -> str:
        """Derive the sole internal source reference needed for cleanup."""

        return f"task-resume-demo-source:{task_ref}"

    def _ensure_open(self) -> None:
        if self._closed:
            raise TaskResumeDemoError("task-resume demonstration host is closed")


__all__ = [
    "TASK_RESUME_DEMO_SCHEMA_VERSION",
    "TaskResumeBuilderFactory",
    "TaskResumeDemoConfigurationError",
    "TaskResumeDemoError",
    "TaskResumeDemoHost",
    "TaskResumeDemoReceipt",
    "TaskResumeDemoSeed",
    "TaskResumeDemoStatus",
]
