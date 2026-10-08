/* ============================================================
   Tritium BMS web dashboard — front-end logic (vanilla JS, no deps)
   Renders live telemetry (polled from /api/state) and replays CSV logs.
   ============================================================ */
'use strict';

const $  = (id) => document.getElementById(id);
const POLL_MS = 500;
const CHART_WINDOW = 240;           // points shown per chart

/* ---------- formatting helpers ---------- */
const isNum = (v) => typeof v === 'number' && isFinite(v);
const dash = '—';
function fmt(v, d = 0)  { return isNum(v) ? v.toFixed(d) : dash; }
function ival(v)        { return (v === null || v === undefined) ? dash : String(v); }
function loc(cmu, cell) {
  if (!isNum(cmu)) return '';
  const c = String(cmu).padStart(2, '0');
  if (isNum(cell)) return `@ N${c}:C${String(cell).padStart(2, '0')}`;
  return `@ N${c}`;
}

async function getJSON(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

/* ============================================================
   Canvas strip chart (auto-scaling, DPR-aware) — pure renderer
   ============================================================ */
class Chart {
  constructor(canvas, { series, unit = '', fmt = (v) => v.toFixed(0) }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.series = series;         // [{name, color}]
    this.unit = unit;
    this.fmt = fmt;
    this.last = null;             // last data drawn (for resize redraw)
  }

  draw(data) {
    this.last = data;
    const c = this.ctx, cv = this.canvas;
    const css = getComputedStyle(document.documentElement);
    const gridCol = css.getPropertyValue('--border').trim() || '#252b36';
    const faint   = css.getPropertyValue('--faint').trim()  || '#7b8698';

    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (w < 20 || h < 20) return;
    if (cv.width !== w * dpr || cv.height !== h * dpr) {
      cv.width = w * dpr; cv.height = h * dpr;
    }
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);

    // title
    c.fillStyle = faint;
    c.font = '600 11px system-ui, sans-serif';
    c.textBaseline = 'top';
    c.fillText(this.title || '', 6, 4);

    const all = [];
    for (const s of this.series) for (const v of (data[s.name] || [])) if (isNum(v)) all.push(v);
    if (!all.length) {
      c.fillStyle = faint;
      c.font = '11px system-ui, sans-serif';
      c.textAlign = 'center';
      c.fillText('waiting for data…', w / 2, h / 2 - 6);
      c.textAlign = 'left';
      return;
    }

    let vmin = Math.min(...all), vmax = Math.max(...all);
    if (vmin === vmax) { vmin -= 1; vmax += 1; }
    const pad = (vmax - vmin) * 0.14; vmin -= pad; vmax += pad;

    const padL = 48, padR = 8, padT = 20, padB = 8;
    const pw = w - padL - padR, ph = h - padT - padB;
    const X = (i, n) => padL + pw * (i / Math.max(n - 1, 1));
    const Y = (v) => padT + ph * (1 - (v - vmin) / (vmax - vmin));

    // grid + y labels
    c.strokeStyle = gridCol; c.fillStyle = faint;
    c.font = '10px ui-monospace, monospace'; c.textAlign = 'right'; c.textBaseline = 'middle';
    for (const frac of [0, 0.5, 1]) {
      const val = vmin + (vmax - vmin) * frac, yy = Y(val);
      c.beginPath(); c.moveTo(padL, yy); c.lineTo(w - padR, yy); c.stroke();
      c.fillText(this.fmt(val), padL - 5, yy);
    }
    c.textAlign = 'left'; c.textBaseline = 'top';

    // series lines + legend
    let lx = padL + 6, ly = 5;
    for (const s of this.series) {
      const d = data[s.name] || [], n = d.length;
      c.strokeStyle = s.color; c.lineWidth = 2; c.beginPath();
      let started = false, last = null;
      for (let i = 0; i < n; i++) {
        const v = d[i];
        if (!isNum(v)) { started = false; continue; }
        const x = X(i, n), y = Y(v);
        if (!started) { c.moveTo(x, y); started = true; } else c.lineTo(x, y);
        last = v;
      }
      c.stroke();
      if (last !== null) {
        c.fillStyle = s.color; c.font = '700 11px ui-monospace, monospace';
        const t = `${s.name}: ${this.fmt(last)}${this.unit}`;
        c.fillText(t, lx, ly); lx += c.measureText(t).width + 14;
      }
    }
  }
}

