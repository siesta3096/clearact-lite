"use strict";

const capabilities = { editing: null, browserVisible: false, browserPolling: false, browserTimer: null, browserState: null, imageURL: null, browserBusy: false };

function renderAttachments() {
  const list = $("#attachment-list");
  list.replaceChildren();
  list.classList.toggle("hidden", !ui.attachments.length);
  for (const file of ui.attachments) {
    const pill = el("span", "attachment-pill");
    pill.title = file.path;
    pill.append(el("span", "", `▤ ${file.name} · ${Math.max(1, Math.ceil(file.bytes / 1024))} KB`));
    const remove = el("button", "attachment-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `移除附件 ${file.name}`);
    remove.addEventListener("click", () => { ui.attachments = ui.attachments.filter((f) => f.id !== file.id); renderAttachments(); setButtons(); });
    pill.append(remove);
    list.append(pill);
  }
}

async function uploadFiles(files) {
  if (!ui.state || running() || ui.uploading) { notice("请先停止或等待任务结束，再上传文件。"); return; }
  ui.uploading = true;
  setButtons();
  try {
    for (const file of files) {
      if (file.size > ui.state.upload_limit_bytes) throw new Error(`${file.name} 超过当前 ${ui.state.upload_limit_bytes / 1024 / 1024} MB 上传上限`);
      notice(`正在上传 ${file.name}…`);
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/uploads", { method: "POST", headers: { "X-ClearAct-Token": token }, body: form });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "上传未完成");
      ui.attachments.push(result.file);
      renderAttachments();
    }
    notice("附件已保存。描述任务后发送，模型会按需读取。");
  } catch (error) { showError(error); }
  finally { ui.uploading = false; setButtons(); $("#file-input").value = ""; }
}

$("#attach-files").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", (event) => uploadFiles([...event.target.files]));
$("#run-form").addEventListener("dragover", (event) => { if ([...event.dataTransfer.types].includes("Files")) { event.preventDefault(); $("#run-form").classList.add("dragging"); } });
$("#run-form").addEventListener("dragleave", () => $("#run-form").classList.remove("dragging"));
$("#run-form").addEventListener("drop", (event) => { event.preventDefault(); $("#run-form").classList.remove("dragging"); uploadFiles([...event.dataTransfer.files]); });
$("#goal").addEventListener("paste", (event) => { if (event.clipboardData.files.length) { event.preventDefault(); uploadFiles([...event.clipboardData.files]); } });

function mcpConfigsForSave() {
  const result = {};
  for (const server of ui.state.mcp_servers || []) {
    result[server.name] = Object.fromEntries(["enabled", "transport", "command", "args", "url", "connect_timeout", "call_timeout"].map((key) => [key, server[key]]));
    result[server.name].env = null;
    result[server.name].headers = null;
  }
  return result;
}

async function saveMCP(servers) {
  await api("/api/mcp", { servers });
  await refreshState();
  $("#mcp-feedback").textContent = "配置已保存在本机。可测试连接，或让模型按需调用。";
}

function renderMCPServers() {
  $("#mcp-count").textContent = (ui.state.mcp_servers || []).filter((s) => s.enabled).length;
  const list = $("#mcp-servers");
  list.replaceChildren();
  for (const server of ui.state.mcp_servers || []) {
    const row = el("div", "mcp-server");
    row.dataset.server = server.name;
    const copy = el("div", "mcp-server-copy");
    copy.append(el("b", "", server.name), el("small", "", `${server.transport} · ${!server.enabled ? "已停用" : server.connected ? `已连接 · ${server.tool_count} 个工具` : server.error || "尚未连接"}`));
    row.append(copy);
    const connect = el("button", "text-button config-action", server.connected ? "断开" : "连接");
    connect.type = "button";
    connect.addEventListener("click", async () => {
      connect.disabled = true;
      connect.textContent = "处理中…";
      try {
        await api(`/api/mcp/${encodeURIComponent(server.name)}/connect`, { disconnect: server.connected });
        await refreshState();
      } catch (error) { $("#mcp-feedback").textContent = error.message; renderMCPServers(); }
    });
    const edit = el("button", "text-button config-action", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => editMCP(server));
    const enabled = el("input");
    enabled.type = "checkbox";
    enabled.checked = server.enabled;
    enabled.className = "config-action mcp-enabled";
    enabled.setAttribute("aria-label", `启用 ${server.name}`);
    enabled.addEventListener("change", async () => {
      const configs = mcpConfigsForSave();
      configs[server.name].enabled = enabled.checked;
      try { await saveMCP(configs); } catch (error) { $("#mcp-feedback").textContent = error.message; }
    });
    const remove = el("button", "text-button config-action", "删除");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      const configs = mcpConfigsForSave();
      delete configs[server.name];
      try { await saveMCP(configs); } catch (error) { $("#mcp-feedback").textContent = error.message; }
    });
    row.append(enabled, connect, edit, remove);
    list.append(row);
  }
  if (!list.children.length) list.append(el("p", "empty-list", "还没有配置 MCP 服务。添加本地命令、远程地址或导入 JSON。"));
  setButtons();
}

