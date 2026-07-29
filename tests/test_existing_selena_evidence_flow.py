"""Public existing-Selena evidence stays optional and never triggers a build."""

from pathlib import Path
from types import SimpleNamespace

from core.stages import plan_user_run_stages
from core.user_config import UserRunConfig
from radar_sim_sdk import RadarSimClient
from tests.test_api_v1_service import run_config_dict


def _existing_with_evidence() -> dict:
    config = run_config_dict()
    config["selena"] = {
        "source": "existing",
        "existing_path": "D:/runtime/Selena",
        "runtime_xml": "D:/runtime/Runtime.xml",
        "code_path": "D:/workspace",
        "branch": "release/od25",
        "selena_build_script": "D:/workspace/tools/build_selena.bat",
        "package_build_script": "D:/workspace/tools/build_package.bat",
    }
    return config


def test_existing_evidence_roundtrips_without_enabling_build_stages():
    config = UserRunConfig.from_dict(_existing_with_evidence())
    roundtrip = UserRunConfig.from_yaml(config.to_yaml())
    assert roundtrip == config
    assert roundtrip.selena.source == "existing"

    stages = {
        stage.stage_type: stage
        for stage in plan_user_run_stages(roundtrip).stages
    }
    assert stages["prepare_source"].initial_status == "skipped"
    assert stages["build_selena"].initial_status == "skipped"


def test_existing_minimal_config_keeps_required_folder_and_runtime():
    raw = _existing_with_evidence()
    for field in (
        "code_path",
        "branch",
        "selena_build_script",
        "package_build_script",
    ):
        raw["selena"].pop(field)

    config = UserRunConfig.from_dict(raw)

    assert config.to_dict()["selena"] == {
        "source": "existing",
        "existing_path": "D:/runtime/Selena",
        "runtime_xml": "D:/runtime/Runtime.xml",
    }


def test_web_keeps_optional_existing_workspace_evidence():
    root = Path(__file__).parents[1] / "radar_sim_web" / "static"
    app = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    assert "code_path: codePath" in app
    assert "selena_build_script: selenaBuildScript" in app
    assert "buildFields\").hidden = false" in app
    assert "以下代码仓和脚本为可选识别证据" in app
    assert "使用已有 Selena 时可选" in html
    assert "Selena 产物文件夹（必填）" in html
    assert "Runtime XML（必填）" in html
    assert 'byId("existingFields").hidden = !usingExisting' in app
    assert 'byId("existingPath").required = usingExisting' in app
    assert "请填写 Selena 产物文件夹" in app
    assert "当前配置将从本地代码编译 Selena" in app


def test_web_does_not_require_optional_package_build_script():
    static = Path(__file__).parents[1] / "radar_sim_web" / "static"
    app = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")

    assert "本地编译需要填写软件包编译脚本" not in app
    assert "if (packageBuildScript) selena.package_build_script = packageBuildScript" in app
    assert '["codePath", "selenaBuildScript", "packageBuildScript"]' not in app
    assert 'byId("packageBuildScript").required = false' in app
    assert "本地编译需要代码仓和两个脚本" not in app
    assert "可选；用于补充产品识别和环境依赖线索。" in html


def test_web_requires_final_target_and_source_confirmation_after_yaml_import():
    root = Path(__file__).parents[1] / "radar_sim_web" / "static"
    app = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")

    assert 'id="finalExecutionSummary"' in html
    assert "最终执行位置：自动（提交前确认）" in html
    assert 'id="finalSelenaSummary"' in html
    assert "Selena 来源：本地编译" in html
    assert 'id="importSelectionWarning"' in html
    assert "state.importedSelection = {" in app
    assert "执行位置已从" in app
    assert "Selena 来源已从" in app
    assert "注意：导入 YAML 后" in app
    assert "window.confirm(" in app
    assert "confirmSubmission(config, validation)" in app
    assert "已取消提交，配置保持不变" in app
    assert 'target: selectedValue("target") || "auto"' in app


def test_sdk_passes_existing_workspace_evidence_to_local_import(tmp_path, monkeypatch):
    existing = tmp_path / "Selena"
    existing.mkdir()
    runtime = tmp_path / "Runtime.xml"
    runtime.write_text("<runtime/>", encoding="utf-8")
    raw = _existing_with_evidence()
    raw["selena"]["existing_path"] = str(existing)
    raw["selena"]["runtime_xml"] = str(runtime)
    raw["data"]["path"] = "//shared/data"
    raw["simulation"]["mat_filter"] = "//shared/signals.filter"
    config = UserRunConfig.from_dict(raw)
    captured = {}
    client = RadarSimClient("http://127.0.0.1:1")

    def upload(folder, runtime_path, **evidence):
        captured.update(
            {
                "folder": folder,
                "runtime": runtime_path,
                **evidence,
            }
        )
        return "selena-bundle:sha256:" + "a" * 64

    monkeypatch.setattr(client, "_upload_existing_selena", upload)
    payload, bundle_id = client._prepare_user_run(config, dry_run=False)

    assert bundle_id.endswith("a" * 64)
    assert captured["code_path"] == "D:/workspace"
    assert captured["selena_build_script"].endswith("build_selena.bat")
    assert captured["package_build_script"].endswith("build_package.bat")
    assert payload["selena"]["source"] == "existing"


def test_sdk_does_not_block_user_inputs_on_signal_contract(tmp_path, monkeypatch):
    existing = tmp_path / "Selena"
    existing.mkdir()
    runtime = tmp_path / "runtime_fcta.xml"
    runtime.write_text("<runtime><DataPlayer input='ConfiguredSignal'/></runtime>", encoding="utf-8")
    data = tmp_path / "Radar"
    data.mkdir()
    (data / "sample.MF4").write_bytes(b"not-a-real-mf4")

    raw = _existing_with_evidence()
    raw["selena"]["existing_path"] = str(existing)
    raw["selena"]["runtime_xml"] = str(runtime)
    raw["data"]["path"] = str(data)
    raw["simulation"]["target"] = "cluster"
    raw["simulation"]["adapter_file"] = ""
    raw["simulation"]["mat_filter"] = "//shared/signals.filter"
    config = UserRunConfig.from_dict(raw)
    client = RadarSimClient("http://127.0.0.1:1")

    monkeypatch.setattr(
        "core.preflight.assert_runtime_data_signal_contract",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SDK must not pre-judge user-selected simulation inputs")
        ),
    )
    monkeypatch.setattr(
        client,
        "_upload_existing_selena",
        lambda *_args, **_kwargs: "selena-bundle:sha256:" + "a" * 64,
    )
    monkeypatch.setattr(
        client,
        "upload_run_data",
        lambda source: SimpleNamespace(
            data_path="dataset://sha256/" + "b" * 64,
        ),
    )

    payload, bundle_id = client._prepare_user_run(config, dry_run=False)

    assert bundle_id == "selena-bundle:sha256:" + "a" * 64
    assert payload["data"]["path"] == "dataset://sha256/" + "b" * 64
