"""v3.9.4 — pyproject.toml manifest-completeness regression tests.

Per CONDUCTOR canonical msg:f21d82cc + scope extension msg:146a5892.

Bug class: top-level modules and subpackage directories added to the
repo but NOT declared in ``pyproject.toml`` ``[tool.setuptools]`` are
invisible to ``pip install -e .``'s frozen MAPPING. Subprocess MCP
spawns from arbitrary cwd fail with ``ModuleNotFoundError``. The bug
that motivated this file: 4 missing top-level modules (``debate``,
``reflection``, ``reflection_apply``, ``reflection_dao``) added in
v3.7.x–v3.9.x cycles + ``tools/`` subpackage not in
``packages.find.include`` — caught by EXECUTOR audit per msg:fa3e2b18.

Test 1a covers top-level ``.py`` modules.
Test 1b covers subpackages (directories with ``__init__.py``) — the
audit dimension that the original spec recipe missed (msg:146a5892
meta-lesson encoded as CI invariant).
Test 2 verifies subprocess import from arbitrary cwd succeeds for all
the v3.9.x-new modules + the ``tools.gbrain_bridge`` subpackage.
Test 3 probes the ``sqlite-memory-intel`` console entrypoint as a
stdio MCP server (per ADVOCATE msg:db0b9001: blocks on stdin, never
exits cleanly — must timeout-then-grep-stderr, not exit-0).
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

try:  # Python 3.11+: stdlib tomllib. Older Pythons need a backport.
    import tomllib
except ImportError:  # pragma: no cover - we target 3.11+ in CI
    import tomli as tomllib  # type: ignore[no-redef]


REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _load_manifest() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _toplevel_py_files() -> set[str]:
    """All importable top-level ``.py`` module bases in the repo.

    Excludes test files (``test_*.py``, ``conftest.py``), private
    modules (``_foo.py``), and dot-prefixed files. Subpackages are
    handled in Test 1b — they are directories, not files.
    """
    out: set[str] = set()
    for entry in os.listdir(REPO_ROOT):
        if not entry.endswith(".py"):
            continue
        base = entry[:-3]
        if base.startswith("_"):
            continue
        if base.startswith("test_") or base == "conftest":
            continue
        out.add(base)
    return out


def _toplevel_subpackages() -> set[str]:
    """All top-level subpackage directories (contain ``__init__.py``).

    Excludes private dirs (``_foo``), build/cache dirs (``__pycache__``,
    ``build``, ``dist``, ``.git``, ``.pytest_cache``, ``.ruff_cache``,
    etc.), the ``tests`` directory, and any directory starting with
    a dot.
    """
    cache_or_build = {
        "__pycache__",
        "build",
        "dist",
        "tests",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".tox",
        "press-releases",
        "docs",  # not Python packages
    }
    out: set[str] = set()
    for entry in os.listdir(REPO_ROOT):
        path = REPO_ROOT / entry
        if not path.is_dir():
            continue
        if entry.startswith(".") or entry.startswith("_"):
            continue
        if entry in cache_or_build:
            continue
        if not (path / "__init__.py").exists():
            continue
        out.add(entry)
    return out


# ════════════════════════════════════════════════════════════════════
# Test 1a — top-level .py modules vs py-modules
# ════════════════════════════════════════════════════════════════════


def test_pyproject_lists_every_toplevel_module():
    """Every importable top-level ``.py`` MUST be in
    ``[tool.setuptools] py-modules``. Otherwise ``pip install -e .``'s
    frozen MAPPING omits it and subprocess imports fail from arbitrary
    cwd. This caught the v3.9.3 ship bug retroactively (4 missing
    modules: debate, reflection, reflection_apply, reflection_dao).
    """
    manifest = _load_manifest()
    declared = set(manifest["tool"]["setuptools"]["py-modules"])
    on_disk = _toplevel_py_files()
    missing = on_disk - declared
    assert not missing, (
        f"Top-level modules on disk but missing from "
        f"pyproject.toml py-modules: {sorted(missing)}. "
        f"Add them to [tool.setuptools] py-modules and run "
        f"`pip install -e .` to refresh the editable MAPPING."
    )


def test_pyproject_does_not_declare_phantom_modules():
    """Reverse check: ``py-modules`` MUST NOT declare modules that
    don't exist on disk. Catches typos and rename-leftovers."""
    manifest = _load_manifest()
    declared = set(manifest["tool"]["setuptools"]["py-modules"])
    on_disk = _toplevel_py_files()
    phantom = declared - on_disk
    assert not phantom, (
        f"pyproject.toml py-modules declares modules with no "
        f"matching ``.py`` file: {sorted(phantom)}. Remove them or "
        f"restore the missing files."
    )


