const state = { project: "", buildTaskId: null, simTaskId: null, pollTimer: null };

const titles = {
  sim: ["仿真", "选 Selena 来源 + 数据 + 执行方式，一键仿真"],
  cluster: ["Cluster 仿真", "提交到 SZHRADAR 集群执行（无需本机工具链）"],
  check: ["环境校验", "多分支/多仓/多数据统一校验 + 修复"],
  config: ["配置", "用户私有配置（存 local.yaml，切项目自动带出）"],
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
  const el = document.querySelector('input[name="backend"]:checked');
  return el ? el.value : "local";
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
  setLog("configOutput", data.effective_config);
  // Cluster tab depends on project config too.
  loadClusterProfiles();
}

function applyUserConfig(uc) {
  // Sim tab
  const source = uc.source || "build";
  document.querySelector(`input[name="selenaSource"][value="${source}"]`).checked = true;
  toggleSourceBlocks(source);
  qs("codePath").value = uc.code_path || "";
  qs("selenaBuildScript").value = uc.selena_build_script || "";
  qs("selenaBranch").value = uc.selena_branch || "";
  qs("envBuildScript").value = uc.env_build_script || "";
  qs("selenaExe").value = uc.selena_exe || "";
  qs("dataPath").value = uc.data_path || "";
  document.querySelector(`input[name="backend"][value="${uc.backend || 'local'}"]`).checked = true;
  updateDataHint();
  // Config tab
  document.querySelector(`input[name="cfgSource"][value="${source}"]`).checked = true;
  toggleCfgExe(source);
  qs("cfgCodePath").value = uc.code_path || "";
  qs("cfgSelenaBuildScript").value = uc.selena_build_script || "";
  qs("cfgSelenaBranch").value = uc.selena_branch || "";
  qs("cfgEnvBuildScript").value = uc.env_build_script || "";
  qs("cfgRuntimePath").value = uc.runtime_path || "";
  qs("cfgAdapterPath").value = uc.adapter_path || "";
  qs("cfgDataPath").value = uc.data_path || "";
  qs("cfgSelenaExe").value = uc.selena_exe || "";
  // backend is NOT set here — it lives only in the sim tab (selectedBackend).
  // The sim-tab radio was already set above from uc.backend.
}

async function loadEffectiveConfig(project) {
  const data = await api(`/api/config?project=${encodeURIComponent(project)}`);
  setLog("configOutput", data);
}

function toggleSourceBlocks(source) {
  qs("sourceBuild").style.display = source === "build" ? "" : "none";
  qs("sourcePath").style.display = source === "path" ? "" : "none";
}
function toggleCfgExe(source) {
  qs("cfgExeLabel").style.display = source === "path" ? "" : "none";
}
function updateDataHint() {
  const p = qs("dataPath").value.trim();
  const backend = selectedBackend();
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
  const payload = {
    project: activeProject(),
    source: document.querySelector('input[name="cfgSource"]:checked').value,
    code_path: qs("cfgCodePath").value.trim(),
    selena_build_script: qs("cfgSelenaBuildScript").value.trim(),
    selena_branch: qs("cfgSelenaBranch").value.trim(),
    env_build_script: qs("cfgEnvBuildScript").value.trim(),
    runtime_path: qs("cfgRuntimePath").value.trim(),
    adapter_path: qs("cfgAdapterPath").value.trim(),
    data_path: qs("cfgDataPath").value.trim(),
    selena_exe: qs("cfgSelenaExe").value.trim(),
    // backend comes from the sim-tab radio (single source of truth).
    backend: selectedBackend(),
  };
  qs("saveStatus").textContent = "保存中...";
  const data = await api("/api/user-config", { method: "POST", body: JSON.stringify(payload) });
  qs("saveStatus").textContent = `已保存到 ${data.local_yaml_path}`;
  await loadAllTabs(activeProject());
}

