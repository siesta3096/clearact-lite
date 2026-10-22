"use strict";

const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="clearact-token"]').content;
const ui = { state: null, branch: null, report: null, stream: null, refresh: null, busy: false, attachments: [], uploading: false };
const statuses = { ready: "待开始", finished: "本轮已结束", stopped: "已停止", failed: "运行失败", interrupted: "记录中断", context_limit: "上下文已达上限", output_limit: "输出已达上限" };
const toolNames = { file_read: "读取文件", file_write: "写入文件", file_list: "浏览文件夹", document_read: "读取文档", history_read: "读取历史", artifact_register: "登记交付", browser_open: "打开浏览器", browser_read: "读取网页", browser_action: "操作浏览器", mcp_list_tools: "发现 MCP 工具", mcp_call_tool: "调用 MCP 工具" };
Object.assign(toolNames, { file_edit: "分段编辑文件", task_plan: "保存文件计划", agent_task: "运行子代理", task_checkpoint: "保存工作断点" });

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

async function api(path, body) {
  const options = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-ClearAct-Token": token }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.detail || `请求失败：${response.status}`);
  return data;
}

function notice(message = "") {
  $("#notice").textContent = message;
  $("#notice").classList.toggle("hidden", !message);
}

function running() { return Boolean(ui.state?.active_branch); }
function selectedRunning() { return running() && ui.state.active_branch === ui.branch; }
function showStage(event) {
  const labels = { "run.started": "正在准备任务", "model.started": "模型正在处理", "tool.started": "正在执行：" + (toolNames[event.payload.name] || event.payload.name), "file.written": "文件已写入，快照已保存", "run.ended": statuses[event.payload.status] || "本轮结束" };
  Object.assign(labels, { "context.compacted": "上下文已压缩，完整历史保留", "capacity.continuation": "已保存容量断点，继续请求模型", "agent.started": `子代理正在执行：${event.payload.task_id || ""}`, "agent.finished": "子代理已返回，主代理继续整合" });
  if (labels[event.kind]) $("#stage-label").textContent = labels[event.kind];
  if (event.kind === "agent.progress") $("#stage-label").textContent = `子代理 ${event.payload.task_id}：${toolNames[event.payload.name] || event.payload.kind}`;
}
function timeLabel(value) {
  return value ? new Date(value).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "新话题";
}

function setButtons() {
  const active = running();
  $("#send").disabled = active || ui.busy || ui.uploading || !ui.state;
  $("#goal").disabled = active;
  for (const id of ["settings-trigger", "workspace-trigger", "choose-workspace", "fork-topic"]) {
    $("#" + id).disabled = active || ui.uploading || !ui.state || (id === "fork-topic" && !ui.branch);
  }
  $("#export-history").disabled = !ui.branch;
  $("#stage").classList.toggle("hidden", !active);
  if (active && !selectedRunning()) $("#stage-label").textContent = "另一个话题正在运行";
  for (const button of document.querySelectorAll(".branch-button,.restore-button")) button.disabled = active;
  $("#attach-files").disabled = active || ui.uploading || !ui.state;
  $("#new-topic").disabled = !ui.state;
  $("#browser-toggle").disabled = !ui.state;
  $("#mcp-trigger").disabled = !ui.state;
  for (const button of document.querySelectorAll(".config-action,.attachment-remove")) button.disabled = active || ui.uploading;
}

function renderTopics() {
  const list = $("#topics");
  list.replaceChildren();
  const branches = ui.state.branches;
  $("#topic-count").textContent = branches.length;
  if (!branches.length) list.append(el("p", "empty-list", "你的工作，从这里开始。"));
  for (const branch of branches) {
    const button = el("button", "topic" + (branch.id === ui.branch ? " active" : ""));
    button.type = "button";
    button.dataset.branchId = branch.id;
    button.append(el("b", "", (branch.parent_id ? "↳ " : "") + branch.title));
    const status = branch.id === ui.state.active_branch ? "运行中" : (statuses[branch.status] || branch.status);
    button.append(el("small", "", `${status} · ${timeLabel(branch.updated_at)}`));
    button.addEventListener("click", () => selectBranch(branch.id).catch(showError));
    list.append(button);
  }
}

