from __future__ import annotations

import pytest

from protoprompt.testing import run_long_dialog_scenario


@pytest.mark.asyncio
async def test_old_fact_survives_beyond_fifo_and_lru_capacity():
    result = await run_long_dialog_scenario(turns=100, capacity=12)
    assert result.fifo_recalled is False
    assert result.lru_recalled is False
    assert result.semantic_recalled is True
    assert result.semantic_memory_id == "turn-2"


@pytest.mark.asyncio
async def test_scenario_validates_parameters():
    with pytest.raises(ValueError, match="turns must exceed"):
        await run_long_dialog_scenario(turns=10, capacity=9)
    with pytest.raises(ValueError, match="capacity must be positive"):
        await run_long_dialog_scenario(turns=10, capacity=0)
