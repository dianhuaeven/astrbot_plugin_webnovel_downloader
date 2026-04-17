from __future__ import annotations

import asyncio
import inspect
import logging
from functools import wraps
from typing import Any, Callable, get_type_hints

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
    try:
        resolved_annotations = get_type_hints(func)
    except Exception:
        resolved_annotations = {}
    event_parameter = signature.parameters.get("event")
    event_index = None
    event_annotation = None
    if event_parameter is not None:
        event_index = list(signature.parameters).index("event")
        event_annotation = resolved_annotations.get("event", event_parameter.annotation)
    filtered_parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.name != "event"
    ]
    filtered_annotations = dict(getattr(func, "__annotations__", {}))
    filtered_annotations.pop("event", None)

    def _normalize_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if event_parameter is None or "event" in kwargs:
            return args, kwargs
        if event_index is not None and len(args) > event_index:
            candidate = args[event_index]
            if _looks_like_event_argument(candidate, event_annotation):
                return args, kwargs
        normalized_args = list(args)
        insert_index = event_index if event_index is not None else len(normalized_args)
        normalized_args.insert(insert_index, None)
        return tuple(normalized_args), kwargs

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            normalized_args, normalized_kwargs = _normalize_call(args, kwargs)
            return await func(*normalized_args, **normalized_kwargs)

    else:

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            normalized_args, normalized_kwargs = _normalize_call(args, kwargs)
            return func(*normalized_args, **normalized_kwargs)

    wrapper.__signature__ = signature.replace(parameters=filtered_parameters)
    wrapper.__annotations__ = filtered_annotations
    return wrapper


def _looks_like_event_argument(value: Any, annotation: Any) -> bool:
    if value is None or annotation is inspect.Signature.empty:
        return False
    if isinstance(annotation, type):
        return isinstance(value, annotation)
    return False


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