/* ============================================================
   Chart set (live + replay share the class, separate instances)
   ============================================================ */
function makeCharts(suffix) {
  const v = new Chart($('chV' + suffix), {
    series: [{ name: 'min', color: '#4aa3ff' }, { name: 'max', color: '#ff7a45' }],
    unit: ' mV', fmt: (x) => x.toFixed(0),
  });
  v.title = 'Cell voltage (mV)';
  const pv = new Chart($('chPV' + suffix), {
    series: [{ name: 'pack', color: '#37c871' }], unit: ' mV', fmt: (x) => x.toFixed(0),
  });
  pv.title = 'Pack voltage (mV)';
  const i = new Chart($('chI' + suffix), {
    series: [{ name: 'current', color: '#ffd23f' }], unit: ' mA', fmt: (x) => x.toFixed(0),
  });
  i.title = 'Pack current (mA)';
  return { v, pv, i };
}

/* ============================================================
   Rendering (shared by live + replay via a suffix)
   ============================================================ */
function fillColor(pct) {
  if (pct >= 60) return 'var(--green)';
  if (pct >= 30) return 'var(--yellow)';
  if (pct >= 15) return 'var(--orange)';
  return 'var(--red)';
}

function renderBattery(view, sfx) {
  const b = view.battery || {};
  const soc = b.soc_v;
  $('batFill' + sfx).style.width = isNum(soc) ? `${soc}%` : '0%';
  $('batFill' + sfx).style.background = isNum(soc) ? fillColor(soc) : 'var(--faint)';
  $('socNum' + sfx).textContent = isNum(soc) ? `${soc.toFixed(0)}%` : dash;

  const kw = isNum(b.power_w) ? b.power_w / 1000 : null;
  const powerEl = $('roPower' + sfx);
  powerEl.textContent = isNum(kw) ? `${kw >= 0 ? '+' : ''}${kw.toFixed(2)} kW` : dash;
  powerEl.style.color = isNum(kw) && Math.abs(kw) >= 0.001
    ? (kw > 0 ? 'var(--orange)' : 'var(--green)') : '';

  $('roPackV' + sfx).textContent = isNum(b.pack_v) ? `${b.pack_v.toFixed(1)} V` : dash;
  $('roEnergy' + sfx).textContent = isNum(b.energy_kwh) ? `${b.energy_kwh.toFixed(2)} kWh` : dash;
  $('roCharge' + sfx).textContent = isNum(b.remaining_ah)
    ? `${b.remaining_ah.toFixed(1)}${isNum(b.capacity_ah) ? ` / ${b.capacity_ah} Ah` : ' Ah'}` : dash;
  $('roAvg' + sfx).textContent = isNum(b.avg_mv) ? `${b.avg_mv.toFixed(0)} mV` : dash;
  $('roSocBms' + sfx).textContent = isNum(b.soc_bms) ? `${b.soc_bms.toFixed(0)}%` : dash;

  const st = $('roState' + sfx);
  const dir = b.direction;
  st.textContent = dir || dash;
  st.style.color = dir === 'DISCHARGING' ? 'var(--orange)'
    : dir === 'CHARGING' ? 'var(--green)'
    : dir === 'IDLE' ? 'var(--muted)' : '';
}

function tile(k, v, s, cls = '', warn = false) {
  return `<div class="tile${warn ? ' warn' : ''}"><div class="k">${k}</div>` +
         `<div class="v ${cls}">${v}</div><div class="s">${s || ''}</div></div>`;
}