async function newProject() {
  const name = window.prompt("新项目名称（英文标识符）：");
  if (!name) return;
  const data = await api("/api/config/new", { method: "POST", body: JSON.stringify({ project: name }) });
  if (data.ok) {
    await loadConfigFiles();
    // Select the new project's (empty) local.yaml.
    const sel = qs("configFileSelect");
    for (const opt of sel.options) {
      if (opt.textContent === name) { sel.value = opt.value; loadAllTabs(opt.value); break; }
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
    // Reload the config-file dropdown so imported files appear, then re-select
    // the current project's local.yaml and refresh the form.
    await loadConfigFiles();
    const sel = qs("configFileSelect");
    const target = state.localYamlPath || (sel.options.length ? sel.options[0].value : "");
    if (target) {
      sel.value = target;
      await loadAllTabs(target);
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
  // Save current sim-tab form to local.yaml so backend uses it.
  const source = selectedSource();
  const payload = {
    project: activeProject(),
    source,
    code_path: qs("codePath").value.trim(),
    selena_build_script: qs("selenaBuildScript").value.trim(),
    selena_branch: qs("selenaBranch").value.trim(),
    env_build_script: qs("envBuildScript").value.trim(),
    runtime_path: "",  // keep existing runtime; sim tab doesn't edit it
    data_path: qs("dataPath").value.trim(),
    selena_exe: qs("selenaExe").value.trim(),
    backend: selectedBackend(),
  };
  await api("/api/user-config", { method: "POST", body: JSON.stringify(payload) });
}

async function startSim() {
  await persistSimTabConfig();
  const backend = selectedBackend();
  const dataPath = qs("dataPath").value.trim();
  const dryRun = qs("dryRun").checked;
  if (!dataPath) { qs("simSummary").textContent = "请填写数据路径"; return; }

  // UI 限制：本地仿真 + 服务器(UNC)数据 → 提示将下载到本地
  const isUNC = dataPath.startsWith("\\\\");
  if (backend === "local" && isUNC && !dryRun) {
    if (!window.confirm(
      "本地仿真 + 服务器(UNC)数据：\n\n" +
      "数据将从服务器下载到本地临时目录，输出也写在本地，避免服务器写入失败。\n" +
      "大文件（几百MB~GB）下载可能较慢。\n\n" +
      "确认继续？"
    )) {
      qs("simSummary").textContent = "已取消";
      return;
    }
  }
  if (backend === "local" && isUNC && dryRun) {
    qs("simSummary").textContent = "提示：dry-run 不下载，真实执行时 UNC 数据会自动下载到本地";
  }

  qs("simSummary").textContent = `${backend} 仿真启动中...${dryRun ? "（DRY-RUN，不产生输出）" : ""}`;
  setLog("simLog", "");
  try {
    const data = await api("/api/sim/start", { method: "POST", body: JSON.stringify({
      project: activeProject(), backend, data_path: dataPath, dry_run: dryRun,
    })});
    if (data.blocked) {
      qs("simSummary").textContent = "环境校验未通过，请先到'环境校验'tab 修复";
      setLog("simLog", data.items);
      switchView("check");
      renderCheckItems(data.items);
      return;
    }
    state.simTaskId = data.task_id;
    LS.set("lastTaskId", data.task_id); LS.set("lastTaskKind", "sim");
    simSince = 0;
    pollSim();
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
    const buildRadio = document.querySelector('input[name="selenaSource"][value="path"]');
    if (buildRadio) buildRadio.checked = true;
    if (typeof toggleSourceBlocks === "function") toggleSourceBlocks("path");
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
  const backend = qs("envCheckBackend").value;
  // Pass the currently-selected cluster profile so the check inspects the
  // actual selena.exe/runtime_xml the user will submit with.
  const profile = qs("clusterProfileSelect") ? qs("clusterProfileSelect").value : "";
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
  qs("viewTitle").textContent = titles[name][0];
  qs("viewSub").textContent = titles[name][1];
}

// ---------- Bindings ----------

function bindEvents() {
  document.querySelectorAll(".nav-button").forEach((btn) => btn.addEventListener("click", () => switchView(btn.dataset.view)));
  // Source radio toggles (sim tab)
  document.querySelectorAll('input[name="selenaSource"]').forEach((r) => r.addEventListener("change", () => toggleSourceBlocks(selectedSource())));
  document.querySelectorAll('input[name="cfgSource"]').forEach((r) => r.addEventListener("change", () => toggleCfgExe(r.value)));
  qs("dataPath").addEventListener("input", updateDataHint);
  document.querySelectorAll('input[name="backend"]').forEach((r) => r.addEventListener("change", updateDataHint));
  qs("buildBtn").addEventListener("click", startBuild);
  qs("simBtn").addEventListener("click", startSim);
  qs("envCheckBtn").addEventListener("click", runEnvCheck);
  qs("autoRepairBtn").addEventListener("click", () => runRepair("auto_repair_all"));
  qs("saveConfigBtn").addEventListener("click", saveConfig);
  // Cluster tab bindings
  document.querySelectorAll('input[name="clusterDataSource"]').forEach((r) => r.addEventListener("change", () => {
    qs("clusterDataDataset").style.display = selectedClusterDataSource() === "dataset" ? "" : "none";
    qs("clusterDataPath").style.display = selectedClusterDataSource() === "path" ? "" : "none";
  }));
  qs("clusterProfileSelect").addEventListener("change", updateClusterProfileDetail);
  qs("clusterRunBtn").addEventListener("click", startClusterRun);
}

// ---------- localStorage persistence (refresh recovery) ----------
const LS = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

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
