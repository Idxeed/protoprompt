"""Optional observability hooks for context building and compression.

All hooks are plain callables; any exception they raise is logged and
swallowed so observability can never break the main flow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from protoprompt.injector_budgeted import BudgetReport
    from protoprompt.session.types import CompressedBlock, Session

logger = logging.getLogger(__name__)


def fire(hook: Callable[..., Any] | None, *args: Any) -> None:
    """Invoke ``hook`` if set; log and swallow any exception."""
    if hook is None:
        return
    try:
        hook(*args)
    except Exception:
        logger.exception("protoprompt hook %r failed", getattr(hook, "__name__", hook))


@dataclass
class ContextHooks:
    """Callbacks emitted by (budgeted) context builders.

    - ``on_section_used(label, tokens)`` — a section or block entered the
      final prompt (labels: ``system``, ``profile``, ``rag[i]``, ``session[i]``).
    - ``on_block_dropped(label, reason)`` — a candidate did not fit
      (reasons: ``over_budget``, ``truncated_empty``, ``budget_exhausted``).
    - ``on_build_done(report)`` — end of a build; ``report`` is a
      ``BudgetReport`` for the budgeted builder and ``None`` for the base one.
    """

    on_section_used: Callable[[str, int], None] | None = None
    on_block_dropped: Callable[[str, str], None] | None = None
    on_build_done: Callable[["BudgetReport | None"], None] | None = None


@dataclass
class PipelineHooks:
    """Callbacks emitted by :class:`protoprompt.Pipeline`.

    - ``on_skip_compress(session)`` — below the message threshold.
    - ``on_before_compress(session)`` — compression starting.
    - ``on_after_compress(session, blocks)`` — compression finished and
      persisted.
    """

    on_skip_compress: Callable[["Session"], None] | None = None
    on_before_compress: Callable[["Session"], None] | None = None
    on_after_compress: Callable[["Session", "list[CompressedBlock]"], None] | None = None