function renderTiles(view, sfx) {
  const p = view.pack || {};
  const dMv = (isNum(p.max_mv) && isNum(p.min_mv)) ? p.max_mv - p.min_mv : null;
  const warnFlags = (p.status_flag_names || []).filter((n) => n !== 'CMU PWR OK');
  const packA = isNum(p.pack_ma) ? p.pack_ma / 1000 : null;

  const html = [
    tile('Min cell', `${ival(p.min_mv)}<small> mV</small>`, loc(p.min_v_cmu, p.min_v_cell), 'blue'),
    tile('Max cell', `${ival(p.max_mv)}<small> mV</small>`, loc(p.max_v_cmu, p.max_v_cell), 'orange'),
    tile('Cell spread', `${ival(dMv)}<small> mV</small>`, 'max − min'),
    tile('Min temp', `${fmt(p.min_c, 1)}<small> °C</small>`, loc(p.min_c_cmu)),
    tile('Max temp', `${fmt(p.max_c, 1)}<small> °C</small>`, loc(p.max_c_cmu),
         isNum(p.max_c) && p.max_c >= 45 ? 'orange' : ''),
    tile('Pack voltage', `${isNum(p.pack_mv) ? (p.pack_mv / 1000).toFixed(2) : dash}<small> V</small>`,
         `${ival(p.pack_mv)} mV`),
    tile('Pack current', `${isNum(packA) ? (packA >= 0 ? '+' : '') + packA.toFixed(2) : dash}<small> A</small>`,
         `${ival(p.pack_ma)} mA`, packA === null ? '' : (packA > 0.1 ? 'orange' : packA < -0.1 ? 'green' : '')),
    tile('Balance thr.', `${ival(p.bal_rising)}/${ival(p.bal_falling)}`, 'rising / falling mV'),
    tile('CMU count', ival(p.cmu_count), ''),
    tile('Precharge', p.prechg_name || dash, isNum(p.prechg_state) ? `state ${p.prechg_state}` : ''),
    tile('Flags', warnFlags.length ? warnFlags.join(', ') : (p.status_flag_names || []).join(', ') || '(none)',
         isNum(p.status_flags) ? `0x${p.status_flags.toString(16).toUpperCase().padStart(2, '0')}` : '',
         warnFlags.length ? 'red' : '', warnFlags.length > 0),
    tile('Fans', `${ival(p.fan0)}/${ival(p.fan1)}`, 'rpm'),
    tile('SOC (BMS)', `${fmt(p.soc_pct, 1)}<small> %</small>`, isNum(p.soc_ah) ? `${p.soc_ah.toFixed(2)} Ah` : ''),
    tile('Balance SOC', `${fmt(p.bal_pct, 1)}<small> %</small>`, isNum(p.bal_ah) ? `${p.bal_ah.toFixed(2)} Ah` : ''),
    tile('Capacity', `${ival(p.capacity_ah)}<small> Ah</small>`, ''),
    tile('Firmware', ival(p.fw_build), 'build'),
  ].join('');
  $('tiles' + sfx).innerHTML = html;
}

