"use strict";

const API = "/api/v1";
// The task center is a live overview, not the job-detail archive.  Fetch a
// bounded recent page here; selecting a row still loads the complete job from
// GET /jobs/{job_id}. This prevents legacy payload fields from blocking the
// first paint when a shared service has many historical jobs.
const TASK_CENTER_PAGE_SIZE = 20;
const state = {
  view: sessionStorage.getItem("rsimView") || "create",
  jobs: [],
  jobsSignature: "",
  jobsLoaded: false,
  selectedJobId: sessionStorage.getItem("rsimSelectedJobId") || "",
  eventsByJob: new Map(),
  pollTimer: null,
  jobsRequestInFlight: false,
  capabilitiesRequestInFlight: false,
  capabilities: null,
  connectorAwait: null,
  accessToken: sessionStorage.getItem("rsimAccessToken") || "",
  // In the trusted no-auth deployment this is an explicit, durable grouping
  // label (for example the company's NTID), not a generated browser identity.
  // Both clients use the lower-case `user-<id>` namespace.  Authenticated
  // deployments ignore it and derive owner from the Bearer principal.
  userId: storedWebUserId() || legacyWebUserId(),
  legacyIdentity: !storedWebUserId() && Boolean(legacyWebUserId()),
  identityRequired: false,
  authenticationRequired: false,
  importedSelection: null,
  validatedTarget: "",
  // Keep an in-flight submission key across a page refresh. If the server
  // committed the Job just before the tab disconnected, the next click can
  // safely retrieve it instead of creating a duplicate task.
  submitIdempotencyKey: sessionStorage.getItem("rsimSubmitIdempotencyKey") || "",
  submitConfigSignature: sessionStorage.getItem("rsimSubmitConfigSignature") || "",
  resultDownloadsInFlight: new Map(),
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

function requestHeaders(initial = {}) {
  const headers = new Headers(initial || {});
  if (state.userId) headers.set("X-Rsim-User", state.userId);
  if (state.accessToken) headers.set("Authorization", `Bearer ${state.accessToken}`);
  return headers;
}

async function fetchBinary(path, options = {}) {
  if (state.identityRequired && !state.authenticationRequired && path !== "/health") {
    throw new Error("请先输入用户标识；它仅用于可信内网的任务隔离，不是登录认证");
  }
  const headers = requestHeaders(options.headers);
  const controller = new AbortController();
  // Connector packages are small; result archives can be large and should
  // get a longer header/body deadline while still failing instead of hanging
  // a browser tab forever after a service restart.
  const timeoutMs = Math.max(1000, Number(options.timeoutMs || 20000));
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      method: options.method || "GET",
      headers,
      body: options.body,
      signal: options.signal || controller.signal,
    });
    if (!response.ok) {
      const type = response.headers.get("content-type") || "";
      const payload = type.includes("json") ? await response.json().catch(() => ({})) : {};
      if (response.status === 401) showAuthenticationEntry("访问令牌无效或已失效");
      throw new ApiError(response.status, payload);
    }
    return await response.blob();
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error("文件下载超时，请检查 Linux 服务状态后重试");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  // Keep the object URL alive until Chromium has accepted the synthetic
  // download. Revoking it in the same task can leave a complete payload as
  // an "Unconfirmed *.crdownload" file on some managed browser builds.
  document.body.append(link);
  link.click();
  window.setTimeout(() => {
    link.remove();
    URL.revokeObjectURL(url);
  }, 1000);
}

async function api(path, options = {}) {
  if (state.identityRequired && !state.authenticationRequired && path !== "/health") {
    throw new Error("请先输入用户标识；它仅用于可信内网的任务隔离，不是登录认证");
  }
  const headers = requestHeaders(options.headers);
  if (options.json !== undefined) headers.set("Content-Type", "application/json");
  // A browser fetch has no default deadline.  Without one, a restarted or
  // unreachable Linux service leaves the Task Center stuck on “正在加载任务”
  // forever and also blocks later polling.  Keep the timeout local to the
  // control-plane request; long-running simulations are observed by polling
  // their persisted Job and are not held open in this request.
  const controller = new AbortController();
  const timeoutMs = Math.max(1000, Number(options.timeoutMs || 20000));
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      method: options.method || "GET",
      headers,
      body: options.json !== undefined ? JSON.stringify(options.json) : options.body,
      signal: options.signal || controller.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error("Linux 服务响应超时，请检查服务状态；任务状态不会丢失，稍后会自动重试");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    if (response.status === 401) showAuthenticationEntry("访问令牌无效或已失效");
    throw new ApiError(response.status, payload);
  }
  return payload;
}

function storedWebUserId() {
  const key = "rsimUserId";
  try {
    const current = localStorage.getItem(key) || "";
    return /^user-[a-z0-9][a-z0-9_.-]{0,127}$/.test(current.trim()) ? current.trim() : "";
  } catch {
    return "";
  }
}

function legacyWebUserId() {
  try {
    const current = localStorage.getItem("rsimBrowserUserId") || "";
    return /^web-[a-f0-9]{24,64}$/i.test(current.trim()) ? current.trim() : "";
  } catch {
    return "";
  }
}

function stableWebUserId(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9_.-]{0,127}$/.test(normalized)) return "";
  return normalized.startsWith("user-") ? normalized : `user-${normalized}`;
}

function showStableIdentityEntry(message = "请输入与 SDK 相同的用户标识", { required = true } = {}) {
  if (state.authenticationRequired) return;
  state.identityRequired = required;
  const entry = byId("identityEntry");
  if (entry) entry.hidden = false;
  const input = byId("userIdentity");
  if (input) {
    input.value = state.legacyIdentity ? "" : state.userId;
    input.setAttribute("aria-invalid", "false");
    input.focus();
  }
  const status = byId("apiState");
  if (status) {
    status.textContent = message;
    status.className = "api-state local-reconnecting";
  }
}

