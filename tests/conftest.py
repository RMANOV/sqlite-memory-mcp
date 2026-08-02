"""Filesystem isolation for the whole test suite.

The suite used to run against the operator's live ``~/.claude/memory``: a
440 MB ``memory.db``, a git bridge repo, and a ``server.log`` shared with seven
running MCP server processes. ``SQLITE_MEMORY_LOG_DIR`` alone does not fix that
-- it only covers :func:`db_utils.setup_logger`, and a measured 1893 bytes still
reached ``$HOME`` because roughly twenty tests spawn subprocesses whose imports
build their own paths from ``~``, and because ``db_utils.DB_PATH`` /
``BRIDGE_REPO`` / ``TASK_ATTACHMENT_ROOT`` are resolved from ``~`` at *import*
time.

The load-bearing protection is therefore ``HOME``. This module redirects it
before pytest imports any test module -- conftest is imported first, which is
the only point early enough for the import-time constants above.

Redirecting ``HOME`` on its own breaks the run: ``~/.local/lib/pythonX.Y/
site-packages`` is where this project is installed, so every ``subprocess.run(
[sys.executable, "-c", "import server"])`` child dies with ModuleNotFoundError.
``PYTHONUSERBASE`` (and ``PYTHONPATH`` as a belt-and-braces fallback for
interpreters started with user-site disabled) pins that back to the real home.

Deliberately *not* done: the repository root is not added to the children's
``PYTHONPATH``. Those subprocesses resolve ``server``/``task_server`` through
the editable install, i.e. from whichever checkout is installed, and that is
pre-existing behaviour this file has no business silently changing.
"""

from __future__ import annotations

import os
import shutil
import site
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# Captured before anything below touches the environment. ``site`` resolved
# these at interpreter start-up, so they still describe the real user.
_REAL_USER_BASE = site.USER_BASE or os.path.expanduser("~/.local")
_REAL_USER_SITE = site.getusersitepackages()


def _real_home() -> Path:
    """The account's home directory, read from the password database.

    Deliberately not ``expanduser``/``$HOME``: this value is what the guard
    below compares against, and it has to stay correct after this module has
    rewritten ``$HOME`` (and when a caller had already rewritten it).
    """
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # pragma: no cover - Windows / no passwd entry
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


_LIVE_MEMORY_DIR = _real_home() / ".claude" / "memory"

# A nested pytest (a test that shells out to ``python -m pytest``) inherits the
# parent's sandbox instead of building a second one it would then delete.
_SENTINEL = "SQLITE_MEMORY_TEST_HOME"
_INHERITED = bool(os.environ.get(_SENTINEL))

if _INHERITED:
    ISOLATED_HOME = Path(os.environ[_SENTINEL])
else:
    ISOLATED_HOME = Path(tempfile.mkdtemp(prefix="sqlite-memory-pytest-home-"))

_MEMORY_DIR = ISOLATED_HOME / ".claude" / "memory"


def _isolate() -> None:
    for sub in (
        _MEMORY_DIR,
        _MEMORY_DIR / "backups" / "auto",
        _MEMORY_DIR / "task_attachments",
        ISOLATED_HOME / "logs",
        ISOLATED_HOME / ".config",
        ISOLATED_HOME / ".local" / "state",
        ISOLATED_HOME / ".cache",
    ):
        sub.mkdir(parents=True, exist_ok=True)

    env = os.environ
    env[_SENTINEL] = str(ISOLATED_HOME)
    env["HOME"] = str(ISOLATED_HOME)
    env["USERPROFILE"] = str(ISOLATED_HOME)  # Windows equivalent of HOME
    env["XDG_CONFIG_HOME"] = str(ISOLATED_HOME / ".config")
    env["XDG_STATE_HOME"] = str(ISOLATED_HOME / ".local" / "state")
    env["XDG_CACHE_HOME"] = str(ISOLATED_HOME / ".cache")

    # Explicit, and identical to what the redirected HOME already implies. The
    # duplication is the point: a test that restores HOME still cannot reach the
    # live database, bridge repo or attachment store.
    env["SQLITE_MEMORY_DB"] = str(_MEMORY_DIR / "memory.db")
    env["BRIDGE_REPO"] = str(_MEMORY_DIR / "bridge")
    env["TASK_ATTACHMENT_ROOT"] = str(_MEMORY_DIR / "task_attachments")
    env["SQLITE_MEMORY_LOG_DIR"] = str(ISOLATED_HOME / "logs")
    env["SQLITE_MEMORY_BACKUP_DIR"] = str(_MEMORY_DIR / "backups" / "auto")

    # Keep the real interpreter's packages reachable now that HOME has moved.
    env["PYTHONUSERBASE"] = str(_REAL_USER_BASE)
    existing = [p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]
    if _REAL_USER_SITE and _REAL_USER_SITE not in existing:
        existing.append(_REAL_USER_SITE)
    env["PYTHONPATH"] = os.pathsep.join(existing)

    # git reads identity from $HOME/.gitconfig, which just moved. Provide a
    # fallback rather than GIT_AUTHOR_*/GIT_COMMITTER_* env vars: those would
    # *override* the repo-local `git config user.email` that several bridge
    # tests set for themselves. Credential helpers are intentionally not
    # carried over -- a test must not be able to authenticate as the operator.
    gitconfig = ISOLATED_HOME / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text(
            "[user]\n\tname = sqlite-memory tests\n\temail = tests@localhost\n",
            encoding="utf-8",
        )

    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))


_isolate()


def pytest_report_header() -> str:
    state = "inherited" if _INHERITED else "created"
    return f"sqlite-memory isolation: HOME={ISOLATED_HOME} ({state})"


@pytest.fixture(scope="session", autouse=True)
def _isolation_is_real() -> None:
    """Fail the run loudly if the sandbox above ever stops taking effect.

    Imported here rather than at module scope: db_utils resolves its path
    constants at import time, and importing it before :func:`_isolate` has run
    is exactly the bug this guards against.
    """
    import db_utils

    live = _LIVE_MEMORY_DIR
    for label, value in (
        ("HOME", os.environ.get("HOME")),
        ("DB_PATH", db_utils.DB_PATH),
        ("BRIDGE_REPO", db_utils.BRIDGE_REPO),
        ("TASK_ATTACHMENT_ROOT", db_utils.TASK_ATTACHMENT_ROOT),
        ("BACKUP_ROOT", db_utils.BACKUP_ROOT),
    ):
        resolved = Path(str(value)).expanduser().resolve()
        assert resolved == ISOLATED_HOME or ISOLATED_HOME in resolved.parents, (
            f"{label}={value!r} escapes the test sandbox {ISOLATED_HOME}"
        )
        assert resolved != live and live not in resolved.parents, (
            f"{label}={value!r} points into the operator's live memory directory"
        )


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    if not _INHERITED:
        shutil.rmtree(ISOLATED_HOME, ignore_errors=True)
