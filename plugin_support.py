from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from astrbot.api.event import filter

try:
    from astrbot.api import llm_tool as astrbot_llm_tool
except ImportError:
    astrbot_llm_tool = None

try:
    from astrbot.api import logger
except ImportError:
    logger = logging.getLogger(__name__)


def compat_llm_tool(name: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        if astrbot_llm_tool is not None:
            return astrbot_llm_tool(name=name)(func)

        llm_tool_factory = getattr(filter, "llm_tool", None)
        if llm_tool_factory is None:
            return func

        for args, kwargs in (
            ((), {"name": name}),
            ((name,), {}),
            ((), {}),
        ):
            try:
                return llm_tool_factory(*args, **kwargs)(func)
            except TypeError:
                continue
        return func

    return decorator


async def run_blocking(func: Callable[..., Any], *args: Any) -> Any:
    to_thread = getattr(asyncio, "to_thread", None)
    if to_thread is not None:
        return await to_thread(func, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))