function renderCmuTable(view, sfx) {
  const cmus = view.cmus || [];
  const head = $('cmuHead' + sfx), body = $('cmuBody' + sfx);
  const emptyEl = $('cmuEmpty');

  if (!cmus.length) {
    head.innerHTML = ''; body.innerHTML = '';
    if (sfx === '' && emptyEl) emptyEl.style.display = 'block';
    return;
  }
  if (sfx === '' && emptyEl) emptyEl.style.display = 'none';

  // present cell columns = union of non-null cell indices
  const present = new Set();
  for (const c of cmus) (c.cells || []).forEach((v, i) => { if (isNum(v)) present.add(i); });
  const cellIdx = [...present].sort((a, b) => a - b);

  const p = view.pack || {};
  const alarmCmus = new Set(view.alarms ? view.alarms.cmus : []);

  head.innerHTML = '<tr><th>CMU</th><th>Serial</th><th>PCB °C</th><th>Cell °C</th>' +
    cellIdx.map((i) => `<th>C${i} mV</th>`).join('') + '</tr>';

  body.innerHTML = cmus.map((c) => {
    const rowAlarm = alarmCmus.has(c.n) ? ' class="row-alarm"' : '';
    const cells = cellIdx.map((i) => {
      const v = (c.cells || [])[i];
      let cls = '';
      if (c.n === p.min_v_cmu && i === p.min_v_cell) cls = ' class="cell-min"';
      else if (c.n === p.max_v_cmu && i === p.max_v_cell) cls = ' class="cell-max"';
      return `<td${cls}>${isNum(v) ? v : ''}</td>`;
    }).join('');
    return `<tr${rowAlarm}><td class="cmu-name">CMU ${c.n}</td>` +
           `<td>${ival(c.serial)}</td><td>${fmt(c.pcb, 1)}</td><td>${fmt(c.cell, 1)}</td>${cells}</tr>`;
  }).join('');
}

function renderView(view, sfx) {
  renderBattery(view, sfx);
  renderTiles(view, sfx);
  renderCmuTable(view, sfx);
}

/* ---------- alarm banner + sound ---------- */
let soundOn = false, audioCtx = null, lastBeep = 0;
function beep() {
  if (!soundOn) return;
  const now = Date.now();
  if (now - lastBeep < 3000) return;
  lastBeep = now;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = 880; o.type = 'square';
    g.gain.value = 0.06; o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + 0.2);
  } catch (e) { /* ignore */ }
}
function updateBanner(view) {
  const el = $('alarmBanner');
  const list = view && view.alarms ? view.alarms.list : [];
  if (list && list.length) {
    const shown = list.slice(0, 5).join('   ');
    const more = list.length > 5 ? `   (+${list.length - 5} more)` : '';
    el.textContent = '⚠ ALARM:  ' + shown + more;
    el.hidden = false;
    beep();
  } else {
    el.hidden = true;
  }
}

/* ============================================================
   Live controller
   ============================================================ */
const live = {
  charts: null,
  hist: { min: [], max: [], pack: [], current: [] },
  lastFc: null, lastT: null, rate: 0,
  view: null,
};

function pushHist(view) {
  const p = view.pack || {};
  const H = live.hist;
  H.min.push(p.min_mv); H.max.push(p.max_mv);
  H.pack.push(p.pack_mv); H.current.push(p.pack_ma);
  for (const k of Object.keys(H)) while (H[k].length > CHART_WINDOW) H[k].shift();
}
function drawLiveCharts() {
  const H = live.hist;
  live.charts.v.draw({ min: H.min, max: H.max });
  live.charts.pv.draw({ pack: H.pack });
  live.charts.i.draw({ current: H.current });
}

async function backfillHistory() {
  try {
    const { history } = await getJSON('/api/history');
    const tail = history.slice(-CHART_WINDOW);
    live.hist = {
      min: tail.map((r) => r.min_mv), max: tail.map((r) => r.max_mv),
      pack: tail.map((r) => r.pack_mv), current: tail.map((r) => r.pack_ma),
    };
  } catch (e) { /* start empty */ }
}

