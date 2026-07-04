/* app.js
 * ========
 * Single-page dashboard. No build step, no framework - plain DOM + fetch +
 * socket.io, matching the rest of this stack's "no unnecessary moving
 * parts" philosophy. The browser only ever talks to our Flask backend
 * (REST + WebSocket) - never to Redfish/BMCs directly.
 */

const CATEGORY_META = {
  battery:   { icon: "fa-car-battery",        label: "Battery" },
  chassis:   { icon: "fa-cube",               label: "Chassis" },
  fans:      { icon: "fa-fan",                label: "Fans" },
  memory:    { icon: "fa-memory",             label: "Memory" },
  processor: { icon: "fa-microchip",          label: "Processor" },
  storage:   { icon: "fa-database",           label: "Storage" },
  power:     { icon: "fa-plug",               label: "Power" },
  thermal:   { icon: "fa-temperature-half",   label: "Thermal" },
  voltage:   { icon: "fa-bolt",               label: "Voltage" },
  network:   { icon: "fa-network-wired",      label: "Network" },
  pcie:      { icon: "fa-layer-group",        label: "PCI / Cables" },
  firmware:  { icon: "fa-code-branch",        label: "Firmware" },
  security:  { icon: "fa-shield-halved",      label: "Security" },
};
const CATEGORY_ORDER = Object.keys(CATEGORY_META);

const HISTORY_METRICS = {
  thermal:   [["temperature", "Temperature (C)"]],
  power:     [["power_consumption", "Power (W)"], ["psu_wattage", "PSU Output (W)"]],
  fans:      [["fan_rpm", "Fan RPM"]],
  voltage:   [["voltage", "Voltage (V)"]],
  memory:    [["memory_errors", "Memory Errors"], ["memory_temperature", "DIMM Temp (C)"]],
  storage:   [["disk_wear", "Disk Wear (%)"], ["disk_temperature", "Disk Temp (C)"]],
  processor: [["cpu_temperature", "CPU Temp (C)"]],
};

let state = {
  servers: [],
  selectedServerId: null,
  openCards: new Set(["chassis"]),   // categories expanded by default
  alerts: [],
  charts: {},                        // category -> Chart.js instance
};

const $ = (sel) => document.querySelector(sel);
const $all = (sel) => Array.from(document.querySelectorAll(sel));

// ---------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------
async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    let msg = resp.statusText;
    try { const body = await resp.json(); if (body.error) msg = body.error; } catch (_) {}
    throw new Error(msg);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

// ---------------------------------------------------------------------
// Toasts
// ---------------------------------------------------------------------
function toast(message) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = message;
  $("#toastContainer").appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ---------------------------------------------------------------------
// Health / status pill helpers
// ---------------------------------------------------------------------
function healthClass(h) {
  if (h === "OK") return "pill-ok";
  if (h === "Warning") return "pill-warn";
  if (h === "Critical") return "pill-crit";
  return "pill-unknown";
}
function healthDotColor(h) {
  if (h === "OK") return "var(--ok)";
  if (h === "Warning") return "var(--warn)";
  if (h === "Critical") return "var(--crit)";
  return "var(--unknown)";
}
function connDotColor(status) {
  if (status === "connected") return "var(--ok)";
  if (status === "auth_failed" || status === "unreachable") return "var(--crit)";
  return "var(--unknown)";
}

// ---------------------------------------------------------------------
// Sidebar: server list
// ---------------------------------------------------------------------
async function loadServers() {
  state.servers = await api("/api/servers");
  renderServerList();
}

