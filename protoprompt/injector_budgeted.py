from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from time import perf_counter

from protoprompt.context import ContextInput, ContextOutput
from protoprompt.exceptions import TokenBudgetExceededError
from protoprompt.events import ContextEvent, EventDispatcher, EventSink, RetrieveEvent, dispatch, elapsed_ms, new_trace_id, scope_id
from protoprompt.hooks import ContextHooks, fire
from protoprompt.i18n import section_header
from protoprompt.injector import ContextBuilder
from protoprompt.llm import EmbeddingClientProtocol
from protoprompt.profile.render import render
from protoprompt.rag.retriever import Retriever
from protoprompt.rag.types import RetrievedChunk
from protoprompt.scope import MemoryScope
from protoprompt.store.protocol import StoreProtocol, await_if_needed
from protoprompt.tokens.protocol import TokenCounter
from protoprompt.tokens.regex_counter import RegexTokenCounter

logger = logging.getLogger(__name__)

Priority = str
SEGMENT_RAG = "rag"
SEGMENT_SESSION = "session"
SEGMENT_PROFILE = "profile"
SEGMENT_SYSTEM = "system"

DEFAULT_PRIORITIES: tuple[Priority, ...] = (
    SEGMENT_SYSTEM,
    SEGMENT_PROFILE,
    SEGMENT_SESSION,
    SEGMENT_RAG,
)


@dataclass
class BudgetReport:
    """Observability for a single context build.

    After :meth:`build`, ``used_tokens`` is the assembled ``system_prompt``.
    After :meth:`TokenBudgetedContextBuilder.build_messages`, it is the final
    message-list cost including provider framing. ``dropped_blocks`` lists
    block identifiers that did not fit; UI may surface this to the user.
    """

    used_tokens: int = 0
    budget: int = 0
    remaining_tokens: int = 0
    dropped_blocks: list[str] = field(default_factory=list)
    section_tokens: dict[str, int] = field(default_factory=dict)
    history_kept: int = 0
    history_tokens: int = 0
    # ``build_messages`` reserves this amount before allocating any input
    # message.  It is zero for the legacy/default configuration.
    output_reserve_tokens: int = 0
    # Populated by ``build_messages``.  Unlike ``used_tokens`` after a plain
    # ``build()``, this includes provider message overhead for the final
    # caller-supplied turn(s).
    user_message_tokens: int = 0


@dataclass
class _Candidate:
    section: Priority
    text: str
    label: str
    chunk: RetrievedChunk | None = None


_RESPONSE_CALL_OUTPUT_TYPES = {
    "program": "program_output",
    "function_call": "function_call_output",
    "custom_tool_call": "custom_tool_call_output",
    "shell_call": "shell_call_output",
    "apply_patch_call": "apply_patch_call_output",
    "computer_call": "computer_call_output",
    "local_shell_call": "local_shell_call_output",
    "mcp_approval_request": "mcp_approval_response",
    "tool_search_call": "tool_search_output",
}
_RESPONSE_OUTPUT_TYPES = set(_RESPONSE_CALL_OUTPUT_TYPES.values())
_RESPONSE_STREAMED_OUTPUT_TYPES = frozenset({
    "shell_call_output",
    "tool_search_output",
})
# Most Responses tool pairs use ``call_id`` on both sides. Hosted MCP
# approvals are deliberately different: the request is identified by ``id``
# and its response refers to it as ``approval_request_id``. Keep that schema
# detail here so every protocol boundary uses the same identity rules.
_RESPONSE_CALL_ID_FIELDS = {"mcp_approval_request": "id"}
_RESPONSE_OUTPUT_ID_FIELDS = {
    "local_shell_call_output": "id",
    "mcp_approval_response": "approval_request_id",
}
_RESPONSE_ACTIVE_PROGRAM_CHILD_TYPES = frozenset({
    "hosted_tool_call",
    "file_search_call",
    "web_search_call",
    "code_interpreter_call",
    "image_generation_call",
    "mcp_list_tools",
    "mcp_call",
    "mcp_approval_request",
    "mcp_approval_response",
})
_RESPONSE_STANDALONE_ITEM_TYPES = (
    _RESPONSE_ACTIVE_PROGRAM_CHILD_TYPES
    | frozenset({
        "message",
        "additional_tools",
        "compaction",
    })
)
_RESPONSE_INPUT_ONLY_ITEM_TYPES = frozenset({
    "compaction_trigger",
    "item_reference",
})


def _response_call_type(item: Mapping[str, object]) -> str | None:
    """Return a known Responses call type, including hosted MCP wrappers."""
    item_type = item.get("type")
    if isinstance(item_type, str) and item_type in _RESPONSE_CALL_OUTPUT_TYPES:
        return item_type
    provider_data = item.get("provider_data")
    if (
        item_type == "hosted_tool_call"
        and isinstance(provider_data, Mapping)
        and provider_data.get("type") == "mcp_approval_request"
    ):
        return "mcp_approval_request"
    return None


def _response_output_type(item: Mapping[str, object]) -> str | None:
    """Return a known Responses output type, if the item is one."""
    item_type = item.get("type")
    return item_type if isinstance(item_type, str) and item_type in _RESPONSE_OUTPUT_TYPES else None