async function saveStableIdentity() {
  const input = byId("userIdentity");
  const entered = String(input?.value || "").trim();
  const value = stableWebUserId(entered);
  if (!value) {
    if (input) input.setAttribute("aria-invalid", "true");
    showStableIdentityEntry("用户标识需为字母、数字、点、下划线或短横线");
    return;
  }
  try {
    localStorage.setItem("rsimUserId", value);
    localStorage.removeItem("rsimBrowserUserId");
  } catch {
    // A private browsing context may reject storage.  Keep the value for the
    // current page; the user can enter it again in a new context.
  }
  state.userId = value;
  state.legacyIdentity = false;
  state.identityRequired = false;
  if (input) input.setAttribute("aria-invalid", "false");
  const entry = byId("identityEntry");
  if (entry) entry.hidden = true;
  try {
    await refreshCapabilities();
    const status = byId("apiState");
    if (status) {
      status.textContent = "Linux 服务已连接";
      status.className = "api-state ok";
    }
    if (state.view === "tasks") await loadJobs();
  } catch (error) {
    showStableIdentityEntry(error.message || "用户标识连接失败");
  }
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
    await refreshCapabilities();
    byId("apiState").textContent = "Linux 服务已连接";
    byId("apiState").className = "api-state ok";
    if (state.view === "tasks") await loadJobs();
  } catch (error) {
    showAuthenticationEntry(error.message || "连接失败");
  }
}

function hasWindowsCapability(_mode, capabilities = state.capabilities) {
  const snapshot = capabilities?.capabilities || capabilities || {};
  return Boolean(snapshot.windows?.available);
}

function hasConfiguredWindows(_mode, capabilities = state.capabilities) {
  const snapshot = capabilities?.capabilities || capabilities || {};
  return Number(snapshot.windows?.configured_count || 0) > 0;
}

function configuredWindowsCount(capabilities = state.capabilities) {
  const snapshot = capabilities?.capabilities || capabilities || {};
  return Math.max(0, Number(snapshot.windows?.configured_count || 0));
}

function connectedWindowsCount(capabilities = state.capabilities) {
  const snapshot = capabilities?.capabilities || capabilities || {};
  return Math.max(0, Number(snapshot.windows?.count || 0));
}

function multiWindowsHint(capabilities = state.capabilities) {
  const count = Math.max(configuredWindowsCount(capabilities), connectedWindowsCount(capabilities));
  return count > 1
    ? "当前账号已配置多台 Windows 电脑，请在存放本任务路径的电脑上运行连接程序。"
    : "";
}

function windowsConnectorNeedsUpdate(capabilities = state.capabilities) {
  const snapshot = capabilities?.capabilities || capabilities || {};
  return snapshot.windows_connector?.update_required === true;
}

function updateConnectorUpdateBanner(capabilities = state.capabilities) {
  const banner = byId("connectorUpdateBanner");
  if (!banner) return;
  const snapshot = capabilities?.capabilities || capabilities || {};
  const connector = snapshot.windows_connector || {};
  const updateRequired = connector.update_required === true;
  banner.hidden = !updateRequired;
  if (!updateRequired) return;
  const count = Math.max(1, Number(connector.outdated_count || 0));
  byId("connectorUpdateMessage").textContent =
    `检测到 ${count} 个旧连接组件，需要更新后才能领取新任务。更新会保留用户身份、路径绑定、自启动配置和历史任务。`;
}

function updateConnectionStates(capabilities = state.capabilities) {
  const local = byId("windowsState");
  if (!local) return;
  const snapshot = capabilities?.capabilities || capabilities || {};
  const connected = Boolean(snapshot.windows?.available);
  const configured = Number(snapshot.windows?.configured_count || 0) > 0;
  const count = Math.max(0, Number(snapshot.windows?.count || 0));
  const configuredCount = Math.max(0, Number(snapshot.windows?.configured_count || 0));
  const updateRequired = windowsConnectorNeedsUpdate(capabilities);
  updateConnectorUpdateBanner(capabilities);
  local.textContent = connected
    ? `当前账号有 ${count} 台 Windows 电脑已连接`
    : updateRequired ? "当前账号的 Windows 连接组件需更新"
      : configured
        ? `当前账号有 ${configuredCount} 台 Windows 电脑正在自动重连`
        : "当前账号尚未连接 Windows 电脑";
  local.className = connected
    ? "api-state ok"
    : configured || updateRequired ? "api-state local-reconnecting" : "api-state local-offline";
}

async function refreshCapabilities() {
  if (state.capabilitiesRequestInFlight) return state.capabilities;
  state.capabilitiesRequestInFlight = true;
  try {
    const previous = state.capabilities;
    const current = await api("/capabilities");
    state.capabilities = current;
    updateConnectionStates(current);
    updateCreateWindowsCallout();
    const waiting = state.connectorAwait;
    if (waiting && !hasWindowsCapability(waiting.mode, previous) && hasWindowsCapability(waiting.mode, current)) {
      state.connectorAwait = null;
      showToast("当前账号已有 Windows 电脑上线，等待中的任务将自动继续", 5000);
    }
    if (windowsConnectorNeedsUpdate(previous) && !windowsConnectorNeedsUpdate(current)) {
      byId("connectorUpdateStatus").textContent = "更新完成，Windows 连接组件已经重新上线";
      showToast("Windows 连接组件已更新并重新上线", 5000);
    }
    return current;
  } finally {
    state.capabilitiesRequestInFlight = false;
  }
}

