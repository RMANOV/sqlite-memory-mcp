from __future__ import annotations

from functools import wraps
from typing import Any

from fastmcp import FastMCP as _FastMCP


def _attach_fn_alias(tool_obj: Any, fn: Any) -> Any:
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