def _response_call_pair(item: dict) -> tuple[str, str] | None:
    """Return the canonical output-type/ID pair declared by a Responses call."""
    call_type = _response_call_type(item)
    if call_type is None:
        return None
    output_type = _RESPONSE_CALL_OUTPUT_TYPES[call_type]
    provider_data = item.get("provider_data")
    if (
        item.get("type") == "hosted_tool_call"
        and call_type == "mcp_approval_request"
        and isinstance(provider_data, Mapping)
    ):
        # The SDK can wrap a hosted approval in ``hosted_tool_call``. Match
        # its own identity precedence: nested ``id`` first, then wrapper IDs.
        call_id = provider_data.get("id")
        if call_id is None:
            call_id = item.get("call_id") or item.get("id")
    else:
        call_id = item.get(_RESPONSE_CALL_ID_FIELDS.get(call_type, "call_id"))
    if not isinstance(call_id, str) or not call_id:
        return None
    return output_type, call_id


def _response_output_pair(item: dict) -> tuple[str, str] | None:
    """Return the canonical output-type/ID pair completed by a Responses item."""
    output_type = _response_output_type(item)
    if output_type is None:
        return None
    call_id = item.get(_RESPONSE_OUTPUT_ID_FIELDS.get(output_type, "call_id"))
    if call_id is None and output_type == "local_shell_call_output":
        # The public Responses schema uses ``id``; the Agents executor also
        # emits an internal ``call_id`` form, so accept both without changing
        # the canonical input representation.
        call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    return output_type, call_id


def _program_caller_id(item: Mapping[str, object]) -> str | None:
    """Return the owning program call id for a program-issued Responses item."""
    caller = item.get("caller")
    if not isinstance(caller, Mapping) or caller.get("type") != "program":
        return None
    caller_id = caller.get("caller_id")
    return caller_id if isinstance(caller_id, str) and caller_id else None


def _is_opaque_response_item(item: Mapping[str, object]) -> bool:
    """Return whether an SDK-hosted Responses output is self-contained here.

    ``message`` is ambiguous in the Responses input schema: a user, system,
    or developer message is input, whereas an assistant message with an item
    id is a replayable model output.  Treating every ``type=message`` item as
    a model follower can retain an encrypted reasoning item immediately before
    a user message, which the Responses API rejects.  Likewise,
    ``compaction_trigger`` and ``item_reference`` are input-only controls and
    must never be admitted as optional replay history.
    """
    item_type = item.get("type")
    if (
        not isinstance(item_type, str)
        or item_type not in _RESPONSE_STANDALONE_ITEM_TYPES
    ):
        return False
    if _response_call_type(item) is not None or _response_output_type(item) is not None:
        return False
    if item_type == "message":
        return (
            item.get("role") == "assistant"
            and isinstance(item.get("id"), str)
            and bool(item.get("id"))
            and item.get("status") in {"in_progress", "completed", "incomplete"}
        )
    if item_type == "compaction":
        return (
            isinstance(item.get("id"), str)
            and bool(item.get("id"))
            and isinstance(item.get("encrypted_content"), str)
        )
    if item_type == "hosted_tool_call":
        provider_data = item.get("provider_data")
        return (
            isinstance(item.get("id"), str)
            and bool(item.get("id"))
        ) or (
            isinstance(provider_data, Mapping)
            and isinstance(provider_data.get("id"), str)
            and bool(provider_data.get("id"))
        )
    return isinstance(item.get("id"), str) and bool(item.get("id"))


def _is_model_emitted_response_item(item: Mapping[str, object]) -> bool:
    """Return whether ``item`` can legally follow an encrypted reasoning item."""
    return _response_call_type(item) is not None or _is_opaque_response_item(item)


def _is_response_protocol_item(item: Mapping[str, object]) -> bool:
    """Return whether an item belongs to a contiguous Responses replay run."""
    return (
        item.get("type") == "reasoning"
        or _response_call_type(item) is not None
        or _response_output_type(item) is not None
        or _is_opaque_response_item(item)
        or _program_caller_id(item) is not None
    )


def _is_response_input_only_item(item: Mapping[str, object]) -> bool:
    """Return whether an item is a Responses control unsafe in optional history."""
    return item.get("type") in _RESPONSE_INPUT_ONLY_ITEM_TYPES


def _is_active_program_child(item: Mapping[str, object], program_id: str) -> bool:
    """Return whether an owned item keeps an unfinished program resumable."""
    if _program_caller_id(item) != program_id:
        return False
    item_type = item.get("type")
    if item_type in _RESPONSE_ACTIVE_PROGRAM_CHILD_TYPES:
        return True
    if _response_output_type(item) not in {None, "program_output"}:
        return True
    return item_type == "shell_call" and item.get("status") in {None, "in_progress"}


def _uncompleted_program_call_is_valid(item: Mapping[str, object]) -> bool:
    """Return whether an owned call may remain active without its output."""
    call_type = _response_call_type(item)
    return call_type == "mcp_approval_request" or (
        call_type == "shell_call" and item.get("status") in {None, "in_progress"}
    )


def _is_anonymous_tool_search_call(item: Mapping[str, object]) -> bool:
    """Return whether ``item`` is an SDK-valid tool search without a call ID.

    The Agents SDK treats a server-side tool search call and a later output as
    an ordered pair when both omit ``call_id``.  Replay items may omit the
    optional ``execution`` field, which carries the same server-side meaning;
    an explicitly client-executed search must carry an ID.
    """
    return (
        _response_call_type(item) == "tool_search_call"
        and not isinstance(item.get("call_id"), str)
        and item.get("execution") != "client"
    )