function renderServerList() {
  const container = $("#serverList");
  const filter = ($("#serverSearch").value || "").toLowerCase();
  const filtered = state.servers.filter((s) =>
    (s.hostname + s.ip_address + (s.display_name || "")).toLowerCase().includes(filter)
  );

  if (filtered.length === 0) {
    container.innerHTML = `<div class="sidebar-empty">No servers ${state.servers.length ? "match your search" : "yet. Click + to add one."}</div>`;
    return;
  }

  container.innerHTML = "";
  for (const s of filtered) {
    const row = document.createElement("div");
    row.className = "server-row" + (s.id === state.selectedServerId ? " active" : "");
    row.innerHTML = `
      <span class="health-dot" style="background:${healthDotColor(s.health_status)}"></span>
      <div class="server-meta">
        <div class="server-name">${escapeHtml(s.display_name || s.hostname)}</div>
        <div class="server-ip">${escapeHtml(s.ip_address)}</div>
      </div>
      <div style="display:flex; flex-direction:column; align-items:flex-end; gap:4px;">
        <span class="conn-indicator" style="background:${connDotColor(s.connection_status)}" title="${s.connection_status}"></span>
        <i class="fa-solid ${s.power_state === 'On' ? 'fa-power-off' : 'fa-circle-stop'} power-icon" title="${s.power_state}"></i>
      </div>
    `;
    row.addEventListener("click", () => selectServer(s.id));
    container.appendChild(row);
  }
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------------
// Server selection + header + cards
// ---------------------------------------------------------------------
async function selectServer(serverId) {
  if (state.selectedServerId) {
    socket.emit("unsubscribe_server", { server_id: state.selectedServerId });
  }
  state.selectedServerId = serverId;
  socket.emit("subscribe_server", { server_id: serverId });
  renderServerList();
  await renderMain();
}

async function renderMain() {
  const main = $("#main");
  const server = await api(`/api/servers/${state.selectedServerId}`);
  const componentsByCategory = await api(`/api/servers/${state.selectedServerId}/components`);

  main.innerHTML = `
    <div class="server-header" id="serverHeader"></div>
    <div class="cards-grid" id="cardsGrid"></div>
  `;
  renderHeader(server);
  renderCards(componentsByCategory);
}

function renderHeader(server) {
  const header = $("#serverHeader");
  const lastUpdated = server.last_successful_poll ? new Date(server.last_successful_poll).toLocaleString() : "never";
  header.innerHTML = `
    <div>
      <h1>${escapeHtml(server.display_name || server.hostname)}</h1>
      <div class="sub">${escapeHtml(server.ip_address)} &middot; ${escapeHtml(server.vendor || "unknown vendor")} ${escapeHtml(server.model || "")}</div>
    </div>
    <span class="pill ${healthClass(server.health_status)}"><span class="dot" style="background:${healthDotColor(server.health_status)}"></span>${server.health_status || "Unknown"}</span>
    <span class="pill ${server.power_state === 'On' ? 'pill-ok' : 'pill-unknown'}"><i class="fa-solid fa-power-off"></i> ${server.power_state || "Unknown"}</span>
    <span class="pill ${server.connection_status === 'connected' ? 'pill-ok' : 'pill-crit'}"><span class="dot" style="background:${connDotColor(server.connection_status)}"></span>${server.connection_status || "unknown"}</span>
    <div class="header-stats">
      <div class="stat"><div class="label">Firmware</div><div class="value">${escapeHtml(server.firmware_version || "-")}</div></div>
      <div class="stat"><div class="label">Service Tag</div><div class="value">${escapeHtml(server.service_tag || "-")}</div></div>
      <div class="stat"><div class="label">Last Updated</div><div class="value">${lastUpdated}</div></div>
      <div class="stat"><button class="btn-secondary" id="pollNowBtn"><i class="fa-solid fa-rotate"></i> Poll now</button></div>
    </div>
  `;
  $("#pollNowBtn").addEventListener("click", async () => {
    await api(`/api/servers/${state.selectedServerId}/poll-now`, { method: "POST" });
    toast("Poll queued");
  });
}

function renderCards(componentsByCategory) {
  const grid = $("#cardsGrid");
  grid.innerHTML = "";
  for (const category of CATEGORY_ORDER) {
    let comps = componentsByCategory[category] || [];
    if (category === "storage") {
      comps = comps.concat(
        componentsByCategory["storage_controller"] || [],
        componentsByCategory["storage_drive"] || [],
        componentsByCategory["storage_volume"] || []
      );
    }
    grid.appendChild(buildCategoryCard(category, comps));
  }
  // logs card gets its own custom body
  grid.appendChild(buildLogsCard());
}

function worstHealth(components) {
  const order = { OK: 0, Warning: 1, Critical: 2 };
  let worst = null;
  for (const c of components) {
    if (c.health && (worst === null || order[c.health] > order[worst])) worst = c.health;
  }
  return worst;
}

function buildCategoryCard(category, components) {
  const meta = CATEGORY_META[category];
  const isOpen = state.openCards.has(category);
  const card = document.createElement("div");
  card.className = "card" + (isOpen ? " open" : "");
  card.dataset.category = category;

  const worst = worstHealth(components);
  card.innerHTML = `
    <div class="card-header">
      <i class="fa-solid ${meta.icon} icon"></i>
      <span class="title">${meta.label}</span>
      ${worst ? `<span class="dot" style="width:8px;height:8px;border-radius:50%;background:${healthDotColor(worst)}"></span>` : ""}
      <span class="count">${components.length}</span>
      <i class="fa-solid fa-chevron-right chevron"></i>
    </div>
    <div class="card-body"></div>
  `;
  const header = card.querySelector(".card-header");
  const body = card.querySelector(".card-body");

  header.addEventListener("click", () => {
    const nowOpen = !card.classList.contains("open");
    card.classList.toggle("open");
    if (nowOpen) {
      state.openCards.add(category);
      renderCategoryBody(body, category, components);
    } else {
      state.openCards.delete(category);
    }
  });

  if (isOpen) renderCategoryBody(body, category, components);
  return card;
}

function renderCategoryBody(body, category, components) {
  body.innerHTML = "";

  if (HISTORY_METRICS[category]) {
    body.appendChild(buildHistorySection(category));
  }

  if (components.length === 0) {
    const empty = document.createElement("div");
    empty.className = "card-empty";
    empty.textContent = "No data reported by Redfish for this category on this server.";
    body.appendChild(empty);
    return;
  }

  for (const c of components) {
    body.appendChild(buildComponentItem(c));
  }
}

function buildComponentItem(c) {
  const item = document.createElement("div");
  item.className = "component-item";
  item.innerHTML = `
    <div class="component-item-header">
      <span class="dot" style="width:7px;height:7px;border-radius:50%;background:${healthDotColor(c.health)};flex-shrink:0;"></span>
      <span class="name">${escapeHtml(c.name || c.odata_id)}</span>
      ${c.location ? `<span class="loc">${escapeHtml(c.location)}</span>` : ""}
      <i class="fa-solid fa-chevron-right chevron" style="font-size:10px;"></i>
    </div>
    <div class="component-props"></div>
  `;
  const itemHeader = item.querySelector(".component-item-header");
  const props = item.querySelector(".component-props");
  itemHeader.addEventListener("click", () => {
    item.classList.toggle("open");
    if (item.classList.contains("open") && !props.dataset.rendered) {
      props.appendChild(buildPropGrid(c.properties));
      props.dataset.rendered = "1";
    }
  });
  return item;
}

// Flattens a Redfish resource JSON into a flat key -> value property
// grid so "every available property" is genuinely visible, including
// nested objects/arrays (rendered as compact JSON) and OEM extensions.
function buildPropGrid(obj, prefix = "") {
  const grid = document.createElement("div");
  grid.className = "prop-grid";
  const skipTopLevelKeys = new Set([
    "@odata.context", "@odata.etag", "@odata.id", "@odata.type",
    "Id", "Name", "Description", "Links", "Actions", "Oem", "Assembly"
  ]);

  function walk(value, path) {
    if (value === null || value === undefined) {
      addRow(path, "null");
      return;
    }
    
    // Skip noisy metadata in nested objects too
    const pathParts = path.split(".");
    const lastPart = pathParts[pathParts.length - 1];
    if (lastPart === "@odata.id" || lastPart === "@odata.type" || lastPart === "@odata.context") {
        return; 
    }

    if (Array.isArray(value)) {
      if (value.length === 0) { addRow(path, "[]"); return; }
      const allPrimitive = value.every((v) => typeof v !== "object" || v === null);
      if (allPrimitive) {
        addRow(path, JSON.stringify(value));
      } else {
        value.forEach((v, i) => walk(v, `${path}[${i}]`));
      }
      return;
    }
    if (typeof value === "object") {
      const keys = Object.keys(value).filter((k) => !path && skipTopLevelKeys.has(k) ? false : true);
      if (keys.length === 0) {
          if (path) addRow(path, "{}"); 
          return; 
      }
      for (const k of keys) walk(value[k], path ? `${path}.${k}` : k);
      return;
    }
    addRow(path, String(value));
  }

  function addRow(path, value) {
    const k = document.createElement("div");
    k.className = "k";
    k.textContent = path;
    const v = document.createElement("div");
    v.className = "v";
    v.textContent = value;
    grid.appendChild(k);
    grid.appendChild(v);
  }

  walk(obj, prefix);
  return grid;
}

// ---------------------------------------------------------------------
// History charts (per category, when it has chartable metrics)
// ---------------------------------------------------------------------
function buildHistorySection(category) {
  const wrap = document.createElement("div");
  wrap.className = "card";
  wrap.style.margin = "10px 16px";
  wrap.style.border = "1px dashed var(--border)";

  const metrics = HISTORY_METRICS[category];
  const metricOptions = metrics.map(([m, label]) => `<option value="${m}">${label}</option>`).join("");

  wrap.innerHTML = `
    <div class="history-toolbar">
      <select class="metricSelect">${metricOptions}</select>
      <div style="flex:1"></div>
      <button class="btn-secondary range-btn active" data-range="1h">1H</button>
      <button class="btn-secondary range-btn" data-range="24h">24H</button>
      <button class="btn-secondary range-btn" data-range="7d">7D</button>
      <button class="btn-secondary range-btn" data-range="30d">30D</button>
    </div>
    <div class="chart-wrap"><canvas height="90"></canvas></div>
  `;

  const canvas = wrap.querySelector("canvas");
  const metricSelect = wrap.querySelector(".metricSelect");
  let currentRange = "1h";

  async function refresh() {
    const metric = metricSelect.value;
    const data = await api(`/api/servers/${state.selectedServerId}/history/${metric}?range=${currentRange}`);
    drawChart(canvas, category, data);
  }

  wrap.querySelectorAll(".range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      wrap.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      currentRange = btn.dataset.range;
      refresh();
    });
  });
  metricSelect.addEventListener("change", refresh);

  refresh();
  return wrap;
}

