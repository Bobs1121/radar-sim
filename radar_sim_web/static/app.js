"use strict";

const API = "/api/v1";
const state = {
  view: sessionStorage.getItem("rsimView") || "create",
  jobs: [],
  jobsSignature: "",
  selectedJobId: sessionStorage.getItem("rsimSelectedJobId") || "",
  eventsByJob: new Map(),
  pollTimer: null,
  jobsRequestInFlight: false,
  accessToken: sessionStorage.getItem("rsimAccessToken") || "",
  authenticationRequired: false,
  dataFolderFiles: [],
  uploadedDataPath: "",
  selectedFolderLabel: "",
};

const byId = (id) => document.getElementById(id);
const q = (selector, root = document) => root.querySelector(selector);
const qa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

class ApiError extends Error {
  constructor(status, payload) {
    super(payload?.message || `请求失败 (${status})`);
    this.status = status;
    this.payload = payload || {};
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.accessToken) headers.set("Authorization", `Bearer ${state.accessToken}`);
  if (options.json !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API}${path}`, {
    method: options.method || "GET",
    headers,
    body: options.json !== undefined ? JSON.stringify(options.json) : options.body,
  });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    if (response.status === 401) showAuthenticationEntry("访问令牌无效或已失效");
    throw new ApiError(response.status, payload);
  }
  return payload;
}

function showAuthenticationEntry(message = "需要访问令牌") {
  state.authenticationRequired = true;
  byId("authEntry").hidden = false;
  byId("accessToken").value = state.accessToken;
  byId("apiState").textContent = message;
  byId("apiState").className = "api-state error";
}

async function saveAccessToken() {
  state.accessToken = byId("accessToken").value.trim();
  if (state.accessToken) sessionStorage.setItem("rsimAccessToken", state.accessToken);
  else sessionStorage.removeItem("rsimAccessToken");
  try {
    await api("/capabilities");
    byId("apiState").textContent = "服务已连接";
    byId("apiState").className = "api-state ok";
    if (state.view === "tasks") await loadJobs();
  } catch (error) {
    showAuthenticationEntry(error.message || "连接失败");
  }
}

async function uploadConfigAsset(kind, file, targetId) {
  if (!file) return;
  const button = kind === "adapter" ? byId("chooseAdapter") : byId("chooseMatFilter");
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "正在上传…";
  try {
    const asset = await api("/config-assets", {
      method: "POST",
      headers: { "X-Asset-Kind": kind, "X-Asset-Filename": file.name },
      body: file,
    });
    byId(targetId).value = asset.uri;
    showToast(`${kind === "adapter" ? "Adapter" : "MatFilter"} 已保存为可复用配置引用`);
  } catch (error) {
    showFormError(error);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function showToast(message, duration = 3200) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, duration);
}

function showFormError(error) {
  const panel = byId("formError");
  const detail = error?.payload?.detail;
  let message = error?.message || String(error);
  if (detail?.errors?.length) {
    const first = detail.errors[0];
    message += `：${(first.loc || []).join(".")} ${first.msg || ""}`;
  } else if (detail?.error) {
    message += `：${detail.error}`;
  }
  panel.textContent = message;
  panel.hidden = false;
}

function clearFormError() {
  byId("formError").hidden = true;
  byId("formError").textContent = "";
  qa("[aria-invalid=true]").forEach((node) => node.removeAttribute("aria-invalid"));
}

function selectedValue(name) {
  return q(`input[name="${name}"]:checked`)?.value || "";
}

function setSelectedValue(name, value) {
  const input = q(`input[name="${name}"][value="${CSS.escape(value)}"]`);
  if (input) input.checked = true;
}

function runConfigFromForm() {
  const dataPath = state.uploadedDataPath || byId("dataPath").value.trim();
  if (!dataPath) {
    byId("dataPath").setAttribute("aria-invalid", "true");
    throw new Error("请填写数据路径");
  }

  const source = byId("selenaSource").value;
  const codePath = byId("codePath").value.trim();
  const branch = byId("selenaBranch").value.trim();
  const selenaBuildScript = byId("selenaBuildScript").value.trim();
  const packageBuildScript = byId("packageBuildScript").value.trim();
  const existingPath = byId("existingPath").value.trim();
  const runtimeXml = byId("runtimeXml").value.trim();
  const adapterFile = byId("adapterFile").value.trim();
  const matFilter = byId("matFilter").value.trim();
  if (source === "build" && !codePath) throw new Error("本地编译需要填写代码路径");
  if (source === "build" && !selenaBuildScript) throw new Error("本地编译需要填写 Selena 编译脚本");
  if (source === "build" && !packageBuildScript) throw new Error("本地编译需要填写软件包编译脚本");
  if (source === "existing" && !existingPath) throw new Error("请选择已有 Selena 文件夹");
  if (!runtimeXml) throw new Error("请选择与 Selena 匹配的 Runtime XML");
  if (!matFilter) throw new Error("请选择 MatFilter 配置文件");

  return {
    schema_version: "2.0",
    selena: {
      source,
      code_path: source === "build" ? codePath : "",
      branch: source === "build" ? branch : "",
      selena_build_script: source === "build" ? selenaBuildScript : "",
      package_build_script: source === "build" ? packageBuildScript : "",
      existing_path: source === "existing" ? existingPath : "",
      runtime_xml: runtimeXml,
    },
    data: { path: dataPath },
    simulation: {
      target: selectedValue("target") || "auto",
      adapter_file: adapterFile,
      mat_filter: matFilter,
    },
  };
}

function applyRunConfig(config) {
  state.dataFolderFiles = [];
  state.uploadedDataPath = "";
  state.selectedFolderLabel = "";
  byId("dataPath").value = config.data?.path || "";
  byId("selenaSource").value = config.selena?.source || "build";
  byId("codePath").value = config.selena?.code_path || "";
  byId("selenaBranch").value = config.selena?.branch || "";
  // Import endpoints return the migrated contract (selena_build_script /
  // package_build_script). Fall back to legacy build_script for older bundles
  // that still carry the single legacy Selena build entry point.
  const selena = config.selena || {};
  byId("selenaBuildScript").value = selena.selena_build_script || selena.build_script || "";
  byId("packageBuildScript").value = selena.package_build_script || "";
  byId("existingPath").value = selena.existing_path || "";
  byId("runtimeXml").value = selena.runtime_xml || "";
  setSelectedValue("target", config.simulation?.target || "auto");
  byId("adapterFile").value = config.simulation?.adapter_file || "";
  byId("matFilter").value = config.simulation?.mat_filter || "";
  updateConditionalFields();
  updateRouteSummary();
}

function chooseDataFolder(fileList) {
  const files = Array.from(fileList || []).filter((file) => /\.mf4$/i.test(file.name) && file.size > 0);
  if (!files.length) {
    state.dataFolderFiles = [];
    showToast("所选文件夹中没有可上传的 MF4 文件");
    return;
  }
  const firstPath = files[0].webkitRelativePath || files[0].name;
  const folder = firstPath.includes("/") ? firstPath.split("/", 1)[0] : "本机数据";
  state.dataFolderFiles = files;
  state.uploadedDataPath = "";
  state.selectedFolderLabel = folder;
  byId("dataPath").value = folder;
  byId("dataUploadState").textContent = `已选择 ${files.length} 个 MF4；提交或校验时自动上传`;
}

async function ensureSelectedDataUploaded() {
  if (!state.dataFolderFiles.length || state.uploadedDataPath) return state.uploadedDataPath;
  const files = state.dataFolderFiles;
  const manifest = files.map((file) => ({
    relative_path: file.webkitRelativePath || file.name,
    size: file.size,
  }));
  const progress = byId("dataUploadProgress");
  const bar = byId("dataUploadBar");
  const percent = byId("dataUploadPercent");
  progress.hidden = false;
  let uploaded = 0;
  let displayedValue = -1;
  const total = files.reduce((sum, file) => sum + file.size, 0);
  const update = () => {
    const value = total ? Math.round(uploaded * 100 / total) : 100;
    if (value === displayedValue) return;
    displayedValue = value;
    bar.value = value;
    percent.textContent = `${value}% · ${formatBytes(uploaded)} / ${formatBytes(total)}`;
  };
  const session = await api("/run-data-uploads", { method: "POST", json: { files: manifest } });
  const remoteByPath = new Map((session.files || []).map((item) => [item.relative_path, item]));
  const chunkSize = Math.max(1, Number(session.chunk_size) || 4 * 1024 * 1024);
  for (const file of files) {
    const relative = file.webkitRelativePath || file.name;
    const remote = remoteByPath.get(relative);
    if (!remote) throw new Error(`上传会话缺少文件：${relative}`);
    let offset = Number(remote.received_bytes) || 0;
    uploaded += offset;
    update();
    while (offset < file.size) {
      const blob = file.slice(offset, Math.min(file.size, offset + chunkSize));
      await api(
        `/dataset-uploads/${encodeURIComponent(session.session_id)}/files/${encodeURIComponent(remote.file_id)}`,
        { method: "PATCH", headers: { "Upload-Offset": String(offset) }, body: blob },
      );
      offset += blob.size;
      uploaded += blob.size;
      update();
    }
  }
  const completed = await api(`/dataset-uploads/${encodeURIComponent(session.session_id)}/finalize`, { method: "POST" });
  state.uploadedDataPath = completed.data_path;
  byId("dataPath").value = completed.data_path;
  byId("dataUploadState").textContent = `数据已就绪：${files.length} 个 MF4`;
  showToast("本机数据已上传，配置已自动换成可复用的数据路径");
  return completed.data_path;
}

function updateConditionalFields() {
  const source = byId("selenaSource").value;
  byId("buildFields").hidden = source !== "build";
  byId("existingFields").hidden = source !== "existing";
}

function updateRouteSummary() {
  const target = selectedValue("target") || "auto";
  const source = byId("selenaSource").value;
  const targetText = { auto: "自动选择本地或 Cluster", local: "在完整 Windows 节点本地仿真", cluster: "由 Cluster 执行仿真" }[target];
  const selenaText = source === "build"
    ? (byId("selenaBranch").value.trim() ? "隔离切换分支并编译 Selena" : "编译当前工作区修改")
    : "使用已有 Selena 文件夹";
  byId("routeSummary").textContent = `${selenaText}，${targetText}`;
}

function renderExecutionPlan(result) {
  const stages = Array.isArray(result?.execution_plan) ? result.execution_plan : [];
  if (!stages.length) return;
  const target = result?.execution?.selected_target;
  const route = target === "local" ? "Windows 本地" : target === "cluster" ? "Cluster" : "待调度";
  byId("planStatus").textContent = `配置有效，当前将使用 ${route} 路径。`;
  const list = byId("planStages");
  list.replaceChildren();
  stages.forEach((stage, index) => {
    const item = document.createElement("li");
    const number = document.createElement("span");
    number.textContent = String(index + 1);
    const detail = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = stageName(stage.stage_type);
    const note = document.createElement("small");
    note.textContent = stage.status === "skipped"
      ? `自动跳过：${friendlySkipReason(stage.skip_reason)}`
      : "按依赖关系自动调度";
    detail.append(title, note);
    item.append(number, detail);
    list.append(item);
  });
}

function switchView(view) {
  state.view = view;
  sessionStorage.setItem("rsimView", view);
  qa(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
  byId("createView").classList.toggle("is-active", view === "create");
  byId("tasksView").classList.toggle("is-active", view === "tasks");
  if (view === "tasks") loadJobs();
  schedulePolling();
}

async function validateCurrentSpec() {
  clearFormError();
  try {
    await ensureSelectedDataUploaded();
    const config = runConfigFromForm();
    const result = await api("/run-configs/validate", { method: "POST", json: config });
    renderExecutionPlan(result);
    byId("formError").className = "notice success";
    byId("formError").textContent = `配置检查通过，指纹 ${result.fingerprint.slice(0, 19)}...`;
    byId("formError").hidden = false;
    return result;
  } catch (error) {
    showFormError(error);
    throw error;
  }
}

async function submitCurrentSpec(event) {
  event.preventDefault();
  clearFormError();
  const button = byId("submitJob");
  button.disabled = true;
  button.textContent = "正在提交";
  try {
    await ensureSelectedDataUploaded();
    const config = runConfigFromForm();
    await api("/run-configs/validate", { method: "POST", json: config });
    const job = await api("/run-jobs", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}` },
      json: { config, dry_run: false },
    });
    state.selectedJobId = job.id;
    showToast("任务已提交");
    switchView("tasks");
  } catch (error) {
    showFormError(error);
  } finally {
    button.disabled = false;
    button.textContent = "提交任务";
  }
}

