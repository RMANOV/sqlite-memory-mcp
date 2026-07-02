"""Claim-freeze / surface-contract guard (D0).

Snapshots the PUBLIC surface of the project and fails on ANY drift:

  (a) the set of MCP server console scripts in pyproject.toml
  (b) per-server MCP tool names per server module
  (c) presence of claim-sensitive files (README.md)

The checked-in fixture ``tests/fixtures/surface_contract_snapshot.json`` is the
frozen contract. The test compares the LIVE surface against it and fails with a
precise diff. Tool/server renames or removals are claim-freeze violations.

Intentional change procedure:
  1. regenerate the fixture:
       python tests/test_surface_contract_guard.py --regenerate
  2. pass the diff through the ADVOCATE gate before merging.

Deterministic: no network, no DB writes; everything is sorted before compare.
"""

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bridge_server  # noqa: E402
import collab_server  # noqa: E402
import entity_server  # noqa: E402
import intel_server  # noqa: E402
import server  # noqa: E402
import session_server  # noqa: E402
import task_server  # noqa: E402
import unified_server  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / ("surface_contract_snapshot.json")
)

# Same module set tests/test_server_imports.py exercises — the public split
# micro-servers plus the unified aggregate.
_SERVER_MODULES = {
    "server": server,
    "session_server": session_server,
    "task_server": task_server,
    "bridge_server": bridge_server,
    "collab_server": collab_server,
    "entity_server": entity_server,
    "intel_server": intel_server,
    "unified_server": unified_server,
}

# Files whose PRESENCE is claim-sensitive (D0: no README/public-claim removals).
_CLAIM_SENSITIVE_FILES = ("README.md",)

_DRIFT_INSTRUCTIONS = (
    "Surface drift detected (claim-freeze guard). If this change is "
    "INTENTIONAL: regenerate the fixture with "
    "`python tests/test_surface_contract_guard.py --regenerate` and take the "
    "diff through the ADVOCATE gate before merge. If it is NOT intentional, "
    "revert the rename/removal — D0 claim-freeze forbids it."
)


def _live_console_scripts() -> dict:
    with (_ROOT / "pyproject.toml").open("rb") as fh:
        return dict(sorted(tomllib.load(fh)["project"]["scripts"].items()))


def _live_server_tools() -> dict:
    out = {}
    for name, module in sorted(_SERVER_MODULES.items()):
        if hasattr(module.mcp, "list_tools"):
            tools = asyncio.run(module.mcp.list_tools())
            out[name] = sorted(tool.name for tool in tools)
        else:
            tools = asyncio.run(module.mcp.get_tools())
            if isinstance(tools, dict):
                out[name] = sorted(tools.keys())
            else:
                out[name] = sorted(tool.name for tool in tools)
    return out


def _live_claim_sensitive_files() -> dict:
    return {rel: (_ROOT / rel).is_file() for rel in _CLAIM_SENSITIVE_FILES}


def build_live_surface() -> dict:
    return {
        "console_scripts": _live_console_scripts(),
        "server_tools": _live_server_tools(),
        "claim_sensitive_files": _live_claim_sensitive_files(),
    }


def _load_fixture() -> dict:
    assert _FIXTURE_PATH.is_file(), (
        f"missing surface-contract fixture {_FIXTURE_PATH}; {_DRIFT_INSTRUCTIONS}"
    )
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _diff_sets(kind: str, frozen: list, live: list) -> list[str]:
    frozen_set, live_set = set(frozen), set(live)
    lines = []
    for name in sorted(frozen_set - live_set):
        lines.append(f"  - {kind}: {name!r} REMOVED (was frozen)")
    for name in sorted(live_set - frozen_set):
        lines.append(f"  + {kind}: {name!r} ADDED (not in frozen contract)")
    return lines


def test_console_scripts_match_frozen_contract():
    frozen = _load_fixture()["console_scripts"]
    live = _live_console_scripts()
    lines = _diff_sets("console-script", list(frozen), list(live))
    for name in sorted(set(frozen) & set(live)):
        if frozen[name] != live[name]:
            lines.append(
                f"  ~ console-script {name!r}: entry point changed "
                f"{frozen[name]!r} -> {live[name]!r}"
            )
    assert not lines, "\n".join(lines) + "\n" + _DRIFT_INSTRUCTIONS


def test_per_server_tool_names_match_frozen_contract():
    frozen = _load_fixture()["server_tools"]
    live = _live_server_tools()
    lines = _diff_sets("server-module", list(frozen), list(live))
    for name in sorted(set(frozen) & set(live)):
        lines.extend(_diff_sets(f"{name} tool", frozen[name], live[name]))
    assert not lines, "\n".join(lines) + "\n" + _DRIFT_INSTRUCTIONS


def test_claim_sensitive_files_present():
    frozen = _load_fixture()["claim_sensitive_files"]
    live = _live_claim_sensitive_files()
    lines = _diff_sets("claim-sensitive file", list(frozen), list(live))
    for rel in sorted(set(frozen) & set(live)):
        if frozen[rel] and not live[rel]:
            lines.append(f"  - claim-sensitive file {rel!r} is MISSING on disk")
    assert not lines, "\n".join(lines) + "\n" + _DRIFT_INSTRUCTIONS


def test_fixture_is_normalized():
    """The checked-in fixture must be exactly the canonical serialization.

    Hand-edited or stale fixtures would make drift diffs unreliable.
    """
    raw = _FIXTURE_PATH.read_text(encoding="utf-8")
    canonical = json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"
    assert raw == canonical, (
        "fixture is not canonically formatted; regenerate it with "
        "`python tests/test_surface_contract_guard.py --regenerate`"
    )


def _regenerate() -> None:
    _FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FIXTURE_PATH.write_text(
        json.dumps(build_live_surface(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {_FIXTURE_PATH}")


if __name__ == "__main__":
    if "--regenerate" in sys.argv[1:]:
        _regenerate()
    else:
        print(__doc__)
        print("usage: python tests/test_surface_contract_guard.py --regenerate")