function drawChart(canvas, category, readings) {
  const key = category + canvas.dataset.chartKey;
  if (state.charts[canvas]) {
    state.charts[canvas].destroy();
  }
  // group by source_name into separate lines
  const bySource = {};
  for (const r of readings) {
    const name = r.source_name || "value";
    if (!bySource[name]) bySource[name] = [];
    bySource[name].push({ x: r.recorded_at, y: r.value });
  }
  const palette = ["#3fb6ff", "#2ecc71", "#f1c40f", "#e74c3c", "#a78bfa", "#fb923c"];
  const datasets = Object.entries(bySource).map(([name, points], i) => ({
    label: name,
    data: points,
    borderColor: palette[i % palette.length],
    backgroundColor: "transparent",
    tension: 0.25,
    pointRadius: 0,
    borderWidth: 1.6,
  }));

  state.charts[canvas] = new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      animation: false,
      scales: {
        x: { type: "time", ticks: { color: "#8b96a8", maxTicksLimit: 6 }, grid: { color: "rgba(255,255,255,0.04)" } },
        y: { ticks: { color: "#8b96a8" }, grid: { color: "rgba(255,255,255,0.04)" } },
      },
      plugins: {
        legend: { labels: { color: "#d7dee8", boxWidth: 10, font: { size: 10 } } },
      },
    },
  });
}

