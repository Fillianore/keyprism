/** Multi-lane DL workspace (Phase 3): Demucs stems + polyphonic notes.
 *
 *  Fetches /api/stems?method=demucs_4|demucs_6 (background task: POST,
 *  then polls /api/task/{id} — long separations never hold an HTTP
 *  request open), decodes every stem WAV into the SHARED AudioContext
 *  owned by player.js and renders one stacked lane per stem. Gain
 *  architecture lives in the shared MixerState (mixer.js): every lane
 *  feeds the MASTER GainNode (−10 dB ceiling) before the destination and
 *  the DAW mute/solo matrix is computed in ONE place. Row DOM (label +
 *  icon, slider, M/S, scope cell) comes from the shared factory in
 *  controls.js so the two panels can never drift apart visually.
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
 *  Mixer defaults (same as the Phase 2 stem player): loading lanes never
 *  changes what the user hears — the Mix keeps playing, every Demucs
 *  lane starts MUTED until unmuted/soloed; unmuting any lane silences
 *  the Mix (its content is inside the lanes — summing both would clip).
 *
 *  Lane rendering (Phase 3.6 polish):
 *  - each decoded buffer's peak envelope (min/max per column, ~1024
 *    columns) is computed ONCE, cached on the lane, and drawn to the
 *    waveform canvas IMMEDIATELY — content is visible before playback
 *    and redrawn on resize (throttled) and plotly_relayout;
 *  - a per-lane decode failure renders an i18n placeholder inside that
 *    lane instead of failing the whole panel;
 *  - the Notes (扒谱) button is uniform on every poly-eligible lane and
 *    is strictly LAZY: availability is checked on click (awaiting the
 *    /api/ping capabilities) and reported as a dismissible toast with
 *    the precise reason (onnxruntime missing vs basic-pitch unavailable
 *    on Python ≥ 3.12) — never auto-triggered on panel load.
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
import { MixerState } from './mixer.js';
import { trackRow, wireMuteSolo } from './controls.js';
import { showToast } from './toast.js';
import { throttled } from './util.js';

const START_LEAD = 0.06; // keep identical to player.js START_LEAD
const POLL_MS = 700;

/** Fixed DL stem contract per method (mirrors
 *  keyprism.dlsep.STEM_SPECS — the frontend never reads Python data).
 *  The /api/stems response MUST match EXACTLY this list, in this order,
 *  for every file length. */
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

/** Cached peak-envelope resolution: min/max per column, computed once
 *  per decoded buffer and drawn at any zoom (D2). */
const ENVELOPE_COLUMNS = 1024;

/** Raised when the lazy (click-time) capabilities check reports the
 *  Notes feature unavailable; `kind` selects the precise toast. */
