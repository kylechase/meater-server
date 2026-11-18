"use strict";

/* The web UI.  No build step and no CDN: a probe-side server is often on a
   network with no route to the internet, so everything it needs ships with it. */

const state = {
  snapshot: null,
  catalog: null,
  units: "C",
  lastCookState: {},   // slot id -> cook state, for firing alerts on transitions
  wakeLock: null,
  pickerSlot: null,
  pickerChoice: null,
};

const $ = (sel) => document.querySelector(sel);
const probesEl = $("#probes");
const connectionEl = $("#connection");

/* ---------------------------------------------------------------- formatting */

const unitSuffix = () => (state.units === "F" ? "°F" : "°C");

function fmtTemp(value, digits = 1) {
  if (value === null || value === undefined) return "–";
  return value.toFixed(digits);
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return "under a minute";
  const minutes = Math.round(total / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

function fmtClock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

/* The state chip always carries an icon and a word, so colour is never the
   only thing distinguishing one state from another. */
const STATE_CHIP = {
  idle:    { icon: "○", text: "Idle",            cls: "" },
  cooking: { icon: "▲", text: "Cooking",         cls: "" },
  ready:   { icon: "✓", text: "Ready to remove", cls: "good" },
  resting: { icon: "⏸", text: "Resting",         cls: "warning" },
  done:    { icon: "✓", text: "Done",            cls: "good" },
};

/* --------------------------------------------------------------- rendering */

function render(snapshot) {
  state.snapshot = snapshot;
  if (snapshot.units !== state.units) setUnits(snapshot.units, false);

  const probes = snapshot.probes || [];
  if (!probes.length) return;

  // Rebuild only when the slot set changes; otherwise patch in place so the
  // dialog and scroll position survive an update.
  const wanted = probes.map((p) => p.slot_id).join(",");
  if (probesEl.dataset.slots !== wanted) {
    probesEl.dataset.slots = wanted;
    probesEl.innerHTML = probes.map((p) => cardShell(p)).join("");
    wireCards();
  }
  probes.forEach((probe) => updateCard(probe, snapshot.now));
}

function cardShell(probe) {
  return `
<section class="card" id="card-${probe.slot_id}" data-index="${probe.index}">
  <div class="card-head">
    <span class="dot" data-role="dot"></span>
    <span class="name" data-role="name">${probe.name}</span>
    <span class="spacer"></span>
    <span data-role="meta"></span>
  </div>
  <div class="hero">
    <div class="temp" data-role="temp"></div>
    <div>
      <span class="chip" data-role="chip"></span>
      <div class="cook-label" data-role="cook"></div>
    </div>
  </div>
  <div class="eta" data-role="eta"></div>
  <div class="eta-note" data-role="eta-note"></div>
  <div class="progress" data-role="progress"><span style="width:0%"></span></div>
  <div class="stats" data-role="stats"></div>
  <div class="chart-wrap" data-role="chart-wrap">
    <div class="legend">
      <span><i style="background:var(--series-tip)"></i>Tip</span>
      <span><i style="background:var(--series-ambient)"></i>Ambient</span>
    </div>
    <div data-role="chart"></div>
    <div class="tooltip" data-role="tooltip"></div>
    <details class="table-view">
      <summary>Show readings as a table</summary>
      <div data-role="table"></div>
    </details>
  </div>
  <div class="actions">
    <button class="btn primary" data-act="choose">Choose cook</button>
    <button class="btn" data-act="rest">Start resting</button>
    <button class="btn" data-act="clear">Clear</button>
  </div>
</section>`;
}

function updateCard(probe, now) {
  const card = document.getElementById(`card-${probe.slot_id}`);
  if (!card) return;
  const at = (role) => card.querySelector(`[data-role="${role}"]`);

  at("name").textContent = probe.name;
  at("dot").className = `dot ${probe.connected ? "on" : "off"}`;

  const meta = [];
  if (probe.battery !== null) meta.push(`${probe.battery}% battery`);
  if (!probe.connected) {
    meta.push(probe.last_seen ? `last seen ${fmtClock(probe.last_seen)}` : "searching…");
  }
  at("meta").textContent = meta.join(" · ");

  const tempEl = at("temp");
  if (probe.tip === null) {
    tempEl.className = "temp none";
    tempEl.textContent = probe.connected ? "waiting for a reading…" : "no probe";
  } else {
    tempEl.className = "temp";
    tempEl.innerHTML = `${fmtTemp(probe.tip)}<span class="unit">${unitSuffix()}</span>`;
  }

  const cook = probe.cook;
  const chipInfo = STATE_CHIP[cook ? cook.state : "idle"] || STATE_CHIP.idle;
  const chip = at("chip");
  chip.className = `chip ${chipInfo.cls}`;
  chip.textContent = `${chipInfo.icon} ${chipInfo.text}`;

  at("cook").textContent = cook ? cook.label : "Nothing selected";

  renderEta(at("eta"), at("eta-note"), probe, now);
  renderProgress(at("progress"), probe);
  renderStats(at("stats"), probe);
  renderChart(at("chart"), at("tooltip"), at("table"), probe);

  card.querySelector('[data-act="rest"]').disabled =
    !cook || cook.state === "resting" || cook.state === "done";
  card.querySelector('[data-act="clear"]').disabled = !cook;

  maybeAlert(probe);
}

function renderEta(etaEl, noteEl, probe, now) {
  const cook = probe.cook;
  const est = probe.estimate || {};

  if (!cook) {
    etaEl.className = "eta muted";
    etaEl.textContent = probe.connected
      ? "Pick what you are cooking to get a time estimate."
      : "Waiting for a probe.";
    noteEl.textContent = "";
    return;
  }

  if (cook.state === "ready") {
    etaEl.className = "eta";
    etaEl.textContent = "Take it off the heat";
    noteEl.textContent = cook.rest_minutes
      ? `Then rest ${Math.round(cook.rest_minutes)} min before serving.`
      : "";
    return;
  }

  if (cook.state === "resting") {
    etaEl.className = "eta";
    etaEl.textContent = `Resting — ${fmtDuration(cook.rest_remaining_s) || "done"}`;
    noteEl.textContent = `Serve at ${fmtClock(now + (cook.rest_remaining_s || 0))}.`;
    return;
  }

  if (cook.state === "done") {
    etaEl.className = "eta";
    etaEl.textContent = "Ready to serve";
    noteEl.textContent = cook.peak !== null ? `Peaked at ${fmtTemp(cook.peak)}${unitSuffix()}.` : "";
    return;
  }

  const served = est.seconds_until_served;
  if (served === null || served === undefined) {
    etaEl.className = "eta muted";
    etaEl.textContent = est.reason || "No estimate yet.";
    noteEl.textContent = "";
    return;
  }

  etaEl.className = "eta";
  etaEl.textContent = `Ready in ${fmtDuration(served)}`;
  const parts = [`about ${fmtClock(now + served)}`];
  if (est.rate_per_min) parts.push(`${est.rate_per_min > 0 ? "+" : ""}${est.rate_per_min.toFixed(1)}${unitSuffix()}/min`);
  if (est.confidence === "low") parts.push("estimate still settling");
  noteEl.textContent = parts.join(" · ");
}

function renderProgress(el, probe) {
  const cook = probe.cook;
  if (!cook || probe.tip === null) {
    el.style.display = "none";
    return;
  }
  el.style.display = "";
  const done = cook.state !== "cooking";
  el.className = `progress${done ? " done" : ""}`;
  const span = el.firstElementChild;
  if (done) {
    span.style.width = "100%";
    return;
  }
  // Measure from the coldest reading of this cook, so the bar reflects the
  // journey rather than the distance from absolute zero.
  const graph = probe.graph || [];
  const start = graph.length ? Math.min(...graph.map((row) => row[1])) : probe.tip;
  const span2 = cook.pull - start;
  const pct = span2 > 0 ? ((probe.tip - start) / span2) * 100 : 100;
  span.style.width = `${Math.max(0, Math.min(100, pct)).toFixed(1)}%`;
}

function renderStats(el, probe) {
  const cook = probe.cook;
  const cells = [];
  const push = (k, v, sub) => cells.push(
    `<div class="stat"><div class="k">${k}</div><div class="v${sub ? " sub" : ""}">${v}</div></div>`
  );

  if (cook) {
    push("Target", `${fmtTemp(cook.target)}${unitSuffix()}`);
    if (cook.carryover_c > 0) push("Remove at", `${fmtTemp(cook.pull)}${unitSuffix()}`, true);
  }
  push("Ambient", probe.ambient === null ? "–" : `${fmtTemp(probe.ambient, 0)}${unitSuffix()}`, true);
  if (cook && cook.peak !== null) push("Peak", `${fmtTemp(cook.peak)}${unitSuffix()}`, true);
  if (cook && cook.rest_minutes) push("Rest", `${Math.round(cook.rest_minutes)} min`, true);
  if (probe.rssi !== null) push("Signal", `${probe.rssi} dBm`, true);

  el.innerHTML = cells.join("");
}

/* ------------------------------------------------------------------- chart */

/* The chart is drawn at the container's real pixel width so one SVG unit is
   one CSS pixel. Scaling a fixed viewBox down to phone width would shrink the
   axis text with it, to about four pixels - legible on a laptop, useless at
   the grill. */
const CHART = { h: 190, top: 10, bottom: 20 };

function chartMetrics(el) {
  const w = Math.max(260, Math.round(el.clientWidth || el.parentElement?.clientWidth || 600));
  // Narrow screens cannot spare 46px of gutter for the end labels.
  const narrow = w < 420;
  return { ...CHART, w, left: narrow ? 28 : 36, right: narrow ? 30 : 46 };
}

function renderChart(el, tooltipEl, tableEl, probe) {
  const rows = (probe.graph || []).filter((r) => r[1] !== null);
  if (rows.length < 2) {
    el.innerHTML = "";
    el.closest(".chart-wrap").style.display = "none";
    return;
  }
  el.closest(".chart-wrap").style.display = "";

  const { w, h, left, right, top, bottom } = chartMetrics(el);
  const plotW = w - left - right;
  const plotH = h - top - bottom;

  const target = probe.cook ? probe.cook.pull : null;
  const values = rows.flatMap((r) => [r[1], r[2]]).filter((v) => v !== null);
  if (target !== null) values.push(target);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  const pad = Math.max(4, (hi - lo) * 0.1);
  lo -= pad;
  hi += pad;

  const t0 = rows[0][0];
  const t1 = Math.max(rows[rows.length - 1][0], t0 + 1);
  const x = (t) => left + ((t - t0) / (t1 - t0)) * plotW;
  const y = (v) => top + plotH - ((v - lo) / (hi - lo)) * plotH;

  const line = (idx) =>
    rows
      .filter((r) => r[idx] !== null)
      .map((r) => `${x(r[0]).toFixed(1)},${y(r[idx]).toFixed(1)}`)
      .join(" ");

  const ticks = niceTicks(lo, hi, 4);
  const grid = ticks
    .map(
      (v) =>
        `<line x1="${left}" x2="${left + plotW}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="var(--grid)" stroke-width="1" vector-effect="non-scaling-stroke"/>` +
        `<text x="${left - 6}" y="${(y(v) + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--text-muted)">${Math.round(v)}</text>`
    )
    .join("");

  // The target label sits just inside the plot rather than in the right
  // gutter, where a narrow phone would clip it.
  const targetMark =
    target === null
      ? ""
      : `<line x1="${left}" x2="${left + plotW}" y1="${y(target).toFixed(1)}" y2="${y(target).toFixed(1)}"
            stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4 3" vector-effect="non-scaling-stroke"/>
         <text x="${left + 3}" y="${(y(target) - 4).toFixed(1)}" font-size="10" fill="var(--text-muted)">remove at ${fmtTemp(target, 0)}${unitSuffix()}</text>`;

  const last = rows[rows.length - 1];
  // Direct labels at the live end of each series, so identity never rests on
  // colour alone. The two series sit on top of each other whenever the meat
  // has reached the air temperature around it, which is most of the time
  // before the cook starts, so nudge them apart rather than letting them
  // overprint into an unreadable smudge.
  const endLabels = (() => {
    const x0 = left + plotW + 4;
    let tipY = y(last[1]);
    const label = (ly, text) =>
      `<text x="${x0}" y="${(ly + 3.5).toFixed(1)}" font-size="10" font-weight="600" fill="var(--text-secondary)">${text}</text>`;
    if (last[2] === null) return label(tipY, "tip");

    let ambY = y(last[2]);
    const MIN_GAP = 11;
    if (Math.abs(tipY - ambY) < MIN_GAP) {
      const mid = (tipY + ambY) / 2;
      tipY = mid - MIN_GAP / 2;
      ambY = mid + MIN_GAP / 2;
    }
    return label(tipY, "tip") + label(ambY, "amb");
  })();

  const span = fmtDuration(t1 - t0) || "";
  el.innerHTML = `
<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Tip and ambient temperature over time">
  ${grid}
  ${targetMark}
  <polyline fill="none" stroke="var(--series-ambient)" stroke-width="2" stroke-linejoin="round"
            stroke-linecap="round" vector-effect="non-scaling-stroke" points="${line(2)}"/>
  <polyline fill="none" stroke="var(--series-tip)" stroke-width="2" stroke-linejoin="round"
            stroke-linecap="round" vector-effect="non-scaling-stroke" points="${line(1)}"/>
  <circle cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="4"
          fill="var(--series-tip)" stroke="var(--surface-1)" stroke-width="2" vector-effect="non-scaling-stroke"/>
  ${endLabels}
  <text x="${left}" y="${h - 5}" font-size="10" fill="var(--text-muted)">${span} ago</text>
  <text x="${left + plotW}" y="${h - 5}" font-size="10" text-anchor="end" fill="var(--text-muted)">now</text>
  <line data-role="crosshair" y1="${top}" y2="${top + plotH}" x1="0" x2="0"
        stroke="var(--text-muted)" stroke-width="1" vector-effect="non-scaling-stroke" opacity="0"/>
  <rect data-role="hit" x="${left}" y="${top}" width="${plotW}" height="${plotH}" fill="transparent"/>
</svg>`;

  tableEl.innerHTML = buildTable(rows);
  wireChartHover(el, tooltipEl, rows, { x, y, t0, t1, left, plotW, w });
}

function buildTable(rows) {
  // Thin to a readable number of rows; the chart carries the full resolution.
  const step = Math.max(1, Math.ceil(rows.length / 24));
  const body = rows
    .filter((_, i) => i % step === 0 || i === rows.length - 1)
    .map(
      (r) =>
        `<tr><td>${fmtClock(r[0])}</td><td>${fmtTemp(r[1])}</td><td>${r[2] === null ? "–" : fmtTemp(r[2], 0)}</td></tr>`
    )
    .join("");
  return `<table class="data"><thead><tr><th>Time</th><th>Tip ${unitSuffix()}</th><th>Ambient ${unitSuffix()}</th></tr></thead><tbody>${body}</tbody></table>`;
}

function wireChartHover(el, tooltipEl, rows, geo) {
  const svg = el.querySelector("svg");
  const hit = svg.querySelector('[data-role="hit"]');
  const crosshair = svg.querySelector('[data-role="crosshair"]');

  const move = (event) => {
    const box = svg.getBoundingClientRect();
    const scale = geo.w / box.width;
    const svgX = (event.clientX - box.left) * scale;
    const frac = Math.max(0, Math.min(1, (svgX - geo.left) / geo.plotW));
    const when = geo.t0 + frac * (geo.t1 - geo.t0);

    let best = rows[0];
    let bestGap = Infinity;
    for (const row of rows) {
      const gap = Math.abs(row[0] - when);
      if (gap < bestGap) { bestGap = gap; best = row; }
    }

    crosshair.setAttribute("x1", geo.x(best[0]).toFixed(1));
    crosshair.setAttribute("x2", geo.x(best[0]).toFixed(1));
    crosshair.setAttribute("opacity", "0.55");

    tooltipEl.innerHTML =
      `${fmtClock(best[0])}<br>Tip <b>${fmtTemp(best[1])}${unitSuffix()}</b>` +
      (best[2] === null ? "" : `<br>Ambient <b>${fmtTemp(best[2], 0)}${unitSuffix()}</b>`);
    tooltipEl.classList.add("show");
    const wrapBox = el.closest(".chart-wrap").getBoundingClientRect();
    const px = (geo.x(best[0]) / geo.w) * box.width + (box.left - wrapBox.left);
    tooltipEl.style.left = `${Math.max(0, Math.min(px + 10, wrapBox.width - tooltipEl.offsetWidth - 2))}px`;
    tooltipEl.style.top = `${box.top - wrapBox.top + 6}px`;
  };

  const leave = () => {
    crosshair.setAttribute("opacity", "0");
    tooltipEl.classList.remove("show");
  };

  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerdown", move);
  hit.addEventListener("pointerleave", leave);
  hit.addEventListener("pointercancel", leave);
}

function niceTicks(lo, hi, count) {
  const raw = (hi - lo) / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || magnitude * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}

/* ------------------------------------------------------------- cook picker */

async function loadCatalog() {
  if (state.catalog) return state.catalog;
  const response = await fetch("api/presets");
  state.catalog = (await response.json()).categories;
  return state.catalog;
}

async function openPicker(index) {
  const catalog = await loadCatalog();
  state.pickerSlot = index;
  renderPickerCuts(catalog, index);
  $("#picker").showModal();
}

/* Step one: what cut is it?  The picker is two steps rather than one long
   page, because appending the doneness choice under sixty-odd presets means
   scrolling past all of them on a phone to answer the second question. */
function renderPickerCuts(catalog, index) {
  $("#picker-title").textContent = `What is on probe ${index}?`;
  const body = $("#picker-body");
  body.innerHTML = catalog
    .map(
      (group) => `
<h3>${group.category}</h3>
<div class="opts">
  ${group.presets
    .map((preset) => `<button type="button" data-preset="${preset.id}">${preset.label}</button>`)
    .join("")}
</div>`
    )
    .join("");
  body.scrollTop = 0;
  body.querySelectorAll("[data-preset]").forEach((button) => {
    button.addEventListener("click", () => choosePreset(button.dataset.preset));
  });
}

function findPreset(presetId) {
  for (const group of state.catalog) {
    const found = group.presets.find((p) => p.id === presetId);
    if (found) return found;
  }
  return null;
}

/* Step two: how do you want it?  Replaces the cut list rather than extending
   it, so the answer is always the first thing on screen. */
function choosePreset(presetId) {
  const preset = findPreset(presetId);
  if (!preset) return;

  const toUnits = (c) => (state.units === "F" ? Math.round((c * 9) / 5 + 32) : c.toFixed(1));
  const single = preset.donenesses.length === 1;
  $("#picker-title").textContent = `${preset.label}`;

  const body = $("#picker-body");
  body.innerHTML = `
<div class="opts" style="margin-top:12px">
  <button type="button" id="picker-back">&#8249; All cuts</button>
</div>
<h3>${single ? "Start this cook" : "How do you want it?"}</h3>
<div class="opts">
  ${preset.donenesses
    .map(
      (d) =>
        `<button type="button" data-doneness="${d.id}">${d.label}<small>${toUnits(d.target_c)}${unitSuffix()}</small></button>`
    )
    .join("")}
</div>
${preset.note ? `<p class="note">${preset.note}</p>` : ""}
${
  preset.carryover_c > 0
    ? `<p class="note">Comes off the heat ${preset.carryover_c}\u00b0C early so carryover finishes it${preset.rest_minutes ? `, then rests ${Math.round(preset.rest_minutes)} min` : ""}.</p>`
    : preset.rest_minutes
      ? `<p class="note">Rests ${Math.round(preset.rest_minutes)} min before serving.</p>`
      : ""
}
<h3>Or set your own</h3>
<div class="custom-row">
  <input type="number" id="custom-target" step="0.5" inputmode="decimal" placeholder="Target ${unitSuffix()}">
  <button class="btn" type="button" id="custom-go">Use this</button>
</div>`;
  body.scrollTop = 0;

  $("#picker-back").addEventListener("click", () =>
    renderPickerCuts(state.catalog, state.pickerSlot)
  );
  body.querySelectorAll("[data-doneness]").forEach((button) => {
    button.addEventListener("click", () => startCook(preset.id, button.dataset.doneness, null));
  });
  $("#custom-go").addEventListener("click", () => {
    const value = parseFloat($("#custom-target").value);
    if (!Number.isFinite(value)) return;
    startCook(preset.id, preset.donenesses[0].id, value);
  });
}

async function startCook(presetId, donenessId, target) {
  const index = state.pickerSlot;
  await post(`api/probe/${index}/cook`, {
    preset_id: presetId,
    doneness_id: donenessId,
    target,
  });
  $("#picker").close();
}

/* -------------------------------------------------------------- transport */

async function post(path, body, method = "POST") {
  try {
    const response = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      console.warn("request failed", path, response.status, await response.text());
    }
    return response.ok;
  } catch (error) {
    console.warn("request error", path, error);
    return false;
  }
}

function wireCards() {
  probesEl.querySelectorAll("[data-act]").forEach((button) => {
    const card = button.closest("section");
    const index = card.dataset.index;
    button.addEventListener("click", () => {
      if (button.dataset.act === "choose") openPicker(index);
      else if (button.dataset.act === "rest") post(`api/probe/${index}/rest`);
      else if (button.dataset.act === "clear") post(`api/probe/${index}/cook`, undefined, "DELETE");
    });
  });
}

let eventSource = null;
let retryDelay = 1000;

function connect() {
  eventSource = new EventSource("api/events");
  eventSource.onopen = () => {
    retryDelay = 1000;
    connectionEl.hidden = true;
  };
  eventSource.onmessage = (event) => {
    try {
      render(JSON.parse(event.data));
    } catch (error) {
      console.warn("bad snapshot", error);
    }
  };
  eventSource.onerror = () => {
    // EventSource retries on its own, but only on some failures; take it over
    // so a laptop waking from sleep or a phone changing wifi reconnects too.
    eventSource.close();
    connectionEl.hidden = false;
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 1.7, 20000);
  };
}