# ════════════════════════════════════════════════════════════════════
# Test 1b — subpackages vs packages.find.include
# (NEW per EXECUTOR finding msg:fa3e2b18 + CONDUCTOR scope-extension
# msg:146a5892 meta-lesson: subpackage audit is a CI invariant.)
# ════════════════════════════════════════════════════════════════════


def test_pyproject_includes_every_toplevel_subpackage():
    """Every top-level subpackage directory with ``__init__.py`` MUST
    have a matching include pattern in
    ``[tool.setuptools.packages.find].include``.

    Setuptools accepts both ``name`` (the package itself) and
    ``name.*`` (its subpackages). This test treats EITHER as
    sufficient — the canonical pattern emitted by the v3.9.4 spec
    pairs both.

    Caught the v3.9.4 ship bug retroactively (``tools/`` subpackage
    not in include list while ``intel_server.py:68`` imported
    ``tools.gbrain_bridge``).
    """
    manifest = _load_manifest()
    include_patterns = set(
        manifest["tool"]["setuptools"]["packages"]["find"]["include"]
    )
    on_disk = _toplevel_subpackages()
    missing: list[str] = []
    for pkg in on_disk:
        # Accept either exact-name include or a name.* wildcard.
        if pkg in include_patterns or f"{pkg}.*" in include_patterns:
            continue
        missing.append(pkg)
    assert not missing, (
        f"Subpackage directories on disk but NOT covered by "
        f"[tool.setuptools.packages.find].include: {sorted(missing)}. "
        f"Add both ``{missing[0]}`` and ``{missing[0]}.*`` to the "
        f"include list and run `pip install -e .` to refresh."
    )


# ════════════════════════════════════════════════════════════════════
# Test 2 — subprocess import probe from arbitrary cwd
# ════════════════════════════════════════════════════════════════════


# Modules added across the v3.7.x–v3.9.x cycle that the v3.9.3 ship
# bug had missing from the editable MAPPING. tools.gbrain_bridge is a
# subpackage; the rest are top-level modules.
_V3_9_4_IMPORT_PROBE_MODULES = (
    "debate",
    "reflection",
    "reflection_apply",
    "reflection_dao",
    "tools.gbrain_bridge",
    # Tier-1 sanity probe so a future regression is loud:
    "intel_server",
)


