from pathlib import Path
from types import SimpleNamespace

import pytest


def _cluster_config(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    runtime = assets / "runtime.xml"
    runtime.write_text("<runtime />", encoding="utf-8")
    matfilter = assets / "matfilefilter.txt"
    matfilter.write_text("*", encoding="utf-8")
    template = assets / "selena_config_tmpl.txt"
    template.write_text(
        "config={{RUNTIME_XML}}\n"
        "input={{INPUT_MF4}}\n"
        "output={{OUTPUT_MF4}}\n"
        "log={{LOG_FILE}}\n"
        "source={{SOURCE}}\n"
        "matfilefilter={{MATFILEFILTER}}\n",
        encoding="utf-8",
    )

    build_dir = tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo"
    build_dir.mkdir(parents=True)
    (build_dir / "selena.exe").write_text("", encoding="utf-8")

    software = tmp_path / "cluster_software"
    software.mkdir()
    for name in ("client.py", "manager.py", "worker.py", "database.py", "simulation_runtime.py"):
        (software / name).write_text("# stub", encoding="utf-8")

    workspace = tmp_path / "Cluster"
    workspace.mkdir()

    return {
        "_meta": {"project": "test"},
        "project": {"name": "test", "platform": "gen5_selena"},
        "paths": {"build_output": str(tmp_path / "build")},
        "assets": {
            "root": str(assets),
            "runtime_xml": str(runtime),
            "matfilefilter": str(matfilter),
            "config_template": str(template),
            "fixed_config_path": str(tmp_path / "generated" / "paramconfig.txt"),
        },
        "simulation": {
            "source": "RadarFL",
            "mounting_position": "CFL",
            "runtime_xml": str(runtime),
            "matfilefilter": str(matfilter),
            "tolerant": True,
            "enable_multibuffer_border": True,
            "enable_doorkeeper": True,
        },
        "selena": {
            "exe_pattern": "dc_tools/selena/core/{build_mode}/selena.exe",
            "build_mode": "RelWithDebInfo",
            "executable_name": "selena.exe",
        },
        "cluster": {
            "software_path": str(software),
            "workspace_root": str(workspace),
            "project_folder": "radar-sim",
            "python_path": str(tmp_path / "Python27" / "python.exe"),
            "kill_password": "1234",
            "group": "Radar",
            "subgroup": "PSS2",
        },
    }


def test_prepare_cluster_job_generates_package(tmp_path):
    from core.cluster import prepare_cluster_job

    config = _cluster_config(tmp_path)
    input_mf4 = tmp_path / "input.MF4"
    input_mf4.write_text("dummy", encoding="utf-8")

    package = prepare_cluster_job(config, input_path=str(input_mf4), run_id="run001")

    assert Path(package.job_dir).exists()
    assert Path(package.config_path).exists()
    assert Path(package.simulation_script).exists()
    assert Path(package.manifest_path).exists()
    assert package.datafile_path == str(input_mf4)
    assert package.submit_command[1].endswith("client.py")
    assert package.submit_command[3] == "<redacted>"
    assert "1234" not in " ".join(package.submit_command)

    cfg = Path(package.config_path).read_text(encoding="utf-8")
    assert "simulation =" in cfg
    assert "datafile_path =" in cfg
    assert "selenaPathExe =" in cfg
    assert "runTimeConfigFile =" in cfg

    script = Path(package.simulation_script).read_text(encoding="utf-8")
    assert "def simulation(inputfile, outputpath, infos=None):" in script
    assert "--paramconfig" in script


def test_prepare_cluster_job_copies_runtime_assets_from_effective_simulation(tmp_path):
    from core.cluster import prepare_cluster_job

    config = _cluster_config(tmp_path)
    resolved_runtime = tmp_path / "resolved-runtime.xml"
    resolved_runtime.write_text("<runtime resolved='true' />", encoding="utf-8")
    resolved_filter = tmp_path / "resolved.filter"
    resolved_filter.write_text("resolved", encoding="utf-8")
    adapter = tmp_path / "adapter.txt"
    adapter.write_text("adapter", encoding="utf-8")
    config["simulation"]["runtime_xml"] = str(resolved_runtime)
    config["simulation"]["matfilefilter"] = str(resolved_filter)
    config["simulation"]["adapter_file"] = str(adapter)
    # v2 Stage execution resolves these into simulation.*.  They must still be
    # preferred over stale static project assets and copied into the shared job.
    input_mf4 = tmp_path / "input.MF4"
    input_mf4.write_text("dummy", encoding="utf-8")

    package = prepare_cluster_job(config, input_path=str(input_mf4), run_id="stage-assets")

    cfg = Path(package.config_path).read_text(encoding="utf-8")
    job_assets = Path(package.job_dir) / "assets"
    assert f'runTimeConfigFile = "{job_assets / "resolved-runtime.xml"}";' in cfg
    assert f'matfilefilter = "{job_assets / "resolved.filter"}";' in cfg
    assert f'adapterFile = "{job_assets / "adapter.txt"}";' in cfg
    assert str(tmp_path / "assets" / "runtime.xml") not in cfg
    assert str(tmp_path / "assets" / "matfilefilter.txt") not in cfg


def test_submit_package_rejects_linux_local_worker_assets(tmp_path):
    from core.cluster import _validate_submit_package

    script = tmp_path / "SIMULATION_RADAR_SIM.py"
    script.write_text("import sys\nsys.path.append('cluster')\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "Config.cfg"
    cfg.write_text(
        "\n".join([
            f'simulation = "{script}";',
            'simulation_prio = 4;',
            'python_version = "*";',
            f'datafile_path = "{data}";',
            'extension = "*.MF4";',
            'skip_dir = "";',
            'skip_filename = "";',
            'finalstep = 0;',
            'send_email = 0;',
            'send_netsend = 0;',
            'group = "Radar";',
            'subgroup = "PSS2";',
            'selenaPathExe = "/home/server/selena.exe";',
            'runTimeConfigFile = "/home/server/runtime.xml";',
            'matfilefilter = "/home/server/filter.txt";',
        ]),
        encoding="utf-8",
    )

    errors = _validate_submit_package(cfg)

    assert any("Selena executable must use a Cluster-visible UNC path" in item for item in errors)
    assert any("Runtime XML must use a Cluster-visible UNC path" in item for item in errors)
    assert any("MatFilter must use a Cluster-visible UNC path" in item for item in errors)


def test_prepare_cluster_job_infers_fr_radar_from_input_name(tmp_path):
    from core.cluster import prepare_cluster_job

    config = _cluster_config(tmp_path)
    config["simulation"]["source"] = "auto"
    config["simulation"]["mounting_position"] = "auto"
    input_mf4 = tmp_path / "Vehicle_FR5CP_20090101_055502_0042.MF4"
    input_mf4.write_text("dummy", encoding="utf-8")

    package = prepare_cluster_job(config, input_path=str(input_mf4), run_id="run_fr")

    cfg = Path(package.config_path).read_text(encoding="utf-8")
    assert 'radar = "RadarFR";' in cfg
    assert 'mountingPosition = "CFR";' in cfg

    script = Path(package.simulation_script).read_text(encoding="utf-8")
    assert "__INPUT_MF4__" in script
    assert "chr(123) + chr(123)" in script


def test_cluster_profile_overrides_runtime_and_selena(tmp_path):
    from core.cluster import list_cluster_profiles, prepare_cluster_job

    config = _cluster_config(tmp_path)
    profile_runtime = tmp_path / "profile_runtime.xml"
    profile_runtime.write_text("<runtime profile />", encoding="utf-8")
    profile_selena = tmp_path / "profile_selena.exe"
    profile_selena.write_text("", encoding="utf-8")
    config["cluster"]["profiles"] = [
        {
            "name": "shared",
            "description": "shared runtime",
            "runtime_xml": str(profile_runtime),
            "selena_exe": str(profile_selena),
            "source": "RadarFC",
            "mounting_position": "",
            "required_input_signals": [],
            "subgroup": "PSS1",
        }
    ]
    input_mf4 = tmp_path / "input.MF4"
    input_mf4.write_text("dummy", encoding="utf-8")

    profiles = list_cluster_profiles(config)
    package = prepare_cluster_job(config, input_path=str(input_mf4), run_id="profile_run", profile="shared")
    cfg = Path(package.config_path).read_text(encoding="utf-8")

    assert [item["name"] for item in profiles] == ["default", "shared"]
    assert package.profile == "shared"
    copied_runtime = Path(package.job_dir) / "assets" / profile_runtime.name
    assert f'runTimeConfigFile = "{copied_runtime}";' in cfg
    assert f'selenaPathExe = "{profile_selena}";' in cfg
    assert 'radar = "RadarFC";' in cfg
    assert 'subgroup = "PSS1";' in cfg


def test_cluster_cli_prepare_dry_package(tmp_path, capsys):
    from cli.cluster import run

    config = _cluster_config(tmp_path)
    input_mf4 = tmp_path / "input.MF4"
    input_mf4.write_text("dummy", encoding="utf-8")
    args = SimpleNamespace(
        cluster_command="prepare",
        input_path=str(input_mf4),
        dataset="",
        run_id="run002",
        copy_data=False,
        copy_selena=False,
        json=False,
    )

    code = run(args, config)
    out = capsys.readouterr().out

    assert code == 0
    assert "Cluster job package prepared" in out
    assert "Submit command" in out


def test_submit_defaults_to_dry_run(tmp_path, capsys):
    from cli.cluster import run

    config = _cluster_config(tmp_path)
    cfg = tmp_path / "Config.cfg"
    cfg.write_text("simulation = \"stub\";", encoding="utf-8")

    args = SimpleNamespace(cluster_command="submit", config_path=str(cfg), execute=False, json=False)

    code = run(args, config)
    out = capsys.readouterr().out

    assert code == 0
    assert "Dry-run submit mode" in out
    assert "client.py" in out


def test_submit_dry_run_reports_xmlrpc_mode_without_python2(tmp_path):
    from core.cluster import submit_cluster_job

    config = _cluster_config(tmp_path)
    config["cluster"]["python_path"] = str(tmp_path / "missing-python.exe")
    cfg = tmp_path / "Config.cfg"
    cfg.write_text("simulation = \"stub\";", encoding="utf-8")

    result = submit_cluster_job(str(cfg), config, dry_run=True)

    assert result.dry_run is True
    assert result.mode == "xmlrpc"
    assert result.command[0].endswith("missing-python.exe")


def test_inspect_and_fetch_cluster_job_outputs(tmp_path):
    from core.cluster import fetch_cluster_job, inspect_cluster_job

    job = tmp_path / "job"
    out = job / "output"
    out.mkdir(parents=True)
    (out / "caseout.MF4").write_text("x" * 2048, encoding="utf-8")
    (out / "selena.log").write_text("ok", encoding="utf-8")
    (out / "result.ini").write_text("successfull=1", encoding="utf-8")

    status = inspect_cluster_job(str(job))

    assert status["state"] == "finished-success"
    assert status["file_count"] == 3
    assert status["success_count"] == 1
    assert status["output_mf4"][0]["relative_path"].endswith("caseout.MF4")

    dest = tmp_path / "fetched"
    result = fetch_cluster_job(str(job), str(dest))

    assert (dest / "caseout.MF4").exists()
    assert len(result["copied"]) == 3


def test_inspect_cluster_job_rejects_worker_success_without_output_and_reads_zip(tmp_path):
    import zipfile
    from core.cluster import inspect_cluster_job

    job = tmp_path / "job"
    out = job / "OUT" / "case.MF4"
    out.mkdir(parents=True)
    (out / "result.ini").write_text(
        "successfull=1\nout_size=0\nerror_message=\n", encoding="utf-8"
    )
    with zipfile.ZipFile(out / "logfile.txt.zip", "w") as archive:
        archive.writestr(
            "logfile.txt",
            "Traceback (most recent call last):\n"
            "ImportError: No module named simulation_runtime\n",
        )

    status = inspect_cluster_job(str(job))

    assert status["state"] == "finished-failed"
    assert status["success_count"] == 1
    assert status["output_mf4"] == []
    assert any("simulation_runtime" in line for line in status["error_summary"])
    assert any("no simulation output MF4" in line for line in status["error_summary"])


def test_inspect_cluster_job_extracts_error_summary(tmp_path):
    from core.cluster import inspect_cluster_job

    job = tmp_path / "job"
    out = job / "output"
    out.mkdir(parents=True)
    (out / "selena.log").write_text(
        "[info]: config errors: 0\n"
        "[error]: no signal found in channel cache for port demo\n",
        encoding="utf-8",
    )
    (out / "result.ini").write_text("successfull=0\nerror_message=unknown (Returncode = -1)\n", encoding="utf-8")

    status = inspect_cluster_job(str(job))

    assert status["state"] == "finished-failed"
    assert any("no signal found" in line for line in status["error_summary"])
    assert not any("config errors: 0" in line for line in status["error_summary"])


def test_cluster_check_allows_xmlrpc_without_python2(tmp_path, monkeypatch):
    import core.cluster as cluster_mod

    config = _cluster_config(tmp_path)
    config["cluster"]["python_path"] = str(tmp_path / "missing-python.exe")

    monkeypatch.setattr(cluster_mod, "_manager_item", lambda cluster: cluster_mod.CheckItem("Manager XML-RPC port", True, "host:8123"))

    items = cluster_mod.check_cluster_environment(config)
    by_name = {item.name: item for item in items}

    assert by_name["Python for client.py"].ok is True
    assert "optional" in by_name["Python for client.py"].detail
    assert by_name["Submit path"].ok is True
    assert by_name["Submit path"].detail == "xmlrpc"


def test_scan_cluster_data_detects_required_signal_and_skips_outputs(tmp_path):
    from core.cluster import scan_cluster_data

    config = _cluster_config(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "case_a.MF4").write_bytes(b"header g_Golf_Fct_Hmi_RunnableHmi_internalstates trailer")
    (data_dir / "case_b.MF4").write_bytes(b"header only")
    (data_dir / "case_aout.MF4").write_bytes(b"g_Golf_Fct_Hmi_RunnableHmi_internalstates")

    result = scan_cluster_data(config, input_path=str(data_dir), limit=10, max_read_mb=1)
    by_name = {Path(item["path"]).name: item for item in result["files"]}

    assert set(by_name) == {"case_a.MF4", "case_b.MF4"}
    assert by_name["case_a.MF4"]["signal_status"] == "present"
    assert by_name["case_b.MF4"]["signal_status"] == "missing"


def test_scan_cluster_data_reports_missing_in_prefix(tmp_path):
    from core.cluster import scan_cluster_data

    config = _cluster_config(tmp_path)
    mf4 = tmp_path / "large.MF4"
    mf4.write_bytes(
        (b"x" * (1024 * 1024))
        + b"g_Golf_Fct_Hmi_RunnableHmi_internalstates"
        + (b"x" * (1024 * 1024))
    )

    result = scan_cluster_data(config, input_path=str(mf4), limit=1, max_read_mb=1)

    assert result["files"][0]["signal_status"] == "missing-in-prefix"


def test_submit_command_supports_py_launcher(tmp_path):
    from core.cluster import build_submit_command

    config_path = tmp_path / "Config.cfg"
    cluster = {
        "python_path": "py -2",
        "software_path": str(tmp_path),
        "client_py": "client.py",
        "kill_password": "1234",
    }

    cmd = build_submit_command(config_path, cluster=cluster)

    assert cmd[:2] == ["py", "-2"]
    assert cmd[2].endswith("client.py")


def test_cluster_secret_comes_from_deployment_env_and_dry_run_is_redacted(tmp_path, monkeypatch):
    from core.cluster import build_submit_command, get_cluster_config, submit_cluster_job

    monkeypatch.delenv("RSIM_CLUSTER_KILL_PASSWORD", raising=False)
    config = {"cluster": {"software_path": str(tmp_path), "python_path": "py -2"}}
    cluster = get_cluster_config(config)
    assert "kill_password" not in cluster
    with pytest.raises(RuntimeError, match="RSIM_CLUSTER_KILL_PASSWORD"):
        build_submit_command(tmp_path / "Config.cfg", cluster=cluster)

    dry_run = submit_cluster_job(str(tmp_path / "Config.cfg"), config, dry_run=True)
    assert "<redacted>" in dry_run.command

    monkeypatch.setenv("RSIM_CLUSTER_KILL_PASSWORD", "deployment-secret")
    configured = get_cluster_config(config)
    assert configured["kill_password"] == "deployment-secret"
    command = build_submit_command(tmp_path / "Config.cfg", cluster=configured)
    assert command[-1] == "deployment-secret"


def test_get_cluster_web_status_preserves_readable_state(tmp_path, monkeypatch):
    import core.cluster as cluster_mod

    config = _cluster_config(tmp_path)
    config["cluster"]["web_url"] = "http://cluster.test/cluster/"
    job_dir = str(tmp_path / "job_a")

    jobs_html = f"""
    <html>
      <body>
        <td>{job_dir}\\Config.cfg</td>
        <button onclick="changeprio('10321','1')">prio</button>
      </body>
    </html>
    """
    tasks_html = """
    <table class="table2" width="100%">
      <tr><td>task_id</td><td>1</td></tr>
      <tr><td>simulation_state</td><td><font>simulating</font></td></tr>
      <tr><td>worker_host</td><td>szhradar26</td></tr>
    </table>
    <table class='table2' id='task_extended_1' style='display:none'>
      <tr><td>id</td><td>5445489</td></tr>
      <tr><td>simulation_state</td><td>3</td></tr>
      <tr><td>python_version</td><td>python27</td></tr>
    </table>
    """

    def fake_read_url(url, *, timeout=20):
        if "page=jobs" in url:
            return jobs_html
        if "page=tasks" in url:
            return tasks_html
        raise AssertionError(url)

    monkeypatch.setattr(cluster_mod, "_read_url", fake_read_url)

    status = cluster_mod.get_cluster_web_status(config, job_dir)

    assert status["found"] is True
    assert status["job_id"] == "10321"
    assert status["state"] == "simulating"
    assert status["worker_hosts"] == ["szhradar26"]
    assert status["tasks"][0]["simulation_state"] == "simulating"
    assert status["tasks"][0]["simulation_state_code"] == "3"


def test_wait_diagnosis_reports_running_and_completion():
    import cli.cluster as cluster_cli

    running = cluster_cli._diagnose_wait_state(
        {
            "tasks": [
                {
                    "simulation_state": "simulating",
                    "time_simulation_is_running": "2099-01-01 00:00:00",
                    "timeout": "120",
                    "time_finished": "0000-00-00 00:00:00",
                }
            ]
        },
        {"state": "running-or-started", "success_count": 0, "fail_count": 0, "output_mf4": []},
        max_minutes=0,
    )

    assert running["done"] is False
    assert running["outcome"] == "running"
    assert running["shared_state"] == "running-or-started"
    assert running["stale_after_minutes"] == 120

    success = cluster_cli._diagnose_wait_state(
        {"tasks": [{"simulation_state": "finished", "time_finished": "2026-07-01 14:00:00"}]},
        {"state": "finished-success", "success_count": 1, "fail_count": 0, "output_mf4": [{"path": "out.MF4"}]},
        max_minutes=0,
    )

    assert success["done"] is True
    assert success["outcome"] == "success"
    assert success["output_count"] == 1


def test_wait_job_dir_resolution_keeps_explicit_dir_for_numeric_job():
    import cli.cluster as cluster_cli

    explicit = r"\\server\share\job_a"

    assert cluster_cli._resolve_wait_job_dir("10321", explicit) == explicit
    assert cluster_cli._resolve_wait_job_dir("10321", "") == ""
    assert cluster_cli._resolve_wait_job_dir(explicit, "") == explicit


def test_prepare_selena_source_build_copies_runtime(tmp_path):
    """A profile with selena.source=build should copy the local runtime into the job folder."""
    from core.cluster import prepare_cluster_job

    config = _cluster_config(tmp_path)
    config["profiles"] = [
        {
            "name": "build-to-cloud",
            "backend": "cluster",
            "selena": {"source": "build", "exe": ""},
            "data": {"copy": False, "required_signals": []},
        }
    ]
    input_mf4 = tmp_path / "input.MF4"
    input_mf4.write_text("dummy", encoding="utf-8")

    package = prepare_cluster_job(config, input_path=str(input_mf4), run_id="build_src")
    # The job folder should contain a staged selena.exe copied from the local build.
    staged_selena = Path(package.job_dir) / "selena" / "selena.exe"
    assert staged_selena.exists()
    cfg = Path(package.config_path).read_text(encoding="utf-8")
    assert f'selenaPathExe = "{staged_selena}";' in cfg


def test_prepare_local_data_without_copy_warns(tmp_path):
    """Local-drive data with copy=false should warn (not silently submit an invisible path)."""
    from core.cluster import prepare_cluster_job

    config = _cluster_config(tmp_path)
    # Local-drive input on D: (this PC), profile does not copy data.
    input_mf4 = tmp_path / "local_input.MF4"
    input_mf4.write_text("dummy", encoding="utf-8")
    config["profiles"] = [
        {
            "name": "cloud-no-copy",
            "backend": "cluster",
            "selena": {"source": "path", "exe": str(tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo" / "selena.exe")},
            "data": {"copy": False, "required_signals": []},
        }
    ]

    package = prepare_cluster_job(config, input_path=str(input_mf4), run_id="local_data", profile="cloud-no-copy")
    # tmp_path is a local drive path, so a warning about worker visibility is expected.
    assert any("invisible to workers" in w for w in package.warnings)
    # Data path should still point at the original (not staged).
    assert package.datafile_path == str(input_mf4)


def test_prepare_local_data_with_copy_stages_it(tmp_path):
    """Local-drive data with copy=true should be staged into the job folder."""
    from core.cluster import prepare_cluster_job

    config = _cluster_config(tmp_path)
    input_mf4 = tmp_path / "staged_input.MF4"
    input_mf4.write_text("dummy", encoding="utf-8")
    config["profiles"] = [
        {
            "name": "cloud-copy",
            "backend": "cluster",
            "selena": {"source": "path", "exe": str(tmp_path / "build" / "dc_tools" / "selena" / "core" / "RelWithDebInfo" / "selena.exe")},
            "data": {"copy": True, "required_signals": []},
        }
    ]

    package = prepare_cluster_job(config, input_path=str(input_mf4), run_id="copy_data", profile="cloud-copy")
    staged = Path(package.job_dir) / "data" / "staged_input.MF4"
    assert staged.exists()
    assert package.datafile_path == str(staged)
