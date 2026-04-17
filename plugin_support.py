from __future__ import annotations

import asyncio
import inspect
import logging
from functools import wraps
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


def _hide_system_parameters(func: Callable) -> Callable:
    signature = inspect.signature(func)
    filtered_parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.name != "event"
    ]
    filtered_annotations = dict(getattr(func, "__annotations__", {}))
    filtered_annotations.pop("event", None)

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

    else:

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

    wrapper.__signature__ = signature.replace(parameters=filtered_parameters)
    wrapper.__annotations__ = filtered_annotations
    return wrapper


def compat_llm_tool(name: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        schema_safe_func = _hide_system_parameters(func)
        if astrbot_llm_tool is not None:
            return astrbot_llm_tool(name=name)(schema_safe_func)

        llm_tool_factory = getattr(filter, "llm_tool", None)
        if llm_tool_factory is None:
            return schema_safe_func

        for args, kwargs in (
            ((), {"name": name}),
            ((name,), {}),
            ((), {}),
        ):
            try:
                return llm_tool_factory(*args, **kwargs)(schema_safe_func)
            except TypeError:
                continue
        return schema_safe_func

    return decorator


async def run_blocking(func: Callable[..., Any], *args: Any) -> Any:
    to_thread = getattr(asyncio, "to_thread", None)
    if to_thread is not None:
        return await to_thread(func, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))
