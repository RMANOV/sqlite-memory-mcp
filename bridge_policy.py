"""Explicit authority for the private, complete trusted-device bridge.

This protects the bridge tool boundary, not arbitrary code running as its OS
owner. A private repository check cannot hide already published Git history or
enumerate account-level credentials. Each network attempt uses a fresh check;
remote permissions can still change after that check.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.parse import urlsplit

_NOWIN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

# These affect which repository/index/object namespace Git operates on. The
# policy describes a normal self-contained checkout, not an ambient override.
_GIT_REPOSITORY_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_PREFIX",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    }
)


class BridgePolicyError(RuntimeError):
    """The destination or reader set is not currently authorized."""


def blocked_result(exc: Exception) -> dict:
    return {
        "entities": 0,
        "tasks": 0,
        "pushed": False,
        "imported_new": 0,
        "imported_updated": 0,
        "blocked_by_bridge_policy": True,
        "message": f"Bridge policy refused sync: {exc}",
    }


def policy_path() -> Path:
    configured = os.environ.get("SQLITE_MEMORY_BRIDGE_POLICY")
    if configured:
        return Path(configured).expanduser()
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "sqlite-memory-mcp"
        / "bridge-policy.json"
    )


def _positive_id(value, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise BridgePolicyError(f"invalid {label}")
    return value


def _resolved_path(value, label: str) -> Path:
    try:
        return Path(value).resolve()
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise BridgePolicyError(f"invalid {label}") from exc


def _repository_from_url(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        raise BridgePolicyError("invalid GitHub remote URL")
    if value.startswith("git@github.com:"):
        path = value[len("git@github.com:") :]
    else:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise BridgePolicyError("invalid GitHub remote URL") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
            or port
            or parsed.query
            or parsed.fragment
        ):
            raise BridgePolicyError(
                "only canonical github.com SSH or HTTPS URLs are supported"
            )
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", path):
        raise BridgePolicyError("invalid GitHub repository identity")
    return path


def _git_read(repo_dir: str, *args: str, missing_ok: bool = False) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            **_NOWIN,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgePolicyError("cannot inspect Git routing") from exc
    if missing_ok and result.returncode == 1:
        return ""
    if result.returncode != 0:
        raise BridgePolicyError("cannot inspect Git routing")
    return result.stdout.strip()


def _load_policy(repo_dir: str) -> dict:
    path = None
    try:
        path = policy_path()
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise BridgePolicyError("policy must be a bounded regular JSON file")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise BridgePolicyError(
                "policy must be owner-controlled and not group/world writable"
            )
        if _resolved_path(path, "policy path").is_relative_to(
            _resolved_path(repo_dir, "bridge path")
        ):
            raise BridgePolicyError(
                "bridge policy must be global, outside the transport repository"
            )
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError) as exc:
        raise BridgePolicyError(f"missing or invalid global policy: {path}") from exc
    if (
        not isinstance(policy, dict)
        or type(policy.get("version")) is not int
        or policy["version"] != 1
    ):
        raise BridgePolicyError("unsupported bridge policy version")
    if policy.get("mode") != "trusted_device":
        raise BridgePolicyError("explicit mode='trusted_device' is required")
    configured_path = policy.get("bridge_path")
    if not isinstance(configured_path, str) or not Path(configured_path).is_absolute():
        raise BridgePolicyError("policy requires an absolute bridge_path")
    approved_root = _resolved_path(configured_path, "policy bridge_path")
    if str(approved_root) != configured_path:
        raise BridgePolicyError("policy bridge_path must be its resolved realpath")
    if approved_root != _resolved_path(repo_dir, "bridge path"):
        raise BridgePolicyError("bridge realpath differs from the approved destination")
    repository = _repository_from_url(policy.get("remote_url"))
    if policy.get("repository") != repository:
        raise BridgePolicyError("policy repository and remote_url disagree")
    _positive_id(policy.get("repository_id"), "repository_id")
    _positive_id(policy.get("owner_id"), "owner_id")
    readers = policy.get("allowed_collaborators")
    if (
        not isinstance(readers, list)
        or not readers
        or any(
            not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", x)
            for x in readers
        )
        or len({x.casefold() for x in readers}) != len(readers)
        or repository.split("/")[0].casefold() not in {x.casefold() for x in readers}
    ):
        raise BridgePolicyError(
            "policy requires a distinct collaborator allowlist including the owner"
        )
    keys = policy.get("allowed_deploy_key_ids")
    if (
        not isinstance(keys, list)
        or any(type(x) is not int or x <= 0 for x in keys)
        or len(set(keys)) != len(keys)
    ):
        raise BridgePolicyError("policy requires a distinct deploy-key ID allowlist")
    return policy


def _validate_git_checkout(repo_dir: str) -> None:
    """Bind the effective Git tree and index to the approved checkout.

    Linked worktrees/separate git directories require a different explicit
    policy contract; accepting their outside index implicitly would recreate
    the export boundary defect. No routing variable is silently removed.
    """
    overrides = sorted(
        name
        for name in os.environ
        if name in _GIT_REPOSITORY_ENV
        or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    )
    if overrides:
        raise BridgePolicyError(
            "Git repository-routing environment is not authorized: "
            + ", ".join(overrides)
        )
    root = _resolved_path(repo_dir, "bridge path")
    expected_git = root / ".git"
    if _git_read(repo_dir, "rev-parse", "--is-bare-repository") != "false":
        raise BridgePolicyError("bridge must be a non-bare working checkout")
    if (
        _resolved_path(
            _git_read(repo_dir, "rev-parse", "--show-toplevel"), "Git worktree"
        )
        != root
    ):
        raise BridgePolicyError(
            "effective Git worktree differs from approved bridge_path"
        )
    actual_git = _resolved_path(
        _git_read(repo_dir, "rev-parse", "--absolute-git-dir"), "Git directory"
    )
    common_git = _resolved_path(
        _git_read(repo_dir, "rev-parse", "--path-format=absolute", "--git-common-dir"),
        "Git common directory",
    )
    if actual_git != expected_git or common_git != expected_git:
        raise BridgePolicyError(
            "bridge requires its own .git directory inside the approved checkout"
        )
    actual_index = _resolved_path(
        _git_read(
            repo_dir, "rev-parse", "--path-format=absolute", "--git-path", "index"
        ),
        "Git index",
    )
    if actual_index != expected_git / "index":
        raise BridgePolicyError("effective Git index escapes the approved checkout")


def _github_json(endpoint: str, *, pages: bool = False):
    # Pin both command and environment; GH_HOST cannot certify another host.
    command = ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint]
    if pages:
        command += ["--paginate", "--slurp"]
    env = dict(os.environ, GH_HOST="github.com", GH_PROMPT_DISABLED="1")
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30, env=env, **_NOWIN
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BridgePolicyError("GitHub access verification unavailable") from exc
    if result.returncode != 0 or len(result.stdout) > 16 * 1024 * 1024:
        raise BridgePolicyError(
            "GitHub access verification failed; no cached approval is used"
        )
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise BridgePolicyError("malformed GitHub access response") from exc
    if not pages:
        if not isinstance(data, dict):
            raise BridgePolicyError("malformed GitHub repository response")
        return data
    if (
        not isinstance(data, list)
        or not data
        or any(not isinstance(page, list) or len(page) > 100 for page in data)
        or any(not isinstance(row, dict) for page in data for row in page)
    ):
        raise BridgePolicyError("malformed paginated GitHub access response")
    return [row for page in data for row in page]


def authorize_bridge(repo_dir: str) -> dict:
    """Read-only check of configuration, actual Git routing and current readers."""
    policy = _load_policy(repo_dir)
    _validate_git_checkout(repo_dir)
    expected = policy["remote_url"]
    # Refuse ambiguous/default routing rather than trying to emulate all of Git.
    routing = _git_read(
        repo_dir,
        "config",
        "--get-regexp",
        r"^(url\..*\.(insteadof|pushinsteadof)|remote\.pushdefault|branch\..*\.pushremote)$",
        missing_ok=True,
    )
    if routing:
        raise BridgePolicyError(
            "URL rewriting or alternate push routing is not authorized"
        )
    for arguments in (
        ("config", "--get-all", "remote.origin.url"),
        ("remote", "get-url", "--all", "origin"),
        ("remote", "get-url", "--push", "--all", "origin"),
    ):
        if _git_read(repo_dir, *arguments).splitlines() != [expected]:
            raise BridgePolicyError(
                "fetch/push destination differs from the approved single URL"
            )
    metadata = _github_json(f"repos/{policy['repository']}")
    owner = metadata.get("owner")
    permissions = metadata.get("permissions")
    if (
        type(metadata.get("id")) is not int
        or metadata["id"] != policy["repository_id"]
        or metadata.get("full_name") != policy["repository"]
        or metadata.get("private") is not True
        or metadata.get("visibility") != "private"
        or not isinstance(owner, dict)
        or type(owner.get("id")) is not int
        or owner["id"] != policy["owner_id"]
        or owner.get("login") != policy["repository"].split("/")[0]
        or not isinstance(permissions, dict)
        or permissions.get("admin") is not True
    ):
        raise BridgePolicyError("repository identity or private visibility changed")
    prefix = f"repos/{policy['repository']}"
    collaborators = _github_json(
        prefix + "/collaborators?affiliation=all&per_page=100", pages=True
    )
    allowed = {login.casefold() for login in policy["allowed_collaborators"]}
    actual = set()
    for row in collaborators:
        login, permissions = row.get("login"), row.get("permissions")
        if (
            not isinstance(login, str)
            or not login
            or not isinstance(permissions, dict)
            or type(permissions.get("pull")) is not bool
        ):
            raise BridgePolicyError("malformed collaborator permissions")
        actual.add(login.casefold())
    if (
        policy["repository"].split("/")[0].casefold() not in actual
        or not actual <= allowed
    ):
        raise BridgePolicyError("repository has an unapproved collaborator")
    keys = _github_json(prefix + "/keys?per_page=100", pages=True)
    actual_keys = {_positive_id(row.get("id"), "deploy key ID") for row in keys}
    if not actual_keys <= set(policy["allowed_deploy_key_ids"]):
        raise BridgePolicyError("repository has an unapproved deploy key")
    invitations = _github_json(prefix + "/invitations?per_page=100", pages=True)
    if invitations:
        raise BridgePolicyError(
            "pending repository invitations require explicit access review"
        )
    return policy


def run_network_git(
    repo_dir: str, operation: str, *, timeout: int = 30, max_retries: int = 3
) -> subprocess.CompletedProcess:
    """Pin fetch/push to the approved repo and re-authorize every actual attempt."""
    if operation not in ("fetch", "push") or not 1 <= max_retries <= 3:
        raise BridgePolicyError("unsupported bridge network operation")
    last = None
    for attempt in range(max_retries):
        policy = authorize_bridge(repo_dir)
        remote = policy["remote_url"]
        arguments = (
            ["fetch", "--no-tags", remote, "+refs/heads/main:refs/remotes/origin/main"]
            if operation == "fetch"
            else ["push", remote, "HEAD:refs/heads/main"]
        )
        command = [
            "git",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            *arguments,
        ]
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        try:
            last = subprocess.run(
                command,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                **_NOWIN,
            )
        except subprocess.TimeoutExpired:
            last = subprocess.CompletedProcess(
                command, 124, "", f"bridge {operation} timed out"
            )
        except OSError as exc:
            last = subprocess.CompletedProcess(command, 127, "", str(exc))
        if last.returncode == 0:
            return last
        if attempt + 1 < max_retries:
            time.sleep(2 ** (attempt + 1))
    return last