function createFormWindowsRequirement() {
  const target = selectedValue("target") || "auto";
  const source = byId("selenaSource")?.value || "build";
  const paths = [
    "dataPath", "codePath", "selenaBuildScript", "packageBuildScript",
    "existingPath", "runtimeXml", "adapterFile", "matFilter",
  ].map((id) => byId(id)?.value || "");
  const usesWindowsPath = paths.some(isWindowsLocalPath);
  if (target === "local" && !hasWindowsCapability("full")) {
    return {
      mode: "unified",
      title: "本地仿真需要连接这台电脑",
      capability: "需要连接本机",
      reason: `运行 Selena、准备本地输入和收集结果需要由这台 Windows 电脑完成。只需安装一次连接程序。${multiWindowsHint() ? ` ${multiWindowsHint()}` : ""}`,
    };
  }
  if (source === "build" && !hasWindowsCapability("light")) {
    return {
      mode: "unified",
      title: "任务需要连接这台电脑",
      capability: "需要连接本机",
      reason: `代码仓和编译脚本只在 Windows 电脑上可访问；完成一次连接后，Linux 会自动调度编译、文件准备和后续仿真。${multiWindowsHint() ? ` ${multiWindowsHint()}` : ""}`,
    };
  }
  if (usesWindowsPath && !hasWindowsCapability("light")) {
    return {
      mode: "unified",
      title: "任务需要连接这台电脑",
      capability: "需要连接本机",
      reason: `配置包含本地路径，需要这台电脑读取 Selena、Runtime、MatFilter 或数据并交给执行端。已有 Selena 不需要重新编译。${multiWindowsHint() ? ` ${multiWindowsHint()}` : ""}`,
    };
  }
  return null;
}

function updateCreateWindowsCallout() {
  const panel = byId("createWindowsCallout");
  if (!panel) return;
  const requirement = createFormWindowsRequirement();
  panel.hidden = !requirement;
  panel.dataset.mode = requirement?.mode || "";
  if (!requirement) return;
  byId("createWindowsTitle").textContent = requirement.title;
  byId("createWindowsCapability").textContent = requirement.capability;
  byId("createWindowsReason").textContent = requirement.reason;
  const updateRequired = windowsConnectorNeedsUpdate();
  const configured = hasConfiguredWindows(requirement.mode);
  const button = byId("connectWindowsFromCreate");
  button.textContent = updateRequired ? "一键更新本机组件" : configured ? "重新连接本机" : "一键连接本机";
  byId("createWindowsStatus").textContent = updateRequired
    ? "现有连接组件版本过旧；更新会保留原有绑定和任务配置"
    : configured
    ? "本机已经配置过，通常会自动恢复；长时间未连接时可重新下载连接程序"
    : "安装一次，后续开机自动连接";
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
  const dataPath = byId("dataPath").value.trim();
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
  const resultPath = byId("resultPath").value.trim();
  const radarSource = byId("radarSource").value.trim();
  if (source === "build" && !codePath) throw new Error("本地编译需要填写代码路径");
  if (source === "build" && !selenaBuildScript) throw new Error("本地编译需要填写 Selena 编译脚本");
  if (source === "existing" && !existingPath) {
    byId("existingPath").setAttribute("aria-invalid", "true");
    throw new Error("请填写 Selena 产物文件夹");
  }
  if (!runtimeXml) throw new Error("请选择与 Selena 匹配的 Runtime XML");

  const selena = {
    source,
    code_path: codePath,
    branch,
    selena_build_script: selenaBuildScript,
    existing_path: source === "existing" ? existingPath : "",
    runtime_xml: runtimeXml,
  };
  if (packageBuildScript) selena.package_build_script = packageBuildScript;

  return {
    schema_version: "2.0",
    selena,
    data: { path: dataPath },
    simulation: {
      target: selectedValue("target") || "auto",
      source: radarSource,
      adapter_file: adapterFile,
      mat_filter: matFilter,
    },
    result: { path: resultPath },
  };
}

function applyRunConfig(config) {
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
  byId("radarSource").value = config.simulation?.source || "";
  byId("adapterFile").value = config.simulation?.adapter_file || "";
  byId("matFilter").value = config.simulation?.mat_filter || "";
  byId("resultPath").value = config.result?.path || "";
  updateConditionalFields();
  updateRouteSummary();
}

function updateConditionalFields() {
  const source = byId("selenaSource").value;
  const usingExisting = source === "existing";
  byId("buildFields").hidden = false;
  byId("existingFields").hidden = !usingExisting;
  byId("existingPath").required = usingExisting;
  byId("existingPath").setAttribute("aria-required", String(usingExisting));
  if (!usingExisting) byId("existingPath").removeAttribute("aria-invalid");
  for (const id of ["codePath", "selenaBuildScript"]) {
    byId(id).required = source === "build";
  }
  byId("packageBuildScript").required = false;
  byId("workspaceEvidenceHint").textContent = source === "build"
    ? "本地编译需要代码仓和 Selena 编译脚本；软件包脚本可选，用于补充依赖线索。"
    : "以下代码仓和脚本为可选识别证据；填写后系统会与 Selena/Runtime 交叉校验，不一致时阻止任务。";
  updateCreateWindowsCallout();
}

function updateRouteSummary() {
  const target = selectedValue("target") || "auto";
  const source = byId("selenaSource").value;
  const finalTarget = state.validatedTarget || (target === "auto" ? "" : target);
  const targetText = { auto: "自动选择本地或 Cluster", local: "在本机执行本地仿真", cluster: "由 Cluster 执行仿真" }[target];
  const selenaText = source === "build"
    ? (byId("selenaBranch").value.trim() ? "校验期望分支并编译当前工作区" : "编译当前工作区修改")
    : "使用已有 Selena 文件夹";
  byId("finalExecutionSummary").textContent = `最终执行位置：${{
    local: "本机",
    cluster: "Cluster",
  }[finalTarget] || "自动（提交前确认）"}`;
  byId("finalSelenaSummary").textContent = `Selena 来源：${source === "existing" ? "已有产物" : "本地编译"}`;
  byId("routeSummary").textContent = `${selenaText}，${targetText}`;
  updateImportedSelectionWarning(target, source);
  updateCreateWindowsCallout();
}