async function refreshState() {
  const previousWorkspace = ui.state?.settings.workspace;
  ui.state = await api("/api/state");
  if (previousWorkspace && previousWorkspace !== ui.state.settings.workspace) {
    ui.branch = null;
    ui.attachments = [];
    if (typeof renderAttachments === "function") renderAttachments();
  }
  if (ui.branch && !ui.state.branches.some((b) => b.id === ui.branch)) ui.branch = null;
  const settings = ui.state.settings;
  $("#workspace-short").textContent = settings.workspace.split(/[\\/]/).filter(Boolean).pop() || settings.workspace;
  $("#workspace-trigger").title = settings.workspace;
  $("#model-caption").textContent = settings.model;
  renderTopics();
  setButtons();
  if (ui.state.last_error) notice(ui.state.last_error);
  if (typeof renderMCPServers === "function") renderMCPServers();
}

function renderText(container, text) {
  const blocks = text.split("```");
  blocks.forEach((block, index) => {
    if (index % 2) {
      const code = block.includes("\n") ? block.slice(block.indexOf("\n") + 1) : block;
      container.append(el("pre", "", code));
    } else container.append(document.createTextNode(block));
  });
}

async function forkAt(eventId = null) {
  if (!ui.branch) return;
  const data = await api(`/api/branches/${ui.branch}/fork`, { at_event: eventId });
  await refreshState();
  await selectBranch(data.branch_id);
  notice("已创建对话分支，原话题保留。工作区文件保持当前内容。");
}

function renderMessages(report) {
  const target = $("#messages");
  target.replaceChildren();
  let attached = null;
  for (const event of report.events) {
    if (event.kind === "attachment.linked") { attached = event.payload; continue; }
    const message = event.payload;
    if (event.kind !== "message" || !["user", "assistant"].includes(message.role)) continue;
    const article = el("article", "message " + message.role);
    article.append(el("div", "avatar", message.role === "user" ? "你" : "✦"));
    const body = el("div", "message-body");
    body.append(el("div", "message-label", message.role === "user" ? "你" : "ClearAct"));
    const displayed = message.role === "user" && attached ? attached.prompt : message.content;
    if (displayed) {
      const text = el("div", "message-text");
      renderText(text, displayed);
      body.append(text);
    }
    if (message.role === "user" && attached) {
      const files = el("div", "message-attachments");
      for (const file of attached.files) files.append(el("span", "attachment-pill", `▤ ${file.name}`));
      body.append(files);
      attached = null;
    }
    if (message.tool_calls?.length) {
      const details = el("details", "message-tools");
      details.append(el("summary", "", "使用工具：" + message.tool_calls.map((c) => toolNames[c.name] || c.name).join("、")));
      details.append(el("pre", "", JSON.stringify(message.tool_calls, null, 2)));
      body.append(details);
    } else {
      const actions = el("div", "message-actions");
      const branch = el("button", "text-button branch-button", "从这里创建分支");
      branch.type = "button";
      branch.addEventListener("click", () => forkAt(event.id).catch(showError));
      actions.append(branch);
      body.append(actions);
    }
    article.append(body);
    target.append(article);
  }
  const latest = report.runs.at(-1);
  if (latest && latest.status !== "finished") {
    const start = report.events.findLast((e) => e.kind === "run.started")?.id || 0;
    const partial = report.events.findLast((e) => e.kind === "model.partial" && e.id > start);
    if (partial) {
      const article = el("article", "message assistant");
      article.append(el("div", "avatar", "✦"));
      const body = el("div", "message-body");
      body.append(el("div", "message-label", "未完成的输出"), el("div", "message-text", partial.payload.content));
      article.append(body);
      target.append(article);
    }
  }
}

function renderSummary(report) {
  const summary = $("#run-summary");
  summary.replaceChildren();
  const run = report.runs.at(-1);
  summary.classList.toggle("hidden", !run);
  if (!run) return;
  const items = [statuses[run.status] || run.status, `${run.model_calls} 次模型调用`, `${run.elapsed_seconds} 秒`, `输入 ${run.usage.input_tokens ?? "未知"} / 输出 ${run.usage.output_tokens ?? "未知"} token`];
  items.forEach((item) => summary.append(el("span", "", item)));
  if (run.error) summary.append(el("span", "", run.error));
}

