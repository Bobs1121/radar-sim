# radar-sim Handoff

Last updated: 2026-07-07

Current state:

- configuration system expanded
- simulation config normalized
- repo/branch semantics added
- second-project config skeleton started
- tests green
- cluster batch simulation environment identified
- Linux control-plane migration path implemented as server shell + Windows polling agent
- remote web mode can submit jobs through `--server-url`; real Selena work still runs on user Windows machines
- 2026-07-07 verification: focused control-plane tests 56 passed, full suite 336 passed

## What is already done

### 1. Unified simulation config

Added:

- [core/simulation.py](/D:/RamboStar/idea/radar-sim/core/simulation.py)

Key behavior now:

- `run / prepare-sim / check / render_selena_config` all use one normalized `simulation` model
- batch dataset runs supported
- one generated paramconfig per MF4
- historical outputs like `*out.MF4` and `*out (n).MF4` are skipped
- radar position auto-detection is wired in for the BYD Gen5-style MF4

### 2. ovrs25 config simplified

Main file:

- [config/projects/ovrs25/config.yaml](/D:/RamboStar/idea/radar-sim/config/projects/ovrs25/config.yaml)

Current idea:

- user gives project-level build entry
- system derives as much as possible

Derived fields now include:

- `project_root`
- `binding`
- `build_config`
- `build_mode`
- `r2d2_script`
- `hex_build_script`
- `build_output`

### 3. Repo and Selena branch support

Config model now includes:

- `repos.outer_repo_root`
- `repos.inner_repo_root`
- `build.selena_branch`

Build behavior:

- `rsim build` checks inner repo branch before compile
- if branch differs and repo is clean, it tries to switch
- if repo is dirty, it stops and reports instead of forcing checkout

Relevant files:

- [cli/build.py](/D:/RamboStar/idea/radar-sim/cli/build.py)
- [cli/check.py](/D:/RamboStar/idea/radar-sim/cli/check.py)

### 4. Script-based Selena build is more flexible now

Added config support for:

- `build.script_args_template`

Reason:

- do not hardcode all projects to the `mode + config + binding` bat-call shape
- keep one user-facing `rsim build`, but allow per-project internal invocation differences

### 5. Paramconfig model expanded

Config/rendering now supports:

- `simulation.runtime_xml`
- `simulation.adapter_file`
- `simulation.matfilefilter`
- `simulation.paramconfig_options`

Template placeholders now support:

- `{{ADAPTER_FILE}}`
- `{{EXTRA_PARAMCONFIG_LINES}}`

Relevant files:

- [core/config.py](/D:/RamboStar/idea/radar-sim/core/config.py)
- [core/simulation.py](/D:/RamboStar/idea/radar-sim/core/simulation.py)
- [config/projects/ovrs25/assets/selena/selena_config_tmpl.txt](/D:/RamboStar/idea/radar-sim/config/projects/ovrs25/assets/selena/selena_config_tmpl.txt)

### 6. Environment/setup documentation added

Added:

- [docs/environment-setup.md](/D:/RamboStar/idea/radar-sim/docs/environment-setup.md)

README links to it.

### 7. New project skeleton started for shared config files

Added recipe:

- [config/recipes/g3n_fvg3_od25.yaml](/D:/RamboStar/idea/radar-sim/config/recipes/g3n_fvg3_od25.yaml)

Added project skeleton:

- [config/projects/bydod25/config.yaml](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/config.yaml)
- [config/projects/bydod25/local.example.yaml](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/local.example.yaml)
- [config/projects/bydod25/assets/README.md](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/assets/README.md)
- [config/projects/bydod25/assets/selena/selena_config_tmpl.txt](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/assets/selena/selena_config_tmpl.txt)

This skeleton maps the shared folder info into config fields:

- runtime XML
- adapter file
- matlab filter
- `source=RadarFC`
- extra paramconfig option `distilled-mat=true`

## Important findings

### 0. Cluster batch simulation environment is available

User provided a Bosch internal cluster environment for batch simulation:

- Outside compliance room:
  - Online submit / job page: [http://szhradar01/cluster/?page=jobs](http://szhradar01/cluster/?page=jobs)
  - Docupedia: `Submit Gen5 Cluster Simulation Task Online - XC-AS/EDY-CN - Docupedia`
  - Cluster software share: `\\szhradar01\_cluster_software\`
  - Tool / project share path: `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster`
- Inside compliance room / VDI:
  - Docupedia: `Remote(VDI) - XC-CN Data Compliance Solution - Docupedia`
  - Docupedia: `01_Cluster+KPI+VDI - XC-DA/EDY-CN - Docupedia (bosch.com)`
  - VDI cluster path: `\\selena01\_cluster_software\`

Current interpretation:

- This may become the preferred backend for large dataset / multi-MF4 batch simulation.
- Keep local `rsim run` as the baseline execution path, then add a cluster submission backend instead of hardwiring cluster behavior into core simulation logic.
- Cluster V2.0 is script/config based. `client.py` is a Python 2 client and expects:
  - command shape: `python.exe client.py <Config.cfg> <kill-password> [username]`
  - required config keys include `simulation`, `simulation_prio`, `python_version`, `datafile_path`, `extension`, `skip_dir`, `skip_filename`, `finalstep`, `send_email`, `send_netsend`, `group`, `subgroup`
  - `datafile_path` can point to a single file; the manager treats an existing file path as a one-task job
  - simulation scripts must include `sys.path.append('\\\\szhradar01\\_CLUSTER_SOFTWARE\\')` or equivalent
  - sample BYD_OVRS files are under `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER`
- Access status from current Codex session on 2026-06-26:
  - `\\szhradar01\_cluster_software\` is readable
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster` is readable
  - `\\selena01\_cluster_software\` is not resolvable from this session, although the user can open it in Explorer
  - `http://szhradar01/cluster/?page=jobs` and XML-RPC `szhradar01.apac.bosch.com:8123` time out from this session
  - likely next step: run submission from VDI / compliance-room environment or another shell/browser context with manager HTTP/XML-RPC reachability
- External cluster path health check on 2026-06-26:
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster` supports read/write from current session
  - a small probe directory/file under `...\Cluster\radar-sim_probe\...` was created and read back successfully
  - executing a Python script from the UNC path works in the current session
  - a 1 MB read/write roundtrip succeeded but was slow, so large MF4 files should preferably already live on a shared path instead of being copied from local `D:\data` for every run
  - `\\szhradar01\_cluster_software\` is readable and contains `client.py`, `manager.py`, `worker.py`, `simulation_runtime.py`, `python27_deprecated_modules`, and Python 2.7 installer assets
  - MySQL `szhradar01:3306` is reachable; read-only query showed `cluster_config.state=1`, `state_message=Online`, `manager_host=SZHRADAR01`, `manager_port=8123`, `http_host=https://szhradar01`
  - available external cluster groups from DB include `Radar/PSS1`, `Radar/PSS2`, `Radar/ACC`, `Radar/Jenkins`, `Radar/RA6`
  - HTTP/HTTPS status page ports `80/443` and XML-RPC manager port `8123` time out from this session
- Local ovrs25 cloud-run asset status:
  - local compiled Selena exists at `C:\BYD_OVS_CB\ip_dc\build\ROS_PER_SIT_RPM_FCT_RECR\dc_tools\selena\core\RelWithDebInfo`
  - full local `RelWithDebInfo` is about 640 MB
  - runtime-essential files excluding `.pdb`, `.ilk`, logs, and missing-signal text are about 90 MB
  - runtime-essential file set observed: `selena.exe`, `selena_dll.dll`, `selena_core.dll`, `selena_gui.dll`, `Mdf4Lib_x64.dll`, `MdfLibSort_x64.dll`, `MDFSort_x64.dll`, `Qt5Core.dll`, `Qt5Xml.dll`, `XmlParser_x64.dll`
  - project assets already include `config/projects/ovrs25/assets/selena/runtime.xml`, `matfilefilter.txt`, and `selena_config_tmpl.txt`
  - local input MF4 under `D:\data\...` is not directly usable by cluster workers; input data must be copied to or already exist on a worker-readable shared path
- Current architectural conclusion:
  - a cloud/cluster second path is feasible as a separate backend from local `rsim run`
  - user can compile Selena locally, then publish a trimmed runtime package plus runtime XML/filter/input data to the shared cluster workspace
  - `rsim cluster prepare` can generate a self-contained job package (`Config.cfg`, worker `SIMULATION_RADAR_SIM.py`, paramconfig template/assets)
  - actual queue submission still needs either official manager access on port `8123`, Python 2 `client.py` from an environment that can reach it, or an explicitly approved direct DB enqueue experiment
  - direct DB enqueue is technically plausible because DB is reachable, but it bypasses manager validation and should not be done without explicit user approval and a single-file smoke-test plan

### 1. Do not over-generalize low-level logic

Strong recommendation:

- keep user entry unified
  - `rsim check`
  - `rsim build`
  - `rsim prepare-sim`
  - `rsim run`
- split internal adaptation by project/recipe

### 2. The new repo/project is not structurally the same as ovrs25

Observed:

- `fvg3_lfs` repo root is not a `bindings/<name>/...` style repo
- shared ParamConfig has fields not covered by the original ovrs25 assumptions
- `adapterfile` is required
- current known source is `RadarFC`

So:

- do not force this project into the old `binding`-style build semantics
- use `recipe` or project-specific build/run shaping

## Browser / repo lookup notes

Only read-only browsing was done.

Confirmed from Bitbucket page:

- repo root visible
- `jenkins/`
- `jenkins/configs`
- `jenkins/jenkinsfiles`
- branch shown in page UI: `develop_evo`

No authenticated write action was performed.

## What is not finished yet

### 1. Recipe system is only half-done

Right now:

- recipe exists in config layering
- project skeleton exists

But not done yet:

- full execution-layer dispatch for `build / run / check / prepare-sim`

### 2. bydod25 is still a config skeleton

Important:

- local checkout paths like `D:/byd/...` are not validated yet
- they are placeholders / intended local layout, not confirmed local files

### 3. Need local-project verification later

Still needed when local repo exists:

- confirm actual `jenkins_selena_build.bat` location
- confirm actual script argument convention
- confirm actual `build_output`
- confirm actual `selena.sln`
- confirm actual `selena.exe`

## Recommended next steps for the next AI

1. Finish recipe execution model

Suggested direction:

- explicit recipe dispatch module
- separate internal handling for:
  - `ovrs25`
  - `g3n_fvg3_od25`

2. Validate bydod25 local checkout once it exists

Check:

- script path
- script args
- build output
- VS solution path
- selena.exe path

Then update:

- [config/projects/bydod25/config.yaml](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/config.yaml)
- [config/projects/bydod25/local.example.yaml](/D:/RamboStar/idea/radar-sim/config/projects/bydod25/local.example.yaml)

3. Update config docs

Specifically document:

- `project.recipe`
- `repos.*`
- `build.selena_branch`
- `build.script_args_template`
- `simulation.adapter_file`
- `simulation.paramconfig_options`

4. Investigate cluster backend

Check:

- whether submission should use:
  - existing Python 2 `client.py`
  - a small Python 3 XML-RPC adapter to `addSimulation`
  - the online Docupedia workflow
- required input packaging: MF4, paramconfig, runtime XML, Selena executable/build artifact, filters/adapters
- where logs and output MF4 files are stored
- whether jobs can be queried from `http://szhradar01/cluster/?page=jobs`
- whether the target environment can reach `szhradar01.apac.bosch.com:8123`

Then consider adding:

- `rsim cluster submit`
- `rsim cluster status`
- `rsim cluster fetch`
- a config section such as `cluster.url`, `cluster.tool_path`, `cluster.software_path`

## Test status

Latest full test run:

- `pytest -q`
- result: `98 passed`

## Cluster smoke test on shared data, 2026-06-26

User-provided BYD_SR data source:

- `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon2\OverseaData\Driving\AU_data\BYD_SR\`

Smoke-test package created on the external Cluster share:

- root: `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\smoke_20260626_201242`
- Selena runtime copy: `...\selena\RelWithDebInfo`
- config/assets: `...\assets`
- extracted input data: `...\data\Gen5_2009-01-01_05-56_0114.MF4`
- output/logs: `...\output`

What was validated:

- BYD_SR source share is readable from this session.
- `23-4-26_CBNA.zip` can be inspected with `\\szhradar01\_cluster_software\7za.exe`.
- One MF4 was extracted to the Cluster project share.
- A trimmed local Selena runtime package was copied to the Cluster project share.
- Local compiled Selena can start using the shared config/assets and shared MF4 path.

Current result:

- The run is not yet a successful simulation.
- Output `Gen5_2009-01-01_05-56_0114out.MF4` was created but is only 1448 bytes.
- `selena_cbna.log` stops after runnable loading / input file setup, with no completed simulation progress.
- Directly running `selena.exe` from the UNC runtime copy in this local session failed or timed out; use local exe or copy runtime to a worker-local temp folder before launch.

Likely next isolation:

- Emulate Cluster worker behavior by copying the input MF4 from shared storage to a local temp folder, running local Selena with local temp input/output and shared assets, then copying output back.
- If that passes, the packaging/config is good and the remaining blocker is official Cluster manager/worker submission.
- If that still hangs, inspect runtime XML / radar source / mounting position / input MF4 compatibility.

## Cluster backend and Web Console progress, 2026-06-30

User clarified that Cluster batch simulation is server-deployed, not cloud-hosted:

- `http://szhradar01/cluster/?page=jobs` is primarily a status/progress page.
- Simulation assets must be staged under `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster`.
- Scheduling is handled by the server XAMPP environment plus the Cluster software package from `\\szhradar01\cluster_software` / `\\szhradar01\_cluster_software`.
- `client.py` submits from the user's PC, `manager.py` receives/dispatches, `worker.py` runs simulation, and `database.py` updates status.

Implemented first backend slice:

- `core/cluster.py`
  - `check_cluster_environment(config)`
  - `prepare_cluster_job(config, ...)`
  - `submit_cluster_job(config_path, config, dry_run=True)`
  - Generates `Config.cfg`, `SIMULATION_RADAR_SIM.py`, copied assets, `manifest.json`, and submit command.
  - The generated worker script is Python2-compatible and writes a per-task Selena paramconfig before running `selena.exe --paramconfig`.
- `cli/cluster.py`
  - `rsim cluster check`
  - `rsim cluster prepare [input_path] --dataset BYD_SR --run-id ...`
  - `rsim cluster submit <Config.cfg>` defaults to dry-run; use `--execute` for a real `client.py` call.
- `cli/web.py` plus `web/`
  - `rsim web --host 127.0.0.1 --port 8765`
  - Local Web Console with tabs for local simulation diagnostics, server Cluster simulation, and effective config.
  - API endpoints include `/api/projects`, `/api/config`, `/api/local/check`, `/api/cluster/check`, `/api/cluster/prepare`, `/api/cluster/submit`.

Current verified state:

- `pytest -q tests/test_cluster.py` passes (`3 passed`).
- `python -m py_compile core\cluster.py cli\cluster.py cli\web.py` passes.
- `python rsim.py --project ovrs25 cluster prepare --dataset BYD_SR --run-id dryrun_20260630_bydsr --json` created a real package under:
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\dryrun_20260630_bydsr`
- The generated package points `datafile_path` to the shared BYD_SR dataset:
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon2\OverseaData\Driving\AU_data\BYD_SR`
- `rsim cluster check` shows:
  - OK: Cluster software path, `client.py`, `manager.py`, `worker.py`, `database.py`, `simulation_runtime.py`
  - OK: Cluster workspace root and write probe
  - OK: worker dependency paths for MATLAB/Boost/Qt network shares
  - Missing: `C:\Python27\python.exe`
- Web server was started and verified:
  - `http://127.0.0.1:8765/` returns 200
  - `/api/projects` returns `bydod25` and `ovrs25`
  - `/api/cluster/check?project=ovrs25` returns the same Cluster diagnostics

Next concrete steps:

- Configure or detect the real local Python2 runtime for `client.py` (possibly from the mapped Cluster package or an installed `py -2` launcher).
- Set `cluster.selena_exe` to a worker-visible Selena runtime path or run `rsim cluster prepare --copy-selena` for a single smoke test.
- Submit one single-MF4 package with `rsim cluster submit <Config.cfg> --execute` after Python2 is available.
- Add status/fetch commands once a real submitted job exposes the manager-created output folder naming.

2026-06-30 continuation:

- Added Python2 runtime discovery:
  - `rsim cluster python`
  - API: `/api/cluster/python`
  - It checks configured `cluster.python_path`, common Python27 paths, `py -2`, and Python on PATH.
  - Current machine result: no usable Python2 found; Python 3.12 on PATH is detected but rejected.
- Added prepared job discovery and status:
  - `rsim cluster list`
  - `rsim cluster status <job_dir>`
  - API: `/api/cluster/jobs`, `/api/cluster/status`
  - Status inspects `output/` and manager-style `OUT*` folders, output MF4s, logs, and `result.ini`.
- Added output fetch:
  - `rsim cluster fetch <job_dir> --dest <dir>`
  - API: `/api/cluster/fetch`
  - Copies output files back to `results/<project>/cluster/<run_id>` by default.
- Extended Web Console:
  - Cluster tab now has Python2 detection, prepared job list, per-job status, fetch, and dry-run submit actions.
  - Local tab now exposes `rsim run` through `/api/local/run`; default is dry-run unless `execute` is explicitly set.
- Verified:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q` -> `118 passed`
  - Web server restarted on `http://127.0.0.1:8765/`
  - `/api/cluster/jobs?project=ovrs25&limit=2` returns prepared packages.
  - `/api/cluster/python?project=ovrs25` returns all Python2 candidates as not usable.

2026-06-30 continuation 2:

- Cluster submit path is now productized around the current environment:
  - `rsim cluster check` no longer treats missing local Python2 as a blocker when XML-RPC manager submission is reachable.
  - Current verified submit mode is `xmlrpc`; check output shows `Submit path: xmlrpc`.
  - `Python for client.py` reports `C:\Python27\python.exe (not found); optional because XML-RPC submit path is reachable`.
- Status inspection now extracts high-signal failure summaries from `result.ini` and `selena.log`:
  - latest smoke state: `finished-failed`
  - OK/NOK: `0/1`
  - worker: `szhradar25`
  - useful Selena error: `no signal found in channel cache for port g_Golf_Fct_Hmi_RunnableHmi_internalstates`
  - interpretation: Cluster infrastructure path is proven through worker execution and output copy-back; the current failure is selected MF4/runtime signal compatibility, not submission plumbing.
- Web Console polish:
  - `web/index.html` and `web/app.js` restored to valid UTF-8 Chinese labels.
  - Cluster tab now has protected real submit buttons in addition to dry-run.
  - job list shows state, OK/NOK counts, file count, and first error summary line.
  - static server now returns `charset=utf-8` for HTML/CSS/JS.
- Verification:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q` -> `122 passed`
  - `python rsim.py --project ovrs25 cluster check` -> `Cluster check passed`
  - `http://127.0.0.1:8765/` -> 200, `text/html; charset=utf-8`
  - `http://127.0.0.1:8765/app.js` -> `text/javascript; charset=utf-8`
  - `/api/cluster/check?project=ovrs25` returns all Cluster checks OK, including XML-RPC submit path.
  - `/api/cluster/status?...smoke_fr_20260630_safeout` returns the real failed smoke result and error summary above.

Next best work:

- Find or generate one MF4/runtime pair that contains `g_Golf_Fct_Hmi_RunnableHmi_internalstates`, then submit a single-file smoke through the existing XML-RPC path.
- After one smoke succeeds, enable guarded batch prepare/submit from the Web Console for a selected directory rather than the whole BYD_SR root by default.

2026-06-30 continuation 3:

- Added bounded Cluster input data scanning:
  - CLI: `rsim cluster data [input_path] --dataset BYD_SR --limit N --max-read-mb M --required-signal <name>`
  - API: `/api/cluster/data?project=ovrs25&dataset=BYD_SR&limit=...&max_read_mb=...&required_signal=...`
  - Web Console: Cluster tab now has `扫描候选数据`, `Required signal`, `扫描数量`, and `每文件读取 MB` controls.
  - Candidate rows show `present`, `missing`, `missing-in-prefix`, `not-scanned`, or `error`; clicking `选用` fills the Cluster input path.
- Implementation notes:
  - scanner skips generated `*out.MF4` files.
  - it searches bounded head/tail byte segments instead of opening huge MF4s with `asammdf`.
  - it searches UTF-8 and UTF-16LE encodings of required signal names.
  - default required signal comes from `cluster.required_input_signals`, currently `g_Golf_Fct_Hmi_RunnableHmi_internalstates`.
- Data findings:
  - Direct `asammdf` metadata listing on remote BYD_SR MF4s was too slow for interactive use.
  - BYD_SR first 30 files, scanning 4 MB each, did not find `g_Golf_Fct_Hmi_RunnableHmi_internalstates`.
  - BYD_SR first 10 files, scanning 8 MB head/tail each, did not find it.
  - BYD_SR `28-4-26_DMS_FCW` first 5 files, scanning 32 MB head/tail each, did not find it.
  - Prior CBNA smoke file `...\smoke_20260626_201242\data\Gen5_2009-01-01_05-56_0114.MF4` was fully scanned (`273234976` bytes) and does not contain it.
- Runtime findings:
  - current `config/projects/ovrs25/assets/selena/runtime.xml` includes `g_Golf_Fct_Hmi_RunnableHmi`.
  - historical shared BYD_OVRS runtime exists at `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER\runtime_1r1v.xml`; it uses older runnable names such as `g_Fct_RunnableHmi_RunnableHmi_A`.
  - historical Selena executables exist:
    - `...\BYD_OVRS\BL01V7_ER\BYD_OVRS_Selena_Master\selena.exe`
    - `...\BYD_OVRS\BL01V7_ER\BYD_OVRS_Selena_Slave\selena.exe`
  - historical Config points at `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\DA\Radar\02_GEN5\09_BYD\EM2E\Pre-ER\PreER_10044C`, but that data path is currently not reachable from this session.
- Verification:
  - `pytest -q` -> `124 passed`
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `python rsim.py --project ovrs25 cluster data --dataset BYD_SR --limit 2 --max-read-mb 1`
  - `/api/cluster/data?project=ovrs25&dataset=BYD_SR&limit=2&max_read_mb=1&required_signal=g_Golf_Fct_Hmi_RunnableHmi_internalstates`
  - Web static files still return UTF-8 charset and include the new scan controls.

Current interpretation:

- The Cluster backend path remains operational.
- The next real smoke-success blocker is not submission, but choosing a runtime/Selena/data combination that agrees on HMI runnable measurement names.
- Do not submit large BYD_SR batches until a single-file candidate reports `present` or a known-compatible runtime is selected.

2026-07-01 continuation:

- Added Cluster runtime profiles so the Web/CLI can switch between multiple Selena/runtime/data assumptions without editing config by hand:
  - Core:
    - `list_cluster_profiles(config)`
    - `apply_cluster_profile(config, profile_name)`
    - `check_cluster_environment(..., profile=...)`
    - `scan_cluster_data(..., profile=...)`
    - `prepare_cluster_job(..., profile=...)`
  - CLI:
    - `rsim cluster profiles`
    - `rsim cluster check --profile <name>`
    - `rsim cluster data --profile <name>`
    - `rsim cluster prepare --profile <name>`
  - API:
    - `/api/cluster/profiles`
    - `/api/cluster/check?...&profile=...`
    - `/api/cluster/data?...&profile=...`
    - `/api/cluster/prepare` accepts `profile`
  - Web Console:
    - Cluster tab now has a `Profile` selector loaded from `/api/cluster/profiles`.
- Configured ovrs25 profiles:
  - `default`: current local build/runtime assets.
  - `byd-ovrs-bl01v7-er-shared`: historical shared BYD_OVRS BL01V7_ER Master/RadarFC/PSS1 profile.
  - `byd-ovrs-bl01v7-er-shared-fl-pss2`: historical shared BYD_OVRS BL01V7_ER Slave/RadarFL/PSS2 profile.
- Profile check results:
  - both historical profile Selena executables are reachable:
    - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER\BYD_OVRS_Selena_Master\selena.exe`
    - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER\BYD_OVRS_Selena_Slave\selena.exe`
  - shared runtime is reachable:
    - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\BYD_OVRS\BL01V7_ER\runtime_1r1v.xml`
- Generated profile packages:
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\dryrun_profile_api_20260701`
  - `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\smoke_profile_fl_pss2_20260701`
- Real submissions:
  - `dryrun_profile_api_20260701` submitted through XML-RPC, manager returned `value=1`.
    - official page job id: `10320`
    - subgroup: `PSS1`
    - observed page status: `1/0/0`, output `0 MB`, end `unknown`
    - output folder: `OUT_260701_130630`
    - local status: `running-or-started`, only copied `Config.cfg` so far.
  - `smoke_profile_fl_pss2_20260701` submitted through XML-RPC, manager returned `value=1`.
    - official page job id: `10321`
    - subgroup: `PSS2`
    - observed page status: `1/0/0`, output `0 MB`, end `unknown`
    - output folder: `OUT_260701_132004`
    - local status: `running-or-started`, only copied `Config.cfg` so far.
- Official web page:
  - `http://szhradar01/cluster/?page=jobs` is reachable from this session and returns 200.
  - HTTPS fails certificate trust from this shell, but HTTP is usable for viewing status.
- Verification:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q` -> `125 passed`
  - `/api/cluster/profiles?project=ovrs25` returns all three profiles.
  - `/api/cluster/check?project=ovrs25&profile=byd-ovrs-bl01v7-er-shared` returns profile Selena/runtime OK.
  - `/api/cluster/prepare` with `profile=byd-ovrs-bl01v7-er-shared` generated a valid package.

Current interpretation after profiles:

- The app now supports multiple Cluster runtime profiles end to end.
- XML-RPC submission and official web visibility are proven for profile jobs too.
- The latest blocker is worker execution progress for the historical shared profiles: both jobs are visible on the official page but have not written worker logs/result.ini yet.
- Do not submit broader batches until one profile smoke job reaches either `finished-success` or a clear worker/runtime failure.

2026-07-01 later continuation:

- Added official Cluster V2.0 status parsing:
  - CLI: `rsim cluster web-status <job-id-or-job-dir>`
  - API: `/api/cluster/web-status?project=ovrs25&job=<job-id-or-job-dir>`
  - Web Console: prepared job rows now include an `Official` action that queries the Cluster web page.
  - The parser reads `http://szhradar01/cluster/?page=jobs` to map a prepared package path to a job id, then reads `?page=tasks&jobid=<id>` for task details.
  - It preserves the readable state such as `simulating` and stores numeric DB state as `simulation_state_code` when present.
- Latest official status check:
  - job `10320` (`dryrun_profile_api_20260701`) is assigned to `szhradar14 (CC-DA.Simulation_Room)`, task DB id `5445488`, state `simulating`, started simulation at `2026-07-01 13:06:36`, python version `python27`.
  - job `10321` (`smoke_profile_fl_pss2_20260701`) is assigned to `szhradar26 (CC-DA.Simulation_Room)`, task DB id `5445489`, state `simulating`, started simulation at `2026-07-01 13:20:10`, python version `python27`.
  - Shared output folders currently contain only the manager-copied `Config.cfg`; no worker `result.ini`, logs, or output MF4 have appeared yet.
- Verification after the official-status parser:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q tests\test_cluster.py` -> `13 passed`
  - `pytest -q` -> `126 passed`
  - `python rsim.py --project ovrs25 cluster web-status 10320 --json` -> `state: simulating`
  - `python rsim.py --project ovrs25 cluster web-status 10321 --json` -> `state: simulating`
  - Web Console restarted on `http://127.0.0.1:8765/` with process id `73988`.
  - `/api/cluster/web-status?project=ovrs25&job=<job-dir>` maps `smoke_profile_fl_pss2_20260701` to official job `10321` and returns `state: simulating`.

Next cluster step:

- Poll `rsim cluster web-status 10320` and `rsim cluster web-status 10321` until either job writes `time_finished` or the server timeout/worker error appears.
- If they finish successfully, run `rsim cluster fetch <job_dir>` and use that as the first known-good profile batch template.
- If they fail or time out without logs, compare the official output path with the shared output folder and inspect whether the worker can execute the historical shared Selena path directly.

2026-07-01 wait-command continuation:

- Added a Cluster polling command:
  - `rsim cluster wait <job-id-or-job-dir> [--job-dir <prepared-job-dir>]`
  - `--once` prints one combined snapshot and exits.
  - `--json` includes official web status, shared-output status, and a `diagnosis` block.
  - `--interval` and `--max-minutes` support longer watch sessions without hand-running repeated `web-status`/`status` commands.
- Diagnosis logic combines:
  - official task state from `http://szhradar01/cluster/?page=tasks&jobid=<id>`
  - shared output folders from `inspect_cluster_job`
  - `success_count`, `fail_count`, output MF4 count, task error messages, runtime minutes, and configured Cluster timeout.
- Added Web Console wait integration:
  - API: `/api/cluster/wait?project=ovrs25&job=<job-id-or-job-dir>`
  - prepared job rows now include a `Wait` action.
  - Web `Wait` defaults to official-status-only so the page does not block on slow UNC output scans.
  - Use the existing `状态` action for shared-output inspection; API callers can pass `shared=1&job_dir=<dir>` when they explicitly want combined official/shared diagnosis and can tolerate slow UNC scans.
- Fixed a `wait` argument-resolution bug where numeric job ids could ignore an explicit `--job-dir`.
- Latest real wait snapshots:
  - `10320`: `simulating`, worker `szhradar14 (CC-DA.Simulation_Room)`, shared state `running-or-started`, no outputs/logs/result files, runtime about `45.7` minutes, stale `false`, timeout `120` minutes.
  - `10321`: `simulating`, worker `szhradar26 (CC-DA.Simulation_Room)`, shared state `running-or-started`, no outputs/logs/result files, runtime about `32.2` minutes, stale `false`, timeout `120` minutes.
- Verification:
  - `python -m py_compile core\cluster.py cli\cluster.py cli\web.py`
  - `pytest -q tests\test_cluster.py` -> `15 passed`
  - `pytest -q` -> `128 passed`
  - `http://127.0.0.1:8765/` -> 200, `text/html; charset=utf-8`
  - `/api/cluster/wait?project=ovrs25&job=10321` -> `outcome: running`, `state: simulating`, `stale: false`

Current cluster conclusion:

- Submission, official web visibility, worker assignment, and shared-output inspection are all automated.
- Both real profile smoke jobs are still inside their configured 120-minute runtime window and should not be treated as failed yet.
- Do not submit broader batches until either `rsim cluster wait ... --once` reports success/failure or the stale flag becomes `true`.

## First successful cloud simulation, 2026-07-01 (ovrs25 cloud-build profile)

Root cause of all prior cloud failures (job 10320/10321/10322 `finished-failed` / stuck `simulating`) was found and fixed — it was **not** a runtime/data schema incompatibility, it was a missing radar-source assignment on the cluster path.

### What was wrong

- `prepare_cluster_job` did not run radar orientation auto-detection, while local `rsim run` did (via `build_effective_simulation` → `detect_radar_orientation`).
- With ovrs25 `source: "auto"`, the cluster Config.cfg rendered `radar = ""` and `mountingPosition = ""`. Selena then defaulted to `RadarFC`.
- BYD_SR `12-5-26_CBNA` data is actually `RadarFL` (detected via mounting_position x=3.66, y=0.77, confidence 0.95). Running it as RadarFC caused `no signal found in channel cache for port g_Golf_Fct_Hmi_RunnableHmi_internalstates` and a 1448-byte output.
- A second defect: `cli/cluster.py _run_prepare` passed `copy_selena=bool(False)` instead of `None`, which suppressed the `selena.source=build` auto-package logic. Fixed to `or None` so profile-driven Selena packaging triggers.

### Fixes

- `core/cluster.py prepare_cluster_job`: when `source`/`mounting_position` are auto/unset and the input is a single MF4, call `detect_radar_orientation` and write the result into `sim` before rendering Config.cfg. Mirrors the local path.
- `cli/cluster.py _run_prepare` (and `_run_one_shot` already correct): pass `copy_data`/`copy_selena` as `None` when unset so profile adaptivity decides.

### Successful cloud run

- Profile: `cloud-build` (backend=cluster, selena.source=build → packaged local selena.exe + 10 DLLs ≈90 MB into job folder, data.copy=false → BYD_SR referenced in place on UNC).
- Input: `\\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon2\OverseaData\Driving\AU_data\BYD_SR\12-5-26_CBNA\12-5-26_CBNA\Gen5_2009-01-01_03-57_0115.MF4` (393 MB).
- Detected: `radar=RadarFL`, `mountingPosition=CFL`.
- Worker: `szhradar27` (PSS1). Init 24s + simulation 51s.
- Output: `Gen5_2009-01-01_03-57_0115out.MF4` = **537 MB** (> input, success).
- `result.ini`: `successfull=1`, `simulation_state=4`, `error_message=` (empty), `out_size` reported 0 but the 537 MB MF4 is present on disk.

### Verification

- `pytest -q` → `159 passed` (no regression from the cluster fixes).
- Note: cluster web page `http://szhradar01/cluster/?page=jobs` returned 404 during this session; XML-RPC manager on 8123 was still reachable and submission succeeded. Track cloud jobs via shared `OUT_*` output folders (`rsim cluster status` / `rsim cluster wait`) when the web page is down.

### Note on signal scanning

- Earlier signal scans of BYD_SR concluded the data lacked `g_Golf_Fct_Hmi_RunnableHmi_internalstates`. That conclusion was misleading: the `_internalstates` variant is a runtime port name that does not appear as a raw byte string in the MF4 prefix; both the local-passed CBNA_23-4-26 and the cloud-passed BYD_SR 12-5-26_CBNA scan as `missing-in-prefix` for it, yet both simulate successfully when `source=RadarFL` is set. The plain runnable name `g_Golf_Fct_Hmi_RunnableHmi` is present in both. Do not use `_internalstates` signal scans as a compatibility gate — use radar orientation detection + a real single-file smoke instead.

## Batch cloud simulation + configuration + unified check + dual API, 2026-07-01 (evening)

### Batch cloud verification

- Re-submitted the other 2 MF4s of BYD_SR `12-5-26_CBNA` via `cloud-build` profile. All 3 finished-success, ~512 MB output each, parallel workers (szhradar26/27). Batch path proven.

### Configuration management (Phase 2)

- Reused the existing `local.yaml` gitignored overlay for per-user needs (different Selena branch / repo / data / profile). No new config layer.
- New `rsim config` command (`cli/config.py`): `show` (effective merged config), `init` (copy local.example.yaml → local.yaml), `diff` (which keys local.yaml overrides vs config.yaml).
- `local.example.yaml` expanded with A/B/C scenario docs (develop branch + shared data / feature branch + local data / different machine toolchain).
- profile gained optional `selena.selena_branch` so checks can warn on exe/branch mismatch.
- README gained a "用户配置指南" section.

### Unified environment check (Phase 1)

- `CheckItem` gained `severity` (error|warning|info) and `category` (repo|selena|runtime|data|cluster|profile); defaults keep old callers working.
- New `core/repo.py` consolidates repo checks (outer/inner existence, branch match, dirty tree, submodule init) and `prepare_repo_context` (branch switch before build). `cli/check.py::_check_repo_context` and `cli/build.py::_prepare_repo_context` now delegate to it.
- New `CheckReport` dataclass (`.ok`/`.errors`/`.warnings`/`.items`), `__iter__` for transitional compat. `check_for_backend` returns `CheckReport`.
- `check_local_environment` now covers repo + selena.exe + exe/branch freshness (mtime vs branch ref) + runtime/adapter + data reachability + radar orientation detectability.
- `rsim check --backend local|cluster --profile <name>` prints severity-graded items (OK / W / !!).

### Public Python API (Phase 3)

- New `core/api.py` (API_VERSION 1.0) — stable entry for software integration: `load_project`, `list_profiles`, `check_environment`, `prepare_simulation`, `run_local`, `submit_cluster`. Other software `from core.api import *`.
- `run_local` uses subprocess isolation (same as web) to keep a selena crash from taking down the caller.
- `core/__init__.py` declares `__all__ = ["api"]` + `__version__`.

### Web frontend (Phase 4)

- Fixed `/api/cluster/prepare` passing `bool(False)` instead of `None` for copy_data/copy_selena (same profile-adaptivity bug fixed in cli/cluster.py earlier).
- New endpoints: `GET /api/profiles` (unified, includes local backend), `GET /api/check` (CheckReport with severity), `POST /api/cluster/run` (non-blocking prepare+submit; front-end polls `/api/cluster/wait?once=1`).
- Front-end: new "环境校验" tab renders severity-graded items; Profile dropdown unified via `/api/profiles`.

### Verification

- `pytest -q` → `183 passed` (was 159; +11 environment, +9 api, +4 web).
- `rsim --project ovrs25 check --backend local --profile local-build` reports repo/selena/runtime/data with severity.
- `python -c "from core.api import check_environment; print(check_environment('ovrs25', profile='local-build').ok)"` → True.
- Web `/api/check` and `/api/profiles` return expected JSON; `/api/cluster/prepare` passes None for unset copy flags.

### Next

- bydod25 cloud profile is configured (`cloud-build`) but not yet smoke-tested on cluster (local bydod25 simulation already passes). A bydod25 cloud smoke would need BYD_SR data compatibility or bydod25's own Vehicle_FR5CP data staged to a shared path.
- Web front-end "一键运行" button wiring (poll loop after `/api/cluster/run`) can be polished further; the backend endpoint is in place.

### 控制平面 + web 接入（2026-07-03）

- **清理**：删除误生成的 `profiles.json`（HTTP 404 HTML）和一次性脚本 `_dry.py`。
- **agent 编码**：`cli/agent.py` 的 `Popen` 加 `encoding="utf-8", errors="replace"`，修复 Windows charmap 遇中文 stdout 崩溃。
- **`rsim tcc` CLI**（新 `cli/tcc.py`）：`bootstrap-itc2` / `install <tc>` / `auto-repair` / `status`，把 `core/tcc.py` 的纯 Python API 暴露成子命令，供 agent 调度。
- **agent 加 tcc task_type**：`_build_task_command` 加 `tcc.bootstrap_itc2` / `tcc.install_toolcollection` / `tcc.auto_repair_all` 三分支，`DEFAULT_CAPABILITIES` 加对应能力。
- **适配层 `core/web_control.py`**（新）：把 web 端点的 `BuildTask.tail()` 11 字段 shape 桥接到 control plane 的 job/task 模型——状态映射（`succeeded→success`）、`log_id` 当 `total_lines` 游标、缺字段（exe_path/files_done 等）从 `job.result` 取。`ControlService.list_jobs()` 新增。
- **`rsim web` 内置控制平面**：启动时拉起 control server（线程，127.0.0.1:8877，`results/_control.db`）+ polling agent（线程，复用 `cli.agent._run_task`，走本机 HTTP）。build/sim/tcc/cancel/tasks/repair 端点全转适配层。`_tail_task` 带 legacy `BuildTaskRegistry` fallback（旧 task_id 仍可查）。`--no-control` 退回旧路径。
- **前端零改动**：响应 shape 不变，localStorage 恢复逻辑兼容（job_id 当 task_id 用）。
- **端到端验证**：前端编译 → job 进 control DB → 内置 agent 认领 → 跑 `rsim build selena` → 122 行日志回传 → 前端轮询看到；tcc bootstrap_itc2 → `rsim tcc bootstrap-itc2` → success。重启 web 后残留 queued job 自动被 agent 认领（持久化恢复）。
- **测试**：`pytest -q` → 312 passed（+9 web_control、+2 agent tcc、+4 web 集成）。
- **Linux 迁移路径**：代码已统一，拆 `rsim server serve` 到 Linux + web 加 `--server-url` + agent 留 Windows 即可，零特例。详见 `SIMULATION_WORKFLOW.md` §10。

### 多用户隔离 + 分发（2026-07-03）

- **KI-1 记录**：web 云端仿真失败（Cluster job 10319, Returncode=-1）记入 `docs/KNOWN_ISSUES.md`，含现象/已排除/4个怀疑方向/排查步骤。根因需下次跑时抓 worker stderr。
- **P1 RSIM_HOME**：`core/config.py` 加 `get_data_root()`（读 RSIM_HOME，缺省回退仓库根）。results/DB/task_store 跟随；config/assets 不变（代码资源）。`control_service`/`simulation` 内的 `_data_root()` 同步。向后兼容。
- **P2 每用户 DB**：`core/user.py`（current_user/control_db_path_for_user）。user 标识 = RSIM_USER > OS用户 > default。DB = `_control_<user>.db`。HTTP 链路用 `X-Rsim-User` 头传递；`control_http.make_control_handler` 接受 service 或 `(user)->service` factory；server serve 用 per-user service 缓存。**互不可见验证通过**（alice 看不到 bob job，HTTP 404）。
- **P3 唯一化**：embedded agent_id = `embedded-<user>-<pid>`（不再硬编码）；agent --agent-id 缺省 = `agent-<user>-<hostname>`；web 端口绑定失败 fallback 随机端口。
- **P4 local.yaml 用户目录**：`local_yaml_path_for_project()` 优先 `$RSIM_HOME/config/projects/<name>/local.yaml`，回退仓库内。save/load/list/export/import 端点全适配。
- **P5 _runtime 并发隔离**：`_runtime/<pid>/` 子目录，CRlog.log 和 paramconfig 按进程隔离，同项目并发不覆盖。
- **P6 分发**：`setup.py` install_requires 只留 PyYAML，重依赖移到 `extras_require[full]`，新增 `[control]`（轻量）。`scripts/build_server_pyz.py` 打 14KB zipapp 单文件（server 专用，任意 python3.9+ 跑）。`docs/server-deploy.md` 加分发 + 多用户章节。
- **端到端验证**：两 RSIM_USER（alice/bob）连同一 server，alice agent 只认领 alice job（→succeeded），bob job 不被碰（仍 queued），两独立 DB 文件。
- **测试**：312 → 319 passed（+6 user +1 http 隔离）。

### web 接远程 server + 用户标识（2026-07-03）

- **RemoteControlClient**（`core/remote_control.py`）：轻量 HTTP 客户端，镜像 web 需要的4操作（create_job/get_job/get_logs/cancel_job/list_jobs），所有请求带 `X-Rsim-User` 头。不复用 agent 的 `_ControlClient`（那是 agent 专用）。
- **GET /api/jobs 端点**：`control_http.py` 加列表路由，调 `service.list_jobs(limit)`。返回 `{"jobs":[...]}`。
- **web_control 远程模式**：`set_remote_client(client)` 注入。各函数（start_build/sim/tcc/tail/cancel/list_jobs）有 remote client 走 HTTP、否则走本地 `_service()`。`tail_via_control` 抽出 `_tail_from_job_and_logs` 共享本地/远程（复用 `_map_status`/`_extract_errors` 纯函数）。404 → `{"found":False}`。
- **web --server-url/--user**：`rsim web --server-url http://server:8877 --user alice` → 跳过内置 server/agent，构造 RemoteControlClient 注入 web_control。三种模式：内置（默认）/远程（--server-url）/禁用（--no-control）。浏览器零改动（单用户每 web 实例）。
- **端到端验证**：本机 server + `rsim web --server-url` → web 投 build job → 转发到远程 server → job 进 alice DB → bob 看不到（隔离）。
- **本地 Selena + 云端 UNC 数据 dry-run**：本地 smoke MF4 dry-run 通过（paramconfig 在 `_runtime/<pid>/`，radar 检测 FL conf=0.95）。UNC 375MB 文件 dry-run 因网络读取慢未完成（非 bug，是 UNC 大文件体验问题）。
- **测试**：319 → 326 passed（+7 remote_control + web_control 远程模式）。
- **三种 web 模式定型**：内置 / 远程 / 禁用，覆盖单机、跨机多用户、legacy 三种场景。

## Linux 控制面迁移盘点与交接，2026-07-07

### 目标边界

- Linux 只提供控制面服务：job/task 调度、agent 注册与认领、日志/结果存储、per-user 路由、web/SDK 入口。
- Selena 编译、本地仿真、TCC/VS/bat、用户本机数据访问仍全部在 Windows 用户电脑执行。
- Windows 侧通过 `rsim agent --server-url http://<linux>:8877` 主动轮询，不要求用户电脑开放入站端口。

### 当前实施状态

| 领域 | 状态 | 关键文件 |
|------|------|----------|
| 控制面存储与状态机 | 已实现。SQLite 持久化 agents/jobs/tasks/logs，支持 ordered task、cancel、wrong-agent 拒绝、重复完成拒绝、dead-agent reclaim。 | `core/control_service.py`, `tests/test_control_service.py`, `tests/test_reclaim.py` |
| HTTP 控制接口 | 已实现。stdlib `ThreadingHTTPServer` handler，支持 health、job create/get/list/logs/cancel、agent register/poll/heartbeat、task logs/result，按 `X-Rsim-User` 路由。 | `core/control_http.py`, `tests/test_control_http.py` |
| CLI server | 已实现。`serve/create-job/get-job/get-logs/cancel/reclaim`，`NO_CONFIG=True` 可在 Linux 轻量 server 环境运行。 | `cli/server.py` |
| Windows agent | 已实现。主动 register/poll/heartbeat，调用本机 `rsim check/build/run/cluster/tcc`，回传 stdout/result，支持取消和启动失败回报。 | `cli/agent.py`, `tests/test_control_agent.py` |
| web 接远程 server | 已实现。`rsim web --server-url ... --user ...` 使用 `RemoteControlClient`，远程模式不启动内置 agent。 | `cli/web.py`, `core/remote_control.py`, `core/web_control.py`, `tests/test_remote_control.py`, `tests/test_web_control.py` |
| 多用户隔离 | 已实现。`RSIM_USER` / OS user 映射到 `_control_<user>.db`，HTTP 通过 `X-Rsim-User` 头选择用户 DB。 | `core/user.py`, `tests/test_user.py` |
| Linux 分发 | 已实现。server-only zipapp 和 Dockerfile，zipapp 仅打包 stdlib server 依赖。 | `scripts/build_server_pyz.py`, `Dockerfile`, `tests/test_server_pyz.py`, `docs/linux-server-deploy.md` |
| 部署文档 | 已有。Linux 部署指南、server deploy 文档、跨机 E2E runbook、KNOWN_ISSUES。 | `docs/linux-server-deploy.md`, `docs/server-deploy.md`, `docs/e2e-linux-windows-runbook.md`, `docs/KNOWN_ISSUES.md` |

### 本次复核证据

```bash
python -m pytest tests\test_control_service.py tests\test_control_http.py tests\test_control_agent.py tests\test_user.py tests\test_remote_control.py tests\test_web_control.py tests\test_reclaim.py tests\test_server_pyz.py -q
# 56 passed

python -m pytest -q
# 336 passed in 105.85s

python rsim.py server --help
# serve/create-job/get-job/get-logs/cancel/reclaim present

python rsim.py web --help
# --server-url / --user / --no-control present

python rsim.py agent --help
# --server-url / --agent-id / --capability / --once present
```

### 已知缺口与风险

- ~~`docs/e2e-linux-windows-runbook.md` 写了 `GET /api/agents` 用于验证 agent 注册，但当前 `core/control_http.py` 没有 agent list 路由，`ControlService` 也没有 `list_agents()`。~~ **✅ 2026-07-07 已补齐**：`ControlService.list_agents()`、`GET /api/agents`、`rsim server list-agents`、web `/api/agents` 全部实现并测试。runbook 验证步骤已更新。
- `docs/server-deploy.md` 仍有旧注记“web --server-url 当前未实现”，但代码中已经实现。需要清理旧文档，避免误导。
- `X-Rsim-User` 是可信内网头，不是鉴权。任意人可伪造 user 头访问对应 DB，生产化前必须加 token 或反向代理鉴权。
- remote web 模式只负责投 job 和看状态，不内置执行 agent。没有 Windows agent 时 job 会一直 queued。
- 跨机投递不要传 Linux/Windows 本机路径。Linux 投给 Windows agent 时优先用 `--project` 和 `--dataset`，让 agent 在自己的 Windows 配置里解析路径。**注**：`server create-job` 的 `--project` 曾被空默认值覆盖导致 project 丢失，2026-07-07 已修复（见下方「端到端 loopback 回归」）。
- `server reclaim` 目前是手动 CLI，不是 server 内置周期任务。agent 崩溃后需要人工或外部定时任务调用。
- 文档记录过真实跨机链路通过：Linux Ubuntu + Python 3.10 跑 `rsim_server.pyz`，Windows agent 连接并执行回传。2026-07-07 本次只做本机代码和测试复核，没有重新跑真实跨机 build/sim。

### 下一步改造计划

1. ~~**补齐可观测性 API**~~ **✅ 2026-07-07 完成**
   - ~~增加 `ControlService.list_agents()`。~~ 已实现（`core/control_service.py`）。
   - ~~增加 `GET /api/agents`。~~ 已实现（`core/control_http.py`），返回 agent_id/name/status/last_heartbeat/current_task_id/capabilities/hostname/platform。
   - ~~增加 `rsim server list-agents`。~~ 已实现（`cli/server.py`）。
   - ~~更新 `docs/e2e-linux-windows-runbook.md` 的 agent 注册验证步骤。~~ 已更新。
   - **额外**：web 前端 `/api/agents` 端点（`cli/web.py` + `core/web_control.py:list_agents_via_control` + `core/remote_control.py:RemoteControlClient.list_agents`），嵌入式与远程模式共用。

2. **自动化 dead-agent 回收**
   - 在 `rsim server serve` 增加可选参数：`--reclaim-interval`、`--stale-after`、`--max-attempts`。
   - 默认可先关闭或保守启用，避免误杀长时间无 stdout 但 heartbeat 正常的任务。
   - 保留 `rsim server reclaim` 作为人工运维入口。

3. **补最小鉴权**
   - 增加 `RSIM_SERVER_TOKEN` / `--token`。
   - server 校验 `Authorization: Bearer <token>` 或 `X-Rsim-Token`。
   - agent、RemoteControlClient、web `--server-url` 都带 token。
   - 文档保留可信内网模式，但明确生产必须启 token。

4. **整理部署文档**
   - 合并或互相引用 `docs/linux-server-deploy.md` 与 `docs/server-deploy.md`，消除旧状态冲突。
   - 更新 `README.md` 控制平面章节，列出三种模式：embedded / remote / legacy。
   - 在 runbook 中标注“Linux 投递用 project/dataset，不传本机路径”。

5. **真实跨机回归**
   - 重新构建 `dist/rsim_server.pyz`。
   - Linux 起 server，Windows 起 agent。
   - 依次验证：agent list、local.check、local.build_selena dry path、local.run_sim dry-run、cancel、reclaim。
   - 如果可用真实数据，再跑一条 CBNA smoke，记录 job_id、agent_id、输出大小和日志证据。

6. **SDK 化**
   - 基于 `core.remote_control.RemoteControlClient` 提供稳定 `radar_sim_sdk` 包装。
   - 固定 create/check/build/run/status/logs/cancel 的 Python API。
   - 后续前端和外部自动化只依赖 SDK，不直接拼 HTTP payload。

## 端到端 loopback 回归（2026-07-07）

依据「Linux 控制面迁移盘点」第 5 项「真实跨机回归」执行。真实跨机（Linux server + Windows agent 双机）需 Linux 环境，本次先在本机用 `dist/rsim_server.pyz` + `rsim agent` 跑 loopback 端到端，验证控制面全链路（跨机只差网络，逻辑同构）。

### 已重建产物

- `dist/rsim_server.pyz` 重新构建（16341 bytes），含本次 create-job 修复。
- 启动方式不变：`python rsim_server.pyz server serve --host 0.0.0.0 --port 8877 --db-path <db>`。

### 链路验证证据（loopback，RSIM_HOME=/tmp/rsim-e2e 隔离）

| # | 链路 | 结果 | 证据 |
|---|------|------|------|
| 1 | agent 注册 + local.check | ✅ | job_fef278540388 → succeeded，returncode 0，日志回传 `[agent] starting local.check` + check 输出 |
| 2 | run_sim dry-run（带 project） | ✅ | job_04b9c5a4b007 → succeeded，dry-run 打印完整仿真计划（selena.exe 路径、input/output MF4、paramconfig 在 `_runtime/<pid>/`、radar FL conf=0.95） |
| 3 | cancel | ✅ | job_f9df8632b6de → status=cancelled，cancel_requested=true |
| 4 | reclaim（dead-agent 回收） | ✅ | job_b60fec25861e task 被 agent 认领为 running 后 agent 被 kill；`server reclaim --stale-after 3` 把 task 重新入队 queued，attempt_count=1，assigned_agent 清空 |
| 5 | build_selena dry path | ⚠️ 未单独跑 | build 命令无 `--dry-run`，会真实调 VS/selena 编译链；端到端投递链路已被单测覆盖，真实编译留待有工具链的跨机回归 |
| 6 | agent list（`GET /api/agents`） | ❌ 未实现 | P1 缺口属实：`/api/agents` 返回 404，`rsim server list-agents` 不存在。runbook 验证 agent 注册步骤依赖它 |

### 修复：server create-job project 丢失 bug（阻断跨机投递）

**现象**：`rsim server create-job local.check --project ovrs25 --backend local` 投递后，task payload 里 project 为空。`--payload-json '{"project":"ovrs25"}'` 同样丢失。这直接违反 HANDOFF.md:788「跨机投递优先传 project/dataset」约束——project 根本传不进 task。

**根因**（`cli/server.py:_run_create_job`）：
1. create-job 子命令定义了自己的 `--project`（default=""）。argparse 子命令 namespace 会覆盖父 parser 同名属性，所以全局 `rsim --project ovrs25 server create-job` 的 ovrs25 也被 default="" 覆盖。
2. `task_payload.update({..."project": args.project or ""...})` 无条件用（可能为空的）CLI 字段覆盖 `--payload-json` 里的 project。

**修复**：CLI 标志字段改为「非空才覆盖 payload_json」，让 `--payload-json` 成为 project 等字段的可靠来源。`server create-job --project ovrs25` 和 `--payload-json '{"project":"x"}'` 两种方式现在都正确进 payload。

**回归测试**：`tests/test_server_pyz.py` 新增 `test_create_job_project_flag_lands_in_payload`、`test_create_job_payload_json_project_survives`（subprocess 隔离，避免污染 sys.modules）。全量 336 → 338 passed。

### 新发现的运维问题（未修，记入 KNOWN_ISSUES 候选）

1. **`rsim server get-logs` 在中文 Windows 终端崩溃**：DB 里存的日志含中文（如 check 输出），`get-logs` CLI 用 charmap 打印到 cp936 终端时报 `'charmap' codec can't encode`。需 `PYTHONUTF8=1` 才能正常打印。agent 端已修（HANDOFF.md:709），但 server CLI 端的打印路径未修。
2. **agent 投递 `--select` 任务必失败**：`rsim run --select` 是交互式（要 stdin 输入文件号），agent 的 `subprocess.Popen` 在非交互环境 stdin 为空，`input()` 立即 EOF → "No files selected" → returncode 1。**agent 投递的 run_sim 任务不要用 `--select`**，应直接传 `--input-mf4` 或 `--dataset`（不带 select）。需在 runbook 标注。

## 真实跨机端到端回归（2026-07-07）

在 Linux 服务器（10.190.171.44，Ubuntu 22.04 + Python 3.10.12）部署代码、启动控制服务，Windows 本机起 agent 跨机连接，重跑 P4 全链路。**6 条链路全部通过。**

### 部署方式

- 代码同步：本地 `tar` 打包（排除 `__pycache__`/`results`/`dist`/`*.MF4`/`*.db`）通过 ssh 管道传到 `~/radar-sim/`（Windows 无 rsync，用 tar over ssh）。
- Linux 启动：`python3 rsim.py server serve --host 0.0.0.0 --port 8877 --db-path ~/rsim_data/cross.db`（nohup 后台）。
- 端口：ufw 虽激活但 8877 实测可达（无需额外放行；HANDOFF.md:91 旧注记「需 sudo ufw allow 8877」未复现）。
- Windows agent：`RSIM_USER=alice NO_PROXY=10.190.171.44 python rsim.py agent --server-url http://10.190.171.44:8877`（`NO_PROXY` 绕过 Bosch 代理，与 HANDOFF.md:76 一致）。
- **投 job 到远程 server 必须用 HTTP POST `/api/jobs`**（curl 或 RemoteControlClient）。`rsim server create-job` CLI 只写本地 DB，不投远程——这是设计（server CLI 用于本地 DB 运维）。

### 链路验证证据（跨机，agent=win-agent-cross，user=alice）

| # | 链路 | 结果 | 证据 |
|---|------|------|------|
| 1 | 跨机 health + 空状态 | ✅ | Windows `curl http://10.190.171.44:8877/health` → `{"ok":true}`；`/api/agents` `/api/jobs` 空返回 `[]` |
| 2 | agent 跨机注册 + list-agents | ✅ | Windows agent 注册后，Linux `GET /api/agents` 见 `win-agent-cross \| Windows-11 \| hostname WX8-C-0001A \| idle`；`rsim server list-agents` CLI 同样可查 |
| 3 | local.check 跨机 | ✅ | job_de808aad39d2 → succeeded, rc=0, agent=win-agent-cross；日志跨机回传（含 Windows 路径 `C:\BYD_OVS_CB`） |
| 4 | run_sim dry-run 跨机 | ✅ | job_eec3e82ece5a → succeeded；dry-run 日志显示 selena.exe 路径、paramconfig `_runtime/<pid>/`、radar FL conf=0.95，全跨机回传 |
| 5 | cancel 跨机 | ✅ | job_4103cabe859b 投递 → cancel_requested → agent heartbeat 检测 → task=cancelled, rc=1 → agent 回 idle |
| 6 | reclaim 跨机 + 恢复 | ✅ | 真实 run job 认领后 kill agent → task 卡 running → Linux `rsim server reclaim --stale-after 3` → task 重新 queued, attempt_count=1 → 新 agent 注册立即认领，job 重新 running |
| 7 | build_selena dry path | ⚠️ 未单独跑 | build 无 `--dry-run`，真实编译需 VS 工具链；投递链路已被单测 + 上述链路覆盖 |

### 跨机发现的测试可移植性问题（已修）

- `tests/test_control_agent.py::test_build_task_command_for_local_run_sim_matches_cli_flags` 断言 `sys.executable` 以 `python` 结尾，但 Linux 上是 `/usr/bin/python3`（以 `python3` 结尾）。已修为兼容 `python3`。
- `tests/test_v4.py::TestCLI::*`（18 个）硬编码 `subprocess.run(["python", "rsim.py", ...])`，Linux 无 `python` 只有 `python3` → FileNotFoundError。**未修**（既有的测试可移植性问题，不在本轮控制面目标范围；控制面测试全过）。后续可统一改用 `sys.executable`。

### Linux 测试

- 控制面套件：63 passed（test_control_service/http/agent/user/remote_control/web_control/reclaim/server_pyz）。
- 全量：325 passed, 18 failed（均为 test_v4 的 `python` 硬编码问题，非控制面）。

### 下一步（真实跨机回归剩余项）

- ~~拿到 Linux 环境重跑 6 条链路~~ ✅ 2026-07-07 完成。
- ~~先实现 P1（`GET /api/agents` + `list_agents`）~~ ✅ 已完成并跨机验证。
- 跨机 build_selena 真实编译回归（需 Windows agent 本机有 VS + selena 源码）。
- CBNA smoke 真实跑一条（非 dry-run），记录 job_id/agent_id/输出大小/日志。本机数据 `D:/data/byd/...CBNA_23-4-26` 可用，agent 在 Windows 本机执行即可。
- 修 test_v4.py 的 `python` 硬编码（改 `sys.executable`），让 Linux 全量测试也绿。

## 双模式架构调整（2026-07-07）

### 策略转变

之前 Linux 迁移让 server 接受全部 4 种 task_type（local.check / local.build_selena / local.run_sim / cluster.run），Windows agent 跨机跑 local task。用户认为这不合理：**Linux 提供服务时仿真应仅走 cluster 链路**（依赖最少，集群节点有 selena/MATLAB/Qt，Windows 接入端无需繁重依赖）；**本地编译预设 Windows 用户 clone 仓一键部署**。

确立**双模式**架构（同一份代码，按部署模式启用不同 task_type 集合）：

- **模式 A（Linux 服务，cluster-only）**：Windows 用户不 clone 完整工具链，装 Python+PyYAML+agent 连 Linux server；server 用 `--allowed-task-types cluster.run` 启动，拒绝 local task（HTTP 400）；agent 默认 capability 为 `cluster.run`（+ tcc.*）。
- **模式 B（Windows 本机仓，完整能力）**：clone 仓一键部署，保留 local + cluster 双能力，`rsim web` 前后端齐全。

### 本次完成

**模式 A：**
- `cli/server.py`：`serve` 加 `--allowed-task-types`（逗号分隔，默认空=全允许）。
- `core/control_http.py`：`make_control_handler(service, allowed_task_types=None)`；POST `/api/jobs` 校验 `job_type` 和 `tasks[].task_type`，不在白名单返回 400。**白名单默认空=全允许**，模式 B 零影响。
- `cli/agent.py`：`DEFAULT_CAPABILITIES` 收窄为 `["cluster.run", "tcc.*"]`；新增 `FULL_CAPABILITIES` 供模式 B；`_build_task_command` 的 local 分支**保留**（模式 B 显式 `--capability local.*` 仍能用）。
- `Dockerfile`：CMD 加 `--allowed-task-types cluster.run`。
- 文档：`docs/linux-server-deploy.md` / `docs/e2e-linux-windows-runbook.md` / `SIMULATION_WORKFLOW.md` §10.3/10.5/10.6 全部改为模式 A 仅 cluster.run 示例 + 双模式说明。

**模式 B 核心：**
- `scripts/bootstrap.ps1`：PowerShell 一键部署（Python 检测→venv→依赖→local.yaml→doctor+check），支持 `-Project`/`-SkipDeps`/`-SkipCheck`，幂等可重跑，支持 `third_party/python-wheels/` 离线装。
- `cli/doctor.py`：新增 `rsim doctor` 子命令。系统级诊断（VS2017/2019/2022 实际安装、MATLAB/Qt/Boost/selena_env 路径存在性、Python 包可导入性、集群 UNC 可达性、cluster profile selena source），输出分级 ok/warning/error + 修复建议，支持 `--backend`/`--json`。区别于 `rsim check`（配置一致性），doctor 探测真实机器。

**文档：**
- `README.md`：快速开始重写为双模式表格 + bootstrap.ps1 路径；命令一览加 `rsim doctor`；控制平面示例加 `--allowed-task-types` 和模式 A/B 区分。
- `docs/environment-setup.md` §10：`bootstrap.ps1` 和 `rsim doctor` 从 TODO 标为已实现。

**测试（358 全绿，零回归）：**
- `tests/test_control_http.py`：新增 4 个白名单用例（`cluster_only_server` fixture + 拒绝 local / 接受 cluster.run / 拒绝 tasks[] 内 local / 默认 server 全允许）。
- `tests/test_doctor.py`：新增 11 个用例（路径存在/缺失、VS 版本匹配/不匹配、deferred env path、cluster UNC 可达/不可达、JSON 输出、返回码、backend 过滤）。
- 现有 local task 测试全保留（模式 B 仍支持），未改动。

### 后续待办（HANDOFF）

1. **`scripts/build_agent_pyz.py`（A3）** — 把 agent + cluster 链路打成单文件 pyz，让模式 A 的 Windows 端无需 clone 完整仓。当前模式 A 暂以 clone 仓 + `rsim agent` 接入。
2. **`rsim config init --auto-detect`（B3）** — 扫描本机路径自动填 `local.yaml` 的 `environment.*`，复用 doctor 的检测函数。当前 bootstrap.ps1 用"复制模板 + doctor 诊断 + 手填"。
3. **离线 wheel 目录 `third_party/python-wheels/`（B4）** — 内网 asammdf 等 C 扩展包的离线安装方案。bootstrap.ps1 已预留离线装逻辑（目录非空则 `--no-index --find-links`），但目录本身未预置。
4. **server 端 config.yaml 预置 cluster-inherent 默认（A4 延伸）** — 把 workspace_root / software_path / group / subgroup / 共享 selena 包路径固化到 server 侧 config，让用户侧只指定 project + input + profile。
5. **bootstrap.ps1 注释里的 em dash 在 cp936 终端乱码** — 纯注释问题，不影响执行，低优先级可改 ASCII。
6. **VS 检测增强** — `doctor._check_visual_studio` 目前只扫 `C:\Program Files (x86)\Microsoft Visual Studio\{2017,2019,2022}`，可加 vswhere.exe 或注册表扫描覆盖非标准安装路径。

## 鲁棒性修复 + 实际数据仿真验证（2026-07-07，晚）

### 目标重述

Linux 提供仿真服务（前端页面 + 后端接口），不同用户通过本地 Selena 或指定路径 Selena 在 cluster 完成仿真；Windows 源码用户支持本地仿真 + cluster 仿真；配置精简、环境校验、鲁棒性好；用实际数据做仿真测试。

### 修复的 bug

1. **doctor selena.exe 字段名 bug**（`cli/doctor.py`）：`_check_cluster_dataset_profile` 读 `selena.path`，但 unified profile 字段是 `selena.exe`。改为用 `core.profiles.list_profiles` 统一解析（含 legacy `cluster.profiles[].selena_exe` 转换），字段读 `selena.exe`。之前对 source=path 的 cluster profile 永远报 "no path given" false warning。
2. **check/doctor 在 cluster-only 机器误报 local 工具链缺失**：
   - `cli/doctor.py`：`--backend` 默认改为 auto——有 local profile 或工具链路径时跑 all，否则只跑 cluster。新增 `_infer_backend`。
   - `cli/check.py`：`run` 不带 `--backend` 时，若 `_is_cluster_only_config`（无 local profile + 无 matlab_root/qt_path/boost_root/BOOST_ROOT/selena_env_path/vs_version/python3_path）则自动走 cluster 检查。避免模式 A 机器上 BOOST_ROOT/build 脚本/VS 误报 error。
   - 两者判定 key 同步（含大写 BOOST_ROOT）。
3. **doctor `--json` 模式日志污染**：`--json` 时把 root logger 调到 WARNING，避免 numexpr/asammdf 的 INFO 日志混入 stdout 破坏 JSON 解析。

### 新增文档

- `docs/cluster-only-quickstart.md`：模式 A 快速开始。明确 Windows 端最小配置（无需 local.yaml、无需 MATLAB/Qt/Boost/VS），用 `selena.source: path` 指向集群共享 selena.exe + UNC dataset。含配置精简要点表、Selena 分支说明、故障排查。

### 实际数据仿真验证（真实跑通）

**模式 A（Linux cluster-only 服务）端到端** — 本地起 server + agent 模拟：
- server：`rsim server serve --port 8890 --allowed-task-types cluster.run`
- 白名单生效：`local.run_sim` / `local.build_selena` → HTTP 400；`cluster.run` → HTTP 201 queued。
- agent（`--once`）认领 cluster.run task（job_7d7c3b757302, agent=test-agent-1）→ 子进程执行 `rsim.py --project ovrs25 cluster run --dataset BYD_SR --profile byd-ovrs-bl01v7-er-shared` → job 打包成功（dry-run，source=path 共享 Selena + BYD_SR UNC dataset）→ task status=succeeded, rc=0，日志跨进程回传。
- 证明：模式 A Windows 端用 source=path 共享 Selena + UNC dataset，无需本机工具链即可提交 cluster 仿真。

**模式 B（Windows 本机仓）local 仿真真实跑通**：
- `rsim --project ovrs25 run <CBNA MF4> --profile local-build --timeout 600 --no-retry`
- selena.exe（本机编译，source=build）实际执行 229.6s，产出 `Gen5_2009-01-01_06-02_0115out.MF4` = **364.9 MB**（382606488 字节）。
- `[SUCCESS] Simulation completed`，exit 0。
- 输入：`D:/data/byd/FRGVBYDP-21536/23-4-26_CBNA/CBNA_23-4-26/Gen5_2009-01-01_06-02_0115.MF4`（实际 CBNA 数据）。

**cluster dry-run**：`rsim cluster run --dataset BYD_SR --profile byd-ovrs-bl01v7-er-shared --limit 1` → job 包准备成功，selena 引用 `(profile)`，data path 解析为 BYD_SR UNC。

### 未能验证（环境限制）

真实 cluster 提交（`--execute`）需要 Bosch 内网/VPN 访问 `\\abtvdfs2.de.bosch.com\...` 和 `\\szhradar01\cluster_software`，本机当前 UNC 全部不可达。dry-run 验证了配置解析 + job 打包逻辑，真实提交链路与 dry-run 共用同一代码路径（只差 `--execute` 标志调 XML-RPC submit）。

### 测试

- 全套 363 passed（新增 5 个 doctor 用例：legacy selena_exe、source=path missing exe、auto-backend cluster-only/all 三个）。
- 零回归。

### 后续待办

7. **cluster check 的 severity 语义混乱**（`core/cluster.py` `_check_cluster_environment`）：输出里 `!!`（error 标记）的项最终 `Backend check passed`——某些 CheckItem `severity=error, ok=True` 语义矛盾。需审查 cluster 检查项的 ok/severity 赋值，让 error 级项确实阻断 report.ok。
8. **真实 cluster 提交回归**：连内网后用 `--execute` 跑一条真实 cluster 仿真，记录 job_id/输出大小/日志，确认 XML-RPC submit + worker 执行 + 结果 fetch 全链路。
9. **server 端 config 预置**（同前 #4）：让 Linux server 侧固化 cluster-inherent 默认，Windows 端只带 project + dataset/profile。

## T3 真实 cluster 仿真端到端跑通（2026-07-08，Linux server）

### 突破：Linux server 自己打包+提交 cluster job，无需 Windows agent

通过 SSH（paramiko）在 Linux server（10.190.171.44，hoz2wx@APAC.BOSCH.COM）配置环境并验证 T3 全链路。

### 环境配置（已完成）

1. **SZHRADAR01 解析**：`/etc/hosts` 加 `10.54.5.71 SZHRADAR01 szhradar01.APAC.BOSCH.COM`（Linux DNS 原本不可解析）。
2. **SMB 共享挂载**（`cifs-utils` 已装）：
   - `//abtvdfs2.de.bosch.com/ismdfs` → `/mnt/cluster`（`domain=APAC,uid=hoz2wx`，可写 Cluster 目录）
   - `//szhradar01/_CLUSTER_SOFTWARE` → `/mnt/cluster_sw`
   - 关键：必须 `domain=APAC` 显式域认证，否则 DFS 深层路径（`loc/szh/Isilon3/Cluster`）权限被拒。
3. **local.yaml**（Linux 专用）：config 路径保持 Windows UNC（worker 是 Windows 读 UNC），新增 `cluster.linux_mount_map` 把 UNC 前缀映射到挂载点，让 Linux server 写盘用挂载点、Config.cfg 内容用 UNC。

### 代码改动（已完成 + push）

- **`core/server_cluster_executor.py`**：server 内置 cluster 执行器（`--cluster-executor`），认领 cluster.run task 直接调 `prepare_cluster_job` + `submit_cluster_job`，不经子进程、不需 Windows agent。
- **`core/cluster.py:prepare_cluster_job`**：
  - `linux_mount_map` 机制：Linux 上写盘用 `job_dir_local`（挂载点），Config.cfg/submit/manifest 内容用 `job_dir_unc_str`（UNC，反斜杠分隔）。
  - UNC 路径分隔符强制反斜杠（Linux Path() 用 `/` 会产生混合分隔符 `\\host\share/dir`，Windows manager 解析不了）。
  - 雷达朝向检测的 `Path.is_file()` 容错（SMB/DFS 偶发 ConnectionRefused 不崩）。
- **`core/cluster.py:_validate_submit_package`**：接收 mount_map，本地存在性校验用挂载点路径；datafile 接受目录（dataset input_dir）。
- **`core/cluster.py:_submit_via_xmlrpc`**：校验时 UNC→挂载点转换。

### 端到端验证证据

投 `cluster.run`（execute=true）→ `server-cluster-executor` agent 认领 → prepare 写盘 + 生成 Config.cfg → XML-RPC 提交 SZHRADAR manager → **manager 返回 value=48（job ID）**，returncode=0，task status=succeeded。

```
[executor] preparing cluster job (project=BYD_OVS_CB, profile=byd-ovrs-bl01v7-er-shared, dataset=BYD_SR)
[executor] job package prepared: \\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\linux_t3_real_006
[executor] Config.cfg: \\abtvdfs2.de.bosch.com\ismdfs\loc\szh\Isilon3\Cluster\radar-sim\ovrs25\linux_t3_real_006\Config.cfg
value=48
[executor] submitted via xmlrpc, returncode=0
```

`rsim cluster status 48` → `prepared`（job 进入集群队列，worker 会 picked up 跑 selena）。

### 三档现状

| 档位 | 状态 |
|------|------|
| **T1** 完整本地环境 | ✅ 已实现（模式 B） |
| **T2** 有编译无仿真环境 | ⚠️ server 端提交已具备（T3 执行器）；Selena 上传到集群共享待实现 |
| **T3** 无代码仓无编译无仿真 | ✅ **核心跑通**——Linux server 打包+提交 cluster job，无需 Windows agent |

### 仍待验证

- ~~job 48 worker 执行~~ ✅ **已验证**：用 `verified-shared` profile（指向 `cloud_batch_0117` 已验证的 selena.exe）+ 单文件 MF4 + `radar=RadarFL`，worker 实际跑通 selena 仿真。selena.log 末尾 `Simulation finished` + `Thank you for using Selena`。result.ini `job_id=10338, successfull=0`。

### T3 完整端到端验证证据（2026-07-08）

```
Linux server (10.190.171.44) --cluster-executor
  → cluster.run job (单文件 MF4, verified-shared profile, radar=RadarFL)
  → server-cluster-executor 认领
  → prepare_cluster_job: 写盘到 /mnt/cluster (SMB 挂载), Config.cfg 用 UNC
  → submit_cluster_job: XML-RPC 到 SZHRADAR01:8123, manager 返回 value=1
  → 集群 worker picked up, 跑 selena.exe (共享路径 cloud_batch_0117/selena/)
  → selena.log: "MDF-Scheduler finished: file duration: 45.0s" + "Simulation finished"
  → 产出 OUT_/result.html, result.pickle, selena.log, result.ini
```

**关键配置点（Linux local.yaml）**：
- `linux_mount_map`: UNC→挂载点映射（`\\abtvdfs2\ismdfs`→`/mnt/cluster`）。
- profile `source: "RadarFL"` + `mounting_position: "CFL"` 显式指定（自动检测对部分 MF4 返回 None）。
- 用已验证的共享 selena.exe（`cloud_batch_0117/selena/selena.exe`）而非 `BYD_OVRS/BL01V7_ER` 的（后者版本不匹配，selena 启动但无产出）。

### 后续待办

10. ~~**T2 Selena 上传**~~ ✅ 已实现：`rsim cluster upload-selena`（commit c72da7d）。复制本机 selena.exe+DLL 到 `<workspace_root>/selena-packages/<name>/`，打印 source=path profile 条目供 local.yaml 使用。支持 linux_mount_map。
11. **T3 Selena 选择 UI**：web 前端列出可用共享 Selena 包（扫描 `selena-packages/` + config 预置），供浏览器用户选择。当前 T3 用户靠 curl/脚本提交（已验证可行）。
12. ~~**Linux 持久挂载**~~ ✅ 已完成：`/etc/fstab` 加 cifs 挂载（credentials 文件 `/etc/rsim/smb-creds`，`_netdev,nofail`）；systemd service `rsim-server.service` 开机自启 server（`--cluster-executor`）。重启后自动恢复。
13. **Linux 代码同步**：Linux 无外网，github.com 不可达。当前靠 SFTP 从本机传单文件（paramiko）。可建 Bosch 内网 Git 镜像或用 rsync over ssh 同步整个仓库。

### systemd 持久化回归验证（2026-07-08）

systemd 管理的 server（`rsim-server.service`）+ fstab 持久挂载后，T3 端到端再次跑通：
- job_9a4338a89118（verified-shared profile，单文件 MF4，execute=true）
- worker 执行 selena，selena.log 末尾 `Thank you for using Selena and have a nice day!`
- result.ini: `successfull=0, job_id=10339, filesize=393`
- 证明重启后自动恢复全链路（挂载 + server + 执行器 + 提交 + worker 执行）。