function updateImportedSelectionWarning(target, source) {
  const warning = byId("importSelectionWarning");
  const imported = state.importedSelection;
  const changes = [];
  if (imported && target !== imported.target) {
    changes.push(`执行位置已从 ${submissionTargetName(imported.target)} 改为 ${submissionTargetName(target)}`);
  }
  if (imported && source !== imported.source) {
    changes.push(`Selena 来源已从 ${submissionSourceName(imported.source)} 改为 ${submissionSourceName(source)}`);
  }
  warning.hidden = changes.length === 0;
  warning.textContent = changes.length ? `注意：导入 YAML 后，${changes.join("；")}。提交前请确认。` : "";
}

function submissionTargetName(value) {
  return { auto: "自动", local: "本机", cluster: "Cluster" }[value] || value;
}

function submissionSourceName(value) {
  return value === "existing" ? "已有产物" : "本地编译";
}

function invalidateValidatedTarget() {
  state.validatedTarget = "";
  updateRouteSummary();
}

function confirmSubmission(config, validation) {
  const selectedTarget = validation?.execution?.selected_target || config.simulation?.target || "auto";
  state.validatedTarget = selectedTarget;
  updateRouteSummary();
  const changedWarning = byId("importSelectionWarning").hidden
    ? ""
    : `\n${byId("importSelectionWarning").textContent}`;
  const blockers = Array.isArray(validation?.readiness?.blockers)
    ? validation.readiness.blockers
    : [];
  const waitingNotice = blockers.length
    ? `\n\n当前尚未就绪：\n${blockers.map((item) => `- ${item.message}`).join("\n")}\n提交后任务会保留并等待能力恢复，无需重新提交。`
    : "";
  return window.confirm(
    `请确认本次仿真任务：\n最终执行位置：${submissionTargetName(selectedTarget)}\n`
    + `Selena 来源：${submissionSourceName(config.selena?.source)}${changedWarning}${waitingNotice}`,
  );
}

function renderExecutionPlan(result) {
  const stages = Array.isArray(result?.execution_plan) ? result.execution_plan : [];
  if (!stages.length) return;
  const target = result?.execution?.selected_target;
  state.validatedTarget = target || "";
  updateRouteSummary();
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
    const config = runConfigFromForm();
    const result = await api("/run-configs/validate", { method: "POST", json: config });
    renderExecutionPlan(result);
    const readiness = result.readiness || { can_submit: true, blockers: [] };
    const blockers = Array.isArray(readiness.blockers) ? readiness.blockers : [];
    byId("formError").className = readiness.can_submit ? "notice success" : "notice error";
    byId("formError").textContent = readiness.can_submit
      ? `配置检查通过，指纹 ${result.fingerprint.slice(0, 19)}...`
      : blockers.map((item) => `${item.message}${item.action ? ` ${item.action}` : ""}`).join("\n");
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
    const config = runConfigFromForm();
    const validation = await api("/run-configs/validate", { method: "POST", json: config });
    renderExecutionPlan(validation);
    if (!confirmSubmission(config, validation)) {
      showToast("已取消提交，配置保持不变");
      return;
    }
    // Keep the key tied to the exact YAML. If the POST response is lost after
    // the server commits, clicking submit again returns the original Job
    // instead of creating a duplicate. Editing any field automatically starts
    // a new submission identity.
    const configSignature = JSON.stringify(config);
    if (state.submitConfigSignature !== configSignature || !state.submitIdempotencyKey) {
      state.submitConfigSignature = configSignature;
      state.submitIdempotencyKey = globalThis.crypto?.randomUUID
        ? globalThis.crypto.randomUUID()
        : `${Date.now()}-${Math.random()}`;
      sessionStorage.setItem("rsimSubmitConfigSignature", state.submitConfigSignature);
      sessionStorage.setItem("rsimSubmitIdempotencyKey", state.submitIdempotencyKey);
    }
    const job = await api("/run-jobs", {
      method: "POST",
      headers: { "Idempotency-Key": state.submitIdempotencyKey },
      json: {
        config,
        dry_run: false,
        client_transfer_roles: clientTransferRoles(config),
      },
    });
    state.submitIdempotencyKey = "";
    state.submitConfigSignature = "";
    sessionStorage.removeItem("rsimSubmitConfigSignature");
    sessionStorage.removeItem("rsimSubmitIdempotencyKey");
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
    state.importedSelection = {
      target: result.config?.simulation?.target || "auto",
      source: result.config?.selena?.source || "build",
    };
    state.validatedTarget = "";
    applyRunConfig(result.config);
    showToast(
      result.config?.selena?.source === "existing"
        ? "YAML 已导入：请确认 Selena 产物文件夹和 Runtime XML"
        : "YAML 已导入：当前配置将从本地代码编译 Selena",
    );
  } catch (error) {
    showFormError(error);
  } finally {
    byId("yamlFile").value = "";
  }
}