def _is_anonymous_tool_search_output(item: Mapping[str, object]) -> bool:
    """Return whether ``item`` is an SDK-valid anonymous tool search output."""
    return (
        _response_output_type(item) == "tool_search_output"
        and not isinstance(item.get("call_id"), str)
        and item.get("execution") != "client"
    )


def _streamed_output_events_are_valid(events: list[tuple[int, dict]]) -> bool:
    """Validate ordered chunks for an SDK-streamed call output."""
    terminal_seen = False
    for _index, output in events:
        status = output.get("status")
        if status == "in_progress":
            if terminal_seen:
                return False
            continue
        if status in {"completed", "incomplete"}:
            if terminal_seen:
                return False
            terminal_seen = True
            continue
        # A single legacy output without a status is a completed snapshot;
        # it cannot be combined safely with streaming chunks.
        return len(events) == 1 and status is None
    return True


def _response_run_is_valid(items: list[dict]) -> bool:
    """Validate a contiguous graph of replayable Responses items.

    Programmatic tool calling can interleave owned children from several
    programs, for example ``program-1, child-1, program-2, child-2,
    output-1, output-2``.  A linear parent/child scan incorrectly turns that
    valid graph into orphaned fragments.  This validator instead resolves
    every call/output edge over the complete run, then checks parent program
    ownership and the special ordered, anonymous tool-search edges.
    """
    calls: dict[tuple[str, str], tuple[int, dict]] = {}
    outputs: dict[tuple[str, str], tuple[int, dict]] = {}
    streamed_outputs: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    program_outputs: dict[str, list[tuple[int, dict]]] = {}
    matched_anonymous_program_ids: set[str] = set()
    # Some SDK output schemas omit ``caller`` even when their corresponding
    # call is program-owned.  Retain that correlation-derived ownership so a
    # terminal program result cannot be bypassed with a caller-less output.
    inferred_output_owners: dict[int, str] = {}

    for index, item in enumerate(items):
        if item.get("type") == "reasoning":
            follower_index = index + 1
            while (
                follower_index < len(items)
                and items[follower_index].get("type") == "reasoning"
            ):
                follower_index += 1
            if (
                follower_index >= len(items)
                or not _is_model_emitted_response_item(items[follower_index])
            ):
                return False
            continue

        call_type = _response_call_type(item)
        output_type = _response_output_type(item)
        if call_type is not None:
            if call_type == "program" and _program_caller_id(item) is not None:
                # Nested programs are not a documented Responses replay
                # shape.  Refuse to guess at their completion graph.
                return False
            if _is_anonymous_tool_search_call(item):
                continue
            pair = _response_call_pair(item)
            if pair is None or pair in calls:
                return False
            calls[pair] = (index, item)
            continue

        if output_type is not None:
            if _is_anonymous_tool_search_output(item):
                continue
            pair = _response_output_pair(item)
            if pair is None:
                return False
            if output_type == "program_output":
                program_outputs.setdefault(pair[1], []).append((index, item))
                continue
            if output_type in _RESPONSE_STREAMED_OUTPUT_TYPES:
                streamed_outputs.setdefault(pair, []).append((index, item))
                continue
            if pair in outputs:
                return False
            outputs[pair] = (index, item)
            continue

        if not _is_opaque_response_item(item):
            # ``_is_response_protocol_item`` should only put recognized
            # entries here.  Keep an explicit fail-closed guard for future
            # SDK item shapes that happen to carry a program caller.
            return False

    # Each identified output must close an earlier call of the same kind.
    # Check caller ownership on the edge when both sides carry it, and reject
    # a program-owned output for a direct call.
    for pair, (output_index, output) in outputs.items():
        call_entry = calls.get(pair)
        if call_entry is None:
            return False
        call_index, call = call_entry
        if call_index >= output_index:
            return False
        call_program_id = _program_caller_id(call)
        output_program_id = _program_caller_id(output)
        if output_program_id is not None and output_program_id != call_program_id:
            return False
        if call_program_id is not None:
            inferred_output_owners[output_index] = call_program_id

    for pair, events in streamed_outputs.items():
        call_entry = calls.get(pair)
        if call_entry is None or not _streamed_output_events_are_valid(events):
            return False
        call_index, call = call_entry
        call_program_id = _program_caller_id(call)
        for output_index, output in events:
            if call_index >= output_index:
                return False
            output_program_id = _program_caller_id(output)
            if output_program_id is not None and output_program_id != call_program_id:
                return False
            if call_program_id is not None:
                inferred_output_owners[output_index] = call_program_id

    # Anonymous server tool-search outputs intentionally have no common ID.
    # A single call can therefore be correlated safely with its whole ordered
    # stream across the combined payload.  With multiple calls, preserve only
    # the unambiguous one-output-per-call form by matching from the right.
    anonymous_call_indexes = [
        index
        for index, item in enumerate(items)
        if _is_anonymous_tool_search_call(item)
    ]
    anonymous_output_indexes = [
        index
        for index, item in enumerate(items)
        if _is_anonymous_tool_search_output(item)
    ]
    if len(anonymous_call_indexes) == 1:
        # With one call the optional call_id does not introduce ambiguity, so
        # retain an SDK-valid streamed ``in_progress* -> terminal`` sequence.
        # Multiple anonymous calls remain strictly one-output-per-call below.
        call_index = anonymous_call_indexes[0]
        events = [(index, items[index]) for index in anonymous_output_indexes]
        if (
            not events
            or any(index <= call_index for index, _ in events)
            or not _streamed_output_events_are_valid(events)
        ):
            return False
        call_program_id = _program_caller_id(items[call_index])
        for output_index, output in events:
            output_program_id = _program_caller_id(output)
            if output_program_id is not None and output_program_id != call_program_id:
                return False
            if call_program_id is not None:
                inferred_output_owners[output_index] = call_program_id
        if call_program_id is not None:
            matched_anonymous_program_ids.add(call_program_id)
    elif anonymous_call_indexes or anonymous_output_indexes:
        # The SDK's anonymous fallback has no ID to distinguish concurrent
        # searches.  It can safely preserve only the unambiguous one-output
        # per call shape, matched from the right like the upstream sanitizer.
        if len(anonymous_call_indexes) != len(anonymous_output_indexes):
            return False
        pending_anonymous_outputs: list[int] = []
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if _is_anonymous_tool_search_output(item):
                pending_anonymous_outputs.append(index)
                continue
            if not _is_anonymous_tool_search_call(item):
                continue
            if not pending_anonymous_outputs:
                return False
            output_index = pending_anonymous_outputs.pop()
            call_program_id = _program_caller_id(item)
            output_program_id = _program_caller_id(items[output_index])
            if output_program_id is not None and output_program_id != call_program_id:
                return False
            if call_program_id is not None:
                matched_anonymous_program_ids.add(call_program_id)
                inferred_output_owners[output_index] = call_program_id
        if pending_anonymous_outputs:
            return False

    program_roots: dict[str, tuple[int, dict]] = {}
    for pair, entry in calls.items():
        call_index, call = entry
        if _response_call_type(call) == "program":
            program_id = pair[1]
            if program_id in program_roots:
                return False
            program_roots[program_id] = entry

    terminal_program_outputs: dict[str, tuple[int, dict]] = {}
    for program_id, output_events in program_outputs.items():
        root = program_roots.get(program_id)
        if root is None:
            return False
        terminal_seen = False
        for output_index, output in output_events:
            if output_index <= root[0] or terminal_seen:
                return False
            status = output.get("status")
            if status not in {"incomplete", "completed"}:
                return False
            if status == "completed":
                terminal_program_outputs[program_id] = (output_index, output)
                terminal_seen = True

    # Every program-owned item must have an earlier root.  Keep a program when
    # it either has its own terminal output or contains an owned item that the
    # SDK considers active (including a child output).  A child call itself is
    # only sufficient after its matching output is present.
    for index, item in enumerate(items):
        program_id = _program_caller_id(item)
        if program_id is None:
            continue
        root = program_roots.get(program_id)
        if root is None or root[0] >= index:
            return False

    for program_id, (program_index, _program) in program_roots.items():
        program_output = terminal_program_outputs.get(program_id)
        completed = program_id in program_outputs
        if (
            program_output is not None
            and any(
                index > program_output[0]
                and (
                    _program_caller_id(item) == program_id
                    or inferred_output_owners.get(index) == program_id
                )
                for index, item in enumerate(items)
            )
        ):
            # A terminal program result closes that invocation.  The Agents
            # SDK rejects any subsequently replayed child with the completed
            # program as its caller; ``incomplete`` program results remain
            # resumable and intentionally do not take this branch.
            return False
        has_active_child = any(
            _is_active_program_child(item, program_id)
            for item in items[program_index + 1:]
        )
        # A child output without its caller metadata is still owned by the
        # program if its matched call is owned.  This occurs in SDK callback
        # payloads, so derive activity from the correlation edge as well.
        has_completed_owned_call = any(
            _program_caller_id(call) == program_id
            and (pair in outputs or pair in streamed_outputs)
            for pair, (_, call) in calls.items()
            if _response_call_type(call) != "program"
        )
        if (
            not completed
            and not has_active_child
            and not has_completed_owned_call
            and program_id not in matched_anonymous_program_ids
        ):
            return False

    # Except for a program root (which can remain active) and documented
    # pending program children, calls in a replay run require an output.
    for pair, (_, call) in calls.items():
        if (
            pair in outputs
            or pair in streamed_outputs
            or _response_call_type(call) == "program"
        ):
            continue
        if (
            _program_caller_id(call) is not None
            and _uncompleted_program_call_is_valid(call)
        ):
            continue
        return False
    return True


