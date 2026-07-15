const state = { project: "", buildTaskId: null, simTaskId: null, pollTimer: null };

const titles = {
  sim: ["仿真", "配置驱动仿真，修改数据路径即可运行"],
  check: ["环境校验", "自动检测环境问题并修复"],
  wizard: ["项目配置", "导入/编辑/导出一站式配置文件"],
};

function qs(id) { return document.getElementById(id); }
function escapeHtml(v) {
  return String(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data.error || data.guidance || text || res.statusText);
  return data;
}

function activeProject() { return state.project || ""; }
function setLog(id, value) { qs(id).textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function selectedSource() {
  const el = document.querySelector('input[name="selenaSource"]:checked');
  return el ? el.value : "build";
}
function selectedBackend() {
  return state.currentBackend || "local";
}

// ---------- Project / config loading ----------

async function loadConfigFiles() {
  // Single-config mode: fetch the active config pinned by RSIM_CONFIG (or the
  // default project). The project picker is hidden — users don't see
  // ovrs25/bydod25; they operate on one local.yaml.
  try {
    const info = await api("/api/active-config");
    state.project = info.project || "";
    state.localYamlPath = info.config_path || "";
    const label = qs("activeConfigLabel");
    if (label) label.textContent = info.config_path ? `配置: ${info.config_path}` : "";
  } catch (e) {
    // Fallback: list-files first entry.
    const data = await api("/api/config/list-files");
    if (data.files && data.files.length) {
      state.localYamlPath = data.files[0].path;
      state.project = data.files[0].project;
    }
  }
}

async function loadAllTabs(path) {
  // Load config from a local.yaml path — populates BOTH sim tab and config tab.
  const data = await api(`/api/config/load?path=${encodeURIComponent(path)}`);
  state.project = data.project;
  state.localYamlPath = path;
  LS.set("currentConfigPath", path);
  applyUserConfig(data.user_config);
  if (qs("configOutput")) setLog("configOutput", data.effective_config);
  // Cluster tab depends on project config too.
  loadClusterProfiles();
}

function applyUserConfig(uc) {
  // Sim tab: only populate data path + config summary (everything else is read from config)
  qs("dataPath").value = uc.data_path || "";
  state.currentSource = uc.source || "build";
  state.currentBackend = uc.backend || "local";
  state.currentCodePath = uc.code_path || "";
  state.currentBuildScript = uc.selena_build_script || "";
  state.currentBranch = uc.selena_branch || "";
  state.currentExistingPath = uc.existing_path || "";
  state.currentRuntimeXml = uc.runtime_xml || "";
  updateDataHint();
  updateSimConfigSummary();
}

function updateSimConfigSummary() {
  const el = qs("simConfigSummary");
  if (!el) return;
  const src = state.currentSource || "build";
  const be = state.currentBackend || "local";
  const lines = [];
  lines.push(`编译方式: ${src === "build" ? "本地编译" : "已有 Selena 文件夹"} | 仿真后端: ${be === "cluster" ? "Cluster" : "本地"}`);
  if (src === "build") {
    if (state.currentCodePath) lines.push(`代码仓: ${state.currentCodePath}`);
    if (state.currentBranch) lines.push(`分支: ${state.currentBranch}`);
    if (state.currentBuildScript) lines.push(`编译脚本: ${state.currentBuildScript}`);
  } else {
    if (state.currentExistingPath) lines.push(`已有文件夹: ${state.currentExistingPath}`);
  }
  if (state.currentRuntimeXml) lines.push(`Runtime XML: ${state.currentRuntimeXml}`);
  el.textContent = lines.join("\n");
  // Show/hide build button based on source.
  const buildBtn = qs("buildBtn");
  if (buildBtn) buildBtn.style.display = src === "build" ? "" : "none";
}

async function loadEffectiveConfig(project) {
  const data = await api(`/api/config?project=${encodeURIComponent(project)}`);
  if (qs("configOutput")) setLog("configOutput", data);
}

function updateDataHint() {
  const p = qs("dataPath")?.value?.trim() || "";
  const backend = state.currentBackend || "local";
  let hint = "";
  if (!p) hint = "";
  else if (p.startsWith("\\\\")) {
    hint = backend === "local"
      ? "检测：UNC 服务器数据（本地仿真将自动下载到本地，输出也写本地）"
      : "检测：UNC 服务器数据（cluster worker 可直接访问，无需拷贝）";
  } else if (/\.[Mm][Ff]4$/.test(p)) hint = "检测：本地 MF4 文件";
  else hint = backend === "local"
    ? "检测：本地目录（直接引用）"
    : "检测：本地目录（cluster 仿真会自动迁移到共享盘）";
  qs("dataHint").textContent = hint;
}

// ---------- Save config ----------

async function saveConfig() {
  // Config tab removed — persist from sim-tab values instead.
  await persistSimTabConfig();
  const saveStatus = qs("saveStatus");
  if (saveStatus) saveStatus.textContent = `已保存到 ${state.localYamlPath || "local.yaml"}`;
  await loadAllTabs(state.localYamlPath || activeProject());
}

async function newProject() {
  const name = window.prompt("新项目名称（英文标识符）：");
  if (!name) return;
  const data = await api("/api/config/new", { method: "POST", body: JSON.stringify({ project: name }) });
  if (data.ok) {
    // Reload active config state and refresh all tabs with the new project.
    await loadConfigFiles();
    if (state.localYamlPath) {
      await loadAllTabs(state.localYamlPath);
    }
  }
}

async function exportConfig() {
  if (!state.project) return;
  const data = await api(`/api/config/export?project=${encodeURIComponent(state.project)}`);
  const blob = new Blob([data.yaml_content || ""], { type: "text/yaml" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `${state.project}.local.yaml`;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

async function importConfig(ev) {
  const file = ev.target.files[0];
  if (!file) return;
  const text = await file.text();
  const data = await api("/api/config/import", { method: "POST", body: JSON.stringify({
    project: state.project, yaml_content: text, mode: "replace",
  })});
  if (data.ok) {
    // Reload active config state and refresh all tabs with the imported config.
    await loadConfigFiles();
    if (state.localYamlPath) {
      await loadAllTabs(state.localYamlPath);
    }
    alert("配置已导入");
  }
  ev.target.value = "";
}

// ---------- Build ----------

async function startBuild() {
  // Persist current sim-tab config first so build uses the right paths.
  await persistSimTabConfig();
  qs("buildStatus").textContent = "编译中...";
  setLog("simLog", "启动 Selena 编译...");
  const data = await api("/api/build/selena", { method: "POST", body: JSON.stringify({ project: activeProject() }) });
  state.buildTaskId = data.task_id;
  LS.set("lastTaskId", data.task_id); LS.set("lastTaskKind", "build");
  pollBuild();
}

let buildSince = 0;
async function pollBuild() {
  if (!state.buildTaskId) return;
  const data = await api(`/api/build/status?task_id=${state.buildTaskId}&since=${buildSince}`);
  if (!data.found) { qs("buildStatus").textContent = "任务未找到"; return; }
  if (data.lines && data.lines.length) {
    const log = qs("simLog");
    log.textContent += "\n" + data.lines.join("\n");
    log.scrollTop = log.scrollHeight;
  }
  buildSince = data.total_lines;
  qs("buildStatus").textContent = `${data.status} (${data.duration_sec}s, ${data.total_lines} 行)`;
  if (data.status === "running" || data.status === "queued") {
    setTimeout(pollBuild, 1500);
  } else {
    state.buildTaskId = null;
    LS.del("lastTaskId"); LS.del("lastTaskKind");
    if (data.status === "success") qs("buildStatus").textContent = `编译成功: ${data.exe_path}`;
    else if (data.errors?.length) qs("buildStatus").textContent = `编译失败: ${data.errors[0]}`;
  }
}

// ---------- Simulation ----------

async function persistSimTabConfig() {
  // Only save data_path (the only user-editable field on the sim tab).
  // Everything else comes from config and is managed in the config page.
  const payload = {
    project: activeProject(),
    source: state.currentSource || "build",
    code_path: state.currentCodePath || "",
    selena_build_script: state.currentBuildScript || "",
    selena_branch: state.currentBranch || "",
    existing_path: state.currentExistingPath || "",
    runtime_xml: state.currentRuntimeXml || "",
    data_path: qs("dataPath")?.value?.trim() || "",
    backend: selectedBackend(),
  };
  await api("/api/user-config", { method: "POST", body: JSON.stringify(payload) });
}

async function startSim() {
  // Save current data path to config before starting.
  await persistSimTabConfig();
  const backend = selectedBackend();
  const dataPath = qs("dataPath")?.value?.trim() || "";
  const dryRun = qs("dryRun")?.checked || false;
  if (!dataPath) { qs("simSummary").textContent = "请填写数据路径"; return; }

  qs("simSummary").textContent = `${backend === "cluster" ? "Cluster" : "本地"} 仿真启动中...${dryRun ? "（DRY-RUN）" : ""}`;
  setLog("simLog", "");

  try {
    if (backend === "cluster") {
      // Cluster path: submit via cluster API.
      const res = await api("/api/cluster/submit-job", {
        method: "POST", body: JSON.stringify({
          project: activeProject(), input_mf4: dataPath, execute: !dryRun,
        }),
      });
      state.clusterJobId = res.job_id;
      LS.set("lastClusterJobId", res.job_id);
      clusterSince = 0;
      pollCluster();
    } else {
      // Local path.
      const data = await api("/api/sim/start", { method: "POST", body: JSON.stringify({
        project: activeProject(), backend: "local", data_path: dataPath, dry_run: dryRun,
      })});
      if (data.blocked) {
        qs("simSummary").textContent = "环境校验未通过，请到「环境校验」修复";
        setLog("simLog", JSON.stringify(data.items, null, 2));
        return;
      }
      state.simTaskId = data.task_id;
      LS.set("lastTaskId", data.task_id); LS.set("lastTaskKind", "sim");
      simSince = 0;
      pollSim();
    }
  } catch (e) {
    qs("simSummary").textContent = "启动失败: " + e.message;
  }
}

let simSince = 0;
async function pollSim() {
  if (!state.simTaskId) return;
  const data = await api(`/api/sim/status?task_id=${state.simTaskId}&since=${simSince}`);
  if (!data.found) { qs("simSummary").textContent = "任务未找到"; return; }
  if (data.lines && data.lines.length) {
    const log = qs("simLog");
    log.textContent += data.lines.join("\n") + "\n";
    log.scrollTop = log.scrollHeight;
  }
  simSince = data.total_lines;
  const file = data.current_file ? ` 当前: ${data.current_file}` : "";
  const prog = data.files_total ? ` ${data.files_done}/${data.files_total}` : "";
  qs("simSummary").textContent = `${data.status}${prog}${file} (${data.duration_sec}s)`;
  if (data.status === "running" || data.status === "queued") {
    setTimeout(pollSim, 1500);
  } else {
    state.simTaskId = null;
    LS.del("lastTaskId"); LS.del("lastTaskKind");
  }
}

// ---------- Cluster tab (T3: no Windows needed) ----------

let clusterSince = 0;
let serverInfo = null;

async function loadServerInfo() {
  try {
    serverInfo = await api("/api/server-info");
  } catch { serverInfo = { mode: "embedded", local_sim_available: true, cluster_executor: false }; }
  const banner = qs("clusterModeBanner");
  if (!serverInfo) return;
  if (serverInfo.mode === "remote") {
    if (serverInfo.cluster_executor) {
      banner.textContent = "✓ 连接到集群服务，server 端直接执行 cluster 仿真（无需 Windows agent）";
    } else if (serverInfo.local_sim_available) {
      banner.textContent = "✓ 连接到控制服务，有 Windows agent 可执行";
    } else {
      banner.textContent = "⚠ 连接到控制服务，但无 agent 在线 —— cluster 任务将排队等待";
    }
  } else if (serverInfo.mode === "embedded") {
    banner.textContent = serverInfo.local_sim_available
      ? "本机模式（内置 server+agent）"
      : "本机模式（Linux，无本地仿真能力，仅可提交 cluster）";
  }
  // Adapt UI to server capabilities: if no Windows agent (local sim unavailable),
  // hide local-sim-only controls on the 仿真 tab and force cluster backend.
  const localAvailable = !!serverInfo.local_sim_available;
  document.querySelectorAll('input[name="backend"][value="local"]').forEach((r) => {
    r.disabled = !localAvailable;
    const label = r.closest("label");
    if (label) label.style.display = localAvailable ? "" : "none";
  });
  const buildBtn = qs("buildBtn");
  if (buildBtn) buildBtn.style.display = localAvailable ? "" : "none";
  // If local unavailable, force cluster backend.
  if (!localAvailable) {
    const clusterRadio = document.querySelector('input[name="backend"][value="cluster"]');
    if (clusterRadio) clusterRadio.checked = true;
    const existingRadio = document.querySelector('input[name="selenaSource"][value="existing"]');
    if (existingRadio) existingRadio.checked = true;
    if (typeof toggleSourceBlocks === "function") toggleSourceBlocks("existing");
  }
}

async function loadClusterProfiles() {
  const sel = qs("clusterProfileSelect");
  const dsel = qs("clusterDatasetSelect");
  try {
    const project = activeProject();
    const data = await api(`/api/cluster/profiles?project=${encodeURIComponent(project)}`);
    const profiles = data.profiles || [];
    sel.innerHTML = profiles.length
      ? profiles.map((p) => `<option value="${p.name}">${p.name} — ${p.description || p.backend || ""}</option>`).join("")
      : "<option value=''>（无 cluster profile）</option>";
    updateClusterProfileDetail();
    // Populate datasets from the same response (added in /api/cluster/profiles).
    const datasets = data.datasets || [];
    dsel.innerHTML = datasets.length
      ? datasets.map((d) => `<option value="${d.name}">${d.name}</option>`).join("")
      : "<option value=''>（无数据集，改用路径）</option>";
  } catch (e) {
    sel.innerHTML = `<option value=''>加载失败: ${e.message}</option>`;
    dsel.innerHTML = `<option value=''>加载失败: ${e.message}</option>`;
  }
}

function updateClusterProfileDetail() {
  const sel = qs("clusterProfileSelect");
  const opt = sel.options[sel.selectedIndex];
  qs("clusterProfileDetail").textContent = opt ? opt.textContent : "";
}

function selectedClusterDataSource() {
  const el = document.querySelector('input[name="clusterDataSource"]:checked');
  return el ? el.value : "dataset";
}

async function startClusterRun() {
  const project = activeProject();
  const profile = qs("clusterProfileSelect").value;
  if (!profile) { alert("请选择一个 cluster profile"); return; }
  const dataSource = selectedClusterDataSource();
  let input_mf4 = "", dataset = "";
  if (dataSource === "dataset") {
    dataset = qs("clusterDatasetSelect").value;
    if (!dataset) { alert("请选择数据集"); return; }
  } else {
    input_mf4 = qs("clusterInputMf4").value.trim();
    if (!input_mf4) { alert("请填写输入 MF4 路径"); return; }
  }
  const dryRun = qs("clusterDryRun").checked;
  const body = { project, profile, dataset, input_mf4, execute: !dryRun };
  qs("clusterStatus").textContent = "提交中...";
  qs("clusterLog").textContent = "";
  clusterSince = 0;
  try {
    const res = await api("/api/cluster/submit-job", {
      method: "POST", body: JSON.stringify(body),
    });
    state.clusterJobId = res.job_id;
    LS.set("lastClusterJobId", res.job_id);
    qs("clusterStatus").textContent = `已提交 job ${res.job_id}（${dryRun ? "dry-run" : "执行中"}）`;
    pollCluster();
  } catch (e) {
    qs("clusterStatus").textContent = `提交失败: ${e.message}`;
  }
}

async function pollCluster() {
  if (!state.clusterJobId) return;
  const data = await api(`/api/sim/status?task_id=${state.clusterJobId}&since=${clusterSince}`);
  if (!data.found) { qs("clusterSummary").textContent = "任务未找到"; return; }
  if (data.lines && data.lines.length) {
    const log = qs("clusterLog");
    log.textContent += data.lines.join("\n") + "\n";
    log.scrollTop = log.scrollHeight;
  }
  clusterSince = data.total_lines;
  qs("clusterSummary").textContent = `${data.status} (${data.duration_sec}s)`;
  if (data.status === "running" || data.status === "queued") {
    setTimeout(pollCluster, 2000);
  } else {
    state.clusterJobId = null;
    LS.del("lastClusterJobId");
  }
}

async function runEnvCheck() {
  const backend = selectedBackend();
  qs("envCheckSummary").textContent = "校验中...";
  qs("envCheckList").innerHTML = "";
  const url = `/api/check?project=${encodeURIComponent(activeProject())}&backend=${backend}` + (profile ? `&profile=${encodeURIComponent(profile)}` : "");
  const data = await api(url);
  renderCheckItems(data);
  qs("envCheckSummary").textContent = data.ok
    ? `通过 (${data.items.length} 项, ${data.warnings.length} warning)`
    : `未通过 (${data.errors.length} error, ${data.warnings.length} warning)`;
}

function renderCheckItems(reportOrItems) {
  const items = reportOrItems.items || reportOrItems;
  qs("envCheckList").innerHTML = items.map((item) => {
    const cls = item.ok ? "ok" : (item.severity === "warning" ? "warn" : "err");
    const mark = item.ok ? "OK" : (item.severity === "warning" ? "W" : "!!");
    let actions = "";
    if (!item.ok && item.auto_repairable) {
      actions = ` <button class="repair-btn" data-action="${item.repair_action}">自动修复</button>`;
    } else if (!item.ok && item.repair_hint) {
      actions = ` <button class="repair-btn" data-hint="${escapeHtml(item.repair_hint)}">查看指引</button>`;
    }
    return `<div class="env-row ${cls}"><span class="env-mark">${mark}</span><span class="env-name">${escapeHtml(item.name)}</span><span class="env-detail">${escapeHtml(item.detail)}${actions}</span></div>`;
  }).join("");
  qs("envCheckList").querySelectorAll(".repair-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.hint) { alert(btn.dataset.hint); return; }
      runRepair(btn.dataset.action);
    });
  });
}

async function runRepair(action) {
  qs("envCheckSummary").textContent = `执行修复: ${action}...`;
  const data = await api("/api/repair", { method: "POST", body: JSON.stringify({
    project: activeProject(), repair_action: action,
  })});
  if (data.task_id) {
    state.buildTaskId = data.task_id;
    const msg = data.message || (data.toolcollection ? `安装工具集 ${data.toolcollection}...` : "修复进行中...");
    qs("envCheckSummary").textContent = msg + " 切换到'仿真'tab 查看进度";
    switchView("sim");
    qs("simSummary").textContent = msg;
    setLog("simLog", "");
    buildSince = 0;
    pollBuild();
  } else if (data.guidance) {
    alert(data.guidance);
    qs("envCheckSummary").textContent = "请按指引操作后重新校验";
  } else {
    qs("envCheckSummary").textContent = data.message || "修复完成";
    await runEnvCheck();
  }
}

// ---------- View switching ----------

function switchView(name) {
  document.querySelectorAll(".nav-button").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  qs("viewTitle").textContent = titles[name]?.[0] || name;
  qs("viewSub").textContent = titles[name]?.[1] || "";
  if (name === "wizard") {
    loadConfigEditor();
  }
}

// ---------- Bindings ----------

function bindEvents() {
  document.querySelectorAll(".nav-button").forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
  qs("dataPath")?.addEventListener("input", updateDataHint);
  qs("buildBtn")?.addEventListener("click", startBuild);
  qs("simBtn")?.addEventListener("click", startSim);
  qs("envCheckBtn")?.addEventListener("click", runEnvCheck);
  qs("autoRepairBtn")?.addEventListener("click", () => runRepair("auto_repair_all"));
  // Config form bindings
  qs("cfgSaveBtn")?.addEventListener("click", saveConfigEditor);
  qs("cfgExportBtn")?.addEventListener("click", exportFullConfig);
  qs("cfgImportBtn")?.addEventListener("click", () => qs("cfgFileInput")?.click());
  qs("cfgFileInput")?.addEventListener("change", importFullConfig);
  document.querySelectorAll('input[name="cfgCompileWhere"]').forEach(r =>
    r.addEventListener("change", () => _toggleCfgBlocks(r.value)));
}

// ---------- localStorage persistence (refresh recovery) ----------
const LS = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

// ---------- Config Form (clean user-facing fields only) ----------

function _cfgVal(id) { return qs(id)?.value?.trim() || ""; }
function _cfgSet(id, v) { const el = qs(id); if (el) el.value = v || ""; }

async function loadConfigEditor() {
  // Load current config into form fields.
  try {
    const uc = await api(`/api/user-config?project=${encodeURIComponent(activeProject())}`);
    _cfgSet("cfgProjectName", activeProject());
    _cfgSet("cfgCodePath", uc.code_path);
    _cfgSet("cfgBranch", uc.selena_branch);
    _cfgSet("cfgBuildScript", uc.selena_build_script);
    _cfgSet("cfgEnvScript", uc.env_build_script);
    _cfgSet("cfgRuntimeXml", uc.runtime_xml);
    _cfgSet("cfgExistingPath", uc.existing_path);
    _cfgSet("cfgExistingRuntimeXml", uc.existing_runtime_xml);
    _cfgSet("cfgDataPath", uc.data_path);
    _cfgSet("cfgAdapterFile", uc.adapter_path);
    _cfgSet("cfgMatfilefilter", uc.matfilefilter);
    // Source radio.
    const src = uc.source || "build";
    const srcRadio = document.querySelector(`input[name="cfgCompileWhere"][value="${src === "existing" ? "existing" : "build"}"]`);
    if (srcRadio) srcRadio.checked = true;
    _toggleCfgBlocks(src === "existing" ? "existing" : "build");
    // Backend radio.
    const be = uc.backend || "local";
    const beRadio = document.querySelector(`input[name="cfgSimWhere"][value="${be}"]`);
    if (beRadio) beRadio.checked = true;
  } catch {
    // No config yet — form stays empty.
    _cfgSet("cfgProjectName", activeProject());
  }
}

function _toggleCfgBlocks(val) {
  const buildBlock = qs("cfgBuildBlock");
  const existingBlock = qs("cfgExistingBlock");
  if (buildBlock) buildBlock.style.display = val === "existing" ? "none" : "";
  if (existingBlock) existingBlock.style.display = val === "existing" ? "" : "none";
}

function _collectCfgForm() {
  const compileWhere = document.querySelector('input[name="cfgCompileWhere"]:checked')?.value || "build";
  const simWhere = document.querySelector('input[name="cfgSimWhere"]:checked')?.value || "local";
  const isExisting = compileWhere === "existing";
  return {
    project: _cfgVal("cfgProjectName") || activeProject(),
    source: isExisting ? "existing" : "build",
    code_path: isExisting ? "" : _cfgVal("cfgCodePath"),
    selena_branch: isExisting ? "" : _cfgVal("cfgBranch"),
    selena_build_script: isExisting ? "" : _cfgVal("cfgBuildScript"),
    env_build_script: isExisting ? "" : _cfgVal("cfgEnvScript"),
    existing_path: isExisting ? _cfgVal("cfgExistingPath") : "",
    runtime_xml: isExisting ? _cfgVal("cfgExistingRuntimeXml") : _cfgVal("cfgRuntimeXml"),
    data_path: _cfgVal("cfgDataPath"),
    adapter_path: _cfgVal("cfgAdapterFile"),
    matfilefilter: _cfgVal("cfgMatfilefilter"),
    backend: simWhere,
  };
}

async function saveConfigEditor() {
  const status = qs("cfgSaveStatus");
  const payload = _collectCfgForm();
  if (!payload.project) { status.textContent = "❌ 请填写项目名称"; return; }
  status.textContent = "保存中...";
  try {
    const res = await api("/api/user-config", { method: "POST", body: JSON.stringify(payload) });
    status.textContent = `✅ 已保存`;
    state.project = payload.project;
    await loadConfigFiles();
    if (state.localYamlPath) await loadAllTabs(state.localYamlPath);
  } catch (e) { status.textContent = `❌ ${e.message}`; }
}

async function exportFullConfig() {
  try {
    const data = await api(`/api/config/export-full?project=${encodeURIComponent(activeProject())}`);
    const blob = new Blob([data.yaml_content || ""], { type: "text/yaml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${activeProject() || "project"}.yaml`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) { alert("导出失败: " + e.message); }
}

function importFullConfig(ev) {
  const file = ev.target.files[0];
  if (!file) return;
  file.text().then(async (text) => {
    try {
      const res = await api("/api/config/import-full", {
        method: "POST", body: JSON.stringify({ yaml_content: text }),
      });
      qs("cfgSaveStatus").textContent = `✅ 已导入: ${res.project}`;
      state.project = res.project;
      await loadConfigFiles();
      if (state.localYamlPath) await loadAllTabs(state.localYamlPath);
      await loadConfigEditor(); // Refresh form with imported values.
    } catch (e) { qs("cfgSaveStatus").textContent = `❌ 导入失败: ${e.message}`; }
  });
  ev.target.value = "";
}

async function init() {
  bindEvents();
  await loadConfigFiles();
  if (state.localYamlPath) {
    await loadAllTabs(state.localYamlPath);
  }
  // Resume polling an unfinished task (build/sim) if the page was refreshed mid-run.
  const lastTask = LS.get("lastTaskId");
  const lastTaskKind = LS.get("lastTaskKind");
  if (lastTask && lastTaskKind) {
    const snap = await api(`/api/build/status?task_id=${lastTask}&since=0`).catch(() => null);
    if (snap && snap.found && (snap.status === "running" || snap.status === "queued")) {
      state.buildTaskId = lastTask;
      buildSince = snap.total_lines || 0;
      qs("simSummary").textContent = `恢复任务 ${lastTask} (${snap.status})`;
      setLog("simLog", (snap.lines || []).join("\n"));
      if (lastTaskKind === "build") pollBuild();
      else { state.simTaskId = lastTask; state.buildTaskId = null; simSince = buildSince; pollSim(); }
    } else {
      // Task already finished — clear so we don't keep resuming.
      LS.del("lastTaskId"); LS.del("lastTaskKind");
    }
  }
  // Load server info + resume cluster job polling if refreshed mid-run.
  loadServerInfo();
  const lastCluster = LS.get("lastClusterJobId");
  if (lastCluster) {
    const snap = await api(`/api/sim/status?task_id=${lastCluster}&since=0`).catch(() => null);
    if (snap && snap.found && (snap.status === "running" || snap.status === "queued")) {
      state.clusterJobId = lastCluster;
      clusterSince = snap.total_lines || 0;
      setLog("clusterLog", (snap.lines || []).join("\n"));
      pollCluster();
    } else { LS.del("lastClusterJobId"); }
  }
}

init().catch((err) => { qs("simLog") && setLog("simLog", err.stack || err.message); });
