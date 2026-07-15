from __future__ import annotations

import pytest

from core.agent_policy import NODE_KIND_WINDOWS_AGENT
from core.environment_snapshot import (
    EnvironmentCheckResult,
    EnvironmentSnapshot,
    EnvironmentSnapshotError,
    inspect_selena_build_environment,
)


BINDING_ID = "workspace:sha256:" + "a" * 24


def test_ready_snapshot_is_path_free_and_satisfies_build_requirements():
    snapshot = EnvironmentSnapshot(
        agent_id="agent-alice-host1",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        project="ovrs25",
        workspace_binding_id=BINDING_ID,
        scope="selena_build",
        checks=(
            EnvironmentCheckResult("workspace_binding", "source.workspace.read", "passed"),
            EnvironmentCheckResult("selena_build_toolchain", "build.selena", "passed"),
        ),
        created_at=10,
        expires_at=310,
    )

    result = snapshot.to_dict()
    assert result["status"] == "ready"
    assert result["snapshot_id"].startswith("environment:sha256:")
    assert snapshot.satisfies(["workspace_binding", "selena_build_toolchain"])
    assert "C:\\" not in str(result)


def test_snapshot_rejects_path_leak_in_public_message():
    with pytest.raises(EnvironmentSnapshotError, match="absolute path"):
        EnvironmentCheckResult(
            "workspace_binding",
            "source.workspace.read",
            "failed",
            message="C:/secret/workspace is missing",
        )


def test_build_environment_inspection_returns_ready_snapshot_without_running_build():
    calls = []

    def prepare(payload, store):
        calls.append((dict(payload), store))
        return object()

    snapshot = inspect_selena_build_environment(
        {"project": "ovrs25", "workspace_binding_id": BINDING_ID, "build_mode": "Release"},
        object(),
        agent_id="agent-alice-host1",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        now_fn=lambda: 100,
        prepare_fn=prepare,
    )

    assert calls
    assert snapshot.status == "ready"
    assert snapshot.expires_at == 400
    assert snapshot.satisfies(["workspace_binding", "selena_build_toolchain", "artifact_local_staging"])


def test_build_environment_inspection_returns_blocked_path_free_failure():
    def prepare(payload, store):
        raise ValueError("configured workspace does not match binding")

    snapshot = inspect_selena_build_environment(
        {"project": "ovrs25", "workspace_binding_id": BINDING_ID, "build_mode": "Release"},
        object(),
        agent_id="agent-alice-host1",
        node_kind=NODE_KIND_WINDOWS_AGENT,
        now_fn=lambda: 100,
        prepare_fn=prepare,
    )

    assert snapshot.status == "blocked"
    assert snapshot.checks[0].code == "selena_build_environment_unavailable"