def _response_run_group(
    history: list[dict], index: int
) -> tuple[list[dict], list[int], int] | None:
    """Return one atomic, contiguous Responses replay graph from ``index``."""
    if not _is_response_protocol_item(history[index]):
        return None
    end = index + 1
    while end < len(history) and _is_response_protocol_item(history[end]):
        end += 1
    indices = list(range(index, end))
    run = history[index:end]
    return (run if _response_run_is_valid(run) else [], indices, end)


def _assistant_tool_call_ids(item: dict) -> set[str] | None:
    """Return declared tool-call IDs, ``None`` for a normal message.

    An empty set represents a malformed tool-call request.  It is deliberately
    distinct from ``None`` so history selection drops that request rather than
    emitting an orphaned assistant tool-call message.
    """
    if item.get("role") != "assistant" or "tool_calls" not in item:
        return None
    raw_calls = item.get("tool_calls")
    if raw_calls is None or raw_calls == []:
        return None
    if not isinstance(raw_calls, list) or not raw_calls:
        return set()
    call_ids: list[str] = []
    for call in raw_calls:
        call_id = call.get("id") if isinstance(call, dict) else None
        if not isinstance(call_id, str) or not call_id:
            return set()
        call_ids.append(call_id)
    return set(call_ids) if len(set(call_ids)) == len(call_ids) else set()