function setConn(view) {
  const c = view.connection || { status: 'nodata' };
  const pill = $('connPill');
  pill.className = 'pill ' + c.status;
  const labels = { live: 'LIVE', slow: 'SLOW', stale: 'STALE', nodata: 'NO DATA' };
  let txt = labels[c.status] || c.status.toUpperCase();
  if (c.status !== 'live' && c.status !== 'nodata' && isNum(c.age_s)) txt += ` · ${c.age_s.toFixed(0)}s`;
  $('connText').textContent = txt;

  // frame rate (client-side derived)
  const now = performance.now();
  if (live.lastFc !== null && live.lastT !== null) {
    const dt = (now - live.lastT) / 1000;
    if (dt > 0) live.rate = (view.frame_count - live.lastFc) / dt;
  }
  live.lastFc = view.frame_count; live.lastT = now;
  const age = isNum(c.age_ms) ? (c.age_ms < 1000 ? `${c.age_ms.toFixed(0)} ms` : `${c.age_s.toFixed(1)} s`) : dash;
  $('frameMeta').textContent = `${view.frame_count} frames · ${live.rate.toFixed(0)} fr/s · ${age} ago`;

  const src = `${view.mode ? view.mode.toUpperCase() + ' · ' : ''}${view.source || ''}  (base ${view.base || '?'})`;
  $('sourceLabel').textContent = src;
}

async function pollLive() {
  try {
    const view = await getJSON('/api/state');
    live.view = view;
    setConn(view);
    pushHist(view);
    if (activeTab === 'live') {
      renderView(view, '');
      drawLiveCharts();
      updateBanner(view);
    }
  } catch (e) {
    $('connPill').className = 'pill nodata';
    $('connText').textContent = 'SERVER?';
  } finally {
    setTimeout(pollLive, POLL_MS);
  }
}

/* ============================================================
   Replay controller
   ============================================================ */
const replay = {
  charts: null, data: null, idx: 0, timer: null, playing: false,
};

async function loadLogsList() {
  try {
    const { logs } = await getJSON('/api/logs');
    const sel = $('logSelect');
    if (!logs.length) { sel.innerHTML = '<option value="">(no logs found in ./logs)</option>'; return; }
    sel.innerHTML = logs.map((l) => {
      const kb = (l.size / 1024).toFixed(0);
      const dt = new Date(l.mtime * 1000).toLocaleString();
      return `<option value="${l.name}">${l.name}  ·  ${kb} KB  ·  ${dt}</option>`;
    }).join('');
  } catch (e) {
    $('logSelect').innerHTML = '<option value="">(failed to list logs)</option>';
  }
}

async function loadLog() {
  const name = $('logSelect').value;
  if (!name) return;
  $('logMeta').textContent = 'loading…';
  stopReplay();
  try {
    const data = await getJSON('/api/log?name=' + encodeURIComponent(name));
    if (data.error) { $('logMeta').textContent = 'error: ' + data.error; return; }
    replay.data = data; replay.idx = 0;
    $('scrub').max = String(Math.max(0, data.samples.length - 1));
    $('scrub').value = '0';
    $('transport').hidden = false;
    const note = data.downsampled ? `  ·  downsampled ×${data.step} (${data.count} rows → ${data.returned})` : '';
    $('logMeta').textContent = `${data.count} rows${note}`;
    showReplayFrame(0);
  } catch (e) {
    $('logMeta').textContent = 'failed to load: ' + e.message;
  }
}

function replayCharts(idx) {
  const s = replay.data.samples;
  const start = Math.max(0, idx - CHART_WINDOW + 1);
  const win = s.slice(start, idx + 1);
  const pick = (f) => win.map((v) => f(v.pack));
  replay.charts.v.draw({ min: pick((p) => p.min_mv), max: pick((p) => p.max_mv) });
  replay.charts.pv.draw({ pack: pick((p) => p.pack_mv) });
  replay.charts.i.draw({ current: pick((p) => p.pack_ma) });
}

function showReplayFrame(idx) {
  const d = replay.data;
  if (!d || !d.samples.length) return;
  idx = Math.max(0, Math.min(idx, d.samples.length - 1));
  replay.idx = idx;
  const view = d.samples[idx];
  renderView(view, '_r');
  replayCharts(idx);
  $('scrub').value = String(idx);
  const t = view.iso_time ? view.iso_time.replace('T', '  ') : (isNum(view.ts) ? new Date(view.ts * 1000).toLocaleString() : '');
  $('replayTime').textContent = `${idx + 1} / ${d.samples.length}   ${t}`;
  if (activeTab === 'replay') updateBanner(view);
}