/* ------------------------------------------------------- units, theme, alerts */

function setUnits(units, push = true) {
  state.units = units;
  $("#unit-c").setAttribute("aria-pressed", String(units === "C"));
  $("#unit-f").setAttribute("aria-pressed", String(units === "F"));
  if (push) post("api/units", { units });
}

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  $("#theme").setAttribute("aria-pressed", String(theme === "dark"));
  try {
    if (theme) localStorage.setItem("theme", theme);
    else localStorage.removeItem("theme");
  } catch { /* private browsing */ }
}

/* Alarms.  A cook reaching "ready" or "done" starts an alarm that repeats
   until someone dismisses it, or until the cook moves on (rest started,
   cleared), so a glance away from the screen cannot miss it. */

const ALARM_REPEAT_MS = 2500;
const alarms = new Map();   // slot id -> { message, kind }
let alarmTimer = null;
let audioCtx = null;

function maybeAlert(probe) {
  const previous = state.lastCookState[probe.slot_id];
  const current = probe.cook ? probe.cook.state : "idle";
  state.lastCookState[probe.slot_id] = current;

  // The cook moved on: whatever was ringing for it is no longer news.
  const ringing = alarms.get(probe.slot_id);
  if (ringing && ringing.state !== current) stopAlarm(probe.slot_id);

  if (!previous || previous === current) return;

  let message = null;
  let kind = "ready";
  if (current === "ready") message = `${probe.cook.label} is ready to come off the heat.`;
  else if (current === "done") {
    message = `${probe.cook.label} has finished resting.`;
    kind = "done";
  }
  if (!message) return;

  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(probe.name, { body: message, tag: probe.slot_id, requireInteraction: true });
  }
  startAlarm(probe.slot_id, { title: probe.name, message, kind, state: current });
}