async function saveDownload(path, filename) {
  const response = await fetch(path);
  if (!response.ok) {
    const data = await response.json();
    throw new Error(data.error || "下载失败");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = el("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function renderDeliveries(report) {
  const list = $("#deliveries");
  list.replaceChildren();
  const artifacts = new Map();
  for (const e of report.events) if (e.kind === "artifact.registered") artifacts.set(e.payload.path, e);
  if (!artifacts.size) return;
  list.append(el("p", "delivery-caption", "交付文件 · 登记表示文件存在，内容仍以实际核验为准"));
  for (const event of artifacts.values()) {
    const data = event.payload;
    const card = el("div", "delivery");
    card.append(el("div", "delivery-icon", "▤"));
    const copy = el("div");
    copy.append(el("b", "", data.path), el("small", "", `${data.bytes} 字节 · SHA256 ${data.sha256.slice(0, 10)}…`));
    const download = el("button", "text-button", "下载 ↓");
    download.type = "button";
    download.addEventListener("click", () => saveDownload(`/api/branches/${ui.branch}/files/${event.id}`, data.path.split(/[\\/]/).pop()).catch(showError));
    card.append(copy, download);
    list.append(card);
  }
}

function renderRecords(report) {
  const actions = $("#action-records");
  const changes = $("#change-records");
  const opened = new Set([...document.querySelectorAll(".record[open]")].map((node) => node.dataset.eventId));
  actions.replaceChildren();
  changes.replaceChildren();
  const labels = { "run.started": "开始运行", "run.ended": "本轮结果", "model.started": "请求模型", "model.returned": "模型响应与用量", "context.projected": "上下文整理", "file.written": "文件已写入", "file.restored": "文件已恢复", "tool.started": "工具开始", "tool.returned": "工具返回", "tool.exception": "工具异常", "attachment.linked": "本轮附件", "browser.manual": "用户操作浏览器", "browser.action": "模型操作浏览器" };
  Object.assign(labels, { "context.compacted": "上下文已压缩", "capacity.continuation": "容量断点续跑", "task.plan": "文件与任务计划", "task.checkpoint": "工作断点", "agent.started": "子代理开始", "agent.finished": "子代理结果" });
  for (const event of [...report.events, ...(report.child_changes || [])].sort((a,b) => a.id-b.id)) {
    if (!(event.kind in labels)) continue;
    const detail = el("details", "record");
    detail.dataset.eventId = String(event.id);
    detail.open = opened.has(String(event.id));
    const summary = el("summary", "", `${labels[event.kind]}${event.payload.name ? " · " + (toolNames[event.payload.name] || event.payload.name) : ""}`);
    summary.append(el("small", "", `#${event.id} · ${timeLabel(event.created_at)}`));
    detail.append(summary);
    const data = { ...event.payload };
    // Raw responses remain downloadable; keep the inspector readable.
    if (data.raw_response) data.raw_response = "完整原始响应保存在下载记录中";
    detail.append(el("pre", "", event.kind === "file.written" ? event.payload.diff || "内容未发生变化" : JSON.stringify(data, null, 2)));
    if (event.kind === "file.written") {
      const restore = el("button", "text-button restore-button", "恢复到这次修改之前");
      restore.type = "button";
      restore.dataset.changeEvent = String(event.id);
      restore.addEventListener("click", async () => {
        try {
          await api(`/api/branches/${event.branch_id}/restore`, { event_id: event.id });
          await loadReport();
          notice("已恢复文件，修改前后的历史记录均已保留。");
        } catch (error) { showError(error); }
      });
      detail.append(restore);
      changes.append(detail);
    } else if (event.kind === "file.restored") changes.append(detail);
    else actions.append(detail);
    if (["agent.started", "agent.finished"].includes(event.kind)) {
      const download = el("button", "text-button", "下载子代理完整记录");
      download.type = "button";
      download.addEventListener("click", () => saveDownload(`/api/branches/${event.payload.branch_id}`, `child-${event.payload.task_id}.json`).catch(showError));
      detail.append(download);
    }
  }
  if (!actions.children.length) actions.append(el("p", "empty-list", "运行后，这里会显示真实执行记录。"));
  if (!changes.children.length) changes.append(el("p", "empty-list", "文件写入后的修改预览会显示在这里。"));
}

async function loadReport() {
  const selected = ui.branch;
  if (!selected) return;
  const report = await api(`/api/branches/${selected}`);
  if (selected !== ui.branch) return;
  const area = $(".content-area");
  const follow = area.scrollHeight - area.scrollTop - area.clientHeight < 140;
  ui.report = report;
  $("#welcome").classList.add("hidden");
  $("#conversation").classList.remove("hidden");
  renderMessages(report);
  renderSummary(report);
  renderDeliveries(report);
  renderRecords(report);
  if (!selectedRunning()) $("#live-message").classList.add("hidden");
  else {
    const latestStage = report.events.findLast((e) => ["run.started", "model.started", "tool.started", "file.written"].includes(e.kind));
    if (latestStage) showStage(latestStage);
  }
  setButtons();
  if (follow) area.scrollTop = area.scrollHeight;
}

async function selectBranch(branch) {
  ui.branch = branch;
  ui.report = null;
  $("#live-output").textContent = "";
  $("#live-message").classList.add("hidden");
  $("#sidebar").classList.remove("open");
  notice();
  const info = ui.state.branches.find((b) => b.id === branch);
  $("#topic-title").textContent = info?.title || "新话题";
  $("#topic-subtitle").textContent = info?.parent_id ? "历史分支 · 工作文件使用当前内容" : "所有消息与工具动作保存在本机";
  renderTopics();
  await loadReport();
  $(".content-area").scrollTop = $(".content-area").scrollHeight;
}

function newTopic() {
  ui.branch = null;
  ui.report = null;
  $("#welcome").classList.remove("hidden");
  $("#conversation").classList.add("hidden");
  $("#topic-title").textContent = "新建话题";
  $("#topic-subtitle").textContent = "把想法变成看得见的成果";
  $("#sidebar").classList.remove("open");
  renderTopics();
  notice();
  setButtons();
  $("#goal").focus();
}

function openInspector(open) {
  if (open && typeof showBrowserPanel === "function") showBrowserPanel(false);
  $("#inspector").classList.toggle("hidden", !open);
  $("#inspector-toggle").setAttribute("aria-expanded", String(open));
}

function scheduleReport() {
  if (!ui.refresh) ui.refresh = setTimeout(() => { ui.refresh = null; loadReport().catch(showError); }, 180);
}

function connectStream() {
  ui.stream?.close();
  ui.stream = new EventSource(`/api/stream?after=${ui.state.sequence}`);
  ui.stream.onopen = () => { $("#connection").classList.remove("offline"); $("#connection span").textContent = "本地网关已连接"; };
  ui.stream.onerror = () => { $("#connection").classList.add("offline"); $("#connection span").textContent = "连接中断，正在重连"; };
  ui.stream.onmessage = (message) => {
    const frame = JSON.parse(message.data);
    if (frame.kind === "state") {
      refreshState().then(() => { if (ui.branch) return loadReport(); }).catch(showError);
    } else if (frame.kind === "error") notice(frame.payload.message);
    else if (frame.kind === "browser" && typeof refreshBrowser === "function") refreshBrowser().catch(showError);
    else if (frame.kind === "delta" && frame.payload.branch_id === ui.branch) {
      $("#live-message").classList.remove("hidden");
      $("#live-output").append(document.createTextNode(frame.payload.text));
      const area = $(".content-area");
      if (area.scrollHeight - area.scrollTop - area.clientHeight < 180) area.scrollTop = area.scrollHeight;
    } else if (frame.kind === "event") {
      const event = frame.payload;
      if (event.branch_id !== ui.branch) return;
      showStage(event);
      if (event.kind === "model.started" || (event.kind === "message" && event.payload.role === "assistant")) {
        $("#live-output").textContent = "";
        $("#live-message").classList.add("hidden");
      }
      scheduleReport();
    }
  };
}

function openSettings() {
  $("#sidebar").classList.remove("open");
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(ui.state.settings)) {
    if (form.elements.namedItem(key)) form.elements.namedItem(key).value = typeof value === "boolean" ? String(value) : value ?? "";
  }
  form.elements.namedItem("api_key").value = "";
  $("#key-state").textContent = ui.state.settings.api_key_set ? "已保存密钥。留空可保留；密钥不会回显。" : "尚未保存密钥；服务不需要鉴权时可留空。";
  $("#probe-status").textContent = "";
  $("#settings-feedback").textContent = "";
  $("#settings-dialog").showModal();
}

function settingsPayload() {
  const { api_key_set, ...settings } = ui.state.settings;
  return { ...settings, api_key: null };
}

function openWorkspace() {
  $("#workspace-input").value = ui.state.settings.workspace;
  $("#workspace-feedback").textContent = "";
  $("#workspace-dialog").showModal();
}

function showError(error) { notice(error.message || String(error)); }

$("#run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = $("#goal").value.trim();
  if (!prompt || running() || ui.busy || ui.uploading) return;
  ui.busy = true;
  notice();
  setButtons();
  try {
    const data = await api("/api/runs", { prompt, branch_id: ui.branch, attachment_ids: ui.attachments.map((f) => f.id) });
    ui.attachments = [];
    if (typeof renderAttachments === "function") renderAttachments();
    $("#goal").value = "";
    $("#goal").style.height = "auto";
    await refreshState();
    await selectBranch(data.branch_id);
  } catch (error) { showError(error); }
  finally { ui.busy = false; setButtons(); }
});

