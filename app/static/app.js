(() => {
  "use strict";

  const HOST_COLORS = { alcyone: "#7c9fff", argos: "#ff9f5c", selene: "#c58bff" };
  const DEFAULT_THRESHOLDS = {
    cpu_temp: { warn: 75, crit: 85 },
    nvme_composite_temp: { warn: 70, crit: 82 },
  };

  const state = {
    hosts: [],                 // [{key, display_name, is_backup_host}]
    latest: {},                // host -> most recent metric dict
    lastSeenAt: {},            // host -> Date.now() of last metric (for staleness)
    activeHost: null,
    activeRangeHours: 24,
    seriesCache: {},           // `${host}:${rangeHours}` -> [metric,...] ascending by ts
    backupEvents: [],
  };

  const chartEl = document.getElementById("chart");
  const chart = echarts.init(chartEl, null, { renderer: "svg" });
  window.addEventListener("resize", () => chart.resize());

  function fmtTemp(v) {
    return v === null || v === undefined ? "—" : `${v.toFixed(1)}°C`;
  }
  function fmtPct(v) {
    return v === null || v === undefined ? "—" : `${v}%`;
  }
  function fmtRpm(v) {
    return v === null || v === undefined ? "—" : `${Math.round(v)} RPM`;
  }
  function classifyTemp(v, warn, crit, metricKey) {
    if (v === null || v === undefined) return "na";
    const d = DEFAULT_THRESHOLDS[metricKey] || {};
    const w = warn ?? d.warn, c = crit ?? d.crit;
    if (c !== undefined && v >= c) return "crit";
    if (w !== undefined && v >= w) return "warn";
    return "ok";
  }

  async function fetchJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
    return resp.json();
  }

  function hostColor(host) {
    return HOST_COLORS[host] || "#4fd1ff";
  }

  // ---------- Host tiles ----------

  function renderHostCards() {
    const grid = document.getElementById("host-grid");
    grid.innerHTML = "";
    for (const h of state.hosts) {
      const card = document.createElement("div");
      card.className = "host-card";
      card.id = `card-${h.key}`;
      card.style.setProperty("--host-color", hostColor(h.key));
      card.innerHTML = `
        <h2><span class="swatch"></span>${h.display_name}<span class="stale-badge">STALE</span></h2>
        <div class="metric-row"><span class="label">CPU temp</span><span class="value na" data-f="cpu_temp">—</span></div>
        <div class="metric-row"><span class="label">NVMe composite</span><span class="value na" data-f="nvme_composite_temp">—</span></div>
        <div class="metric-row"><span class="label">NVMe sensor 1 / 2</span><span class="value na" data-f="nvme_sensors">—</span></div>
        <div class="metric-row"><span class="label">Wear (percentage used)</span><span class="value na" data-f="nvme_percentage_used">—</span></div>
        <div class="metric-row"><span class="label">Critical warning</span><span class="value na" data-f="nvme_critical_warning">—</span></div>
        <div class="metric-row"><span class="label">Media errors</span><span class="value na" data-f="nvme_media_errors">—</span></div>
        <div class="metric-row"><span class="label">Fan speed</span><span class="value na" data-f="fan_rpm">—</span></div>
      `;
      grid.appendChild(card);
    }
  }

  function updateHostCard(m) {
    const card = document.getElementById(`card-${m.host}`);
    if (!card) return;

    state.lastSeenAt[m.host] = Date.now();
    card.classList.remove("stale");

    const set = (field, text, cls) => {
      const el = card.querySelector(`[data-f="${field}"]`);
      if (!el) return;
      el.textContent = text;
      el.className = `value ${cls}`;
    };

    set("cpu_temp", fmtTemp(m.cpu_temp), classifyTemp(m.cpu_temp, m.cpu_temp_warn, m.cpu_temp_crit, "cpu_temp"));
    set("nvme_composite_temp", fmtTemp(m.nvme_composite_temp),
      classifyTemp(m.nvme_composite_temp, m.nvme_composite_warn, m.nvme_composite_crit, "nvme_composite_temp"));
    set("nvme_sensors", `${fmtTemp(m.nvme_sensor1_temp)} / ${fmtTemp(m.nvme_sensor2_temp)}`, "na");
    set("nvme_percentage_used", fmtPct(m.nvme_percentage_used), m.nvme_percentage_used > 20 ? "warn" : "ok");
    set("nvme_critical_warning",
      m.nvme_critical_warning === null || m.nvme_critical_warning === undefined ? "—" : (m.nvme_critical_warning ? "YES" : "clear"),
      m.nvme_critical_warning ? "crit" : (m.nvme_critical_warning === 0 ? "ok" : "na"));
    set("nvme_media_errors",
      m.nvme_media_errors === null || m.nvme_media_errors === undefined ? "—" : String(m.nvme_media_errors),
      m.nvme_media_errors > 0 ? "crit" : (m.nvme_media_errors === 0 ? "ok" : "na"));
    set("fan_rpm", fmtRpm(m.fan_rpm), "na");

    if (m.in_backup_window) card.classList.add("in-backup");
  }

  function stalenessSweep() {
    const now = Date.now();
    for (const h of state.hosts) {
      const seenAt = state.lastSeenAt[h.key];
      const card = document.getElementById(`card-${h.key}`);
      if (!card) continue;
      // Stale if nothing heard in 3x the normal poll interval (45s) or 3 minutes, whichever's larger.
      if (!seenAt || now - seenAt > 3 * 60 * 1000) {
        card.classList.add("stale");
      }
    }
  }
  setInterval(stalenessSweep, 15000);

  // ---------- Chart ----------

  function renderHostTabs() {
    const wrap = document.getElementById("host-tabs");
    wrap.innerHTML = "";
    for (const h of state.hosts) {
      const btn = document.createElement("button");
      btn.textContent = h.display_name;
      btn.dataset.host = h.key;
      if (h.key === state.activeHost) btn.classList.add("active");
      btn.addEventListener("click", () => {
        state.activeHost = h.key;
        [...wrap.children].forEach((b) => b.classList.toggle("active", b === btn));
        loadAndRenderChart();
      });
      wrap.appendChild(btn);
    }
  }

  document.getElementById("range-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-range]");
    if (!btn) return;
    state.activeRangeHours = Number(btn.dataset.range);
    [...btn.parentElement.children].forEach((b) => b.classList.toggle("active", b === btn));
    loadAndRenderChart();
  });

  async function loadSeries(host, rangeHours) {
    const cacheKey = `${host}:${rangeHours}`;
    const since = new Date(Date.now() - rangeHours * 3600 * 1000).toISOString();
    const rows = await fetchJSON(`/api/metrics?host=${encodeURIComponent(host)}&since=${encodeURIComponent(since)}&limit=5000`);
    rows.reverse(); // API returns newest-first; chart wants ascending
    state.seriesCache[cacheKey] = rows;
    return rows;
  }

  function backupMarkAreas(host, rangeStartMs) {
    if (!state.backupEvents.length) return [];
    return state.backupEvents
      .filter((ev) => ev.host === host)
      .map((ev) => {
        const start = new Date(ev.start_ts).getTime();
        const end = ev.end_ts ? new Date(ev.end_ts).getTime() : Date.now();
        if (end < rangeStartMs) return null;
        return [
          { xAxis: start, itemStyle: { color: "rgba(255,159,92,0.12)" } },
          { xAxis: end },
        ];
      })
      .filter(Boolean);
  }

  async function loadAndRenderChart() {
    if (!state.activeHost) return;
    const rows = await loadSeries(state.activeHost, state.activeRangeHours);
    const latestThresholds = rows.length ? rows[rows.length - 1] : {};

    const cpuData = rows.map((r) => [r.ts, r.cpu_temp]);
    const nvmeData = rows.map((r) => [r.ts, r.nvme_composite_temp]);
    const rangeStartMs = Date.now() - state.activeRangeHours * 3600 * 1000;

    const cpuWarn = latestThresholds.cpu_temp_warn ?? DEFAULT_THRESHOLDS.cpu_temp.warn;
    const cpuCrit = latestThresholds.cpu_temp_crit ?? DEFAULT_THRESHOLDS.cpu_temp.crit;
    const nvmeWarn = latestThresholds.nvme_composite_warn ?? DEFAULT_THRESHOLDS.nvme_composite_temp.warn;
    const nvmeCrit = latestThresholds.nvme_composite_crit ?? DEFAULT_THRESHOLDS.nvme_composite_temp.crit;

    chart.setOption({
      backgroundColor: "transparent",
      textStyle: { color: "#e6ebf5" },
      grid: { left: 50, right: 24, top: 30, bottom: 40 },
      tooltip: { trigger: "axis", backgroundColor: "#111726", borderColor: "#1e2740", textStyle: { color: "#e6ebf5" } },
      legend: { top: 0, textStyle: { color: "#8794ab" } },
      xAxis: { type: "time", axisLine: { lineStyle: { color: "#1e2740" } }, axisLabel: { color: "#8794ab" } },
      yAxis: {
        type: "value", name: "°C", nameTextStyle: { color: "#8794ab" },
        axisLine: { lineStyle: { color: "#1e2740" } }, axisLabel: { color: "#8794ab" },
        splitLine: { lineStyle: { color: "#161d2e" } },
      },
      series: [
        {
          name: "CPU temp", type: "line", showSymbol: false, data: cpuData,
          lineStyle: { width: 2, color: "#4fd1ff" }, areaStyle: { color: "rgba(79,209,255,0.08)" },
          markLine: {
            symbol: "none", label: { formatter: "{b}", color: "#8794ab" },
            lineStyle: { type: "dashed" },
            data: [
              { yAxis: cpuWarn, lineStyle: { color: "#ffb545" }, name: "CPU warn" },
              { yAxis: cpuCrit, lineStyle: { color: "#ff4d6d" }, name: "CPU crit" },
            ],
          },
          markArea: { data: backupMarkAreas(state.activeHost, rangeStartMs) },
        },
        {
          name: "NVMe composite", type: "line", showSymbol: false, data: nvmeData,
          lineStyle: { width: 2, color: "#ff9f5c" }, areaStyle: { color: "rgba(255,159,92,0.08)" },
          markLine: {
            symbol: "none", label: { formatter: "{b}", color: "#8794ab" },
            lineStyle: { type: "dashed" },
            data: [
              { yAxis: nvmeWarn, lineStyle: { color: "#ffb545" }, name: "NVMe warn" },
              { yAxis: nvmeCrit, lineStyle: { color: "#ff4d6d" }, name: "NVMe crit" },
            ],
          },
        },
      ],
    });
  }

  function appendLiveToChart(m) {
    if (m.host !== state.activeHost) return;
    const cacheKey = `${state.activeHost}:${state.activeRangeHours}`;
    const rows = state.seriesCache[cacheKey];
    if (!rows) return;
    rows.push(m);
    const cutoff = Date.now() - state.activeRangeHours * 3600 * 1000;
    while (rows.length && new Date(rows[0].ts).getTime() < cutoff) rows.shift();
    loadAndRenderChartFromCache();
  }

  function loadAndRenderChartFromCache() {
    // Re-render using already-cached rows (no network round trip) - used for live ticks.
    const cacheKey = `${state.activeHost}:${state.activeRangeHours}`;
    const rows = state.seriesCache[cacheKey] || [];
    const latestThresholds = rows.length ? rows[rows.length - 1] : {};
    chart.setOption({
      series: [
        { data: rows.map((r) => [r.ts, r.cpu_temp]) },
        { data: rows.map((r) => [r.ts, r.nvme_composite_temp]) },
      ],
    });
  }

  // ---------- Backup events table ----------

  function renderEvents() {
    const wrap = document.getElementById("events-wrap");
    if (!state.backupEvents.length) {
      wrap.innerHTML = '<div class="empty-note">No backup events recorded yet.</div>';
      return;
    }
    const rows = state.backupEvents.map((ev) => {
      const start = new Date(ev.start_ts).toLocaleString();
      const dur = ev.duration_seconds ? `${Math.round(ev.duration_seconds / 60)} min` : "—";
      return `<tr>
        <td>${ev.host}</td>
        <td>${start}</td>
        <td>${dur}</td>
        <td>${fmtTemp(ev.peak_nvme_temp)}</td>
        <td>${fmtTemp(ev.peak_cpu_temp)}</td>
        <td><span class="badge ${ev.status}">${ev.status}</span></td>
      </tr>`;
    }).join("");
    wrap.innerHTML = `<table class="events">
      <thead><tr><th>Host</th><th>Start</th><th>Duration</th><th>Peak NVMe</th><th>Peak CPU</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }

  // ---------- WebSocket ----------

  function connectWebSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    const dot = document.getElementById("conn-dot");
    const label = document.getElementById("conn-label");

    ws.addEventListener("open", () => { dot.className = "dot live"; label.textContent = "live"; });
    ws.addEventListener("close", () => {
      dot.className = "dot down"; label.textContent = "disconnected, retrying…";
      setTimeout(connectWebSocket, 3000);
    });
    ws.addEventListener("error", () => ws.close());
    ws.addEventListener("message", (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.type !== "metric") return;
      const m = msg.data;
      state.latest[m.host] = m;
      updateHostCard(m);
      appendLiveToChart(m);
      if (m.host === "argos") {
        // A fresh backup event may have just started/ended - refresh the table lazily.
        refreshBackupEvents();
      }
    });
  }

  async function refreshBackupEvents() {
    state.backupEvents = await fetchJSON("/api/backup-events?limit=100");
    renderEvents();
  }

  // ---------- Boot ----------

  async function boot() {
    state.hosts = await fetchJSON("/api/hosts");
    state.activeHost = state.hosts.find((h) => h.is_backup_host)?.key || state.hosts[0]?.key;

    renderHostCards();
    renderHostTabs();

    for (const h of state.hosts) {
      const rows = await fetchJSON(`/api/metrics?host=${encodeURIComponent(h.key)}&limit=1`);
      if (rows.length) updateHostCard(rows[0]);
    }

    await refreshBackupEvents();
    await loadAndRenderChart();
    connectWebSocket();
  }

  boot().catch((err) => {
    console.error("boot failed", err);
    document.getElementById("conn-label").textContent = "failed to load — check backend";
  });
})();