function startAlarm(slotId, alarm) {
  alarms.set(slotId, alarm);
  renderAlarm();
  if (!alarmTimer) {
    ringOnce();
    alarmTimer = setInterval(ringOnce, ALARM_REPEAT_MS);
  }
  if (navigator.vibrate) navigator.vibrate([400, 200, 400, 200, 400]);
}

function stopAlarm(slotId) {
  if (slotId === undefined) alarms.clear();
  else alarms.delete(slotId);
  renderAlarm();
  if (!alarms.size && alarmTimer) {
    clearInterval(alarmTimer);
    alarmTimer = null;
  }
}

function renderAlarm() {
  const banner = $("#alarm");
  if (!alarms.size) {
    banner.hidden = true;
    document.documentElement.classList.remove("alarming");
    return;
  }
  const items = [...alarms.values()];
  banner.dataset.kind = items.some((a) => a.kind === "ready") ? "ready" : "done";
  $("#alarm-text").replaceChildren(
    ...items.map((a) => {
      const line = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = a.title;
      line.append(name, " \u2014 ", a.message);
      return line;
    })
  );
  banner.hidden = false;
  document.documentElement.classList.add("alarming");
}

function audio() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return null;
  if (!audioCtx) audioCtx = new Ctx();
  if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  return audioCtx;
}

