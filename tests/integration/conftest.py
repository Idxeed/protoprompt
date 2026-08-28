from __future__ import annotations

import asyncio
import selectors
import sys


if sys.platform == "win32":

    def _selector_loop() -> asyncio.AbstractEventLoop:
        return asyncio.SelectorEventLoop(selectors.SelectSelector())


    def pytest_asyncio_loop_factories(config, item):
        """Run live async clients on the Windows-compatible selector loop."""
        return {"windows-selector": _selector_loop}