class PolyUnavailable extends Error {
  constructor(kind) {
    super(kind);
    this.kind = kind;
  }
}

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
  const mixer = new MixerState(ctx, (norm, opts) =>
    player.setMixGain(norm, opts)
  );

  const state = {
    enabled: false,
    caps: null, // /api/ping capabilities (dl/poly availability)
    capsReady: null, // promise resolving once caps are known
    loading: false,
    ready: false,
    method: 'demucs_4',
    savedMix: null,
    rows: [], // {model, row, vol, mute, solo} mirrors for repaint
    lanes: [], // mixer strips + canvas fields (peaks, idx, waveCv, noteCv)
    view: {
      aMs: EPOCH_MS,
      bMs: EPOCH_MS + Math.round(data.durationSec * 1000),
    },
  };

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
    if (lane.decodeFailed) {
      // per-lane decode failure: placeholder inside the lane (D2)
      g.fillStyle = 'rgba(255,255,255,0.4)';
      g.font = '11px system-ui, sans-serif';
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText(t('laneDecodeFailed'), w / 2, mid);
      return;
    }
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
  /** Cached peak envelope: min/max per column (~ENVELOPE_COLUMNS),
   *  computed once per decoded buffer — the waveform canvas paints from
   *  this at any zoom without re-scanning the PCM (D2). */
  function buildEnvelope(buffer) {
    const ch = buffer.getChannelData(0);
    const n = ENVELOPE_COLUMNS;
    const bucket = Math.max(1, Math.floor(ch.length / n));
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

  function startAll(offset, when) {
    stopAll();
    for (const s of mixer.strips) {
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
    for (const s of mixer.strips) {
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
    } else if (type === 'mixgain' && mixer.mix && !mixer.mix.muted) {
      mixer.mix.volume = info.norm;
      const volEl = state.rows.find((r) => r.model === mixer.mix)?.vol;
      if (volEl && document.activeElement !== volEl) {
        volEl.value = String(info.norm);
        paintFill(volEl);
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

  /** Why a row is currently inaudible (tooltip copy for D6) */
  function suppressionTip(model, anySolo) {
    if (anySolo && !model.solo) return t('soloSuppressedTip');
    return model === mixer.mix ? t('mixerMixDuckedTip') : t('mixerMutedTip');
  }

  /** Repaint the matrix on the rows: a lane silenced by OTHERS' solo or
   *  by the anti-clipping mix rule is shown `.dimmed` WITH a tooltip
   *  explaining why; the M/S buttons keep reflecting the USER's own
   *  toggle state only. */
  function paintStates() {
    const anySolo = mixer.anySolo;
    for (const { model, row } of state.rows) {
      const audible =
        model === mixer.mix ? mixer.mixAudible() : mixer.stripAudible(model);
      row.classList.toggle('dimmed', !audible);
      row.title = audible ? '' : suppressionTip(model, anySolo);
    }
  }
  mixer.onRepaint(paintStates);

  /** Notes-button availability (D4/D5): once capabilities are known the
   *  button is disabled with the precise reason as its tooltip; while
   *  unknown it stays clickable and the click-time check toasts. */
  function applyNotesAvailability(lane) {
    const px = lane.notesBtn;
    if (!px || !state.caps) return;
    if (!state.caps.dl) {
      px.disabled = true;
      px.title = t('polyNeedsDL');
    } else if (!state.caps.poly) {
      px.disabled = true;
      px.title = t('polyNeedsBP');
    } else {
      px.disabled = false;
      px.title = t('laneNotesTitle');
    }
  }

  /** Notes (扒谱) toggle: STRICTLY lazy — the capabilities check runs on
   *  click, never on panel load; unavailability surfaces as a
   *  dismissible toast with the precise reason, then the button is
   *  disabled with the same tooltip. */
  async function toggleNotes(lane) {
    const px = lane.notesBtn;
    if (lane.idx) {
      lane.idx = null;
      px.classList.remove('active');
      drawNotes(lane);
      return;
    }
    px.disabled = true;
    try {
      await state.capsReady;
      if (!state.caps || !state.caps.dl) throw new PolyUnavailable('dl');
      if (!state.caps.poly) throw new PolyUnavailable('bp');
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
      if (e instanceof PolyUnavailable) {
        showToast(
          e.kind === 'dl' ? t('polyNeedsDL') : t('polyNeedsBP')
        );
      } else {
        showToast(t('laneNotesFailed', { msg: e.message }));
      }
    } finally {
      applyNotesAvailability(lane);
      if (state.caps && state.caps.dl && state.caps.poly) {
        px.disabled = false;
      }
    }
  }

  function buildLaneRow(container, lane) {
    const parts = trackRow({
      key: lane.key,
      labelText: t(lane.labelKey),
      color: lane.color,
      withLane: true,
    });
    const { row, controls, scope, vol, mute, solo } = parts;
    wireMuteSolo({
      model: lane,
      vol,
      mute,
      solo,
      apply: () => mixer.apply(),
      paintFill,
    });
    state.rows.push({ model: lane, row, vol, mute, solo });
    // Notes (扒谱): uniform on every poly-eligible lane (D4)
    if (POLY_LANES.has(lane.key)) {
      const px = document.createElement('button');
      px.type = 'button';
      px.className = 'stem-btn lane-notes-btn';
      px.textContent = t('laneNotes');
      px.title = t('laneNotesTitle');
      px.addEventListener('click', () => toggleNotes(lane));
      lane.notesBtn = px;
      controls.appendChild(px);
      applyNotesAvailability(lane);
    }
    const wave = document.createElement('canvas');
    wave.className = 'lane-wave';
    const noteCv = document.createElement('canvas');
    noteCv.className = 'lane-notes';
    noteCv.setAttribute('aria-hidden', 'true');
    scope.append(wave, noteCv);
    lane.waveCv = wave;
    lane.noteCv = noteCv;
    container.appendChild(row);
  }

  function renderShell() {
    panel.innerHTML = '';
    state.rows = [];
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
    // Mix lane: the player's own playback, ridden by the master gain.
    // The scope cell stays empty (no canvas) and keeps the grid aligned.
    const mix = trackRow({
      key: 'mix',
      labelText: t('laneMix'),
      color: '#ddd6c8',
      withLane: true,
    });
    mix.row.classList.add('lane-row-mix');
    mix.scope.classList.add('lane-scope-empty');
    wireMuteSolo({
      model: mixer.mix,
      vol: mix.vol,
      mute: mix.mute,
      solo: mix.solo,
      apply: () => mixer.apply(),
      paintFill,
    });
    mix.vol.value = String(player.mixGainNorm());
    paintFill(mix.vol);
    panel.append(mix.row);
    state.rows.push({
      model: mixer.mix,
      row: mix.row,
      vol: mix.vol,
      mute: mix.mute,
      solo: mix.solo,
    });

    for (const lane of state.lanes) buildLaneRow(panel, lane);
    paintStates();
    syncFromPlot(null); // draw into the fresh canvases
  }

  function teardownLanes() {
    stopAll();
    mixer.unroute();
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
    let lanes = [];
    try {
      const list = await requestStems(method);
      // Strict DL stem contract: exactly the fixed registry keys, in
      // order, for every file length — a server that answers
      // chunk-count-dependent stems fails loudly instead of rendering
      // mystery lanes
      const expected = DL_METHODS[method];
      const keys = (list.stems || []).map((s) => s.key);
      if (
        keys.length !== expected.length ||
        keys.some((k, i) => k !== expected[i])
      ) {
        throw new Error(
          `unexpected stems for ${method}: ${keys.join(', ')}`
        );
      }
      for (const item of list.stems) {
        const meta = LANE_META[item.key] || {};
        // makeStrip defaults: MUTED (gain 0) — loading lanes never
        // changes what the user currently hears
        const lane = mixer.makeStrip(item.key, {
          key: item.key,
          color: meta.color || '#ccc',
          labelKey: meta.labelKey || item.key,
          peaks: null,
          idx: null,
          waveCv: null,
          noteCv: null,
          notesBtn: null,
          decodeFailed: false,
        });
        lanes.push(lane);
        try {
          lane.buffer = await decodeStem(item.url);
          lane.peaks = buildEnvelope(lane.buffer);
        } catch {
          // per-lane tolerance: placeholder inside THIS lane, the rest
          // of the panel still loads (D2)
          lane.decodeFailed = true;
        }
      }
      if (state.method !== method) return; // switched away mid-load
      mixer.route(lanes); // wire into the master bus only when complete
      lanes = [];
      state.lanes = mixer.strips;
      state.ready = true;
      // The Mix keeps playing at its current volume; the Mix lane fader
      // mirrors it (lanes stay muted until the user opens one)
      mixer.mix.volume = player.mixGainNorm();
      mixer.mix.muted = false;
      mixer.mix.solo = false;
      renderShell();
      mixer.apply();
      drawAll(); // waveforms visible immediately, before Play (D2)
      if (player.isPlaying()) {
        // jump in synced at the current position (short 60 ms handover)
        startAll(player.currentTime(), ctx.currentTime + START_LEAD);
      }
      setStatus('');
    } catch (e) {
      if (state.method === method) {
        state.ready = false;
        stopAll();
        state.lanes = [];
        renderShell();
        setStatus(t('lanesFailed', { msg: e.message }));
      }
    } finally {
      // a failed/partial load leaves no gain nodes wired anywhere
      lanes.forEach((l) => l.gain.disconnect());
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
    mixer.mix.solo = false;
    // hand the output back to the mix at the pre-enable volume
    if (state.savedMix !== null) {
      player.setMixGain(state.savedMix, { persist: true });
    }
    mixer.mix.volume = state.savedMix ?? mixer.mix.volume;
    mixer.mix.muted = false;
  }

  toggle.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-lanes]');
    if (!btn || btn.disabled) return;
    const want = btn.dataset.lanes === 'on';
    const turning = (want && !state.enabled) || (!want && state.enabled);
    if (!turning) return;
    if (want && state.caps && !state.caps.dl) {
      // DL extra missing: do NOT flip the toggle — a visually "On"
      // button over an empty panel read as an unresponsive switch
      setStatus(t('lanesNeedDL'));
      return;
    }
    toggle
      .querySelectorAll('button')
      .forEach((b) => b.classList.toggle('active', b === btn));
    if (want) enable();
    else disable();
  });

  // ---- capabilities: resolved ONCE and exposed as a promise so the
  // Notes click can await it lazily (never auto-triggering transcription
  // on panel load); the toggle gets the remedy tooltip when DL is
  // missing and existing Notes buttons get their precise state.
  state.capsReady = (async () => {
    try {
      const r = await fetch(`${apiBase}/api/ping`);
      const body = await r.json();
      state.caps = body.capabilities || { dl: false, poly: false };
    } catch {
      state.caps = { dl: false, poly: false };
    }
    if (state.caps && !state.caps.dl) {
      const onBtn = toggle.querySelector('button[data-lanes="on"]');
      if (onBtn) onBtn.title = t('lanesNeedDL');
    }
    for (const lane of state.lanes) applyNotesAvailability(lane);
    if (state.enabled) renderShell();
  })();

  // ---- keep the lanes glued to the main spectrogram's time axis ----
  gd.on('plotly_relayout', syncFromPlot);
  new ResizeObserver(throttled(() => scheduleDraw())).observe(panel);

  // Live language switch: rebuild the panel (state is kept in models)
  onChange(() => {
    if (state.enabled) renderShell();
  });

  return {};
}
