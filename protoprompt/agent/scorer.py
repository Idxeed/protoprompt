"""Backward-compatible alias for the shared significance scorer.

The implementation now lives in :mod:`protoprompt.memory` so the profile
engine (and future decay logic) can reuse it without importing the agent
package. This module is kept so existing imports keep working.
"""

from protoprompt.memory import MemoryScorer, ScorerWeights

__all__ = ["MemoryScorer", "ScorerWeights"]