@pytest.mark.parametrize("module", _V3_9_4_IMPORT_PROBE_MODULES)
def test_module_imports_from_arbitrary_cwd(tmp_path, module):
    """Subprocess spawn from a foreign cwd MUST succeed when importing
    each module listed in the manifest. This is the production-
    representative path: MCP wrappers run from ``~/.claude`` or
    similar, not from the repo root.

    Per ADVOCATE msg:fa3e2b18 + msg:146a5892 — Test 2 extended to
    include ``tools.gbrain_bridge`` so the subpackage scope-extension
    has CI coverage alongside the top-level modules.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"`import {module}` from cwd={tmp_path} exited "
        f"{result.returncode}. stderr:\n{result.stderr}"
    )


# ════════════════════════════════════════════════════════════════════
# Test 3 — sqlite-memory-intel console entrypoint stdio probe
# (per ADVOCATE msg:db0b9001 — stdio MCP server pattern: blocks on
# stdin, no exit-0 expectation. Probe = run with timeout, then grep
# captured stderr for traceback / ModuleNotFoundError.)
# ════════════════════════════════════════════════════════════════════


def test_installed_package_metadata_version_matches_pyproject():
    """The editable install's recorded metadata version MUST match the
    ``[project] version`` in pyproject.toml.

    Per CONDUCTOR scope-extension msg:a09757ff: catches the
    metadata-staleness bug class going forward — a contributor who
    bumps ``version`` in pyproject.toml but forgets to run
    ``pip install -e .`` ships with stale dist-info, which silently
    breaks downstream tools that key off
    ``importlib.metadata.version("sqlite-memory-mcp")``. The v3.9.3
    ship cycle had this very bug: editable MAPPING was frozen at 3.7.2
    while pyproject.toml claimed 3.9.3.
    """
    import importlib.metadata

    manifest = _load_manifest()
    declared_version = manifest["project"]["version"]
    try:
        installed_version = importlib.metadata.version("sqlite-memory-mcp")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip(
            "sqlite-memory-mcp not installed in the current Python "
            "environment — skipping metadata-version probe. CI's "
            "editable-install gate covers this contract."
        )
    assert installed_version == declared_version, (
        f"Installed dist-info metadata version "
        f"{installed_version!r} does not match pyproject.toml "
        f"[project] version {declared_version!r}. Run "
        f"`pip install -e .` to refresh the metadata."
    )


def test_runtime_dunder_version_matches_pyproject():
    """The in-tree runtime version constant must not drift from pyproject."""
    manifest = _load_manifest()
    declared_version = manifest["project"]["version"]
    runtime_metadata = runpy.run_path(str(REPO_ROOT / "__init__.py"))

    assert runtime_metadata["__version__"] == declared_version


def test_package_dunder_version_matches_pyproject():
    """The import-level ``__version__`` must not drift from pyproject.

    Editable install metadata catches packaging drift, but direct imports from
    the checkout can still report stale versions if ``__init__.py`` is not
    updated. That stale value is especially misleading during local release
    gates and runtime diagnostics.
    """
    import __init__ as package_root

    manifest = _load_manifest()
    declared_version = manifest["project"]["version"]

    assert package_root.__version__ == declared_version


_STDIO_MCP_ENTRYPOINTS = (
    "sqlite-memory-mcp",
    "sqlite-memory-core",
    "sqlite-memory-session",
    "sqlite-memory-tasks",
    "sqlite-memory-bridge",
    "sqlite-memory-collab",
    "sqlite-memory-entity",
    "sqlite-memory-intel",
    "sqlite-memory-unified",
)


@pytest.mark.parametrize("entrypoint", _STDIO_MCP_ENTRYPOINTS)
def test_stdio_mcp_entrypoint_starts_without_module_errors(tmp_path, entrypoint):
    """Each stdio MCP console script (installed via
    pyproject ``[project.scripts]``) MUST start without raising a
    ``ModuleNotFoundError`` or any other unhandled Python exception
    when invoked from an arbitrary cwd.

    Per msg:db0b9001: it is a stdio MCP server, so it may block waiting
    for protocol messages on stdin.  The probe is implemented with
    ``subprocess.Popen`` rather than a platform shell's ``timeout``
    command: on Windows ``timeout.exe`` is an interactive delay utility,
    not GNU coreutils, and rejects the executable argument before the
    MCP server is started.  A bounded Python-side wait keeps this gate
    portable while preserving the real foreign-cwd entrypoint path.
    """
    # Locate the installed entry-point binary. ``shutil.which`` falls
    # back to ~/.local/bin in user-site installs.
    import shutil

    binary = shutil.which(entrypoint)
    if binary is None:
        pytest.skip(
            f"{entrypoint} not on PATH — likely a CI sandbox "
            "without the editable install. The manifest tests above "
            "cover the underlying contract anyway."
        )

    process = subprocess.Popen(
        [binary],
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Close stdin immediately so MCP server's stdio transport can
        # detect EOF rather than blocking forever.
        stdin=subprocess.DEVNULL,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=3)

    # EOF-aware servers normally exit 0.  A server that deliberately keeps
    # serving is terminated by this test and is also acceptable; only a
    # spontaneous non-zero exit represents a boot failure.
    if not timed_out:
        assert process.returncode == 0, (
            f"{entrypoint} exited with unexpected code "
            f"{process.returncode}. stderr:\n{stderr}\n"
            f"stdout:\n{stdout}"
        )
    # The critical assertion: stderr must NOT show any Python import
    # failure. Allowed: FastMCP banner, transport.py info logs, the
    # ``Starting MCP server`` message.
    combined = (stderr or "") + "\n" + (stdout or "")
    forbidden = ("Traceback", "ModuleNotFoundError", "ImportError")
    hits = [token for token in forbidden if token in combined]
    assert not hits, (
        f"{entrypoint} boot surfaced forbidden tokens "
        f"{hits} in stderr/stdout. This means a manifest regression "
        f"— a module imported during entrypoint boot is no longer "
        f"reachable via the editable MAPPING.\n\n"
        f"=== stderr ===\n{stderr}\n"
        f"=== stdout ===\n{stdout}"
    )