function stepReplay() {
  if (!replay.data) return;
  if (replay.idx >= replay.data.samples.length - 1) { stopReplay(); return; }
  showReplayFrame(replay.idx + 1);
}
function startReplay() {
  if (!replay.data) return;
  if (replay.idx >= replay.data.samples.length - 1) replay.idx = 0;
  replay.playing = true; $('playBtn').textContent = '⏸ Pause';
  scheduleReplay();
}
function scheduleReplay() {
  clearInterval(replay.timer);
  const speed = parseFloat($('speedSel').value) || 1;
  const step = (replay.data && replay.data.step) || 1;
  const interval = Math.max(16, 1000 * step / speed);   // each sample ≈ `step` seconds
  replay.timer = setInterval(stepReplay, interval);
}
function stopReplay() {
  replay.playing = false; clearInterval(replay.timer); replay.timer = null;
  $('playBtn').textContent = '▶ Play';
}
function toggleReplay() { replay.playing ? stopReplay() : startReplay(); }

/* ============================================================
   Tabs / theme / wiring
   ============================================================ */
let activeTab = 'live';
function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('is-active', b.dataset.tab === tab));
  $('tab-live').hidden = tab !== 'live';
  $('tab-replay').hidden = tab !== 'replay';
  // refresh the visible view + its banner
  if (tab === 'live' && live.view) { renderView(live.view, ''); drawLiveCharts(); updateBanner(live.view); }
  if (tab === 'replay') {
    if (replay.data) { showReplayFrame(replay.idx); }
    else { $('alarmBanner').hidden = true; }
  }
}

function initTheme() {
  const saved = localStorage.getItem('bms-theme');
  const theme = saved || 'dark';
  document.documentElement.setAttribute('data-theme', theme);
  $('themeBtn').textContent = theme === 'dark' ? '☀️' : '🌙';
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('bms-theme', next);
  $('themeBtn').textContent = next === 'dark' ? '☀️' : '🌙';
  // redraw charts (colors are theme-derived)
  if (live.charts) drawLiveCharts();
  if (replay.data) replayCharts(replay.idx);
}

function initSound() {
  const saved = localStorage.getItem('bms-sound') === '1';
  soundOn = saved;
  $('soundBtn').textContent = soundOn ? '🔊' : '🔇';
}
function toggleSound() {
  soundOn = !soundOn;
  localStorage.setItem('bms-sound', soundOn ? '1' : '0');
  $('soundBtn').textContent = soundOn ? '🔊' : '🔇';
  if (soundOn) { lastBeep = 0; beep(); }   // gesture unlocks audio + confirms
}

function wire() {
  document.querySelectorAll('.tab').forEach((b) =>
    b.addEventListener('click', () => switchTab(b.dataset.tab)));
  $('themeBtn').addEventListener('click', toggleTheme);
  $('soundBtn').addEventListener('click', toggleSound);

  $('logLoad').addEventListener('click', loadLog);
  $('playBtn').addEventListener('click', toggleReplay);
  $('scrub').addEventListener('input', (e) => { stopReplay(); showReplayFrame(parseInt(e.target.value, 10)); });
  $('speedSel').addEventListener('change', () => { if (replay.playing) scheduleReplay(); });

  let rz;
  window.addEventListener('resize', () => {
    clearTimeout(rz);
    rz = setTimeout(() => {
      if (activeTab === 'live' && live.charts) drawLiveCharts();
      if (activeTab === 'replay' && replay.data) replayCharts(replay.idx);
    }, 120);
  });
}

/* ---------- boot ---------- */
async function main() {
  initTheme(); initSound(); wire();
  live.charts = makeCharts('');
  replay.charts = makeCharts('_r');
  await backfillHistory();
  loadLogsList();
  pollLive();
}
document.addEventListener('DOMContentLoaded', main);
