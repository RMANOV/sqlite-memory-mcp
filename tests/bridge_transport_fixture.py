"""Explicit trusted transport for pre-existing merge/export regression suites.

Those tests exercise local bare remotes or Git doubles, not remote authority.
The policy tests deliberately do not use this fixture and run the real guard.
"""

import pytest


@pytest.fixture
def trusted_device_transport(monkeypatch):
    import bridge_policy
    import bridge_sync_worker

    monkeypatch.setattr(
        bridge_policy, "authorize_bridge", lambda repo: {"mode": "trusted_device"}
    )

    def network(repo, operation, *, timeout=30, max_retries=3):
        args = ("fetch", "origin", "main") if operation == "fetch" else ("push",)
        return bridge_sync_worker.git_retry(
            repo, *args, timeout=timeout, max_retries=max_retries
        )

    monkeypatch.setattr(bridge_policy, "run_network_git", network)
