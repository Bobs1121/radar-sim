"""Focused Windows Agent CLI deployment-mode policy tests."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

import cli.agent as agent_module
from core.agent_policy import AgentPolicyError


def _parse(*argv: str):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    agent_module.register(subparsers)
    return parser.parse_args(["agent", *argv])


def test_agent_parser_defaults_to_light_mode():
    args = _parse()
    assert args.windows_mode == "light"
    assert args.capability == []
    assert args.agent_token == ""
    assert args.api_token == ""


def test_control_client_sends_agent_bearer_token(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"task": null}'

    def fake_urlopen(request, **_kwargs):
        captured["authorization"] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(__import__("urllib.request", fromlist=[""]), "urlopen", fake_urlopen)
    client = agent_module._ControlClient(
        "http://control.invalid", timeout=1,
        token="agent-token-0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    )
    assert client.poll("agent-1") == {"task": None}
    assert captured["authorization"] == "Bearer agent-token-0123456789_ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def test_local_config_asset_is_downloaded_into_agent_cache_and_authorized(tmp_path):
    digest = "a" * 64
    asset_ref = f"config-asset://sha256/{digest}"
    cached = tmp_path / "adapter" / f"{digest}.txt"
    cached.parent.mkdir()
    cached.write_text("adapter=1\n", encoding="utf-8")
    calls = []

    class FakeClient:
        def download_config_asset(self, asset_id, *, kind):
            calls.append((asset_id, kind))
            return cached

    class FakeAssets:
        def register(self, root):
            calls.append(("register", root))

    resolved = agent_module._materialize_local_config_asset(
        asset_ref,
        kind="adapter",
        assets=FakeAssets(),
        client=FakeClient(),
    )

    assert resolved == str(cached)
    assert calls == [(asset_ref, "adapter"), ("register", cached.parent)]


def test_light_and_full_defaults_match_runtime_boundary():
    mode, node_kind, light = agent_module._capabilities_for_mode(None)
    assert (mode, node_kind) == ("light", "windows_agent")
    assert "local.build_selena" in light
    assert "artifact.upload" in light
    assert "local.run_sim" not in light
    assert "cluster.run" not in light

    mode, node_kind, full = agent_module._capabilities_for_mode("full")
    assert (mode, node_kind) == ("full", "windows_full")
    assert "local.run_sim" in full
    assert "simulation.local" in full
    assert "cluster.run" not in full
    assert "cluster.gateway" not in full
    assert "source.workspace.recognize" in light


@pytest.mark.parametrize(
    "capability",
    ["*", "LOCAL.*", "local.run_sim", "cluster.run", "future.admin"],
)
def test_light_explicit_forbidden_or_unknown_capability_fails_fast(capability):
    with pytest.raises(AgentPolicyError):
        agent_module._capabilities_for_mode("light", [capability])


def test_full_explicit_capabilities_are_also_allowlisted():
    _, _, caps = agent_module._capabilities_for_mode(
        "full", ["LOCAL.CHECK", "local.run_sim"]
    )
    assert caps == ["local.check", "local.run_sim"]
    with pytest.raises(AgentPolicyError, match="windows_full"):
        agent_module._capabilities_for_mode("full", ["cluster.run"])


def test_run_registers_node_kind_and_mode_metadata(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def register_agent(self, **kwargs):
            calls.append(kwargs)
            return {"agent_id": kwargs["agent_id"]}

        def poll(self, _agent_id):
            return {"task": None}

    monkeypatch.setattr(agent_module, "_ControlClient", FakeClient)
    monkeypatch.setattr(
        agent_module,
        "_public_workspace_bindings",
        lambda: [{"id": "workspace:sha256:" + "a" * 24, "project": "ovrs25", "healthy": True}],
    )
    args = SimpleNamespace(
        server_url="http://control.invalid",
        request_timeout=1,
        capability=[],
        windows_mode="light",
        hostname="host-a",
        name="agent-a",
        agent_id="agent-a",
        platform_name="Windows",
        poll_interval=0.01,
        heartbeat_interval=1,
        once=True,
    )
    assert agent_module.run(args, None) == 0
    registration = calls[0]
    assert registration["metadata"]["node_kind"] == "windows_agent"
    assert registration["metadata"]["windows_mode"] == "light"
    assert "cwd" not in registration["metadata"]
    assert registration["metadata"]["workspace_bindings"][0]["project"] == "ovrs25"
    assert "local.run_sim" not in registration["capabilities"]
    assert "cluster.run" not in registration["capabilities"]


def test_light_forbidden_explicit_capability_fails_before_http(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("HTTP client must not be constructed")

    monkeypatch.setattr(agent_module, "_ControlClient", ForbiddenClient)
    args = SimpleNamespace(
        server_url="http://control.invalid",
        request_timeout=1,
        capability=["cluster.run"],
        windows_mode="light",
    )
    with pytest.raises(AgentPolicyError, match="forbidden capability"):
        agent_module.run(args, None)


def test_node_local_project_free_resolution_requires_authorized_binding(tmp_path, monkeypatch):
    from core.agent_bindings import AgentBindingStore
    from core.agent_asset_bindings import AgentAssetBindingStore
    from core.workspace_recognizer import WorkspaceRecognizer
    import core.agent_bindings as bindings_module
    import core.agent_asset_bindings as asset_bindings_module

    workspace = tmp_path / "workspace"
    output = workspace / "build"
    script = workspace / "apl" / "selena" / "jenkins_selena_build.bat"
    output.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    script.write_text("@echo off", encoding="utf-8")
    db = tmp_path / "bindings.db"
    monkeypatch.setattr(bindings_module, "default_agent_binding_db_path", lambda: db)
    monkeypatch.setattr(asset_bindings_module, "default_agent_binding_db_path", lambda: db)
    internal_project = WorkspaceRecognizer().recognize(
        str(workspace), str(script)
    ).internal_project
    binding = AgentBindingStore().register(internal_project, workspace, (output,))
    assets = tmp_path / "assets"
    assets.mkdir()
    runtime = assets / "Runtime.xml"
    adapter = assets / "adapter.txt"
    mat_filter = assets / "signals.filter"
    runtime.write_text("<runtime/>", encoding="utf-8")
    adapter.write_text("adapter", encoding="utf-8")
    mat_filter.write_text("filter", encoding="utf-8")
    asset_binding = AgentAssetBindingStore().register(assets)

    result = agent_module._resolve_v2_run_config(
        {"code_path": str(workspace), "build_script": str(script)}
    )
    assert result["status"] == "resolved"
    assert result["internal_project"] == internal_project
    assert result["workspace_binding_id"] == binding.binding_id
    assert result["adapter_key"] == "generic:selena-script"

    secured = agent_module._resolve_v2_run_config(
        {
            "contract": "user-run-config/2.0",
            "code_path": str(workspace),
            "build_script": str(script),
            "runtime_xml": str(runtime),
            "adapter_file": str(adapter),
            "mat_filter": str(mat_filter),
        }
    )
    assert set(secured["asset_bindings"]) == {"runtime_xml"}
    assert set(secured["asset_bindings"].values()) == {asset_binding.binding_id}

    monkeypatch.setattr("core.datasets.classify_data_path", lambda _path: "shared")
    shared = agent_module._resolve_v2_run_config(
        {
            "contract": "user-run-config/2.0",
            "code_path": str(workspace),
            "build_script": str(script),
            "runtime_xml": str(runtime),
            "data_path": str(tmp_path),
        }
    )
    assert shared["data_binding_id"] == ""

    with pytest.raises(ValueError, match="not uniquely authorized"):
        agent_module._resolve_v2_run_config({"code_path": str(tmp_path / "other")})


def test_light_execution_defense_reports_failure_without_spawning(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.logs = []
            self.results = []

        def append_logs(self, _task_id, lines):
            self.logs.extend(lines)

        def submit_result(self, _task_id, **kwargs):
            self.results.append(kwargs)

        def heartbeat(self, _agent_id, **_kwargs):
            return {"cancel_requested": False}

    monkeypatch.setattr(
        agent_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    client = FakeClient()
    task = {"task_id": "stage-1", "task_type": "cluster.run", "payload": {}}
    assert agent_module._run_task(
        client,
        "agent-a",
        task,
        heartbeat_interval=1,
        node_kind="windows_agent",
    ) == 1
    assert client.results[-1]["status"] == "failed"
    assert client.results[-1]["returncode"] == -1
    assert any("policy forbids" in line for line in client.logs)


def test_environment_check_is_node_local_and_does_not_spawn(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.logs = []
            self.results = []

        def append_logs(self, _task_id, lines):
            self.logs.extend(lines)

        def submit_result(self, _task_id, **kwargs):
            self.results.append(kwargs)

    monkeypatch.setattr(
        agent_module,
        "_check_v5_environment",
        lambda payload, **kwargs: {
            "snapshot_id": "environment:sha256:" + "a" * 64,
            "status": "ready",
            "project": payload["project"],
            "agent_id": kwargs["agent_id"],
            "node_kind": kwargs["node_kind"],
        },
    )
    monkeypatch.setattr(
        agent_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    client = FakeClient()
    task = {
        "task_id": "stage-env",
        "task_type": "environment_check",
        "stage_type": "environment_check",
        "payload": {"project": "ovrs25"},
    }

    assert agent_module._run_task(
        client,
        "agent-a",
        task,
        heartbeat_interval=1,
        node_kind="windows_agent",
    ) == 0
    assert client.results == [
        {
            "agent_id": "agent-a",
            "status": "succeeded",
            "returncode": 0,
            "result": {
                "environment_snapshot": {
                    "snapshot_id": "environment:sha256:" + "a" * 64,
                    "status": "ready",
                    "project": "ovrs25",
                    "agent_id": "agent-a",
                    "node_kind": "windows_agent",
                }
            },
        }
    ]


def test_register_artifact_uses_explicit_uploader_without_spawning(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.results = []
            self.logs = []

        def append_logs(self, _task_id, lines):
            self.logs.extend(lines)

        def submit_result(self, _task_id, **kwargs):
            self.results.append(kwargs)

        def heartbeat(self, _agent_id, **_kwargs):
            return {"cancel_requested": False}

    monkeypatch.setattr(
        agent_module,
        "_upload_v5_artifact",
        lambda client, payload, *, owner="": {
            "artifact": {"storage_ref": "shared://selena/ovrs25/team/a/selena.exe"},
            "storage_ref": "shared://selena/ovrs25/team/a/selena.exe",
            "build_evidence_ref": payload["build_evidence_ref"],
            "owner": owner,
        },
    )
    monkeypatch.setattr(
        agent_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    client = FakeClient()
    task = {
        "task_id": "stage-register",
        "task_type": "register_artifact",
        "stage_type": "register_artifact",
        "owner": "alice",
        "payload": {"build_evidence_ref": "stage-build:1"},
    }
    assert agent_module._run_task(
        client,
        "agent-a",
        task,
        heartbeat_interval=1,
        node_kind="windows_agent",
    ) == 0
    assert client.results[0]["status"] == "succeeded"
    assert client.results[0]["result"]["storage_ref"].startswith("shared://selena/")
    assert client.results[0]["result"]["owner"] == "alice"


def test_prepare_data_uses_authorized_lease_and_uploader_without_spawning(monkeypatch):
    import core.agent_data_bindings as binding_module
    import core.agent_data_lease as lease_module

    class FakeLeaseStore:
        uploaded = []

        def create(self, payload, bindings, *, stage_id, attempt):
            assert payload["data_binding_id"].startswith("data-root:")
            assert stage_id == "stage-data"
            assert attempt == 1
            return SimpleNamespace(
                lease_id="data-lease:sha256:" + "a" * 32,
                files=(SimpleNamespace(relative_path="a.MF4", size=1, checksum="sha256:" + "b" * 64),),
                project="ovrs25",
            )

        def mark_uploaded(self, lease_id, dataset_id):
            self.uploaded.append((lease_id, dataset_id))

    class FakeClient:
        def __init__(self):
            self.results = []
            self.logs = []

        def append_logs(self, _task_id, lines):
            self.logs.extend(lines)

        def heartbeat(self, _agent_id, **_kwargs):
            return {"cancel_requested": False}

        def upload_data_lease(self, evidence_ref, *, agent_id, lease, task_id, owner=""):
            assert evidence_ref == "stage-data:1"
            assert agent_id == "agent-a"
            assert task_id == "stage-data"
            assert owner == "alice"
            return {
                "dataset": {
                    "id": "dataset:sha256:" + "c" * 64,
                    "source_kind": "agent_upload",
                    "storage_ref": "shared://datasets/ovrs25/opaque",
                },
                "data_path": "dataset://sha256/" + "c" * 64,
                "upload_session_id": "dsup_" + "d" * 24,
            }

        def submit_result(self, _task_id, **kwargs):
            self.results.append(kwargs)

    monkeypatch.setattr(lease_module, "AgentDataLeaseStore", FakeLeaseStore)
    monkeypatch.setattr(binding_module, "AgentDataBindingStore", lambda: object())
    monkeypatch.setattr(
        agent_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    client = FakeClient()
    task = {
        "task_id": "stage-data",
        "task_type": "prepare_data",
        "stage_type": "prepare_data",
        "attempt_count": 1,
        "owner": "alice",
        "payload": {"project": "ovrs25", "data_binding_id": "data-root:sha256:" + "e" * 24},
    }
    assert agent_module._run_task(
        client,
        "agent-a",
        task,
        heartbeat_interval=1,
        node_kind="windows_agent",
    ) == 0
    assert client.results[0]["status"] == "succeeded"
    assert client.results[0]["result"]["dataset_id"].startswith("dataset:sha256:")