function editMCP(server = null) {
  capabilities.editing = server?.name || null;
  const form = $("#mcp-editor");
  form.reset();
  form.classList.remove("hidden");
  form.elements.namedItem("name").value = server?.name || "";
  form.elements.namedItem("name").readOnly = Boolean(server);
  for (const key of ["transport", "command", "url", "connect_timeout", "call_timeout"]) {
    if (server) form.elements.namedItem(key).value = server[key];
  }
  form.elements.namedItem("args").value = JSON.stringify(server?.args || []);
  form.elements.namedItem("env").value = "";
  form.elements.namedItem("headers").value = "";
  $("#mcp-secret-hint").textContent = server ? `已保存的环境变量：${server.env_keys.join("、") || "无"}；请求头：${server.header_keys.join("、") || "无"}。留空保留，填写 {} 清空。` : "环境变量和请求头只保存在本机，不回显。填写 JSON 对象。";
  setButtons();
}

$("#mcp-trigger").addEventListener("click", () => { $("#sidebar").classList.remove("open"); $("#mcp-feedback").textContent = ""; renderMCPServers(); $("#mcp-dialog").showModal(); });
$("#mcp-add").addEventListener("click", () => editMCP());
$("#mcp-editor-cancel").addEventListener("click", () => { $("#mcp-editor").reset(); $("#mcp-editor").classList.add("hidden"); });
$("#mcp-editor").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const fields = Object.fromEntries(new FormData(event.currentTarget));
    const name = fields.name.trim();
    delete fields.name;
    fields.args = JSON.parse(fields.args || "[]");
    for (const key of ["env", "headers"]) fields[key] = fields[key].trim() ? JSON.parse(fields[key]) : null;
    for (const key of ["connect_timeout", "call_timeout"]) fields[key] = Number(fields[key]);
    fields.enabled = capabilities.editing ? ui.state.mcp_servers.find((s) => s.name === name).enabled : true;
    const configs = mcpConfigsForSave();
    configs[name] = fields;
    await saveMCP(configs);
    $("#mcp-editor").reset();
    $("#mcp-editor").classList.add("hidden");
  } catch (error) { $("#mcp-feedback").textContent = error.message; }
});
$("#mcp-import").addEventListener("click", async () => {
  try {
    const raw = JSON.parse($("#mcp-import-json").value);
    await saveMCP({ ...mcpConfigsForSave(), ...(raw.mcpServers || raw) });
    $("#mcp-import-json").value = "";
  } catch (error) { $("#mcp-feedback").textContent = error.message; }
});

function showBrowserPanel(open) {
  capabilities.browserVisible = open;
  $("#browser-panel").classList.toggle("hidden", !open);
  $("#browser-toggle").setAttribute("aria-expanded", String(open));
  clearTimeout(capabilities.browserTimer);
  if (open) {
    $("#inspector").classList.add("hidden");
    $("#inspector-toggle").setAttribute("aria-expanded", "false");
    refreshBrowser().catch(showError);
  }
}

