"""Placeholder premium task tray extension for the private repo template."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class PremiumTaskTrayExtension:
    server_name: str

    @property
    def tab_key(self) -> str:
        return "custom_design"

    @property
    def tab_label(self) -> str:
        return "Custom Design"

    @property
    def default_params(self) -> dict[str, object]:
        return {
            "focus": "mixed",
            "group_by": "smart",
            "sort_by": "priority",
            "limit": 25,
            "protected_view_enabled": False,
            "protected_view_scope_key": "",
        }

    @property
    def extra_sort_modes(self) -> dict[str, str]:
        return {
            "updated": "Sort: Updated",
            "client": "Sort: Client",
            "risk": "Sort: Risk",
        }

    def normalize_params(self, params: dict[str, object] | None) -> dict[str, object]:
        normalized = dict(self.default_params)
        normalized.update(dict(params or {}))
        return normalized

    def apply_dialog_params(
        self,
        params: dict[str, object] | None,
    ) -> dict[str, object]:
        return self.normalize_params(params)

    def design_button_label(self, params: dict[str, object] | None) -> str:
        normalized = self.normalize_params(params)
        if bool(normalized.get("protected_view_enabled")):
            return "Unlock / Design..."
        return "Design..."

    def build_rows(
        self,
        *,
        params: dict[str, object] | None = None,
        search_text: str = "",
    ) -> dict[str, Any]:
        normalized = self.normalize_params(params)
        return {
            "rows": [],
            "meta": {
                "placeholder": True,
                "server_name": self.server_name,
                "search_text": search_text,
                "protected_view_enabled": bool(
                    normalized.get("protected_view_enabled")
                ),
                "protected_view_scope_key": str(
                    normalized.get("protected_view_scope_key") or ""
                ),
                "note": (
                    "Template tray extension only. Replace with the real "
                    "Custom Design and password-protected view logic."
                ),
            },
        }


def build_task_tray_extension(
    *,
    server_name: str | None = None,
    mount_context: Any | None = None,
) -> PremiumTaskTrayExtension:
    return PremiumTaskTrayExtension(
        server_name=server_name
        or getattr(mount_context, "server_name", "sqlite-task-tray-premium-template")
    )
