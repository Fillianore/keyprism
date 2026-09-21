/** Multi-lane DL workspace (Phase 3): Demucs stems + polyphonic notes.
 *
 *  Fetches /api/stems?method=demucs_4|demucs_6 (background task: POST,
 *  then polls /api/task/{id} — long separations never hold an HTTP
 *  request open), decodes every stem WAV into the SHARED AudioContext
 *  owned by player.js and renders one stacked lane per stem.
 *
 *  Sync design (inherited from the Phase 2 contract):
 *  - one shared AudioContext: all stems + the mix live on the same
 *    hardware clock;
 *  - the transport emits ('play', {offset, when}) with ONE absolute
 *    timestamp `when = ctx.currentTime + START_LEAD`; every stem's
 *    AudioBufferSourceNode is started with source.start(when, offset) —
 *    never serial awaits, never setTimeout — so all lanes begin on the
 *    same audio frame and stay sample-locked;
 *  - volume/mute/solo only ramp GainNodes (setTargetAtTime, ~12 ms),
 *    which is click- and pop-free; scheduling is never touched.
 *
 *  Rendering design (why canvas, never layout.shapes):
 *  - six lanes of polyphonic notes would mean tens of thousands of SVG
 *    rects — Plotly layout.shapes would freeze the page. Each lane owns
 *    a waveform <canvas> plus a dedicated notes overlay <canvas>
 *    positioned absolutely on top of it;
 *  - the canvases are synced to the main spectrogram's time axis by
 *    listening to plotly_relayout, reading xaxis.range and recomputing
 *    the time→pixel transform (ctx.setTransform for device pixels,
 *    time-to-x mapping for the data) before redrawing — the same
 *    applied-range read player.js uses for its HTML playhead;
 *  - note drawing uses a SPATIAL INDEX (starts sorted ascending +
 *    binary search bounded by the longest note) so a zoom only touches
 *    the visible window's notes, never the whole track.
 */

import { t, onChange } from './i18n.js';
import { EPOCH_MS, pMs } from './spectrogram.js';

const START_LEAD = 0.06; // keep identical to player.js START_LEAD
const POLL_MS = 700;

const DL_METHODS = {
  demucs_4: ['drums', 'bass', 'other', 'vocals'],
  demucs_6: ['drums', 'bass', 'other', 'vocals', 'piano', 'guitar'],
};

// demucs_6 stems eligible for polyphonic transcription (mirrors
// keyprism.poly_transcribe.POLY_TRACKS — the frontend never reads
// Python data)
const POLY_LANES = new Set(['piano', 'guitar', 'other']);

const LANE_META = {
  mix: { labelKey: 'laneMix', color: '#ddd6c8' },
  vocals: { labelKey: 'laneVocals', color: '#e2c48a' },
  drums: { labelKey: 'laneDrums', color: '#d98d7e' },
  bass: { labelKey: 'laneBass', color: '#5ac8fa' },
  piano: { labelKey: 'lanePiano', color: '#ffb74d' },
  guitar: { labelKey: 'laneGuitar', color: '#95d5a2' },
  other: { labelKey: 'laneOther', color: '#8fb8d8' },
};

const PITCH_LO = 21; // A0 — bottom edge of the lane
const PITCH_HI = 108; // C8 — top edge of the lane