def _tail_response_history_dependency(
    history: list[dict], tail_messages: list[dict]
) -> tuple[list[dict], int, bool] | None:
    """Return the trailing Responses graph needed by leading tail outputs.

    A session callback sees history and ``new_input`` separately, but the
    Agents SDK subsequently joins them before validating tool dependencies.
    Validate that same combined graph here and reserve its complete trailing
    history component.  This preserves mixed direct calls, multiple
    interleaved program invocations, and anonymous server tool-search pairs
    without emitting a partial graph to the model.
    """
    if not tail_messages or _response_output_type(tail_messages[0]) is None:
        return None

    tail_end = 0
    while tail_end < len(tail_messages):
        if _response_output_type(tail_messages[tail_end]) is None:
            break
        tail_end += 1

    history_start = len(history)
    while (
        history_start > 0
        and _is_response_protocol_item(history[history_start - 1])
    ):
        history_start -= 1
    if history_start == len(history):
        return [], len(history), False
    dependency_history = history[history_start:]
    valid = _response_run_is_valid([
        *dependency_history,
        *tail_messages[:tail_end],
    ])
    return (
        dependency_history if valid else [],
        history_start,
        valid,
    )


def _tail_history_dependency(
    history: list[dict], tail_messages: list[dict]
) -> tuple[list[dict], int, bool]:
    """Return a trailing history call group required by a leading tail output.

    Agents commonly resume a tool invocation by passing its output as
    ``new_input`` while the corresponding call is still the last session
    item. That call is protocol-mandatory even though it lives on the other
    side of the history/final-input boundary. The returned start index removes
    the group from normal optional-history admission.
    """
    if not tail_messages:
        return [], len(history), True

    first = tail_messages[0]
    if first.get("role") == "tool":
        end = 0
        reply_ids: list[str] = []
        valid = True
        while end < len(tail_messages) and tail_messages[end].get("role") == "tool":
            reply_id = tail_messages[end].get("tool_call_id")
            if not isinstance(reply_id, str) or not reply_id or reply_id in reply_ids:
                valid = False
            else:
                reply_ids.append(reply_id)
            end += 1
        expected_ids = _assistant_tool_call_ids(history[-1]) if history else None
        pairs_match = (
            valid
            and expected_ids is not None
            and bool(expected_ids)
            and len(reply_ids) == len(expected_ids)
            and set(reply_ids) == expected_ids
        )
        return (history[-1:] if pairs_match else [], len(history) - 1, pairs_match)

    response_dependency = _tail_response_history_dependency(history, tail_messages)
    if response_dependency is not None:
        return response_dependency

    return [], len(history), True


def _history_groups(history: list[dict]) -> list[tuple[list[dict], list[int]]]:
    """Keep Chat Completions and Responses tool interactions atomic.

    Selecting one item at a time can leave a ``tool`` result without its
    assistant ``tool_calls`` request (or an Agents Responses output without
    its call), which downstream providers may reject. A malformed or incomplete
    contiguous sequence is returned as an empty group and omitted by the caller.
    """
    groups: list[tuple[list[dict], list[int]]] = []
    index = 0
    while index < len(history):
        item = history[index]
        response_run_group = _response_run_group(history, index)
        if response_run_group is not None:
            group, indices, index = response_run_group
            groups.append((group, indices))
            continue

        if _is_response_input_only_item(item):
            # These are legal only in a caller-controlled final input
            # position.  Optional recalled history must not place either one
            # before a new turn.
            groups.append(([], [index]))
            index += 1
            continue

        expected_ids = _assistant_tool_call_ids(item)
        if expected_ids is not None:
            end = index + 1
            replies: list[dict] = []
            reply_ids: list[str] = []
            while end < len(history) and history[end].get("role") == "tool":
                reply = history[end]
                reply_id = reply.get("tool_call_id")
                replies.append(reply)
                if isinstance(reply_id, str) and reply_id:
                    reply_ids.append(reply_id)
                end += 1
            reply_ids_match = (
                len(replies) == len(expected_ids)
                and len(reply_ids) == len(replies)
                and set(reply_ids) == expected_ids
            )
            indices = list(range(index, end))
            groups.append((history[index:end] if reply_ids_match else [], indices))
            index = end
            continue

        if item.get("role") == "tool":
            groups.append(([], [index]))
        else:
            groups.append(([item], [index]))
        index += 1
    return groups


