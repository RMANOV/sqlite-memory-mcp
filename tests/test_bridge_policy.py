"""Exercise the real policy with real local Git and simulated GitHub wire I/O."""

import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

import bridge_policy as policy
import bridge_sync_worker as worker
import db_utils
from schema import init_db


@pytest.fixture
def approved(tmp_path, monkeypatch):
    repo = tmp_path / "bridge"
    repo.mkdir()
    real_run = subprocess.run

    def git(*args):
        return real_run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-qm", "synthetic initial commit")
    git("remote", "add", "origin", "git@github.com:Example/memory.git")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    approved_policy = {
        "version": 1,
        "mode": "trusted_device",
        "bridge_path": str(repo.resolve()),
        "remote_url": "git@github.com:Example/memory.git",
        "repository": "Example/memory",
        "repository_id": 123,
        "owner_id": 7,
        "allowed_collaborators": ["Example"],
        "allowed_deploy_key_ids": [],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(approved_policy))
    path.chmod(0o600)
    monkeypatch.setenv("SQLITE_MEMORY_BRIDGE_POLICY", str(path))
    responses = {
        "repos/Example/memory": {
            "id": 123,
            "full_name": "Example/memory",
            "private": True,
            "visibility": "private",
            "owner": {"id": 7, "login": "Example"},
            "permissions": {"admin": True},
        },
        "repos/Example/memory/collaborators?affiliation=all&per_page=100": [
            [{"login": "Example", "permissions": {"pull": True}}]
        ],
        "repos/Example/memory/keys?per_page=100": [[]],
        "repos/Example/memory/invitations?per_page=100": [[]],
    }
    calls = []
    network = []
    behavior = {"network_code": 0, "after_network": None}

    def wire(command, **kwargs):
        if command[0] == "gh":
            calls.append((list(command), kwargs.get("env", {}).get("GH_HOST")))
            endpoint = command[command.index("GET") + 1]
            value = responses[endpoint]
            if isinstance(value, subprocess.CompletedProcess):
                return value
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        if command[0] == "git" and any(op in command for op in ("fetch", "push")):
            network.append(list(command))
            callback = behavior["after_network"]
            if callback:
                callback()
            return subprocess.CompletedProcess(
                command,
                behavior["network_code"],
                "",
                "synthetic failed push" if behavior["network_code"] else "",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(policy.subprocess, "run", wire)
    monkeypatch.setattr(policy.time, "sleep", lambda _: None)
    monkeypatch.setattr(worker, "_push_backoff_until", 0)
    return {
        "repo": repo,
        "path": path,
        "policy": approved_policy,
        "responses": responses,
        "calls": calls,
        "network": network,
        "behavior": behavior,
        "git": git,
    }


def test_policy_validates_pinned_identity_and_all_reader_endpoints(
    approved, monkeypatch
):
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    result = policy.authorize_bridge(str(approved["repo"]))
    assert result["repository_id"] == 123
    assert len(approved["calls"]) == 4
    assert all(
        command[command.index("--hostname") + 1] == "github.com"
        and host == "github.com"
        for command, host in approved["calls"]
    )
    for command, _ in approved["calls"][1:]:
        assert "--paginate" in command and "--slurp" in command


@pytest.mark.parametrize(
    "change",
    [
        {"private": False, "visibility": "public"},
        {"id": 124},
        {"id": True},
        {"full_name": "Other/memory"},
        {"owner": {"id": 8, "login": "Example"}},
        {"permissions": {"admin": False}},
        {"visibility": None},
    ],
)
def test_remote_identity_visibility_or_verification_permission_change_denies(
    approved, change
):
    approved["responses"]["repos/Example/memory"].update(change)
    with pytest.raises(policy.BridgePolicyError):
        policy.authorize_bridge(str(approved["repo"]))
    assert approved["network"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "shared"),
        ("mode", None),
        ("version", True),
        ("repository_id", "123"),
        ("owner_id", 0),
        ("remote_url", "https://github.com:notaport/Example/memory"),
        ("remote_url", "https://token@github.com/Example/memory"),
        ("allowed_collaborators", []),
        ("allowed_deploy_key_ids", [True]),
    ],
)
def test_malformed_policy_is_structured_denial_without_domain_writes(
    approved, field, value
):
    data = copy.deepcopy(approved["policy"])
    data[field] = value
    approved["path"].write_text(json.dumps(data))
    result = worker.main(
        bridge_repo=str(approved["repo"]),
        db_path=str(approved["repo"].parent / "absent.db"),
    )
    assert result.get("blocked_by_bridge_policy") is True
    assert not (approved["repo"].parent / "absent.db").exists()
    assert approved["network"] == []


