from __future__ import annotations

import ast
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULES = (
    "claim_confidence.py",
    "control_plane_shadow.py",
    "lazy_verification.py",
    "prediction_calibration.py",
    "source_reliability.py",
    "trust_boundary.py",
)


def _top_level_imports(tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports


def test_optional_analytics_modules_are_not_imported_by_runtime_surfaces():
    names = {Path(name).stem for name in MODULES}
    runtime_files = (
        "server.py",
        "task_server.py",
        "intel_server.py",
        "unified_server.py",
        "task_tray.py",
    )
    for filename in runtime_files:
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
        assert _top_level_imports(tree).isdisjoint(names), filename


def test_optional_analytics_modules_create_no_schema_or_connections():
    forbidden = ("CREATE TABLE", "ALTER TABLE", "sqlite3.connect", "get_conn(")
    for filename in MODULES:
        source = (ROOT / filename).read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, f"{filename}: {marker}"


def test_private_artifact_root_remains_ignored():
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".private-specs/probe.md"],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0
