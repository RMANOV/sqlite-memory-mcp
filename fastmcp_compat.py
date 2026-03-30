from __future__ import annotations

from functools import wraps
from typing import Any

from fastmcp import FastMCP as _FastMCP
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


def _attach_fn_alias(tool_obj: Any, fn: Any) -> Any:
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
            return _attach_fn_alias(result, name_or_fn)

        if callable(result):

            @wraps(result)
            def decorator(fn: Any) -> Any:
                return _attach_fn_alias(result(fn), fn)

            return decorator

        return result