@pytest.mark.parametrize(
    "suffix,value",
    [
        (
            "collaborators?affiliation=all&per_page=100",
            [
                [{"login": "Example", "permissions": {"pull": True}}],
                [{"login": "Intruder", "permissions": {"pull": True}}],
            ],
        ),
        ("collaborators?affiliation=all&per_page=100", [[{"login": "Example"}]]),
        ("collaborators?affiliation=all&per_page=100", []),
        (
            "collaborators?affiliation=all&per_page=100",
            [
                [{"login": "Example", "permissions": {"pull": True}}],
                {"truncated": True},
            ],
        ),
        ("keys?per_page=100", [[], [{"id": 88}]]),
        ("keys?per_page=100", [[{"id": True}]]),
        ("invitations?per_page=100", [[], [{"id": 99}]]),
    ],
)
def test_complete_paginated_reader_set_or_malformed_response_denies(
    approved, suffix, value
):
    approved["responses"]["repos/Example/memory/" + suffix] = value
    with pytest.raises(policy.BridgePolicyError):
        policy.authorize_bridge(str(approved["repo"]))
    assert approved["network"] == []


def test_api_failure_never_reuses_previous_success(approved):
    policy.authorize_bridge(str(approved["repo"]))
    approved["responses"]["repos/Example/memory"] = subprocess.CompletedProcess(
        [], 1, "", "denied"
    )
    with pytest.raises(policy.BridgePolicyError):
        policy.run_network_git(str(approved["repo"]), "push")
    assert approved["network"] == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("remote.origin.pushurl", "git@github.com:Other/memory.git"),
        ("remote.pushDefault", "elsewhere"),
        ("branch.main.pushRemote", "elsewhere"),
        ("url.git@github.com:Other/.insteadOf", "git@github.com:Example/"),
        ("url.git@github.com:Other/.pushInsteadOf", "git@github.com:Example/"),
    ],
)
def test_changed_or_ambiguous_git_routing_denies(approved, key, value):
    approved["git"]("config", key, value)
    with pytest.raises(policy.BridgePolicyError):
        policy.run_network_git(str(approved["repo"]), "push")
    assert approved["network"] == []


def test_multiple_origin_urls_and_rebound_realpath_deny(approved, tmp_path):
    approved["git"](
        "config", "--add", "remote.origin.url", "git@github.com:Example/memory.git"
    )
    with pytest.raises(policy.BridgePolicyError):
        policy.authorize_bridge(str(approved["repo"]))
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(policy.BridgePolicyError):
        policy.authorize_bridge(str(other))


@pytest.mark.parametrize(
    "operation,ending",
    [
        (
            "fetch",
            [
                "fetch",
                "--no-tags",
                "git@github.com:Example/memory.git",
                "+refs/heads/main:refs/remotes/origin/main",
            ],
        ),
        ("push", ["push", "git@github.com:Example/memory.git", "HEAD:refs/heads/main"]),
    ],
)
def test_each_network_operation_uses_the_explicit_approved_url_and_refspec(
    approved, operation, ending
):
    result = policy.run_network_git(str(approved["repo"]), operation)
    assert result.returncode == 0
    assert len(approved["network"]) == 1
    assert approved["network"][0][-len(ending) :] == ending


@pytest.mark.parametrize("drift", ["private", "collaborator", "routing", "policy"])
def test_failed_push_rechecks_policy_and_audience_before_retry(approved, drift):
    approved["behavior"]["network_code"] = 1

    def changed():
        if drift == "private":
            approved["responses"]["repos/Example/memory"]["private"] = False
        elif drift == "collaborator":
            approved["responses"][
                "repos/Example/memory/collaborators?affiliation=all&per_page=100"
            ].append([{"login": "Intruder", "permissions": {"pull": True}}])
        elif drift == "routing":
            approved["git"](
                "config", "remote.origin.pushurl", "git@github.com:Other/memory.git"
            )
        else:
            approved["path"].unlink()

    approved["behavior"]["after_network"] = changed
    with pytest.raises(policy.BridgePolicyError):
        policy.run_network_git(str(approved["repo"]), "push")
    assert len(approved["network"]) == 1