// ---------------------------------------------------------------------
// Logs card
// ---------------------------------------------------------------------
function buildLogsCard() {
  const card = document.createElement("div");
  card.className = "card";
  card.style.gridColumn = "1 / -1";
  card.innerHTML = `
    <div class="card-header">
      <i class="fa-solid fa-scroll icon"></i>
      <span class="title">Logs</span>
      <i class="fa-solid fa-chevron-right chevron"></i>
    </div>
    <div class="card-body">
      <div class="logs-toolbar">
        <input type="text" placeholder="Search messages..." id="logSearch">
        <select id="logSeverity">
          <option value="">All severities</option>
          <option value="OK">Informational</option>
          <option value="Warning">Warning</option>
          <option value="Critical">Critical</option>
        </select>
        <button class="btn-secondary" id="logRefresh"><i class="fa-solid fa-rotate"></i></button>
      </div>
      <div id="logRows"></div>
    </div>
  `;
  const header = card.querySelector(".card-header");
  const body = card.querySelector(".card-body");
  let loaded = false;

  header.addEventListener("click", () => {
    card.classList.toggle("open");
    if (card.classList.contains("open") && !loaded) {
      loaded = true;
      loadLogs();
    }
  });

  async function loadLogs() {
    const q = card.querySelector("#logSearch").value;
    const sev = card.querySelector("#logSeverity").value;
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (sev) params.set("severity", sev);
    const entries = await api(`/api/servers/${state.selectedServerId}/logs?${params}`);
    const rowsEl = card.querySelector("#logRows");
    if (entries.length === 0) {
      rowsEl.innerHTML = `<div class="card-empty">No log entries found.</div>`;
      return;
    }
    rowsEl.innerHTML = entries.map((e) => `
      <div class="log-row">
        <span class="sev" style="color:${healthDotColor(e.severity === 'OK' ? 'OK' : e.severity)}">${escapeHtml(e.severity)}</span>
        <span class="ts">${e.created_at ? new Date(e.created_at).toLocaleString() : ""}</span>
        <span>${escapeHtml(e.message || e.message_id || "")}</span>
      </div>
    `).join("");
  }

  card.querySelector("#logRefresh")?.addEventListener?.("click", loadLogs);
  card.addEventListener("click", (e) => {
    if (e.target.id === "logRefresh") loadLogs();
  });
  card.querySelector("#logSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") loadLogs(); });
  card.querySelector("#logSeverity").addEventListener("change", loadLogs);

  return card;
}