async function refreshBrowser() {
  if (!capabilities.browserVisible || capabilities.browserPolling) return;
  capabilities.browserPolling = true;
  try {
    const data = await api("/api/browser/state");
    capabilities.browserState = data;
    $("#browser-status").textContent = data.ready ? data.title || data.url || "空白页面" : "会话未启动";
    $("#browser-empty").classList.toggle("hidden", data.ready && Boolean(capabilities.imageURL));
    $("#browser-image").classList.toggle("hidden", !data.ready || !capabilities.imageURL);
    if (document.activeElement !== $("#browser-url")) $("#browser-url").value = data.url || "";
    if (data.ready) {
      const response = await fetch("/api/browser/image");
      if (response.ok) {
        const url = URL.createObjectURL(await response.blob());
        const decoded = new Image();
        decoded.src = url;
        try { await decoded.decode(); }
        catch (error) { URL.revokeObjectURL(url); throw error; }
        const old = capabilities.imageURL;
        capabilities.imageURL = url;
        $("#browser-image").src = url;
        $("#browser-image").classList.remove("hidden");
        $("#browser-empty").classList.add("hidden");
        if (old) URL.revokeObjectURL(old);
      }
    } else if (capabilities.imageURL) {
      $("#browser-image").removeAttribute("src");
      URL.revokeObjectURL(capabilities.imageURL);
      capabilities.imageURL = null;
    }
  } catch (error) { $("#browser-feedback").textContent = error.message; }
  finally {
    capabilities.browserPolling = false;
    if (capabilities.browserVisible) capabilities.browserTimer = setTimeout(() => refreshBrowser().catch(showError), 1000);
  }
}

async function browserManual(arguments_) {
  if (capabilities.browserBusy) return;
  capabilities.browserBusy = true;
  $("#browser-feedback").textContent = "操作中…";
  try {
    const data = await api("/api/browser/action", { arguments: arguments_, branch_id: ui.branch });
    capabilities.browserState = data;
    $("#browser-feedback").textContent = "";
    await refreshBrowser();
    if (ui.branch) scheduleReport();
  } catch (error) { $("#browser-feedback").textContent = error.message; }
  finally { capabilities.browserBusy = false; }
}

$("#browser-toggle").addEventListener("click", () => showBrowserPanel(!capabilities.browserVisible));
$("#browser-panel-close").addEventListener("click", () => showBrowserPanel(false));
$("#browser-address-form").addEventListener("submit", (event) => { event.preventDefault(); browserManual({ action: "navigate", url: $("#browser-url").value.trim() }); });
$("#browser-back").addEventListener("click", () => browserManual({ action: "back" }));
$("#browser-reload").addEventListener("click", () => browserManual({ action: "reload" }));
$("#browser-close-session").addEventListener("click", () => browserManual({ action: "close" }));
$("#browser-type-form").addEventListener("submit", async (event) => { event.preventDefault(); await browserManual({ action: "type", value: $("#browser-text").value }); $("#browser-text").value = ""; });
$("#browser-image").addEventListener("click", (event) => {
  const rect = event.currentTarget.getBoundingClientRect();
  const viewport = capabilities.browserState?.viewport;
  if (viewport) browserManual({ action: "click", x: (event.clientX - rect.left) / rect.width * viewport.width, y: (event.clientY - rect.top) / rect.height * viewport.height });
  $("#browser-screen").focus();
});
$("#browser-screen").addEventListener("keydown", (event) => {
  if (!capabilities.browserState?.ready || event.isComposing || ["Control", "Shift", "Alt", "Meta"].includes(event.key)) return;
  event.preventDefault();
  if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) browserManual({ action: "type", value: event.key });
  else {
    const modifiers = [event.ctrlKey && "Control", event.metaKey && "Meta", event.altKey && "Alt", event.shiftKey && "Shift"].filter(Boolean);
    browserManual({ action: "press", value: [...modifiers, event.key === " " ? "Space" : event.key].join("+") });
  }
});
$("#browser-screen").addEventListener("wheel", (event) => { if (capabilities.browserState?.ready) { event.preventDefault(); browserManual({ action: "scroll", x: event.deltaX, y: event.deltaY }); } }, { passive: false });
if (ui.state) renderMCPServers();
