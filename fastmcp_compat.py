from __future__ import annotations

import inspect
from functools import wraps
from typing import Any

from fastmcp import FastMCP as _FastMCP

try:
    from fastmcp.tools.function_tool import FunctionTool
except ImportError:  # pragma: no cover - legacy FastMCP fallback
    from fastmcp.tools.tool import FunctionTool


class _CompatFunctionTool(FunctionTool):
    """Back-compat wrapper: tool.fn is the tool, and the tool stays callable."""

    _legacy_raw_fn: Any | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raw_fn = self._legacy_raw_fn
        if raw_fn is None:
            raise TypeError(f"{self.name!r} is not directly callable")
        return raw_fn(*args, **kwargs)

    async def run(self, arguments: dict[str, Any]) -> Any:
        raw_fn = self._legacy_raw_fn
        if raw_fn is None:
            return await super().run(arguments)

        original_fn = self.fn
        try:
            self.fn = raw_fn
            return await super().run(arguments)
        finally:
            self.fn = original_fn


def _find_registered_tool(owner: Any, fn: Any) -> Any | None:
    local_provider = getattr(owner, "_local_provider", None)
    components = getattr(local_provider, "_components", None)
    if not isinstance(components, dict):
        return None

    raw_fn = getattr(fn, "__wrapped__", fn)
    for component in components.values():
        if (
            isinstance(component, FunctionTool)
            and getattr(component, "fn", None) is raw_fn
        ):
            return component
    return None


def _wrap_callable_tool(fn: Any, registered_tool: Any | None) -> Any:
    @wraps(fn)
    def legacy_wrapper(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    legacy_wrapper.fn = legacy_wrapper

    metadata = getattr(fn, "__fastmcp__", None)
    if metadata is not None:
        legacy_wrapper.__fastmcp__ = metadata

    legacy_description = inspect.getdoc(fn)
    if legacy_description:
        legacy_wrapper.description = legacy_description

    if registered_tool is not None:
        for attr in ("description", "name", "title"):
            value = getattr(registered_tool, attr, None)
            if value is not None and (
                attr != "description" or not hasattr(legacy_wrapper, "description")
            ):
                setattr(legacy_wrapper, attr, value)
        legacy_wrapper.tool = registered_tool

    return legacy_wrapper


def _attach_fn_alias(owner: Any, tool_obj: Any, fn: Any) -> Any:
    registered_tool = _find_registered_tool(owner, fn)

    if isinstance(tool_obj, _CompatFunctionTool):
        if tool_obj._legacy_raw_fn is None:
            tool_obj._legacy_raw_fn = fn
        tool_obj.__wrapped__ = fn
        return tool_obj

    if isinstance(tool_obj, FunctionTool) and not isinstance(
        tool_obj, _CompatFunctionTool
    ):
        try:
            tool_obj.__class__ = _CompatFunctionTool
            tool_obj._legacy_raw_fn = fn
            tool_obj.fn = tool_obj
            tool_obj.__wrapped__ = fn
            return tool_obj
        except Exception:
            pass

    if callable(tool_obj):
        return _wrap_callable_tool(tool_obj, registered_tool)

    if not hasattr(tool_obj, "fn"):
        try:
            setattr(tool_obj, "fn", fn)
        except Exception:
            pass
    return tool_obj


class FastMCP(_FastMCP):
    def tool(self, name_or_fn: str | Any | None = None, **kwargs: Any) -> Any:
        result = super().tool(name_or_fn, **kwargs)

        if callable(name_or_fn) and not isinstance(name_or_fn, str):
            return _attach_fn_alias(self, result, name_or_fn)

        if callable(result):

            @wraps(result)
            def decorator(fn: Any) -> Any:
                return _attach_fn_alias(self, result(fn), fn)

            return decorator

        return result