// ---------------------------------------------------------------------
// Alerts drawer
// ---------------------------------------------------------------------
async function loadAlerts() {
  state.alerts = await api("/api/alerts?resolved=false");
  renderAlertsBadge();
  renderAlertsDrawer();
}

function renderAlertsBadge() {
  const badge = $("#alertsBadge");
  const count = state.alerts.length;
  badge.style.display = count > 0 ? "flex" : "none";
  badge.textContent = count > 99 ? "99+" : count;
}

function renderAlertsDrawer() {
  const list = $("#alertsList");
  if (state.alerts.length === 0) {
    list.innerHTML = `<div class="sidebar-empty">No open alerts.</div>`;
    return;
  }
  list.innerHTML = "";
  for (const a of state.alerts) {
    const server = state.servers.find((s) => s.id === a.server_id);
    const el = document.createElement("div");
    el.className = "alert-item";
    el.innerHTML = `
      <div class="top">
        <span class="pill ${healthClass(a.severity === 'critical' ? 'Critical' : 'Warning')}">${a.severity}</span>
        <strong>${escapeHtml(server ? (server.display_name || server.hostname) : a.server_id)}</strong>
      </div>
      <div class="msg">${escapeHtml(a.message)}</div>
      <div class="meta">${a.category} &middot; ${a.occurrence_count}x &middot; last ${new Date(a.last_occurred_at).toLocaleString()}</div>
      <div class="alert-actions">
        ${!a.acknowledged ? `<button class="btn-secondary ack-btn">Acknowledge</button>` : `<span class="meta">Acknowledged</span>`}
        <button class="btn-secondary resolve-btn">Resolve</button>
      </div>
    `;
    el.querySelector(".ack-btn")?.addEventListener("click", async () => {
      await api(`/api/alerts/${a.id}/acknowledge`, { method: "POST", body: JSON.stringify({}) });
      loadAlerts();
    });
    el.querySelector(".resolve-btn").addEventListener("click", async () => {
      await api(`/api/alerts/${a.id}/resolve`, { method: "POST" });
      loadAlerts();
    });
    list.appendChild(el);
  }
}

