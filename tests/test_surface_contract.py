"""Regression tests for the machine-readable surface contract."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db_utils import (
    CONTENT_FIELDS,
    EXTENDED_MEMORY_KEYS,
    METADATA_FIELDS,
    TASK_FILE_HYDRATION_FIELDS,
)
from surface_contract import (
    BRIDGE_ARTIFACT_SURFACE_CONTRACT,
    BRIDGE_GIT_STAGE_PATHS,
    BRIDGE_PAGES_PUBLISH_PATHS,
    EXTENDED_MEMORY_SURFACE_CONTRACT,
    SURFACE_CONTRACT_VERSION,
    TASK_FIELD_SURFACE_CONTRACT,
    TASK_HYDRATION_SURFACE_CONTRACT,
    build_surface_contract_report,
)


def test_surface_contract_covers_current_task_and_memory_constants():
    assert set(TASK_FIELD_SURFACE_CONTRACT) == set(METADATA_FIELDS) | set(
        CONTENT_FIELDS
    )
    assert set(TASK_HYDRATION_SURFACE_CONTRACT) == set(TASK_FILE_HYDRATION_FIELDS)
    assert set(EXTENDED_MEMORY_SURFACE_CONTRACT) == set(EXTENDED_MEMORY_KEYS)


def test_bridge_stage_and_pages_sets_follow_artifact_contract():
    expected_stage = tuple(
        path
        for path, spec in BRIDGE_ARTIFACT_SURFACE_CONTRACT.items()
        if spec.get("git_stage")
    )
    expected_pages = tuple(
        path
        for path, spec in BRIDGE_ARTIFACT_SURFACE_CONTRACT.items()
        if spec.get("pages_publish")
    )

    assert BRIDGE_GIT_STAGE_PATHS == expected_stage
    assert BRIDGE_PAGES_PUBLISH_PATHS == expected_pages
    assert "extended_memory/" in BRIDGE_GIT_STAGE_PATHS
    assert "extended_memory/" not in BRIDGE_PAGES_PUBLISH_PATHS


def test_surface_contract_report_is_machine_readable():
    report = build_surface_contract_report()

    assert report["version"] == SURFACE_CONTRACT_VERSION
    assert report["task_fields"]["description"]["search"] is True
    assert report["task_fields"]["notes"]["bridge_task_file"] is True
    assert report["task_hydration"]["_field_ts"]["bridge_index"] is True
    assert report["extended_memory"]["memory_artifacts"]["pages_publish"] is False
    assert report["bridge_git_stage_paths"] == list(BRIDGE_GIT_STAGE_PATHS)
