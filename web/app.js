const state = { project: "", buildTaskId: null, simTaskId: null, pollTimer: null };

const titles = {
  sim: ["仿真", "选 Selena 来源 + 数据 + 执行方式，一键仿真"],
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
  const data = await api("/api/config/list-files");
  const select = qs("configFileSelect");
  const previous = select.value;
  select.innerHTML = "";
  (data.files || []).forEach((f) => {
    const option = document.createElement("option");
    option.value = f.path; option.textContent = f.project;
    select.appendChild(option);
  });
  if (data.files && data.files.length) {
    // Keep the current selection if it's still in the list; otherwise pick the first.
    const stillThere = previous && data.files.some((f) => f.path === previous);
    const pick = stillThere ? previous : data.files[0].path;
    state.localYamlPath = pick;
    state.project = (data.files.find((f) => f.path === pick) || data.files[0]).project;
    select.value = pick;
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

// ---------- Environment check + repair ----------

async function runEnvCheck() {
  const backend = qs("envCheckBackend").value;
  qs("envCheckSummary").textContent = "校验中...";
  qs("envCheckList").innerHTML = "";
  const data = await api(`/api/check?project=${encodeURIComponent(activeProject())}&backend=${backend}`);
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
  qs("configFileSelect").addEventListener("change", () => {
    loadAllTabs(qs("configFileSelect").value);
  });
  // Source radio toggles (sim tab)
  document.querySelectorAll('input[name="selenaSource"]').forEach((r) => r.addEventListener("change", () => toggleSourceBlocks(selectedSource())));
  document.querySelectorAll('input[name="cfgSource"]').forEach((r) => r.addEventListener("change", () => toggleCfgExe(r.value)));
  qs("dataPath").addEventListener("input", updateDataHint);
  document.querySelectorAll('input[name="backend"]').forEach((r) => r.addEventListener("change", updateDataHint));
  qs("buildBtn").addEventListener("click", startBuild);
  qs("simBtn").addEventListener("click", startSim);
  qs("newConfigBtn").addEventListener("click", newProject);
  qs("importConfigBtn").addEventListener("click", () => qs("importFileInput").click());
  qs("importFileInput").addEventListener("change", importConfig);
  qs("exportConfigBtn").addEventListener("click", exportConfig);
  qs("envCheckBtn").addEventListener("click", runEnvCheck);
  qs("autoRepairBtn").addEventListener("click", () => runRepair("auto_repair_all"));
  qs("saveConfigBtn").addEventListener("click", saveConfig);
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
  // Restore last-selected config file if still in the list.
  const savedPath = LS.get("currentConfigPath");
  if (savedPath) {
    const sel = qs("configFileSelect");
    const stillThere = Array.from(sel.options).some((o) => o.value === savedPath);
    if (stillThere) { sel.value = savedPath; }
  }
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
}

init().catch((err) => { qs("simLog") && setLog("simLog", err.stack || err.message); });
