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
    lastMessageAt: null,       // Date.now() of the most recent WS message, any host
    activeHost: null,
    activeRangeHours: 24,
    seriesCache: {},           // `${host}:${rangeHours}` -> [metric,...] ascending by ts
    backupEvents: [],
  };

  const chartLeftEl = document.getElementById("chart-left");
  const chartRightEl = document.getElementById("chart-right");
  const chartLeft = echarts.init(chartLeftEl, null, { renderer: "canvas" });
  const chartRight = echarts.init(chartRightEl, null, { renderer: "canvas" });
  window.addEventListener("resize", () => { chartLeft.resize(); chartRight.resize(); });

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
        <div class="card-head">
          <div class="name"><span class="swatch"></span>${h.display_name}<span class="stale-badge">STALE</span></div>
          <div class="hero na" data-f="cpu_temp"><span class="v">—</span><span class="unit">°C CPU</span></div>
        </div>
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

    const heroCls = classifyTemp(m.cpu_temp, m.cpu_temp_warn, m.cpu_temp_crit, "cpu_temp");
    const hero = card.querySelector('[data-f="cpu_temp"]');
    if (hero) {
      hero.className = `hero ${heroCls}`;
      hero.querySelector(".v").textContent = m.cpu_temp === null || m.cpu_temp === undefined
        ? "—" : m.cpu_temp.toFixed(1);
    }

    set("nvme_composite_temp", fmtTemp(m.nvme_composite_temp), classifyTemp(m.nvme_composite_temp, m.nvme_composite_warn, m.nvme_composite_crit, "nvme_composite_temp"));
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
      // Stale if nothing heard in 3 minutes (well past the 3s fast-tier cadence).
      if (!seenAt || now - seenAt > 3 * 60 * 1000) {
        card.classList.add("stale");
      }
    }
  }
  setInterval(stalenessSweep, 15000);

  // ---------- "Updated: Xs ago" ticker ----------

  const updatedAgoEl = document.getElementById("updated-ago");
  function updateAgoText() {
    if (!updatedAgoEl || state.lastMessageAt === null) return;
    const secs = Math.max(0, Math.round((Date.now() - state.lastMessageAt) / 1000));
    updatedAgoEl.textContent = `Updated: ${secs}s ago`;
  }
  setInterval(updateAgoText, 1000);

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
    const rows = await fetchJSON(`/api/metrics?host=${encodeURIComponent(host)}&since=${encodeURIComponent(since)}&limit=8000`);
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
          { xAxis: start, itemStyle: { color: "rgba(255,159,92,0.14)" } },
          { xAxis: end },
        ];
      })
      .filter(Boolean);
  }

  function glowSeries(name, color, data, extra) {
    return {
      name, type: "line", data, showSymbol: false, smooth: 0.55, smoothMonotone: "x", sampling: "lttb",
      animationDuration: 400, animationDurationUpdate: 300,
      lineStyle: { color, width: 2.4, shadowColor: color, shadowBlur: 12 },
      itemStyle: { color },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: color + "50" },
          { offset: 1, color: color + "00" },
        ]),
      },
      ...extra,
    };
  }

  async function loadAndRenderChart() {
    if (!state.activeHost) return;
    const rows = await loadSeries(state.activeHost, state.activeRangeHours);
    buildChartOption(rows);
  }

  const AXIS_COMMON = {
    scale: true,
    axisLine: { lineStyle: { color: "#1e2740" } },
    axisLabel: { color: "#8794ab" },
    splitLine: { lineStyle: { color: "#161d2e" } },
  };

  function hostHasFan(hostKey) {
    return !!state.hosts.find((h) => h.key === hostKey)?.has_fan;
  }

  // Trailing moving average over [ts, value] pairs, ignoring nulls within
  // the window. Window size scales with row count so the line reads as
  // smooth at any zoom level (6h vs 30d) instead of showing raw 3s jitter.
  function smoothingWindow(n) {
    return Math.max(1, Math.round(n / 150));
  }
  function movingAverage(pairs, window) {
    if (window <= 1) return pairs;
    const out = new Array(pairs.length);
    const buf = [];
    let sum = 0, count = 0;
    for (let i = 0; i < pairs.length; i++) {
      const v = pairs[i][1];
      if (v !== null && v !== undefined) { buf.push(v); sum += v; count++; }
      else buf.push(null);
      if (buf.length > window) {
        const removed = buf.shift();
        if (removed !== null && removed !== undefined) { sum -= removed; count--; }
      }
      out[i] = [pairs[i][0], count ? sum / count : null];
    }
    return out;
  }

  function buildLeftOption(rows, rangeStartMs, latestThresholds) {
    const hasFan = hostHasFan(state.activeHost);
    const cpuData = movingAverage(rows.map((r) => [r.ts, r.cpu_temp]), smoothingWindow(rows.length));

    const cpuWarn = latestThresholds.cpu_temp_warn ?? DEFAULT_THRESHOLDS.cpu_temp.warn;
    const cpuCrit = latestThresholds.cpu_temp_crit ?? DEFAULT_THRESHOLDS.cpu_temp.crit;

    const cpuSeries = glowSeries("CPU temp", "#4fd1ff", cpuData, {
      yAxisIndex: 0,
      markLine: {
        symbol: "none", label: { formatter: "{b}", color: "#8794ab" },
        lineStyle: { type: "dashed" },
        data: [
          { yAxis: cpuWarn, lineStyle: { color: "#ffb545" }, name: "CPU warn" },
          { yAxis: cpuCrit, lineStyle: { color: "#ff4d6d" }, name: "CPU crit" },
        ],
      },
      markArea: { data: backupMarkAreas(state.activeHost, rangeStartMs) },
    });

    const series = [cpuSeries];
    const yAxis = [{ type: "value", name: "°C", nameTextStyle: { color: "#8794ab" }, ...AXIS_COMMON }];

    if (hasFan) {
      const fanData = rows.map((r) => [r.ts, r.fan_rpm]);
      series.push(glowSeries("Fan speed", "#3ee08a", fanData, { yAxisIndex: 1, areaStyle: null }));
      yAxis.push({ type: "value", name: "RPM", nameTextStyle: { color: "#8794ab" }, ...AXIS_COMMON, splitLine: { show: false } });
    }

    return {
      backgroundColor: "transparent",
      textStyle: { color: "#e6ebf5" },
      grid: { left: 50, right: hasFan ? 50 : 24, top: 30, bottom: 40 },
      tooltip: { trigger: "axis", backgroundColor: "#111726", borderColor: "#1e2740", textStyle: { color: "#e6ebf5" } },
      legend: { top: 0, textStyle: { color: "#8794ab" } },
      xAxis: { type: "time", axisLine: { lineStyle: { color: "#1e2740" } }, axisLabel: { color: "#8794ab" } },
      yAxis,
      series,
    };
  }

  function buildRightOption(rows, latestThresholds) {
    const nvmeData = movingAverage(rows.map((r) => [r.ts, r.nvme_composite_temp]), smoothingWindow(rows.length));

    const nvmeWarn = latestThresholds.nvme_composite_warn ?? DEFAULT_THRESHOLDS.nvme_composite_temp.warn;
    const nvmeCrit = latestThresholds.nvme_composite_crit ?? DEFAULT_THRESHOLDS.nvme_composite_temp.crit;

    const nvmeSeries = glowSeries("NVMe composite", "#ff9f5c", nvmeData, {
      markLine: {
        symbol: "none", label: { formatter: "{b}", color: "#8794ab" },
        lineStyle: { type: "dashed" },
        data: [
          { yAxis: nvmeWarn, lineStyle: { color: "#ffb545" }, name: "NVMe warn" },
          { yAxis: nvmeCrit, lineStyle: { color: "#ff4d6d" }, name: "NVMe crit" },
        ],
      },
    });

    return {
      backgroundColor: "transparent",
      textStyle: { color: "#e6ebf5" },
      grid: { left: 50, right: 24, top: 30, bottom: 40 },
      tooltip: { trigger: "axis", backgroundColor: "#111726", borderColor: "#1e2740", textStyle: { color: "#e6ebf5" } },
      legend: { top: 0, textStyle: { color: "#8794ab" } },
      xAxis: { type: "time", axisLine: { lineStyle: { color: "#1e2740" } }, axisLabel: { color: "#8794ab" } },
      yAxis: { type: "value", name: "°C", nameTextStyle: { color: "#8794ab" }, ...AXIS_COMMON },
      series: [nvmeSeries],
    };
  }

  function buildChartOption(rows) {
    const latestThresholds = rows.length ? rows[rows.length - 1] : {};
    const rangeStartMs = Date.now() - state.activeRangeHours * 3600 * 1000;

    chartLeft.setOption(buildLeftOption(rows, rangeStartMs, latestThresholds), true);
    chartRight.setOption(buildRightOption(rows, latestThresholds), true);
  }

  function appendLiveToChart(m) {
    if (m.host !== state.activeHost) return;
    const cacheKey = `${state.activeHost}:${state.activeRangeHours}`;
    const rows = state.seriesCache[cacheKey];
    if (!rows) return;
    rows.push(m);
    const cutoff = Date.now() - state.activeRangeHours * 3600 * 1000;
    while (rows.length && new Date(rows[0].ts).getTime() < cutoff) rows.shift();
    const window = smoothingWindow(rows.length);
    const leftSeries = [{ data: movingAverage(rows.map((r) => [r.ts, r.cpu_temp]), window) }];
    if (hostHasFan(state.activeHost)) {
      leftSeries.push({ data: rows.map((r) => [r.ts, r.fan_rpm]) });
    }
    chartLeft.setOption({ series: leftSeries });
    chartRight.setOption({
      series: [
        { data: movingAverage(rows.map((r) => [r.ts, r.nvme_composite_temp]), window) },
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

    ws.addEventListener("open", () => { dot.className = "dot live"; label.textContent = "Live"; });
    ws.addEventListener("close", () => {
      dot.className = "dot down"; label.textContent = "Disconnected — retrying…";
      setTimeout(connectWebSocket, 3000);
    });
    ws.addEventListener("error", () => ws.close());
    ws.addEventListener("message", (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.type !== "metric") return;
      const m = msg.data;
      state.latest[m.host] = m;
      state.lastMessageAt = Date.now();
      updateAgoText();
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