async function importYamlFile(file) {
  if (!file) return;
  clearFormError();
  try {
    const yaml = await file.text();
    const result = await api("/run-configs/import", { method: "POST", json: { yaml_content: yaml } });
    applyRunConfig(result.config);
    showToast("YAML 已导入并完成规范化");
  } catch (error) {
    showFormError(error);
  } finally {
    byId("yamlFile").value = "";
  }
}

async function exportYaml() {
  clearFormError();
  try {
    await ensureSelectedDataUploaded();
    const config = runConfigFromForm();
    const result = await api("/run-configs/export", { method: "POST", json: { config } });
    const blob = new Blob([result.yaml_content], { type: "text/yaml;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${config.result?.name || "radar-sim"}.simulation.yaml`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    showFormError(error);
  }
}

async function loadJobs() {
  if (state.jobsRequestInFlight) return;
  state.jobsRequestInFlight = true;
  const list = byId("jobList");
  if (!state.jobs.length) list.innerHTML = '<div class="empty-state">正在加载任务</div>';
  try {
    const filter = byId("statusFilter").value;
    const page = await api(`/jobs?limit=100${filter ? `&status=${encodeURIComponent(filter)}` : ""}`);
    const jobs = page.jobs || [];
    const signature = JSON.stringify(jobs.map((job) => [job.id, job.status, job.progress, job.current_stage]));
    state.jobs = jobs;
    if (signature !== state.jobsSignature) {
      state.jobsSignature = signature;
      renderJobs();
    }
    if (state.selectedJobId) await loadJobDetail(state.selectedJobId, false);
  } catch (error) {
    list.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = error.message;
    list.append(empty);
  } finally {
    state.jobsRequestInFlight = false;
  }
}

function renderJobs() {
  const list = byId("jobList");
  list.replaceChildren();
  if (!state.jobs.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "当前筛选条件下没有任务";
    list.append(empty);
    return;
  }
  state.jobs.forEach((job) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `job-row${job.id === state.selectedJobId ? " is-active" : ""}`;
    row.addEventListener("click", () => loadJobDetail(job.id, true));
    const header = document.createElement("div");
    header.className = "job-row-header";
    const title = document.createElement("strong");
    title.textContent = job.spec?.result?.name || "仿真任务";
    header.append(title, statusBadge(job.status));
    const code = document.createElement("code");
    code.textContent = job.id;
    const progress = document.createElement("div");
    progress.className = "mini-progress";
    const fill = document.createElement("span");
    fill.style.width = `${Math.round((job.progress || 0) * 100)}%`;
    progress.append(fill);
    const meta = document.createElement("div");
    meta.className = "job-row-meta";
    const stage = document.createElement("span");
    const currentStage = stageName(job.current_stage);
    stage.textContent = currentStage || (
      ["failed", "cancelled", "succeeded"].includes(job.status)
        ? statusName(job.status)
        : "等待调度"
    );
    const time = document.createElement("time");
    time.textContent = formatTime(job.created_at);
    meta.append(stage, time);
    row.append(header, code, progress, meta);
    list.append(row);
  });
}

async function loadJobDetail(jobId, resetEvents) {
  state.selectedJobId = jobId;
  sessionStorage.setItem("rsimSelectedJobId", jobId);
  if (resetEvents) state.eventsByJob.delete(jobId);
  renderJobs();
  try {
    const known = state.eventsByJob.get(jobId) || [];
    const cursor = known.length ? Number(known[known.length - 1].id || 0) : 0;
    const [job, eventPage, manifestPage] = await Promise.all([
      api(`/jobs/${encodeURIComponent(jobId)}`),
      api(`/jobs/${encodeURIComponent(jobId)}/events?since=${cursor}&limit=300`),
      api(`/jobs/${encodeURIComponent(jobId)}/manifest`),
    ]);
    const events = known.concat(eventPage.events || []);
    state.eventsByJob.set(jobId, events.slice(-500));
    renderJobDetail(job, state.eventsByJob.get(jobId), manifestPage.manifest || null);
  } catch (error) {
    byId("jobDetail").replaceChildren(Object.assign(document.createElement("div"), { className: "empty-state", textContent: error.message }));
  }
}

function renderJobDetail(job, events, manifest) {
  const root = byId("jobDetail");
  const previousLog = q(".event-log", root);
  const previousLogTop = previousLog?.scrollTop || 0;
  const followedLogTail = previousLog
    ? previousLog.scrollHeight - previousLog.clientHeight - previousLog.scrollTop < 32
    : true;
  const previousRootTop = root.scrollTop;
  root.replaceChildren();
  const header = document.createElement("div");
  header.className = "detail-header";
  const heading = document.createElement("div");
  const badge = statusBadge(job.status);
  const h2 = document.createElement("h2");
  h2.textContent = job.spec?.result?.name || "仿真任务";
  const id = document.createElement("p");
  id.textContent = job.id;
  heading.append(badge, h2, id);
  const actions = document.createElement("div");
  actions.className = "detail-actions";
  (job.available_actions || []).filter((action) => action.type === "cancel_job").forEach(() => {
    const button = actionButton("取消任务", "danger", () => cancelJob(job.id));
    actions.append(button);
  });
  if (manifest?.result_ref) {
    actions.append(actionButton("下载结果 ZIP", "primary", () => downloadResult(manifest.result_ref)));
  }
  header.append(heading, actions);

  const grid = document.createElement("div");
  grid.className = "detail-grid";
  const stagesSection = document.createElement("section");
  stagesSection.className = "detail-section";
  const stagesTitle = document.createElement("h3");
  stagesTitle.textContent = "执行阶段";
  const stages = document.createElement("div");
  stages.className = "stage-list";
  (job.stages || []).forEach((stage) => stages.append(renderStage(job, stage)));
  stagesSection.append(stagesTitle, stages);

  const summarySection = document.createElement("section");
  summarySection.className = "detail-section";
  const summaryTitle = document.createElement("h3");
  summaryTitle.textContent = "任务配置";
  const summary = document.createElement("dl");
  summary.className = "spec-summary";
  const fields = [
    ["数据", job.spec?.data?.path],
    ["Selena", selenaName(job.spec?.selena?.source || job.spec?.selena?.mode)],
    ["已有 Selena 文件夹", job.spec?.selena?.existing_path],
    ["Runtime XML", job.spec?.selena?.runtime_xml],
    ["Adapter", job.spec?.simulation?.adapter_file],
    ["MatFilter", job.spec?.simulation?.mat_filter],
    ["执行目标", targetName(job.spec?.simulation?.target)],
    ["进度", `${Math.round((job.progress || 0) * 100)}%`],
  ];
  fields.forEach(([label, value]) => {
    const wrap = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value || "未设置";
    wrap.append(dt, dd); summary.append(wrap);
  });
  summarySection.append(summaryTitle, summary);
  grid.append(stagesSection, summarySection);

  const manifestStatus = String(manifest?.status || "").toLowerCase();
  const failure = document.createElement("section");
  failure.className = "manifest-failure";
  if (["failed", "failure", "partial"].includes(manifestStatus)) {
    const failureTitle = document.createElement("h3");
    failureTitle.textContent = "仿真失败原因";
    const failureSummary = document.createElement("p");
    const failed = Number(manifest?.summary?.failed_count ?? manifest?.summary?.fail_count ?? 0);
    const total = Number(manifest?.summary?.task_count ?? 0);
    failureSummary.textContent = total ? `${failed}/${total} 个数据任务失败` : "仿真结果报告失败";
    const errors = document.createElement("ul");
    (manifest?.summary?.errors || []).slice(0, 5).forEach((message) => {
      const item = document.createElement("li");
      item.textContent = message;
      errors.append(item);
    });
    failure.append(failureTitle, failureSummary, errors);
  }

  const log = document.createElement("section");
  log.className = "event-log";
  log.setAttribute("aria-label", "任务事件");
  if (!events.length) log.textContent = "暂无新事件";
  events.forEach((event) => {
    const line = document.createElement("div");
    line.className = "event-line";
    const time = document.createElement("time");
    time.textContent = formatTime(event.created_at || event.timestamp);
    const text = document.createElement("span");
    text.textContent = friendlyEvent(event);
    line.append(time, text); log.append(line);
  });
  root.append(header, grid);
  if (failure.childElementCount) root.append(failure);
  root.append(log);
  root.scrollTop = previousRootTop;
  log.scrollTop = followedLogTail ? log.scrollHeight : Math.min(previousLogTop, log.scrollHeight);
}

async function downloadResult(resultRef) {
  try {
    const headers = new Headers();
    if (state.accessToken) headers.set("Authorization", `Bearer ${state.accessToken}`);
    const response = await fetch(`${API}/results/${encodeURIComponent(resultRef)}/download`, { headers });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new ApiError(response.status, payload);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "radar-sim-result.zip";
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    showToast(error.message || "结果下载失败");
  }
}

function renderStage(job, stage) {
  const row = document.createElement("div");
  row.className = "stage-row";
  row.append(statusBadge(stage.status));
  const copy = document.createElement("div");
  copy.className = "stage-copy";
  const title = document.createElement("strong");
  title.textContent = stageName(stage.stage_type || stage.task_type);
  const detail = document.createElement("small");
  detail.textContent = friendlyStageDetail(stage);
  copy.append(title, detail);
  const actions = document.createElement("div");
  actions.className = "stage-actions";
  if (["failed", "cancelled"].includes(stage.status)) {
    actions.append(actionButton("重试", "secondary", () => retryStage(job.id, stage.stage_id || stage.task_id)));
  }
  const canUpload = (stage.error?.actions || []).some((action) => action.type === "upload_data");
  if (stage.status === "blocked" && canUpload) {
    actions.append(actionButton("检查数据路径", "secondary", () => continueWithDataPath(job.spec)));
  }
  row.append(copy, actions);
  return row;
}

function continueWithDataPath(spec) {
  applyRunConfig(spec || {});
  switchView("create");
  byId("dataPath").focus();
  showToast("请检查数据路径；系统会自动识别本地或共享数据并按执行目标处理");
}

function actionButton(label, style, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${style}`;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

async function cancelJob(jobId) {
  try {
    await api(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    showToast("已请求取消任务");
    await loadJobs();
  } catch (error) { showToast(error.message); }
}

async function retryStage(jobId, stageId) {
  try {
    await api(`/jobs/${encodeURIComponent(jobId)}/stages/${encodeURIComponent(stageId)}/retry`, { method: "POST" });
    showToast("阶段已重新排队");
    await loadJobs();
  } catch (error) { showToast(error.message); }
}

function statusBadge(status) {
  const span = document.createElement("span");
  span.className = `status ${status || "queued"}`;
  span.textContent = statusName(status);
  return span;
}

function statusName(value) {
  return {
    queued: "排队中", running: "运行中", needs_input: "需要处理",
    succeeded: "已完成", failed: "失败", cancelled: "已取消",
    blocked: "已阻塞", skipped: "已跳过", cancel_requested: "取消中",
  }[value] || value || "未知";
}

function stageName(value) {
  return {
    resolve_spec: "识别代码与 Runtime", environment_check: "环境检查", build_selena: "编译 Selena",
    prepare_source: "准备代码工作区", prepare_selena: "准备 Selena", prepare_data: "准备数据",
    register_artifact: "准备 Selena 产物", preflight: "仿真前检查",
    run_simulation: "运行仿真", collect_results: "收集仿真结果",
    finalize_manifest: "生成结果清单", collect_manifest: "生成结果清单", cluster_run: "Cluster 仿真",
  }[value] || value || "";
}

function friendlySkipReason(value) {
  return {
    current_workspace_selected: "使用当前工作区，不切换分支",
    existing_selena_uses_registered_artifact: "使用已有 Selena，不需要编译",
    registered_runtime_bundle_selected: "Selena 已准备完成",
    existing_selena_kept_on_local_full_agent: "已有 Selena 保留在本机",
    dry_run_plan_only: "仅生成计划",
  }[value] || value || "当前路径不需要";
}

function friendlyStageDetail(stage) {
  const byCode = {
    shared_dataset_unavailable: "共享路径未授权，请上传数据或联系管理员配置共享空间",
    agent_data_upload_required: "等待已授权的 Windows Agent 上传数据",
    workspace_snapshot_pending: "等待 Windows Agent 检查当前工作区",
  };
  if (byCode[stage.error?.code]) return byCode[stage.error.code];
  const byReason = {
    resolved_during_submission: "提交时已完成",
    current_workspace_verified_by_environment_check: "由环境检查阶段确认",
    not_needed: "当前执行路径不需要此阶段",
  };
  if (byReason[stage.skip_reason]) return byReason[stage.skip_reason];
  if (stage.error?.message) return stage.error.message;
  if (stage.status === "running" && Number(stage.progress || 0) <= 0) {
    return "正在运行，日志持续更新";
  }
  return `${Math.round((stage.progress || 0) * 100)}%`;
}

function friendlyEvent(event) {
  const message = event.message || "";
  const queued = message.match(/^([a-z_]+) queued$/);
  if (queued) return `${stageName(queued[1])} 已进入队列`;
  const direct = {
    resolved_during_submission: "提交时已完成配置解析",
    current_workspace_verified_by_environment_check: "当前工作区将由环境检查阶段确认",
    "shared path is not under an authorized namespace": "共享路径未授权，需要上传数据或配置共享空间",
  };
  if (direct[message]) return direct[message];
  if (event.event === "job.created") return "任务已创建";
  return message || event.code || event.event || "状态更新";
}

function selenaName(value) {
  return { build: "本地编译 Selena", existing: "已有 Selena 文件夹", auto: "自动选择", current_workspace: "当前工作区", branch: "指定分支" }[value] || value;
}

function targetName(value) {
  return { auto: "自动", local: "本地", cluster: "Cluster" }[value] || value;
}

function formatTime(value) {
  if (!value) return "";
  const numeric = Number(value);
  const date = Number.isFinite(numeric) ? new Date(numeric * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function schedulePolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  if (state.view === "tasks") state.pollTimer = setInterval(loadJobs, 4000);
}

async function initialize() {
  qa(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  qa('input[name="target"]').forEach((input) => input.addEventListener("change", () => {
    updateConditionalFields(); updateRouteSummary();
  }));
  byId("selenaSource").addEventListener("change", () => { updateConditionalFields(); updateRouteSummary(); });
  byId("existingPath").addEventListener("input", updateRouteSummary);
  byId("selenaBranch").addEventListener("input", updateRouteSummary);
  byId("chooseDataFolder").addEventListener("click", () => byId("dataFolderInput").click());
  byId("dataFolderInput").addEventListener("change", (event) => chooseDataFolder(event.target.files));
  byId("dataPath").addEventListener("input", () => {
    const value = byId("dataPath").value.trim();
    if (value !== state.selectedFolderLabel && value !== state.uploadedDataPath) {
      state.dataFolderFiles = [];
      state.uploadedDataPath = "";
      state.selectedFolderLabel = "";
      byId("dataUploadState").textContent = "没有 Windows Agent 时，可直接选择浏览器本机文件夹；提交时自动上传。";
    }
  });
  byId("chooseAdapter").addEventListener("click", () => byId("adapterUpload").click());
  byId("adapterUpload").addEventListener("change", (event) => uploadConfigAsset("adapter", event.target.files[0], "adapterFile"));
  byId("chooseMatFilter").addEventListener("click", () => byId("matFilterUpload").click());
  byId("matFilterUpload").addEventListener("change", (event) => uploadConfigAsset("mat_filter", event.target.files[0], "matFilter"));
  byId("simulationForm").addEventListener("submit", submitCurrentSpec);
  byId("validateSpec").addEventListener("click", () => validateCurrentSpec().catch(() => {}));
  byId("importYaml").addEventListener("click", () => byId("yamlFile").click());
  byId("yamlFile").addEventListener("change", (event) => importYamlFile(event.target.files[0]));
  byId("exportYaml").addEventListener("click", exportYaml);
  byId("refreshJobs").addEventListener("click", loadJobs);
  byId("statusFilter").addEventListener("change", loadJobs);
  byId("saveToken").addEventListener("click", saveAccessToken);
  byId("accessToken").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveAccessToken();
  });
  updateConditionalFields();
  updateRouteSummary();

  try {
    const health = await api("/health");
    state.authenticationRequired = Boolean(health.authentication_required);
    if (state.authenticationRequired) {
      byId("authEntry").hidden = false;
      byId("accessToken").value = state.accessToken;
      if (!state.accessToken) {
        showAuthenticationEntry();
      } else {
        await saveAccessToken();
      }
    } else {
      byId("apiState").textContent = health.ok ? "服务已连接" : "服务异常";
      byId("apiState").className = `api-state ${health.ok ? "ok" : "error"}`;
    }
  } catch (error) {
    byId("apiState").textContent = "服务连接失败";
    byId("apiState").className = "api-state error";
    showToast(error.message, 5000);
  }
  switchView(state.view === "tasks" ? "tasks" : "create");
}

document.addEventListener("DOMContentLoaded", initialize);