def test_direct_locked_worker_denies_without_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_MEMORY_BRIDGE_POLICY", str(tmp_path / "missing"))
    result = worker._main_locked(
        None,
        str(tmp_path / "no.db"),
        str(tmp_path / "no-repo"),
        True,
        False,
        None,
        "synthetic",
    )
    assert result.get("blocked_by_bridge_policy") is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("ambient", [None, "0", "1"])
def test_real_trusted_worker_preserves_full_payload_and_never_publishes_other_channels(
    approved, tmp_path, monkeypatch, ambient
):
    if ambient is None:
        monkeypatch.delenv("BRIDGE_PRIVATE_ONLY", raising=False)
    else:
        monkeypatch.setenv("BRIDGE_PRIVATE_ONLY", ambient)
    database = str(tmp_path / "memory.db")
    init_db(database)
    with db_utils.get_conn(database) as conn:
        db_utils.create_task_with_ledger(
            conn,
            "private-task",
            "PRIVATE_TASK_MARKER",
            db_utils.now_iso(),
            description="PRIVATE_BODY_MARKER",
            project="private",
            visibility="pending_public",
            publish_requested_at="2020-01-01T00:00:00Z",
        )
        db_utils.record_memory_event(
            conn,
            event_type="test_private",
            aggregate_kind="private",
            aggregate_id="secret",
            tool_name="synthetic",
            old_value="PRIVATE_OLD_MARKER",
            new_value="PRIVATE_NEW_MARKER",
            payload={"synthetic": "PRIVATE_PAYLOAD_MARKER"},
        )

    def forbidden(*args, **kwargs):
        pytest.fail(
            "Trusted-device permission reached an unrelated publication channel"
        )

    for name in (
        "publish_peer_payloads",
        "create_public_release",
        "_deploy_pages_privacy_shell",
        "promote_pending_public_entities",
    ):
        monkeypatch.setattr(worker, name, forbidden)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "synthetic-not-secret")
    result = worker.main(
        bridge_repo=str(approved["repo"]), db_path=database, force=True
    )
    assert result["pushed"] is True, result
    assert result["private_only"] is True
    assert (
        result["sync_scope"]["tasks_attachments_extended_memory"]
        == "complete_private_device_replica"
    )
    assert (
        "PRIVATE_BODY_MARKER"
        in (approved["repo"] / "tasks/private-task.json").read_text()
    )
    events = (approved["repo"] / "extended_memory/memory_events.json").read_text()
    assert all(
        marker in events
        for marker in (
            "PRIVATE_OLD_MARKER",
            "PRIVATE_NEW_MARKER",
            "PRIVATE_PAYLOAD_MARKER",
        )
    )
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT visibility FROM tasks WHERE id='private-task'"
            ).fetchone()[0]
            == "pending_public"
        )
    assert result["promoted_to_public"] == {"entities": 0, "tasks": 0}


def test_custom_entity_prefix_matches_files_index_and_relation_ids(tmp_path):
    database = str(tmp_path / "memory.db")
    init_db(database)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DELETE FROM entities")
        conn.executemany(
            "INSERT INTO entities(name,entity_type,project,created_at,updated_at) VALUES (?,?,?,?,?)",
            [
                ("team-one", "concept", "team_abc", "now", "now"),
                ("shared-one", "concept", "shared", "now", "now"),
                ("wildcard-escape", "concept", "teamXabc", "now", "now"),
            ],
        )
        _, legacy_ids = worker._export_entities(conn, "team_")
        file_ids, rows = db_utils.export_entity_files(
            conn, str(bridge), project_prefix="team_"
        )
        db_utils.export_entities_index(conn, str(bridge), rows=rows)
    assert set(file_ids) == legacy_ids
    assert [r["name"] for r in rows] == ["team-one"]
    assert len(list((bridge / "entities").glob("*.json"))) == 1
    assert "shared-one" not in (bridge / "entities_index.json").read_text()