class TokenBudgetedContextBuilder(ContextBuilder):
    """ContextBuilder that enforces a hard token ceiling on the final
    ``system_prompt``.

    Behaviour:
    1. ``system_prompt`` is mandatory and never truncated. If it does not
       fit into ``max_tokens`` alone, ``TokenBudgetExceededError`` is
       raised.
    2. ``profile_text`` is appended in full when ``include_profile`` is
       true; if it would push us over budget, the profile is dropped and
       ``dropped_blocks`` is updated (not raised — profile is a hint).
    3. RAG and session blocks are pooled (top_k * 2) and allocated
       greedily in priority order. The last accepted block is truncated
       at a word boundary if it does not fit whole.
    4. ``BudgetReport`` is attached to the returned ``ContextOutput``
       via ``budget_report`` so the caller can surface a usage indicator.
    5. ``build_messages`` reserves the final user message and configured
       output capacity before allocation, then trims history oldest-first.
       An oversized final user message raises rather than overflowing or being
       silently truncated.
    """

    def __init__(
        self,
        store: StoreProtocol,
        llm: EmbeddingClientProtocol,
        counter: TokenCounter | None = None,
        max_tokens: int = 4096,
        priorities: tuple[Priority, ...] = DEFAULT_PRIORITIES,
        hooks: ContextHooks | None = None,
        retriever: Retriever | None = None,
        *,
        scope: MemoryScope | None = None,
        event_sink: EventSink | EventDispatcher | None = None,
        output_reserve: int = 0,
    ) -> None:
        super().__init__(
            store,
            llm,
            hooks=hooks,
            retriever=retriever,
            scope=scope,
            event_sink=event_sink,
        )
        self._counter: TokenCounter = counter or RegexTokenCounter()
        self._max_tokens = max_tokens
        self._priorities = priorities
        if output_reserve < 0:
            raise ValueError("output_reserve must be non-negative")
        self._output_reserve = output_reserve

    async def build(self, inp: ContextInput) -> ContextOutput:
        """Assemble context under this builder's default output reserve.

        ``build()`` only returns the system-context portion, so callers that
        construct messages themselves remain responsible for reserving their
        own history/current-turn message costs.  ``build_messages()`` is the
        safe request-level API: it accounts for every final message and its
        provider overhead.
        """
        if self._output_reserve > self._max_tokens:
            raise TokenBudgetExceededError(
                self._output_reserve,
                self._max_tokens,
                "output_reserve",
            )
        return await self._build(
            inp,
            max_tokens=self._max_tokens - self._output_reserve,
            report_budget=self._max_tokens,
            output_reserve=self._output_reserve,
            counter=self._counter,
        )

    async def _build(
        self,
        inp: ContextInput,
        *,
        max_tokens: int,
        report_budget: int,
        output_reserve: int,
        counter: TokenCounter,
    ) -> ContextOutput:
        started_at = perf_counter()
        trace_id = new_trace_id()
        report = BudgetReport(
            budget=report_budget,
            output_reserve_tokens=output_reserve,
        )

        system_cost = counter.count(inp.system_prompt) if inp.system_prompt else 0
        if system_cost > max_tokens:
            raise TokenBudgetExceededError(system_cost, max_tokens, SEGMENT_SYSTEM)
        report.section_tokens[SEGMENT_SYSTEM] = system_cost
        fire(self._hooks.on_section_used, SEGMENT_SYSTEM, system_cost)

        requested_profile = ""
        if inp.include_profile:
            if inp.profile is not None:
                requested_profile = render(inp.profile, language=inp.language)
            elif inp.profile_text:
                requested_profile = (
                    f"{section_header('profile', inp.language)}\n{inp.profile_text}"
                )

        profile_block = ""
        used_tokens = system_cost
        if requested_profile:
            proposed = self._assemble_prompt(
                inp.system_prompt, requested_profile, [], [], inp.language
            )
            proposed_cost = counter.count(proposed)
            if proposed_cost > max_tokens:
                logger.warning(
                    "Profile block would exceed context budget (%d > %d); dropping",
                    proposed_cost,
                    max_tokens,
                )
                report.dropped_blocks.append(SEGMENT_PROFILE)
                fire(self._hooks.on_block_dropped, SEGMENT_PROFILE, "over_budget")
            else:
                profile_block = requested_profile
                profile_cost = proposed_cost - used_tokens
                used_tokens = proposed_cost
                report.section_tokens[SEGMENT_PROFILE] = profile_cost
                fire(self._hooks.on_section_used, SEGMENT_PROFILE, profile_cost)

        pool: dict[Priority, list[_Candidate]] = {
            SEGMENT_RAG: [],
            SEGMENT_SESSION: [],
        }

        needs_retrieval = inp.include_rag or (inp.include_session and inp.chat_id)
        if needs_retrieval and used_tokens < max_tokens:
            query_emb = (await self._llm.embed([inp.query], model=inp.embedding_model))[0]

            if inp.include_rag:
                rag_chunks = await self._retriever.retrieve_embedded(
                    query_emb,
                    query_text=inp.query,
                    top_k=max(1, inp.top_k_rag * 2),
                    doc_ids=inp.doc_ids,
                    score_threshold=inp.score_threshold,
                    trace_id=trace_id,
                )
                for i, chunk in enumerate(rag_chunks):
                    pool[SEGMENT_RAG].append(_Candidate(
                        section=SEGMENT_RAG,
                        text=chunk.text,
                        label=f"rag[{i}]",
                        chunk=chunk,
                    ))

            if inp.include_session and inp.chat_id:
                retrieve_started_at = perf_counter()
                session_hits = await await_if_needed(self._store.query(
                    query_emb,
                    top_k=max(1, inp.top_k_session * 2),
                    where=self._session_where(inp.chat_id),
                ))
                dispatch(self._event_sink, RetrieveEvent(
                    action="completed",
                    trace_id=trace_id,
                    scope_id=scope_id(self._scope),
                    duration_ms=elapsed_ms(retrieve_started_at),
                    attributes={
                        "channel": "session",
                        "top_k": max(1, inp.top_k_session * 2),
                        "hit_count": len(session_hits),
                        "doc_filter_count": 1,
                        "threshold_applied": False,
                    },
                ))
                for i, hit in enumerate(session_hits):
                    pool[SEGMENT_SESSION].append(_Candidate(
                        section=SEGMENT_SESSION,
                        text=hit["document"],
                        label=f"session[{i}]",
                    ))
        elif needs_retrieval:
            # No candidate can fit when mandatory system/profile context has
            # already consumed the allocation. Avoid an embedding/retrieval
            # round trip that cannot influence the final request.
            if inp.include_rag:
                report.dropped_blocks.append(SEGMENT_RAG)
                fire(self._hooks.on_block_dropped, SEGMENT_RAG, "no_budget")
            if inp.include_session and inp.chat_id:
                report.dropped_blocks.append(SEGMENT_SESSION)
                fire(self._hooks.on_block_dropped, SEGMENT_SESSION, "no_budget")

        kept_rag: list[str] = []
        kept_rag_chunks: list[RetrievedChunk] = []
        kept_session: list[str] = []

        def proposed_total(cand: _Candidate, text: str) -> int:
            rag = [*kept_rag, text] if cand.section == SEGMENT_RAG else kept_rag
            session = (
                [*kept_session, text]
                if cand.section == SEGMENT_SESSION
                else kept_session
            )
            assembled = self._assemble_prompt(
                inp.system_prompt, profile_block, rag, session, inp.language
            )
            return counter.count(assembled)

        def accept(cand: _Candidate, text: str, total: int) -> None:
            nonlocal used_tokens
            incremental_cost = total - used_tokens
            if cand.section == SEGMENT_RAG:
                kept_rag.append(text)
                if cand.chunk is not None:
                    kept_rag_chunks.append(
                        cand.chunk if text == cand.text else replace(cand.chunk, text=text)
                    )
            else:
                kept_session.append(text)
            used_tokens = total
            report.section_tokens[cand.label] = incremental_cost
            fire(self._hooks.on_section_used, cand.label, incremental_cost)

        def drop(cand: _Candidate, reason: str) -> None:
            if cand.label not in report.dropped_blocks:
                report.dropped_blocks.append(cand.label)
                fire(self._hooks.on_block_dropped, cand.label, reason)

        active_sections = [
            section
            for section in self._priorities
            if section in (SEGMENT_RAG, SEGMENT_SESSION)
        ]
        stop_all = False
        for section_index, section in enumerate(active_sections):
            if not pool[section]:
                continue
            for candidate_index, cand in enumerate(pool[section]):
                total = proposed_total(cand, cand.text)
                if total <= max_tokens:
                    accept(cand, cand.text, total)
                    continue

                trimmed = self._truncate_to_fit(
                    cand.text,
                    lambda value: proposed_total(cand, value) <= max_tokens,
                )
                if trimmed:
                    accept(cand, trimmed, proposed_total(cand, trimmed))
                    for skipped in pool[section][candidate_index + 1:]:
                        drop(skipped, "budget_exhausted")
                    for later in active_sections[section_index + 1:]:
                        for skipped in pool[later]:
                            drop(skipped, "budget_exhausted")
                    stop_all = True
                else:
                    drop(cand, "over_budget")
                    for skipped in pool[section][candidate_index + 1:]:
                        drop(skipped, "over_budget")
                break
            if stop_all:
                break

        system_prompt = self._assemble_prompt(
            inp.system_prompt, profile_block, kept_rag, kept_session, inp.language
        )
        report.used_tokens = counter.count(system_prompt)
        report.remaining_tokens = max_tokens - report.used_tokens

        output = ContextOutput(
            system_prompt=system_prompt,
            rag_chunks=kept_rag_chunks,
            rag_blocks=kept_rag,
            session_blocks=kept_session,
            profile_used=bool(profile_block),
            budget_report=report,
        )
        dispatch(self._event_sink, ContextEvent(
            action="completed",
            trace_id=trace_id,
            scope_id=scope_id(self._scope),
            duration_ms=elapsed_ms(started_at),
            attributes={
                "budgeted": True,
                "budget": report.budget,
                "used_tokens": report.used_tokens,
                "remaining_tokens": report.remaining_tokens,
                "output_reserve_tokens": report.output_reserve_tokens,
                "dropped_block_count": len(report.dropped_blocks),
                "rag_block_count": len(kept_rag),
                "session_block_count": len(kept_session),
                "profile_used": bool(profile_block),
            },
        ))
        fire(self._hooks.on_build_done, report)
        self._last_report = report
        return output

    @staticmethod
    def _assemble_prompt(
        system_prompt: str,
        profile_block: str,
        rag_blocks: list[str],
        session_blocks: list[str],
        language: str,
    ) -> str:
        parts: list[str] = []
        if system_prompt:
            parts.append(system_prompt)
        if profile_block:
            parts.append(profile_block)
        if rag_blocks:
            parts.append("\n\n---\n\n".join(rag_blocks))
        if session_blocks:
            parts.append(
                f"{section_header('session', language)}\n"
                + "\n---\n".join(session_blocks)
            )
        return "\n\n".join(parts)

    @staticmethod
    def _truncate_to_fit(text: str, fits) -> str:
        """Return the longest word-boundary prefix accepted by ``fits``."""
        words = text.split()
        low, high = 0, len(words)
        while low < high:
            mid = (low + high + 1) // 2
            candidate = " ".join(words[:mid]) + "…"
            if fits(candidate):
                low = mid
            else:
                high = mid - 1
        return " ".join(words[:low]) + "…" if low else ""

    async def build_messages(
        self,
        inp: ContextInput,
        history: list[dict] | None = None,
        user_message: str | None = None,
        *,
        final_messages: list[dict] | None = None,
        output_reserve: int | None = None,
        counter: TokenCounter | None = None,
    ) -> list[dict]:
        """Build a request that cannot exceed the configured token budget.

        The budget covers the rendered system context, every retained history
        item (including tool messages), final user or ``final_messages``
        payloads, each message's provider overhead, and an output reserve.
        ``output_reserve`` can override the builder default for one request.
        A caller-supplied final turn is never silently truncated; an oversized
        turn raises :class:`TokenBudgetExceededError` before retrieval happens.
        """
        if user_message is not None and final_messages is not None:
            raise ValueError("pass either user_message or final_messages, not both")
        reserve = self._output_reserve if output_reserve is None else output_reserve
        active_counter = self._counter if counter is None else counter
        if reserve < 0:
            raise ValueError("output_reserve must be non-negative")
        if reserve > self._max_tokens:
            raise TokenBudgetExceededError(reserve, self._max_tokens, "output_reserve")

        tail_messages: list[dict] = []
        if final_messages is not None:
            tail_messages = list(final_messages)
        elif user_message:
            tail_messages = [{"role": "user", "content": user_message}]
        history_items = list(history or [])
        dependency_history, dependency_start, dependency_is_valid = (
            _tail_history_dependency(history_items, tail_messages)
        )
        if not dependency_is_valid:
            raise ValueError(
                "a leading final tool output requires its matching trailing "
                "history call"
            )
        tail_tokens = active_counter.count_messages(tail_messages)
        dependency_tokens = active_counter.count_messages(dependency_history)
        tail_section = "user" if user_message is not None else "new_input"
        required_tokens = tail_tokens + dependency_tokens + reserve
        if required_tokens > self._max_tokens:
            raise TokenBudgetExceededError(
                required_tokens,
                self._max_tokens,
                "history_dependency" if dependency_history else tail_section,
            )

        # Reserve the exact system-message framing cost before allocating the
        # system-context string.  TokenCounter's contract is content +
        # per-message overhead, so this keeps the raw context allocator and
        # final message-list counter aligned for provider-aware counters.
        system_overhead = active_counter.count_messages([
            {"role": "system", "content": ""}
        ])
        context_budget = max(
            0,
            self._max_tokens
            - reserve
            - tail_tokens
            - dependency_tokens
            - system_overhead,
        )
        out = await self._build(
            inp,
            max_tokens=context_budget,
            report_budget=self._max_tokens,
            output_reserve=reserve,
            counter=active_counter,
        )
        report = out.budget_report
        assert report is not None

        messages: list[dict] = []
        if out.system_prompt:
            messages.append({"role": "system", "content": out.system_prompt})

        fixed_tokens = (
            active_counter.count_messages(messages)
            + dependency_tokens
            + tail_tokens
        )
        remaining = self._max_tokens - reserve - fixed_tokens
        if remaining < 0:
            # A conforming counter makes this unreachable because
            # ``system_overhead`` was reserved above.  Keep the invariant hard
            # even for a custom counter with role-specific framing costs.
            raise TokenBudgetExceededError(
                fixed_tokens + reserve,
                self._max_tokens,
                "system",
            )

        kept_history: list[dict] = []
        selection_history = history_items[:dependency_start]
        if selection_history:
            spent = 0
            for group, indices in reversed(_history_groups(selection_history)):
                if not group:
                    for index in indices:
                        label = f"history[{index}]"
                        report.dropped_blocks.append(label)
                        fire(self._hooks.on_block_dropped, label, "tool_pair_incomplete")
                    continue
                cost = active_counter.count_messages(group)
                if spent + cost > remaining:
                    for index in indices:
                        label = f"history[{index}]"
                        report.dropped_blocks.append(label)
                        fire(self._hooks.on_block_dropped, label, "over_budget")
                    continue
                spent += cost
                kept_history[0:0] = group
            report.history_kept = len(kept_history) + len(dependency_history)
            report.history_tokens = spent + dependency_tokens
            remaining -= spent
            messages.extend(kept_history)

        elif dependency_history:
            report.history_kept = len(dependency_history)
            report.history_tokens = dependency_tokens

        messages.extend(dependency_history)
        messages.extend(tail_messages)

        message_tokens = active_counter.count_messages(messages)
        total_tokens = message_tokens + reserve
        if total_tokens > self._max_tokens:
            # This is a last-line guard against a custom TokenCounter whose
            # batch count differs from the sum of individual message counts.
            raise TokenBudgetExceededError(total_tokens, self._max_tokens, "messages")
        report.used_tokens = message_tokens
        report.user_message_tokens = tail_tokens
        report.remaining_tokens = self._max_tokens - total_tokens
        self._last_report = report
        return messages

    def _truncate_to_budget(self, text: str, budget: int) -> str:
        """Cut ``text`` so it fits into ``budget`` tokens, ending on a
        word boundary. Returns empty string if no content fits.
        """
        if budget <= 0:
            return ""
        words = text.split()
        out: list[str] = []
        used = 0
        for w in words:
            w_cost = self._counter.count(w) + 1
            if used + w_cost > budget:
                break
            out.append(w)
            used += w_cost
        if not out:
            return ""
        return " ".join(out) + "…"