$("#goal").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#run-form").requestSubmit(); }
});
$("#goal").addEventListener("input", () => { $("#goal").style.height = "auto"; $("#goal").style.height = Math.min($("#goal").scrollHeight, 160) + "px"; });
$("#new-topic").addEventListener("click", newTopic);
$("#stop-run").addEventListener("click", () => api("/api/stop", {}).then(refreshState).then(loadReport).catch(showError));
$("#settings-trigger").addEventListener("click", openSettings);
$("#workspace-trigger").addEventListener("click", openWorkspace);
$("#choose-workspace").addEventListener("click", openWorkspace);
$("#example-task").addEventListener("click", () => { $("#goal").value = "创建 hello.txt，内容为 Hello ClearAct。读取核对后登记交付这个文件。"; $("#goal").focus(); });
$("#menu-toggle").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#inspector-toggle").addEventListener("click", () => openInspector($("#inspector").classList.contains("hidden")));
$("#inspector-close").addEventListener("click", () => openInspector(false));
$("#fork-topic").addEventListener("click", () => forkAt().catch(showError));
$("#export-history").addEventListener("click", () => saveDownload(`/api/branches/${ui.branch}`, `clearact-${ui.branch}.json`).catch(showError));
for (const kind of ["actions", "changes"]) $("#tab-" + kind).addEventListener("click", () => {
  for (const name of ["actions", "changes"]) {
    const active = name === kind;
    $("#tab-" + name).classList.toggle("active", active);
    $("#tab-" + name).setAttribute("aria-selected", String(active));
    $("#" + (name === "actions" ? "action" : "change") + "-records").classList.toggle("hidden", !active);
  }
});
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => $("#" + button.dataset.close).close());
$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = settingsPayload();
  for (const [key, value] of new FormData(event.currentTarget)) payload[key] = value;
  for (const key of ["context_tokens", "max_output_tokens", "temperature", "request_timeout_seconds", "run_timeout_seconds", "compact_threshold", "compact_target"]) payload[key] = Number(payload[key]);
  for (const key of ["auto_compact", "auto_continue"]) payload[key] = payload[key] === "true";
  payload.reasoning_effort = payload.reasoning_effort || null;
  try {
    await api("/api/settings", payload);
    await refreshState();
    $("#settings-dialog").close();
    notice("模型设置已保存在本机，下次任务会使用新配置。");
  } catch (error) { $("#settings-feedback").textContent = error.message; }
});
$("#workspace-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/settings", { ...settingsPayload(), workspace: $("#workspace-input").value.trim() });
    await refreshState();
    newTopic();
    $("#workspace-dialog").close();
  } catch (error) { $("#workspace-feedback").textContent = error.message; }
});
$("#probe-model").addEventListener("click", async () => {
  $("#probe-model").disabled = true;
  $("#probe-status").textContent = "正在检测已保存的接口…";
  try {
    const data = await api("/api/model/probe", {});
    $("#probe-status").textContent = data.message;
    $("#model-options").replaceChildren(...data.models.map((model) => { const option = el("option"); option.value = model; return option; }));
  } catch (error) { $("#probe-status").textContent = error.message; }
  finally { $("#probe-model").disabled = false; }
});
$("#clear-key").addEventListener("click", async () => {
  try {
    await api("/api/settings", { ...settingsPayload(), clear_api_key: true });
    await refreshState();
    $("#key-state").textContent = "已清除本机保存的密钥。";
    $("#settings-form").elements.namedItem("api_key").value = "";
  } catch (error) { $("#settings-feedback").textContent = error.message; }
});

function setTheme(dark) {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("#theme-toggle span:last-child").textContent = dark ? "切换浅色外观" : "切换深色外观";
  try { localStorage.setItem("clearact-theme", dark ? "dark" : "light"); } catch { /* Private contexts may disable storage. */ }
}
$("#theme-toggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme !== "dark"));
document.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); if (ui.state) newTopic(); } });
try { setTheme(localStorage.getItem("clearact-theme") === "dark"); } catch { setTheme(false); }
setButtons();
refreshState().then(async () => {
  const initial = ui.state.active_branch || ui.state.branches[0]?.id;
  if (initial) await selectBranch(initial);
  connectStream();
}).catch((error) => { showError(error); $("#connection").classList.add("offline"); $("#connection span").textContent = "网关未连接"; });
