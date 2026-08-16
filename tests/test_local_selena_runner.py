from pathlib import Path

from core.agent_local_run import LocalRunRequest
from core.local_selena_runner import run_local_selena


def _request(tmp_path: Path) -> LocalRunRequest:
    run_root = tmp_path / "runs" / "lease"
    work = run_root / "work"
    outputs = run_root / "outputs"
    runtime = tmp_path / "runtime"
    for path, content in (
        (runtime / "selena.exe", b"exe"),
        (runtime / "Runtime.xml", b"runtime"),
        (tmp_path / "data.MF4", b"input"),
        (tmp_path / "adapter.txt", b"adapter"),
        (tmp_path / "mat.filter", b"filter"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    outputs.mkdir(parents=True)
    template = tmp_path / "template.txt"
    template.write_text(
        "input={{INPUT_MF4}}\noutput={{OUTPUT_MF4}}\nruntime={{RUNTIME_XML}}\n"
        "adapterfile={{ADAPTER_FILE}}\nmatfilefilter={{MATFILEFILTER}}\n",
        encoding="utf-8",
    )
    config = {
        "_meta": {"project": "demo"},
        "project": {"name": "demo"},
        "assets": {"config_template": str(template), "fixed_config_path": str(work / "unused.txt")},
        "simulation": {
            "runtime_xml": str(runtime / "Runtime.xml"),
            "adapter_file": str(tmp_path / "adapter.txt"),
            "matfilefilter": str(tmp_path / "mat.filter"),
            "auto_detect_radar": False,
            "source": "RadarFC",
            "mounting_position": "front-center",
            "extra_args": [],
        },
        "environment": {"path_prefix": [str(runtime)]},
        "_local_run": {
            "executable": str(runtime / "selena.exe"),
            "working_directory": str(runtime),
            "controlled_work_directory": str(work),
        },
    }
    return LocalRunRequest(
        lease_id="local-run-lease:sha256:" + "1" * 64,
        item_index=1,
        input_mf4=tmp_path / "data.MF4",
        output_mf4=outputs / "0001-out.MF4",
        executable=runtime / "selena.exe",
        runtime_xml=runtime / "Runtime.xml",
        adapter_file=tmp_path / "adapter.txt",
        mat_filter=tmp_path / "mat.filter",
        working_directory=runtime,
        timeout_seconds=30,
        config=config,
    )


def test_runner_renders_private_paramconfig_and_uses_controlled_output(tmp_path, monkeypatch):
    request = _request(tmp_path)
    observed = {}

    class Process:
        returncode = 0
        _handle = 0

        def __init__(self, command, **kwargs):
            observed["command"] = command
            observed["kwargs"] = kwargs
            request.output_mf4.write_bytes(b"result")

        def poll(self):
            return 0

    monkeypatch.setattr("core.local_selena_runner.subprocess.Popen", Process)
    outcome = run_local_selena(request, lambda: False)

    assert outcome.exit_code == 0
    paramconfig = request.output_mf4.parent.parent / "work" / "paramconfig-0001.txt"
    assert paramconfig.is_file()
    assert str(request.output_mf4) in paramconfig.read_text(encoding="utf-8")
    assert observed["command"][:2] == [str(request.executable), "--paramconfig"]
    assert Path(observed["command"][2]) == paramconfig
    assert observed["kwargs"]["cwd"] == str(request.working_directory)


def test_runner_does_not_apply_a_hidden_timeout_when_timeout_is_zero(tmp_path, monkeypatch):
    request = _request(tmp_path)
    request = LocalRunRequest(**{**request.__dict__, "timeout_seconds": 0})
    observed = {}

    class Process:
        returncode = 0

        def __init__(self, _command, **kwargs):
            observed["started"] = True
            del kwargs

        def poll(self):
            return 0

    monkeypatch.setattr("core.local_selena_runner.subprocess.Popen", Process)
    outcome = run_local_selena(request, lambda: False)

    assert observed["started"] is True
    assert outcome.exit_code == 0


def test_runner_uses_shared_template_when_project_template_is_missing(tmp_path, monkeypatch):
    request = _request(tmp_path)
    request.config["assets"]["config_template"] = str(tmp_path / "missing-project-template.txt")
    shared = tmp_path / "shared-paramconfig.txt"
    shared.write_text(
        "config={{RUNTIME_XML}}\ninput={{INPUT_MF4}}\noutput={{OUTPUT_MF4}}\n"
        "source={{SOURCE}}\nmatfilefilter={{MATFILEFILTER}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.config.get_shared_selena_paramconfig_template", lambda: shared)

    class Process:
        returncode = 0
        _handle = 0

        def __init__(self, command, **kwargs):
            del command, kwargs
            request.output_mf4.write_bytes(b"result")

        def poll(self):
            return 0

    monkeypatch.setattr("core.local_selena_runner.subprocess.Popen", Process)

    outcome = run_local_selena(request, lambda: False)

    assert outcome.exit_code == 0
    rendered = request.output_mf4.parent.parent / "work" / "paramconfig-0001.txt"
    text = rendered.read_text(encoding="utf-8")
    assert f"input={request.input_mf4}" in text
    assert "source=RadarFC" in text


def test_runner_reports_actionable_paramconfig_failure_when_shared_template_is_missing(tmp_path, monkeypatch):
    request = _request(tmp_path)
    request.config["assets"]["config_template"] = str(tmp_path / "missing-project-template.txt")
    monkeypatch.setattr(
        "core.config.get_shared_selena_paramconfig_template",
        lambda: tmp_path / "missing-shared-template.txt",
    )

    outcome = run_local_selena(request, lambda: False)

    assert outcome.error_code == "paramconfig_failed"
    assert "FileNotFoundError" in "\n".join(outcome.diagnostics)
    assert "update or repair the radar-sim Connector" in "\n".join(outcome.diagnostics)


def test_runner_rejects_output_outside_lease(tmp_path):
    request = _request(tmp_path)
    request = LocalRunRequest(**{**request.__dict__, "output_mf4": tmp_path / "escape.MF4"})
    outcome = run_local_selena(request, lambda: False)
    assert outcome.error_code == "runner_contract_failed"


def test_runner_rejects_control_char_runtime_argument(tmp_path):
    request = _request(tmp_path)
    request.config["simulation"]["extra_args"] = ["--ok\n--bad"]
    outcome = run_local_selena(request, lambda: False)
    assert outcome.error_code == "unsafe_runtime_argument"


def test_runner_attaches_bounded_engine_log_tail_on_failure(tmp_path, monkeypatch):
    request = _request(tmp_path)

    class Process:
        returncode = 7
        _handle = 0

        def __init__(self, command, **kwargs):
            del command
            stream = kwargs["stdout"]
            stream.write(b"fatal runnable failure\n")
            stream.write(("local path: " + str(request.input_mf4) + "\n").encode("utf-8"))
            stream.flush()
            crlog = request.output_mf4.parent.parent / "work" / "CRlog-0001.log"
            crlog.write_text("CRlog: runnable=RadarRL failed\n", encoding="utf-8")

        def poll(self):
            return self.returncode

    monkeypatch.setattr("core.local_selena_runner.subprocess.Popen", Process)
    outcome = run_local_selena(request, lambda: False)

    assert outcome.exit_code == 7
    assert outcome.error_code == "selena_failed"
    assert "fatal runnable failure" in outcome.diagnostics
    assert "CRlog: runnable=RadarRL failed" in outcome.diagnostics
    assert str(request.input_mf4) in "\n".join(outcome.diagnostics)


def test_runner_classifies_windows_missing_dll_before_engine_start(tmp_path, monkeypatch):
    request = _request(tmp_path)

    class Process:
        returncode = 0xC0000135
        _handle = 0

        def __init__(self, command, **kwargs):
            del command, kwargs

        def poll(self):
            return self.returncode

    monkeypatch.setattr("core.local_selena_runner.subprocess.Popen", Process)

    outcome = run_local_selena(request, lambda: False)

    assert outcome.exit_code == 0xC0000135
    assert outcome.error_code == "selena_dependency_missing"