export function initLanes({ gd, data, player, apiBase }) {
  const toggle = document.getElementById('lanesToggle');
  const panel = document.getElementById('lanesPanel');
  if (!toggle || !panel) return {};
  if (!apiBase) {
    // static mode: lanes are a backend feature
    toggle.querySelectorAll('button').forEach((b) => (b.disabled = true));
    return {};
  }

  const ctx = player.getContext(); // ONE shared AudioContext, one clock

  const state = {
    enabled: false,
    caps: null, // /api/ping capabilities (dl availability)
    loading: false,
    ready: false,
    method: 'demucs_4',
    savedMix: null,
    lanes: [], // {key, buffer, gain, src, volume, muted, solo, peaks, idx, waveCv, noteCv}
    view: {
      aMs: EPOCH_MS,
      bMs: EPOCH_MS + Math.round(data.durationSec * 1000),
    },
  };
  const mixRow = { volume: 1, muted: true, solo: false, slider: null };

  // ------------------------------------------------- spatial note index
  function buildNoteIndex(notes) {
    const sorted = [...notes].sort((a, b) => a.start - b.start);
    let maxDur = 0;
    for (const n of sorted) maxDur = Math.max(maxDur, n.end - n.start);
    return { sorted, maxDur };
  }

  /** First index in `sorted` with start >= x (binary search) */
  function lowerBound(sorted, x) {
    let lo = 0;
    let hi = sorted.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (sorted[mid].start < x) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  /** Notes overlapping [aSec, bSec]: binary search anchored at
   *  aSec - maxDur (any note ending after aSec must start after that)
   *  and scanned until starts pass bSec — O(log n + visible) */
  function visibleNotes(idx, aSec, bSec) {
    const out = [];
    for (
      let i = lowerBound(idx.sorted, aSec - idx.maxDur);
      i < idx.sorted.length && idx.sorted[i].start <= bSec;
      i++
    ) {
      const n = idx.sorted[i];
      if (n.end >= aSec) out.push(n);
    }
    return out;
  }

  // ------------------------------------------------------ view <-> px
  function viewSec() {
    return [
      (state.view.aMs - EPOCH_MS) / 1000,
      (state.view.bMs - EPOCH_MS) / 1000,
    ];
  }

  function xOf(tSec, aMs, bMs, w) {
    return ((EPOCH_MS + tSec * 1000 - aMs) / (bMs - aMs)) * w;
  }

  /** Canvas backing store sized for the device pixels; the 2D context
   *  transform (ctx.setTransform = scale+translate) is the whole
   *  zoom/pan sync — data coords are mapped per draw from the applied
   *  plotly range */
  function fitCanvas(cv) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, cv.clientWidth);
    const h = Math.max(1, cv.clientHeight);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    return g;
  }

  /** Applied plotly xaxis range -> lane view; wired to plotly_relayout
   *  below (same three event shapes player.js handles) */
  function syncFromPlot(e) {
    let r = null;
    if (e && e['xaxis.range']) r = e['xaxis.range'];
    else if (e && e['xaxis.range[0]'] !== undefined) {
      r = [e['xaxis.range[0]'], e['xaxis.range[1]']];
    } else if (!e || e['xaxis.autorange']) {
      r = gd._fullLayout && gd._fullLayout.xaxis
        ? gd._fullLayout.xaxis.range
        : null;
    } else {
      r = gd._fullLayout && gd._fullLayout.xaxis
        ? gd._fullLayout.xaxis.range
        : null;
    }
    if (!r) return;
    state.view.aMs = pMs(r[0]);
    state.view.bMs = pMs(r[1]);
    scheduleDraw();
  }

  let drawRaf = 0;
  function scheduleDraw() {
    if (!state.enabled || drawRaf) return;
    drawRaf = requestAnimationFrame(() => {
      drawRaf = 0;
      drawAll();
    });
  }

  function drawAll() {
    if (!state.enabled) return;
    for (const lane of state.lanes) {
      drawWave(lane);
      drawNotes(lane);
    }
  }

  function drawWave(lane) {
    const cv = lane.waveCv;
    if (!cv || !cv.clientWidth) return;
    const g = fitCanvas(cv);
    const w = cv.clientWidth;
    const h = cv.clientHeight;
    g.clearRect(0, 0, w, h);
    const mid = h / 2;
    g.fillStyle = 'rgba(255,255,255,0.06)';
    g.fillRect(0, mid, w, 1);
    if (!lane.peaks) return;
    const [aSec, bSec] = viewSec();
    const span = Math.max(bSec - aSec, 1e-6);
    const p = lane.peaks;
    const nB = p.max.length;
    g.fillStyle = lane.color;
    for (let px = 0; px < w; px++) {
      const ta = aSec + (px / w) * span;
      const tb = aSec + ((px + 1) / w) * span;
      let i0 = Math.floor(ta / lane.bucketSec);
      let i1 = Math.max(i0 + 1, Math.ceil(tb / lane.bucketSec));
      i0 = Math.max(0, Math.min(nB, i0));
      i1 = Math.max(0, Math.min(nB, i1));
      let lo = 0;
      let hi = 0;
      for (let i = i0; i < i1; i++) {
        if (p.min[i] < lo) lo = p.min[i];
        if (p.max[i] > hi) hi = p.max[i];
      }
      if (hi <= 0 && lo >= 0) continue;
      const y0 = mid - hi * (mid * 0.92);
      const y1 = mid - lo * (mid * 0.92);
      g.fillRect(px, y0, 1, Math.max(1, y1 - y0));
    }
  }

  function drawNotes(lane) {
    const cv = lane.noteCv;
    if (!cv || !cv.clientWidth) return;
    const g = fitCanvas(cv);
    const w = cv.clientWidth;
    const h = cv.clientHeight;
    g.clearRect(0, 0, w, h);
    if (!lane.idx) return;
    const [aSec, bSec] = viewSec();
    const span = Math.max(bSec - aSec, 1e-6);
    const nH = Math.max(2, Math.min(6, h / 24));
    for (const n of visibleNotes(lane.idx, aSec, bSec)) {
      const x0 = xOf(n.start, state.view.aMs, state.view.bMs, w);
      const x1 = xOf(n.end, state.view.aMs, state.view.bMs, w);
      if (x1 < 0 || x0 > w) continue;
      const cx0 = Math.max(x0, 0);
      const cx1 = Math.min(x1, w);
      const y = ((PITCH_HI - n.pitch) / (PITCH_HI - PITCH_LO)) * (h - nH);
      const a = 0.35 + 0.55 * Math.min(1, Math.max(0, n.conf || 0));
      g.fillStyle = hexToRgba(lane.color, a);
      g.fillRect(cx0, y, Math.max(1.5, cx1 - cx0), nH);
    }
  }

  function hexToRgba(hex, alpha) {
    const v = parseInt(hex.slice(1), 16);
    return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${alpha})`;
  }

  // -------------------------------------------------------------- audio
  function buildPeaks(buffer) {
    const ch = buffer.getChannelData(0);
    const bucket = 2048;
    const n = Math.ceil(ch.length / bucket);
    const min = new Float32Array(n);
    const max = new Float32Array(n);
    for (let b = 0; b < n; b++) {
      let lo = 0;
      let hi = 0;
      const s = b * bucket;
      const e = Math.min(ch.length, s + bucket);
      for (let i = s; i < e; i++) {
        const v = ch[i];
        if (v < lo) lo = v;
        else if (v > hi) hi = v;
      }
      min[b] = lo;
      max[b] = hi;
    }
    return { min, max, bucketSec: bucket / buffer.sampleRate };
  }

  function applyGains() {
    const anySolo = state.lanes.some((s) => s.solo);
    for (const s of state.lanes) {
      const audible = anySolo ? s.solo : !s.muted;
      s.gain.gain.setTargetAtTime(
        audible ? s.volume : 0,
        ctx.currentTime,
        0.012
      );
    }
    // the Mix lane rides the master gain (the player's own playback)
    const mixAudible = anySolo ? false : !mixRow.muted;
    player.setMixGain(mixAudible ? mixRow.volume : 0, { persist: false });
  }

  function startAll(offset, when) {
    stopAll();
    for (const s of state.lanes) {
      if (!s.buffer) continue;
      const end = s.buffer.duration - 0.005;
      if (offset >= end) continue;
      const src = ctx.createBufferSource();
      src.buffer = s.buffer;
      src.connect(s.gain);
      // THE sync primitive: one absolute timestamp for every lane
      src.start(when, Math.min(Math.max(offset, 0), end));
      s.src = src;
    }
  }

  function stopAll() {
    for (const s of state.lanes) {
      if (!s.src) continue;
      s.src.onended = null;
      try {
        s.src.stop();
      } catch {
        /* already stopped */
      }
      s.src.disconnect();
      s.src = null;
    }
  }

  player.onTransport((type, info) => {
    if (!state.enabled) return;
    if (type === 'play' && state.ready) {
      startAll(info.offset, info.when);
    } else if (type === 'pause') {
      stopAll();
    } else if (type === 'mixgain' && mixRow.slider && !mixRow.muted) {
      mixRow.volume = info.norm;
      if (document.activeElement !== mixRow.slider) {
        mixRow.slider.value = String(info.norm);
        paintFill(mixRow.slider);
      }
    }
  });

  // --------------------------------------------------------------- data
  async function decodeStem(url) {
    const ab = await (await fetch(url)).arrayBuffer();
    return await ctx.decodeAudioData(ab); // shared context: same clock
  }

  function setStatus(text, busy = false) {
    const el = panel.querySelector('.lanes-status');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('busy', busy);
  }

  /** POST starts the separation task, then poll /api/task/{id} until
   *  done — a cached method short-circuits to its stem list at once */
  async function requestStems(method) {
    const r = await fetch(`${apiBase}/api/stems?method=${method}`, {
      method: 'POST',
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    if (body.cached || !body.task_id) return body;
    const taskUrl = `${apiBase}${body.status_url}`;
    for (;;) {
      if (!state.enabled || state.method !== method) {
        throw new Error('cancelled');
      }
      const res = await fetch(taskUrl);
      const tb = await res.json();
      if (tb.status === 'done') return tb;
      if (tb.status === 'error') throw new Error(tb.error || 'task failed');
      setStatus(
        t('lanesSeparating', { pct: Math.round((tb.progress || 0) * 100) }),
        true
      );
      await new Promise((ok) => setTimeout(ok, POLL_MS));
    }
  }

  // ---------------------------------------------------------------- UI
  function paintFill(el) {
    const min = parseFloat(el.min);
    const max = parseFloat(el.max);
    const v = parseFloat(el.value);
    const p =
      isFinite(min) && isFinite(max) && max > min && isFinite(v)
        ? ((v - min) / (max - min)) * 100
        : 0;
    el.style.setProperty('--fill', `${Math.min(Math.max(p, 0), 100)}%`);
  }

  function buildControls(container, model, onVolume) {
    const vol = document.createElement('input');
    vol.type = 'range';
    vol.className = 'stem-vol';
    vol.min = '0';
    vol.max = '1';
    vol.step = '0.01';
    vol.value = String(model.volume);
    const mute = document.createElement('button');
    mute.type = 'button';
    mute.className = 'stem-btn';
    mute.textContent = 'M';
    mute.title = t('mute');
    mute.setAttribute('aria-label', t('mute'));
    const solo = document.createElement('button');
    solo.type = 'button';
    solo.className = 'stem-btn';
    solo.textContent = 'S';
    solo.title = t('solo');
    solo.setAttribute('aria-label', t('solo'));
    mute.classList.toggle('active', model.muted);
    solo.classList.toggle('active', model.solo);
    mute.addEventListener('click', () => {
      model.muted = !model.muted;
      mute.classList.toggle('active', model.muted);
      applyGains();
    });
    solo.addEventListener('click', () => {
      model.solo = !model.solo;
      solo.classList.toggle('active', model.solo);
      applyGains();
    });
    vol.addEventListener('input', () => {
      model.volume = parseFloat(vol.value);
      paintFill(vol);
      onVolume();
    });
    container.append(vol, mute, solo);
    paintFill(vol);
  }

  function buildLaneRow(container, lane) {
    const row = document.createElement('div');
    row.className = 'lane-row';
    const head = document.createElement('div');
    head.className = 'lane-head';
    const name = document.createElement('span');
    name.className = 'stem-name';
    name.textContent = t(lane.labelKey);
    name.style.setProperty('--stem-color', lane.color);
    head.appendChild(name);
    buildControls(head, lane, () => applyGains());
    // polyphonic notes overlay toggle (Basic Pitch, demucs_6 stems)
    if (POLY_LANES.has(lane.key)) {
      const px = document.createElement('button');
      px.type = 'button';
      px.className = 'stem-btn lane-notes-btn';
      px.textContent = 'PX';
      px.title = t('laneNotesTitle');
      px.addEventListener('click', async () => {
        if (lane.idx) {
          lane.idx = null;
          px.classList.remove('active');
          drawNotes(lane);
          return;
        }
        px.disabled = true;
        try {
          const r = await fetch(
            `${apiBase}/api/notes?track=${lane.key}&method=poly` +
              `&source=${state.method}`
          );
          const body = await r.json();
          if (!r.ok || body.error) {
            throw new Error(body.error || `HTTP ${r.status}`);
          }
          lane.idx = buildNoteIndex(body.notes[lane.key] || []);
          px.classList.add('active');
          drawNotes(lane);
        } catch (e) {
          setStatus(t('laneNotesFailed', { msg: e.message }));
        } finally {
          px.disabled = false;
        }
      });
      head.appendChild(px);
    }
    const scope = document.createElement('div');
    scope.className = 'lane-scope';
    const wave = document.createElement('canvas');
    wave.className = 'lane-wave';
    const notes = document.createElement('canvas');
    notes.className = 'lane-notes';
    notes.setAttribute('aria-hidden', 'true');
    scope.append(wave, notes);
    row.append(head, scope);
    container.appendChild(row);
    lane.waveCv = wave;
    lane.noteCv = notes;
  }

  function renderShell() {
    panel.innerHTML = '';
    const title = document.createElement('span');
    title.className = 'stems-title';
    title.textContent = t('lanes');
    const methodSel = document.createElement('select');
    methodSel.className = 'stems-method';
    const dlOk = !!state.caps && state.caps.dl;
    for (const m of Object.keys(DL_METHODS)) {
      const o = document.createElement('option');
      o.value = m;
      o.textContent = t(`lanesMethod_${m}`);
      if (m === state.method) o.selected = true;
      methodSel.appendChild(o);
    }
    methodSel.disabled = state.loading || !dlOk;
    if (!dlOk) {
      methodSel.title = t('lanesNeedDL');
    }
    methodSel.addEventListener('change', () => {
      if (!methodSel.disabled && methodSel.value !== state.method) {
        load(methodSel.value);
      }
    });
    const status = document.createElement('span');
    status.className = 'lanes-status stems-status';
    panel.append(title, methodSel, status);

    if (!state.ready) return;
    // Mix lane: the player's own playback, ridden by the master gain
    const mixUi = document.createElement('div');
    mixUi.className = 'lane-row lane-row-mix';
    const mixHead = document.createElement('div');
    mixHead.className = 'lane-head';
    const mixName = document.createElement('span');
    mixName.className = 'stem-name stem-row-mix';
    mixName.textContent = t('laneMix');
    mixHead.appendChild(mixName);
    buildControls(mixHead, mixRow, () => applyGains());
    panel.appendChild(mixUi);
    mixRow.slider = mixUi.querySelector('.stem-vol');
    mixRow.slider.value = String(player.mixGainNorm());
    paintFill(mixRow.slider);

    for (const lane of state.lanes) buildLaneRow(panel, lane);
    syncFromPlot(null); // draw into the fresh canvases
  }

  function teardownLanes() {
    stopAll();
    state.lanes.forEach((s) => s.gain.disconnect());
    state.lanes = [];
  }

  async function load(method) {
    if (state.loading) return;
    state.loading = true;
    state.method = method;
    state.ready = false;
    teardownLanes();
    renderShell();
    setStatus(t('lanesSeparating', { pct: 0 }), true);
    try {
      const list = await requestStems(method);
      const lanes = [];
      for (const item of list.stems) {
        const buffer = await decodeStem(item.url);
        const meta = LANE_META[item.key] || {};
        lanes.push({
          key: item.key,
          color: meta.color || '#ccc',
          labelKey: meta.labelKey || item.key,
          buffer,
          peaks: buildPeaks(buffer),
          gain: ctx.createGain(),
          src: null,
          volume: 1,
          muted: false,
          solo: false,
          idx: null,
          waveCv: null,
          noteCv: null,
        });
      }
      if (state.method !== method) return; // switched away mid-load
      lanes.forEach((l) => l.gain.connect(ctx.destination));
      state.lanes = lanes;
      state.ready = true;
      mixRow.muted = true; // lanes take over the transport's output
      mixRow.solo = false;
      renderShell();
      applyGains();
      if (player.isPlaying()) {
        // jump in synced at the current position (short 60 ms handover)
        startAll(player.currentTime(), ctx.currentTime + START_LEAD);
      }
      setStatus('');
    } catch (e) {
      if (state.method === method) {
        state.ready = false;
        teardownLanes();
        renderShell();
        setStatus(t('lanesFailed', { msg: e.message }));
      }
    } finally {
      state.loading = false;
    }
  }

  function enable() {
    state.enabled = true;
    state.savedMix = player.mixGainNorm();
    panel.hidden = false;
    load(state.method);
  }

  function disable() {
    state.enabled = false;
    teardownLanes();
    state.ready = false;
    state.loading = false;
    panel.hidden = true;
    // hand the output back to the mix at the pre-enable volume
    mixRow.muted = true;
    if (state.savedMix !== null) {
      player.setMixGain(state.savedMix, { persist: true });
    }
  }

  toggle.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-lanes]');
    if (!btn || btn.disabled) return;
    const want = btn.dataset.lanes === 'on';
    toggle
      .querySelectorAll('button')
      .forEach((b) => b.classList.toggle('active', b === btn));
    if (want && !state.enabled) {
      if (state.caps && !state.caps.dl) {
        setStatus(t('lanesNeedDL'));
        return;
      }
      enable();
    } else if (!want && state.enabled) {
      disable();
    }
  });

  // ---- capabilities: hide the Demucs options when the [dl] extra is
  // not installed (graceful degradation contract)
  (async () => {
    try {
      const r = await fetch(`${apiBase}/api/ping`);
      const body = await r.json();
      state.caps = body.capabilities || { dl: false, poly: false };
    } catch {
      state.caps = { dl: false, poly: false };
    }
    if (state.enabled) renderShell();
  })();

  // ---- keep the lanes glued to the main spectrogram's time axis ----
  gd.on('plotly_relayout', syncFromPlot);
  new ResizeObserver(scheduleDraw).observe(panel);

  // Live language switch: rebuild the panel (state is kept in models)
  onChange(() => {
    if (state.enabled) renderShell();
  });

  return {};
}