// ---------------------------------------------------------------------
// Add server modal
// ---------------------------------------------------------------------
function wireAddServerModal() {
  const modal = $("#addServerModal");
  $("#addServerBtn").addEventListener("click", () => modal.classList.add("open"));
  $("#cancelAddServer").addEventListener("click", () => modal.classList.remove("open"));
  $("#submitAddServer").addEventListener("click", async () => {
    const errorEl = $("#addServerError");
    errorEl.style.display = "none";
    const payload = {
      hostname: $("#f_hostname").value.trim(),
      display_name: $("#f_hostname").value.trim(),
      ip_address: $("#f_ip").value.trim(),
      username: $("#f_username").value.trim(),
      password: $("#f_password").value,
      polling_interval_seconds: parseInt($("#f_interval").value, 10) || 30,
    };
    try {
      await api("/api/servers", { method: "POST", body: JSON.stringify(payload) });
      modal.classList.remove("open");
      ["f_hostname", "f_ip", "f_username", "f_password"].forEach((id) => ($(`#${id}`).value = ""));
      await loadServers();
      toast("Server added - discovery in progress");
    } catch (e) {
      errorEl.textContent = e.message;
      errorEl.style.display = "block";
    }
  });
}

// ---------------------------------------------------------------------
// WebSocket wiring
// ---------------------------------------------------------------------
let socket;
function wireSocket() {
  socket = io();

  socket.on("connect", () => {
    $("#wsStatus").className = "pill pill-ok";
    $("#wsStatus").innerHTML = `<span class="dot" style="background:var(--ok)"></span> live`;
    if (state.selectedServerId) socket.emit("subscribe_server", { server_id: state.selectedServerId });
  });

  socket.on("disconnect", () => {
    $("#wsStatus").className = "pill pill-crit";
    $("#wsStatus").innerHTML = `<span class="dot" style="background:var(--crit)"></span> disconnected`;
  });

  socket.on("server_summary_update", (summary) => {
    const idx = state.servers.findIndex((s) => s.id === summary.id);
    if (idx >= 0) state.servers[idx] = summary; else state.servers.push(summary);
    renderServerList();
    if (summary.id === state.selectedServerId) {
      const headerEl = $("#serverHeader");
      if (headerEl) renderHeader({ ...summary, redfish_service_root: null });
    }
  });

  socket.on("component_update", async (payload) => {
    if (payload.server_id !== state.selectedServerId) return;
    
    const componentsByCategory = await api(`/api/servers/${state.selectedServerId}/components`);
    for (const category of CATEGORY_ORDER) {
      let comps = componentsByCategory[category] || [];
      if (category === "storage") {
        comps = comps.concat(
          componentsByCategory["storage_controller"] || [],
          componentsByCategory["storage_drive"] || [],
          componentsByCategory["storage_volume"] || []
        );
      }
      
      const card = document.querySelector(`.card[data-category="${category}"]`);
      if (card) {
        const countEl = card.querySelector(".count");
        if (countEl) countEl.textContent = comps.length;
        if (card.classList.contains("open")) {
          renderCategoryBody(card.querySelector(".card-body"), category, comps);
        }
      }
    }
  });

  socket.on("alert", () => {
    loadAlerts();
  });

  socket.on("log_entries", (payload) => {
    if (payload.server_id === state.selectedServerId) {
      toast(`${payload.entries.length} new log entr${payload.entries.length === 1 ? "y" : "ies"}`);
    }
  });
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------
function wireDrawer() {
  const overlay = $("#drawerOverlay");
  const drawer = $("#alertsDrawer");
  $("#alertsBtn").addEventListener("click", () => {
    overlay.classList.add("open");
    drawer.classList.add("open");
  });
  const close = () => { overlay.classList.remove("open"); drawer.classList.remove("open"); };
  $("#closeDrawerBtn").addEventListener("click", close);
  overlay.addEventListener("click", close);
}

document.addEventListener("DOMContentLoaded", async () => {
  wireAddServerModal();
  wireDrawer();
  wireSocket();
  $("#serverSearch").addEventListener("input", renderServerList);

  await loadServers();
  await loadAlerts();
  setInterval(loadAlerts, 30000);
});