async function exportYaml() {
  clearFormError();
  try {
    const config = runConfigFromForm();
    const result = await api("/run-configs/export", { method: "POST", json: { config } });
    const blob = new Blob([result.yaml_content], { type: "text/yaml;charset=utf-8" });
    triggerBlobDownload(blob, "radar-sim.simulation.yaml");
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
    // Capabilities only affect the waiting badge.  Fetch them alongside the
    // job page so a slow capability snapshot cannot delay the first task-list
    // paint by another network round trip.
    const capabilitiesRequest = refreshCapabilities().catch(() => state.capabilities);
    const filter = byId("statusFilter").value;
    const page = await api(`/jobs?limit=${TASK_CENTER_PAGE_SIZE}${filter ? `&status=${encodeURIComponent(filter)}` : ""}`);
    await capabilitiesRequest;
    const jobs = page.jobs || [];
    const capabilitySignature = JSON.stringify(state.capabilities?.capabilities?.windows || {});
    const signature = JSON.stringify([
      capabilitySignature,
      ...jobs.map((job) => [
        job.id,
        job.status,
        job.progress,
        job.current_stage,
        job.waiting?.reason || "",
        job.waiting?.connection_state || "",
        job.waiting?.message || "",
      ]),
    ]);
    state.jobs = jobs;
    // An empty successful page is still a meaningful state. Always paint it
    // after the first successful response; otherwise a polling cycle can
    // leave the initial "正在加载任务" placeholder visible forever when the
    // signature is unchanged.
    if (!state.jobsLoaded || !jobs.length || signature !== state.jobsSignature) {
      state.jobsSignature = signature;
      renderJobs();
    }
    state.jobsLoaded = true;
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
    title.textContent = "仿真任务";
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
    const waiting = windowsWaitState(job);
    const currentStage = stageName(job.current_stage);
    stage.textContent = waiting
      ? `${waiting.reconnecting ? "本机正在自动重连" : "等待连接本机"} · ${waiting.shortCapability}`
      : currentStage || (
      ["failed", "cancelled", "partial", "succeeded"].includes(job.status)
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

function isWindowsLocalPath(value) {
  const path = String(value || "").trim();
  return /^[a-z]:[\\/]/i.test(path)
    || /^file:\/\/[a-z]:/i.test(path)
    || /^\\\\/.test(path)
    || /^\/\//.test(path);
}

function clientTransferRoles(config) {
  const roles = [];
  const add = (role, value) => {
    const text = String(value || "").trim();
    if (text && isWindowsLocalPath(text) && !/^(?:shared|dataset):\/\//i.test(text)) {
      roles.push(role);
    }
  };
  add("dataset", config.data?.path);
  if (config.selena?.source === "existing") {
    add("runtime_bundle", config.selena?.existing_path);
  }
  add("runtime_xml", config.selena?.runtime_xml);
  add("mat_filter", config.simulation?.mat_filter);
  add("adapter", config.simulation?.adapter_file);
  return [...new Set(roles)];
}

function selectedExecutionTarget(job) {
  return job.resolved_spec?.decisions?.execution?.selected_target
    || job.resolved_spec?.execution?.selected_target
    || job.spec?.simulation?.target
    || "auto";
}

function windowsWaitState(job, candidateStage = null) {
  if (!job || job.cancel_requested || ["failed", "cancelled", "cancelling", "partial", "succeeded"].includes(job.status)) return null;
  const serverWaiting = ["windows_connection_required", "windows_path_access_required", "windows_connector_update_required"].includes(job.waiting?.reason)
    ? job.waiting : null;
  if (candidateStage && (candidateStage.stage_type || candidateStage.task_type) !== job.current_stage) return null;
  const stage = candidateStage || (job.stages || []).find((item) =>
    (item.stage_type || item.task_type) === job.current_stage
    && (["queued", "blocked"].includes(item.status)
      || (serverWaiting?.reason === "windows_connector_update_required" && item.status === "running"))
  );
  if (!stage || (!["queued", "blocked"].includes(stage.status)
    && !(serverWaiting?.reason === "windows_connector_update_required" && stage.status === "running"))) return null;

  const stageType = stage.stage_type || stage.task_type || "";
  const spec = job.spec || {};
  const source = spec.selena?.source || spec.selena?.mode || "auto";
  const target = selectedExecutionTarget(job);
  const pathMismatch = serverWaiting?.reason === "windows_path_access_required";
  const updateRequired = serverWaiting?.reason === "windows_connector_update_required";
  if (serverWaiting && (updateRequired || pathMismatch || !hasWindowsCapability(serverWaiting.mode))) {
    const localTarget = target === "local";
    const build = source === "build";
    const reconnecting = !updateRequired && !pathMismatch && (serverWaiting.connection_state === "reconnecting"
      || hasConfiguredWindows(serverWaiting.mode));
    if (updateRequired) {
      return {
        mode: "unified",
        updateRequired: true,
        reconnecting: false,
        title: "本机连接组件需要更新",
        capability: "更新后任务会自动继续",
        shortCapability: "本机组件更新",
        reason: "服务端已升级任务协议，旧连接组件不会再领取不兼容任务。更新会保留用户身份、路径绑定和 YAML。",
      };
    }
    if (pathMismatch) {
      return {
        mode: "unified",
        reconnecting: false,
        title: "当前在线电脑无法访问这些路径",
        capability: "需要连接存放配置和数据的电脑",
        shortCapability: "本机连接",
        reason: `${serverWaiting.message
          || "当前在线的 Windows 电脑无法确认能访问本任务的本地路径。请在文件所在电脑一键连接，或改用 Cluster 可访问的共享路径。"}${multiWindowsHint() ? ` ${multiWindowsHint()}` : ""}`,
      };
    }
    return {
      mode: "unified",
      reconnecting,
      title: reconnecting ? "本机连接暂时中断，正在自动重连" : "任务正在等待连接本机",
      capability: "需要连接本机",
      shortCapability: "本机连接",
      reason: `${localTarget
        ? "本地仿真需要这台 Windows 电脑执行 Selena 并收集结果。"
        : build
          ? "任务会在这台 Windows 电脑执行编译和文件准备，之后由 Linux/Cluster 继续调度。"
          : "任务需要这台电脑读取本地 Selena、Runtime、MatFilter 或数据；安装一次连接程序即可长期复用。"}${multiWindowsHint() ? ` ${multiWindowsHint()}` : ""}`,
    };
  }
  const paths = [
    spec.data?.path,
    spec.selena?.code_path,
    spec.selena?.selena_build_script,
    spec.selena?.package_build_script,
    spec.selena?.existing_path,
    spec.selena?.runtime_xml,
    spec.simulation?.adapter_file,
    spec.simulation?.mat_filter,
  ];
  const usesWindowsLocalPath = paths.some(isWindowsLocalPath);
  const buildStages = new Set(["resolve_spec", "environment_check", "prepare_source", "build_selena", "register_artifact"]);
  const localStages = new Set(["resolve_spec", "environment_check", "prepare_selena", "prepare_data", "preflight", "run_simulation", "collect_results", "finalize_manifest"]);

  if (target === "local" && !hasWindowsCapability("full")) {
    const reconnecting = hasConfiguredWindows("full");
    return {
      mode: "unified",
      reconnecting,
      title: reconnecting ? "本机连接暂时中断，正在自动重连" : "任务正在等待连接本机",
      capability: "需要连接本机",
      shortCapability: "本机连接",
      reason: "你选择了本地仿真，运行 Selena 和收集结果需要由这台 Windows 电脑完成。",
    };
  }
  if (source === "build" && buildStages.has(stageType) && !hasWindowsCapability("light")) {
    const reconnecting = hasConfiguredWindows("light");
    return {
      mode: "unified",
      reconnecting,
      title: reconnecting ? "本机连接暂时中断，正在自动重连" : "任务正在等待连接本机",
      capability: "需要连接本机",
      shortCapability: "本机连接",
      reason: "任务会编译当前代码工作区，再把 Selena 产物交给 Cluster；代码和编译脚本只在你的 Windows 电脑上可访问。",
    };
  }
  if (target !== "local" && usesWindowsLocalPath && localStages.has(stageType) && !hasWindowsCapability("light")) {
    const reconnecting = hasConfiguredWindows("light");
    return {
      mode: "unified",
      reconnecting,
      title: reconnecting ? "本机连接暂时中断，正在自动重连" : "任务正在等待连接本机",
      capability: "需要连接本机",
      shortCapability: "本机连接",
      reason: "配置中包含 Windows 本地路径，需要由这台电脑准备 Selena、Runtime 或数据，再交给 Cluster。",
    };
  }
  return null;
}

async function loadJobDetail(jobId, resetEvents) {
  state.selectedJobId = jobId;
  sessionStorage.setItem("rsimSelectedJobId", jobId);
  if (resetEvents) state.eventsByJob.delete(jobId);
  renderJobs();
  try {
    const known = state.eventsByJob.get(jobId) || [];
    const cursor = known.length ? Number(known[known.length - 1].id || 0) : 0;
    const tail = known.length ? "" : "&tail=true";
    const [job, eventPage, manifestPage] = await Promise.all([
      api(`/jobs/${encodeURIComponent(jobId)}`),
      api(`/jobs/${encodeURIComponent(jobId)}/events?since=${cursor}&limit=300${tail}`),
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
  h2.textContent = "仿真任务";
  const id = document.createElement("p");
  id.textContent = job.id;
  heading.append(badge, h2, id);
  const actions = document.createElement("div");
  actions.className = "detail-actions";
  (job.available_actions || []).filter((action) => action.type === "cancel_job").forEach(() => {
    const button = actionButton("取消任务", "danger", () => cancelJob(job.id));
    actions.append(button);
  });
  (job.available_actions || []).filter((action) => action.type === "retry_failed_inputs").forEach((action) => {
    const button = actionButton("只重试失败数据", "secondary", () =>
      retryFailedInputs(job.id, action.stage_id || "")
    );
    actions.append(button);
  });
  if (manifest?.result_ref) {
    actions.append(actionButton("下载结果 ZIP", "primary", () => downloadResult(manifest.result_ref)));
  }
  header.append(heading, actions);

  const windowsWaiting = windowsWaitState(job);
  const connectorPanel = windowsWaiting ? renderWindowsConnectionCallout(job, windowsWaiting) : null;
  if (windowsWaiting) state.connectorAwait = { jobId: job.id, mode: windowsWaiting.mode };

  const grid = document.createElement("div");
  grid.className = "detail-grid";
  const stagesSection = document.createElement("section");
  stagesSection.className = "detail-section";
  const stagesTitle = document.createElement("h3");
  stagesTitle.textContent = "执行阶段";
  const stages = document.createElement("div");
  stages.className = "stage-list";
  const visibleSteps = (job.business_steps || []).length ? job.business_steps : (job.stages || []);
  visibleSteps.forEach((stage) => stages.append(renderStage(job, stage)));
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
    ["雷达源", job.spec?.simulation?.source || "自动选择"],
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
    if (manifestStatus === "partial") failure.classList.add("is-partial");
    const failureTitle = document.createElement("h3");
    failureTitle.textContent = manifestStatus === "partial" ? "部分数据仿真完成" : "仿真失败原因";
    const failureSummary = document.createElement("p");
    const failed = Number(manifest?.summary?.failed_input_count ?? manifest?.summary?.failed_count ?? manifest?.summary?.fail_count ?? 0);
    const succeeded = Number(manifest?.summary?.succeeded_input_count ?? manifest?.summary?.success_count ?? 0);
    const total = Number(manifest?.summary?.total_input_count ?? manifest?.summary?.task_count ?? (failed + succeeded));
    failureSummary.textContent = total
      ? (manifestStatus === "partial" ? `${succeeded}/${total} 条成功，${failed} 条失败；成功结果仍可下载` : `${failed}/${total} 个数据任务失败`)
      : "仿真结果报告失败";
    const errors = document.createElement("ul");
    (manifest?.summary?.errors || []).slice(0, 5).forEach((message) => {
      const item = document.createElement("li");
      item.textContent = message;
      errors.append(item);
    });
    failure.append(failureTitle, failureSummary, errors);
  }

  const inputResults = document.createElement("section");
  inputResults.className = "detail-section input-results";
  const manifestInputs = Array.isArray(manifest?.input_results) ? manifest.input_results : [];
  if (manifestInputs.length) {
    const inputTitle = document.createElement("h3");
    inputTitle.textContent = "逐条数据结果";
    const inputList = document.createElement("div");
    inputList.className = "input-result-list";
    manifestInputs.forEach((result, index) => {
      const row = document.createElement("div");
      row.className = "input-result-row";
      const name = document.createElement("code");
      name.textContent = result.input_relative_path || result.relative_path || `数据 ${index + 1}`;
      const outcome = statusBadge(String(result.status || "unknown").toLowerCase());
      row.append(name, outcome);
      inputList.append(row);
    });
    inputResults.append(inputTitle, inputList);
  }

  const log = document.createElement("section");
  log.className = "event-log";
  log.setAttribute("aria-label", "任务事件");
  if (!events.length) log.textContent = "暂无新事件";
  if (events.length && Number(events[0].id || 0) > 1) {
    const notice = document.createElement("div");
    notice.className = "event-line event-history-notice";
    notice.textContent = `仅显示最近 ${events.length} 条事件；更早的编译日志已折叠`;
    log.append(notice);
  }
  events.forEach((event) => {
    const line = document.createElement("div");
    line.className = "event-line";
    const time = document.createElement("time");
    time.textContent = formatTime(event.created_at || event.timestamp);
    const text = document.createElement("span");
    text.textContent = friendlyEvent(event);
    line.append(time, text); log.append(line);
  });
  root.append(header);
  if (connectorPanel) root.append(connectorPanel);
  root.append(grid);
  if (failure.childElementCount) root.append(failure);
  if (inputResults.childElementCount) root.append(inputResults);
  root.append(log);
  root.scrollTop = previousRootTop;
  log.scrollTop = followedLogTail ? log.scrollHeight : Math.min(previousLogTop, log.scrollHeight);
}

function renderWindowsConnectionCallout(job, waiting) {
  const panel = document.createElement("section");
  panel.className = "windows-connect-callout";
  panel.setAttribute("role", "status");

  const copy = document.createElement("div");
  const eyebrow = document.createElement("span");
  eyebrow.className = "callout-eyebrow";
  eyebrow.textContent = waiting.reconnecting ? "自动恢复中" : "等待用户操作";
  const title = document.createElement("h3");
  title.textContent = waiting.title;
  const capability = document.createElement("strong");
  capability.textContent = waiting.capability;
  const reason = document.createElement("p");
  reason.textContent = waiting.reason;
  const reassurance = document.createElement("p");
  reassurance.className = "callout-reassurance";
  reassurance.textContent = waiting.updateRequired
    ? "无需卸载，也无需重新提交任务；运行一次更新程序即可。"
    : waiting.reconnecting
    ? "本机已经配置完成，无需重新安装或重新提交。连接恢复后，调度会自动继续。"
    : "任务没有失败，也不需要重新提交。连接成功后，调度会自动继续。";
  copy.append(eyebrow, title, capability, reason, reassurance);

  const controls = document.createElement("div");
  controls.className = "windows-connect-actions";
  const status = document.createElement("small");
  if (waiting.updateRequired) {
    status.textContent = "用户身份、路径绑定和任务配置会保留";
    const button = actionButton("一键更新本机组件", "primary", () =>
      downloadWindowsConnector(job.id, waiting.mode, button, status)
    );
    controls.append(button, status);
  } else if (waiting.reconnecting) {
    status.textContent = "通常会自动恢复；长时间未连接时可重新连接本机";
    const button = actionButton("重新连接本机", "secondary", () =>
      downloadWindowsConnector(job.id, waiting.mode, button, status)
    );
    controls.append(button, status);
  } else {
    status.textContent = "安装一次，后续自动连接";
    const button = actionButton("一键连接本机", "primary", () =>
      downloadWindowsConnector(job.id, waiting.mode, button, status)
    );
    controls.append(button, status);
  }
  panel.append(copy, controls);
  return panel;
}

async function downloadWindowsConnector(jobId, mode, button, status) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "正在准备";
  status.textContent = "正在生成与当前服务匹配的安装程序…";
  try {
    const headers = requestHeaders();
    const blob = await fetchBinary("/windows-connector/connect.cmd?mode=unified", { headers, timeoutMs: 60000 });
    triggerBlobDownload(blob, "RadarSim-连接本机.cmd");
    state.connectorAwait = { jobId, mode };
    button.textContent = "重新下载";
    status.textContent = "请双击运行已下载的文件；本页会自动检测连接并继续任务";
    showToast("连接程序已下载，双击运行后无需重新提交任务", 5000);
  } catch (error) {
    button.textContent = original;
    status.textContent = error instanceof ApiError && error.status === 404
      ? "一键连接包暂未就绪，请刷新页面后重试"
      : error.message || "连接程序准备失败，请稍后重试";
  } finally {
    button.disabled = false;
  }
}

async function downloadResult(resultRef) {
  const key = String(resultRef || "").trim();
  if (!key) {
    showToast("结果引用为空，暂时无法下载");
    return;
  }
  const inFlight = state.resultDownloadsInFlight.get(key);
  if (inFlight) {
    showToast("结果正在下载，请勿重复点击");
    return inFlight;
  }
  const download = (async () => {
  try {
    // ResultCatalog is owner-scoped. Binary fetches share the same identity
    // builder as JSON API and Connector downloads so a valid result can never
    // be looked up under the Linux service account by accident.
    const headers = requestHeaders();
    const blob = await fetchBinary(
      `/results/${encodeURIComponent(key)}/download`,
      { headers, timeoutMs: 10 * 60 * 1000 },
    );
    triggerBlobDownload(blob, "radar-sim-result.zip");
    showToast("结果 ZIP 已开始下载");
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      showToast("结果不存在、尚未登记或已过期；请先确认任务已完成");
    } else {
      showToast(error.message || "结果下载失败");
    }
  }
  })();
  state.resultDownloadsInFlight.set(key, download);
  try {
    return await download;
  } finally {
    state.resultDownloadsInFlight.delete(key);
  }
}

function renderStage(job, stage) {
  const row = document.createElement("div");
  row.className = "stage-row";
  row.append(statusBadge(stage.status));
  const copy = document.createElement("div");
  copy.className = "stage-copy";
  const title = document.createElement("strong");
  title.textContent = stage.label || stageName(stage.stage_type || stage.task_type || stage.id);
  const detail = document.createElement("small");
  const isBusinessStep = Boolean(stage.id && !stage.stage_id && !stage.task_id);
  const waiting = isBusinessStep ? windowsWaitState(job) : windowsWaitState(job, stage);
  detail.textContent = waiting
    ? `${waiting.reconnecting ? "本机正在自动重连" : "等待连接本机"}：${waiting.capability}`
    : (isBusinessStep ? `${Math.round((stage.progress || 0) * 100)}%` : friendlyStageDetail(stage));
  copy.append(title, detail);
  const actions = document.createElement("div");
  actions.className = "stage-actions";
  if (!isBusinessStep && ["failed", "cancelled"].includes(stage.status)) {
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
  state.importedSelection = null;
  state.validatedTarget = "";
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

async function retryFailedInputs(jobId, stageId) {
  try {
    const job = await api(`/jobs/${encodeURIComponent(jobId)}/retry-failed-inputs`, {
      method: "POST",
      json: { stage_id: stageId || "", input_paths: [] },
    });
    state.selectedJobId = job.id;
    showToast("失败数据已重新排队，成功数据不会重复运行");
    await loadJobs();
    await loadJobDetail(job.id, true);
  } catch (error) {
    showToast(error.message || "失败数据重试失败");
  }
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
    succeeded: "已完成", partial: "部分成功", failed: "失败", cancelled: "已取消",
    blocked: "已阻塞", skipped: "已跳过", cancel_requested: "取消中", cancelling: "取消中",
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
    shared_dataset_unavailable: "共享路径未授权，请改用受控直传或联系管理员配置共享空间",
    agent_data_upload_required: "等待已授权的 Windows Connector 将数据直传执行端",
    workspace_snapshot_pending: "等待本机 Connector 检查当前工作区",
  };
  if (byCode[stage.error?.code]) return byCode[stage.error.code];
  const byReason = {
    resolved_during_submission: "提交时已完成",
    current_workspace_verified_by_environment_check: "由环境检查阶段确认",
    not_needed: "当前执行路径不需要此阶段",
  };
  if (byReason[stage.skip_reason]) return byReason[stage.skip_reason];
  if (stage.error?.message) {
    const action = stage.error?.action || stage.error?.diagnostic?.action || "";
    return action ? `${stage.error.message}；建议：${action}` : stage.error.message;
  }
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
    "shared path is not under an authorized namespace": "共享路径未授权，需要受控直传或配置共享空间",
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
    updateConditionalFields(); invalidateValidatedTarget();
  }));
  byId("selenaSource").addEventListener("change", () => { updateConditionalFields(); invalidateValidatedTarget(); });
  byId("existingPath").addEventListener("input", updateRouteSummary);
  byId("selenaBranch").addEventListener("input", updateRouteSummary);
  byId("dataPath").addEventListener("input", () => {
    state.validatedTarget = "";
    updateRouteSummary();
  });
  byId("simulationForm").addEventListener("input", updateCreateWindowsCallout);
  byId("simulationForm").addEventListener("submit", submitCurrentSpec);
  byId("validateSpec").addEventListener("click", () => validateCurrentSpec().catch(() => {}));
  byId("importYaml").addEventListener("click", () => byId("yamlFile").click());
  byId("yamlFile").addEventListener("change", (event) => importYamlFile(event.target.files[0]));
  byId("exportYaml").addEventListener("click", exportYaml);
  byId("refreshJobs").addEventListener("click", loadJobs);
  byId("statusFilter").addEventListener("change", loadJobs);
  byId("saveToken").addEventListener("click", saveAccessToken);
  byId("saveIdentity").addEventListener("click", saveStableIdentity);
  byId("connectWindowsFromCreate").addEventListener("click", () => {
    const button = byId("connectWindowsFromCreate");
    const status = byId("createWindowsStatus");
    downloadWindowsConnector("", "unified", button, status);
  });
  byId("updateWindowsConnectorGlobal").addEventListener("click", () => {
    const button = byId("updateWindowsConnectorGlobal");
    const status = byId("connectorUpdateStatus");
    downloadWindowsConnector("", "unified", button, status);
  });
  byId("accessToken").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveAccessToken();
  });
  byId("userIdentity").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveStableIdentity();
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
      byId("apiState").textContent = health.ok ? "Linux 服务已连接" : "Linux 服务异常";
      byId("apiState").className = `api-state ${health.ok ? "ok" : "error"}`;
      if (health.ok) {
        if (state.userId) {
          await refreshCapabilities();
          if (state.legacyIdentity) {
            showStableIdentityEntry(
              "当前浏览器仍使用已停用的临时身份。请输入公司 NTID 后继续并更新 Connector；任务与电脑将永久绑定到该稳定身份。",
              { required: true },
            );
          }
        }
        else showStableIdentityEntry();
      }
    }
  } catch (error) {
    byId("apiState").textContent = "Linux 服务连接失败";
    byId("apiState").className = "api-state error";
    showToast(error.message, 5000);
  }
  switchView(state.view === "tasks" ? "tasks" : "create");
}

document.addEventListener("DOMContentLoaded", initialize);