/* Three rising tones: distinct from anything else in a kitchen. */
function ringOnce() {
  try {
    const ctx = audio();
    if (!ctx) return;
    const urgent = [...alarms.values()].some((a) => a.kind === "ready");
    const notes = urgent ? [880, 1175, 1480] : [660, 880];
    notes.forEach((freq, i) => {
      const t = ctx.currentTime + i * 0.28;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "square";
      osc.frequency.value = freq;
      osc.connect(gain).connect(ctx.destination);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.35, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.24);
      osc.start(t);
      osc.stop(t + 0.26);
    });
  } catch { /* autoplay blocked until the page is interacted with */ }
}

/* Keep a phone awake while it is showing a live cook, so you can stand it
   against the grill and glance at it. */
async function refreshWakeLock() {
  if (!("wakeLock" in navigator)) return;
  const cooking = (state.snapshot?.probes || []).some(
    (p) => p.cook && (p.cook.state === "cooking" || p.cook.state === "ready")
  );
  try {
    if (cooking && !state.wakeLock && document.visibilityState === "visible") {
      state.wakeLock = await navigator.wakeLock.request("screen");
      state.wakeLock.addEventListener("release", () => { state.wakeLock = null; });
    } else if (!cooking && state.wakeLock) {
      await state.wakeLock.release();
      state.wakeLock = null;
    }
  } catch { /* denied, or not allowed in this context */ }
}

/* ------------------------------------------------------------------- start */

try {
  applyTheme(localStorage.getItem("theme"));
} catch { /* ignore */ }

$("#unit-c").addEventListener("click", () => setUnits("C"));
$("#unit-f").addEventListener("click", () => setUnits("F"));
$("#theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme === "dark";
  applyTheme(dark ? "light" : "dark");
});

$("#alarm-dismiss").addEventListener("click", () => stopAlarm());

// Unlock audio on the first interaction, in case the browser requires one.
document.addEventListener("pointerdown", () => audio(), { once: true });

// Ask for notification permission on the first real interaction, which is the
// only time browsers will grant it.
document.addEventListener(
  "pointerdown",
  () => {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  },
  { once: true }
);

document.addEventListener("visibilitychange", () => {
  refreshWakeLock();
  if (document.visibilityState === "visible" && eventSource?.readyState === 2) {
    connect();
  }
});
setInterval(refreshWakeLock, 15000);

// The chart is sized to its container, so a resize or a rotation needs a
// redraw; the snapshot we already have is enough to do it.
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (state.snapshot) render(state.snapshot);
  }, 150);
});

fetch("api/state")
  .then((r) => r.json())
  .then(render)
  .catch(() => {});
connect();