def test_hook_denies_before_lock_and_recovery_network(tmp_path, monkeypatch):
    source = Path(worker.__file__).parent / "hooks/bridge_sync_worker.py"
    spec = importlib.util.spec_from_file_location("hook_policy_case", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.BRIDGE_REPO = str(tmp_path / "bridge")
    monkeypatch.setenv("SQLITE_MEMORY_BRIDGE_POLICY", str(tmp_path / "missing-policy"))
    monkeypatch.setattr(module, "notify", lambda *a: None)
    monkeypatch.setattr(
        module, "acquire_lock", lambda: pytest.fail("policy denial attempted lock")
    )
    module.main()
    with pytest.raises(policy.BridgePolicyError):
        module.fix_remote_ahead()


def test_non_fast_forward_recovery_cannot_reset_after_access_drift(
    approved, monkeypatch
):
    def changed():
        approved["responses"]["repos/Example/memory"]["private"] = False

    approved["behavior"]["after_network"] = changed
    calls = []
    monkeypatch.setattr(worker, "git_retry", lambda *a, **kw: calls.append(a))
    with pytest.raises(policy.BridgePolicyError):
        worker._recover_non_fast_forward_push(str(approved["repo"]))
    assert calls == []


def test_detached_head_fallback_fetch_uses_current_authority(approved, monkeypatch):
    calls = []

    def local_git(repo, *args, **kwargs):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return subprocess.CompletedProcess(args, 0, "HEAD", "")
        if args == ("checkout", "main"):
            approved["responses"]["repos/Example/memory"]["private"] = False
            return subprocess.CompletedProcess(args, 1, "", "missing branch")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(db_utils, "_bridge_git_operation_blocker", lambda _: None)
    monkeypatch.setattr(db_utils, "git_run", local_git)
    with pytest.raises(policy.BridgePolicyError):
        db_utils.ensure_bridge_repo_ready(str(approved["repo"]))
    assert approved["network"] == []
    assert not any(c[:2] == ("checkout", "-B") for c in calls)


def test_worker_reports_late_denial_without_push_or_delivery_stamp(approved, tmp_path):
    database = str(tmp_path / "memory.db")
    init_db(database)
    with db_utils.get_conn(database) as conn:
        db_utils.create_task_with_ledger(
            conn, "late-task", "Synthetic", db_utils.now_iso()
        )

    def after_authorized_fetch():
        approved["responses"]["repos/Example/memory"]["private"] = False

    approved["behavior"]["after_network"] = after_authorized_fetch
    result = worker.main(
        bridge_repo=str(approved["repo"]), db_path=database, force=True
    )
    assert result.get("blocked_by_bridge_policy") is True
    assert result.get("local_changes_may_have_occurred") is True
    assert "imported_new" not in result and "imported_updated" not in result
    assert len(approved["network"]) == 1
    assert "fetch" in approved["network"][0]
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT value FROM bridge_meta WHERE key='last_push_at'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize(
    "route", ["environment_git_dir", "core_worktree", "environment_index"]
)
def test_effective_git_checkout_and_index_cannot_escape_approved_realpath(
    approved, tmp_path, monkeypatch, route
):
    other = tmp_path / "unapproved"
    other.mkdir()
    # A second real repository has the same remote URL, so URL checks alone
    # cannot tell whether Git is operating on the approved checkout.
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(other),
            "remote",
            "add",
            "origin",
            approved["policy"]["remote_url"],
        ],
        check=True,
    )
    if route == "environment_git_dir":
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(other))
    elif route == "core_worktree":
        approved["git"]("config", "core.worktree", str(other))
    else:
        monkeypatch.setenv("GIT_INDEX_FILE", str(other / "external-index"))
    with pytest.raises(policy.BridgePolicyError):
        policy.run_network_git(str(approved["repo"]), "push")
    assert approved["network"] == []


def test_nul_in_approved_path_returns_structured_denial(approved):
    data = copy.deepcopy(approved["policy"])
    data["bridge_path"] = "/invalid\u0000path"
    approved["path"].write_text(json.dumps(data))
    result = worker.main(bridge_repo=str(approved["repo"]))
    assert result.get("blocked_by_bridge_policy") is True
    assert approved["network"] == []
