"""Small helpers shared by the test suites."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable


async def eventually(
    predicate: Callable[[], object | Awaitable[object]],
    *,
    within: float = 5.0,
    interval: float = 0.05,
) -> None:
    """Wait until ``predicate`` (sync or async) returns something truthy, or fail the test."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if loop.time() > deadline:
            msg = f"condition not met within {within}s"
            raise AssertionError(msg)
        await asyncio.sleep(interval)
