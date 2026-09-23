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
 *  Solo is DESTRUCTIVE (Phase 3.7 state machine in mixer.js): it
 *  materializes as real mute states on every other row, and unmuting
 *  any row releases it. Each lane fader defaults to its make-up gain
 *  (mix peak / lane peak, clamped to +12 dB); the master bus carries a
 *  brickwall limiter so all-open lanes cannot clip.
 *
 *  Lane rendering (Phase 3.6 polish):
 *  - each decoded buffer's peak envelope (min/max per column, ~1024
 *    columns) is computed ONCE, cached on the lane, and drawn to the
 *    waveform canvas IMMEDIATELY — content is visible before playback
 *    and redrawn on resize (throttled) and plotly_relayout. The
 *    envelope auto-scales to THAT lane's own peak (plus a tiny peak-dB
 *    label) so quiet stems render visible waveforms; muting only DIMS
 *    the envelope (35% alpha) — silence is never invisible (3.8 D1);
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
 *
 *  Phase 3.9 lane visualization engine:
 *  - SHARED PLOT GEOMETRY (geometry.js): the master spectrogram's plot
 *    area and every lane canvas span the identical pixel boundaries
 *    [PLOT_LEFT_PX, W - PLOT_RIGHT_PX] — the lane grid columns are
 *    derived from the same CSS custom properties the master margins are
 *    written from, so a drum hit lands on the same vertical line in
 *    both. Lanes redraw from the master's plotly_relayout with the same
 *    time→pixel mapping (xOf below);
 *  - lane PLAYHEADS are thin DOM hairlines (same technique as the
 *    master cursor), moved by the master cursor's own repaint path via
 *    player.onFrame — ONE time source (ctx.currentTime), zero drift,
 *    and playback never repaints a single canvas pixel;
 *  - HIGH-RES WAVEFORMS (LOD): the cached full-track envelope stays for
 *    the overview; when the visible window is <= LOD_SPAN_SEC the
 *    per-pixel-column min/max are computed ON DEMAND from the stem PCM
 *    inside a Web Worker (wavelod.js — the main thread never scans
 *    samples), cached per [lane, view window, column count] with a
 *    small FIFO bound, and painted pixel-sharp. The worker owns one
 *    transferred mono copy per lane, so zoom/pan requests ship only the
 *    view window;
 *  - [WAVE|SPEC] per-lane toggle (Phase 3.9): the lane canvas flips to a
 *    mini-spectrogram served by GET /api/stem_spec (the Phase 0 semitone
 *    aggregate of the stem, the SAME row space as the master heatmap),
 *    fetched lazily on first click (Notes-chip discipline) through the
 *    shared stemspec.js cache; a failure toasts and degrades to Wave.
 */

import { t, onChange } from './i18n.js';
import { EPOCH_MS, pMs } from './spectrogram.js';
import { MixerState, bufferPeak, makeupDb } from './mixer.js';
import { trackRow, wireMuteSolo } from './controls.js';
import { showToast } from './toast.js';
import { WaveLod } from './wavelod.js';
import { getStemSpec, createSpecImage } from './stemspec.js';
import { openWithMethod } from './stems.js';
import { throttled } from './util.js';

const START_LEAD = 0.06; // keep identical to player.js START_LEAD
const POLL_MS = 700;

/** Inference quality tiers (3.10): server-side demucs shifts —
 *  fast = 1 pass, balanced = 2, best = 3 (compute ~linear in passes).
 *  Mirrors keyprism.dlsep.QUALITY_SHIFTS. */
const QUALITY_TIERS = ['fast', 'balanced', 'best'];
const QUALITY_LABEL_KEYS = {
  fast: 'qualityFast',
  balanced: 'qualityBalanced',
  best: 'qualityBest',
};
/** Passes per tier (shifts + 1): the progress denominator is chunks x
 *  passes server-side, and the status line names the pass count so the
 *  scaling is visible while a tier runs (3.10.1 D3). */
const QUALITY_PASSES = { fast: 1, balanced: 2, best: 3 };
const QUALITY_KEY = 'keyprism-quality';

/** Compute device (3.10.2): Auto = probe chain (GPU preference, CPU
 *  always last), GPU = forced first available GPU EP + CPU, CPU = pure
 *  CPU. Sent as &device= on every POST; persisted like the tier. */
const DEVICE_CHOICES = ['auto', 'gpu', 'cpu'];
const DEVICE_LABEL_KEYS = {
  auto: 'deviceAuto',
  gpu: 'deviceGpu',
  cpu: 'deviceCpu',
};
const DEVICE_KEY = 'keyprism-device';

/** LOD waveforms (Phase 3.9): windows at or below this span switch from
 *  the ~1024-column overview envelope to per-pixel-column min/max
 *  computed in the worker. */
const LOD_SPAN_SEC = 10;

/** Per-lane LOD cache bound (FIFO): keys are [aMs|bMs|columns]; a pan,
 *  zoom or resize allocates a new entry, so the bound keeps memory flat
 *  while covering ordinary browsing. The cache dies with the lane object
 *  (method switch / track switch re-decode). */
const LOD_CACHE_MAX = 8;

/** Fixed DL stem contract per method (mirrors
 *  keyprism.dlsep.STEM_SPECS — the frontend never reads Python data).
 *  The /api/stems response MUST match EXACTLY this list, in this order,
 *  for every file length. demucs_6 order = the reference export's
 *  source order (drums/bass/other/vocals/guitar/piano — guitar BEFORE
 *  piano, StemSplitio/htdemucs-6s-onnx). */
const DL_METHODS = {
  demucs_4: ['drums', 'bass', 'other', 'vocals'],
  demucs_6: ['drums', 'bass', 'other', 'vocals', 'guitar', 'piano'],
};

/** Classic separation variants (mirrors keyprism.stems.STEM_SPECS /
 *  server stems.METHODS — the frontend never reads Python data). They
 *  complete the method registry (3.10.1 D1): the AI Separation panel's
 *  dropdown exposes ALL five backend variants; picking a classic one
 *  hands off to the stems panel (openWithMethod) instead of forking a
 *  classic renderer into the lanes pipeline. */
const CLASSIC_METHODS = ['hpss', 'rpca', 'combined'];

// demucs_6 stems eligible for polyphonic transcription (mirrors
// keyprism.poly_transcribe.POLY_TRACKS — the frontend never reads
// Python data)
const POLY_LANES = new Set(['piano', 'guitar', 'other']);

/** ORT execution-provider display names (mirrors the raw provider names
 *  /api/ping reports in capabilities.ort_providers — the frontend never
 *  reads Python data). */
const EP_NAMES = {
  CUDAExecutionProvider: 'CUDA',
  DmlExecutionProvider: 'DirectML',
  CoreMLExecutionProvider: 'CoreML',
  CPUExecutionProvider: 'CPU',
};

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

/** Marks an unwound separation (user stop, restart, method/panel
 *  switch): the load() catch path goes back to IDLE silently instead of
 *  showing a failure (3.10.3). */
function cancelledError() {
  const e = new Error('cancelled');
  e.cancelled = true;
  return e;
}

export function initLanes({ gd, data, player, apiBase, layers }) {
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
  // One shared LOD worker for every lane (module worker; sync fallback
  // inside WaveLod when Workers are unavailable)
  const lod = new WaveLod();

  const state = {
    enabled: false,
    caps: null, // /api/ping capabilities (dl/poly availability)
    capsReady: null, // promise resolving once caps are known
    loading: false,
    ready: false,
    method: 'demucs_4',
    quality: (() => {
      // persisted quality tier (default balanced); unknown values fall
      // back instead of poisoning every POST
      try {
        const q = localStorage.getItem(QUALITY_KEY);
        if (QUALITY_TIERS.includes(q)) return q;
      } catch {
        /* localStorage unavailable: keep default */
      }
      return 'balanced';
    })(),
    device: (() => {
      // persisted compute device (3.10.2, default auto); unknown
      // values fall back instead of poisoning every POST
      try {
        const d = localStorage.getItem(DEVICE_KEY);
        if (DEVICE_CHOICES.includes(d)) return d;
      } catch {
        /* localStorage unavailable: keep default */
      }
      return 'auto';
    })(),
    savedMix: null,
    rows: [], // {model, row, vol, mute, solo} mirrors for repaint
    lanes: [], // mixer strips + canvas fields (peaks, idx, waveCv, noteCv)
    taskId: null, // /api/task/{id} of the in-flight separation (3.10.3)
    taskState: 'idle', // idle | running | downloading | done | error | cancelled
    abortLoad: false, // restart asks the in-flight load to unwind
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

  /** Lane playheads (Phase 3.9): one thin DOM hairline per lane, moved by
   *  the master cursor's own repaint path (player.onFrame -> reposition
   *  runs in the playback rAF loop AND on every seek/relayout/resize).
   *  Same clock (player.currentTime -> ctx.currentTime), same view range
   *  and same time→pixel mapping as the waveform canvases — the playhead
   *  is in lockstep with the master cursor by construction, and playback
   *  never repaints a canvas. */
  function updatePlayheads() {
    if (!state.enabled || !state.ready) return;
    const t = player.currentTime();
    for (const lane of state.lanes) {
      const ph = lane.phEl;
      if (!ph || !ph.parentElement) continue;
      const w = ph.parentElement.clientWidth;
      if (!w) continue;
      const x = xOf(t, state.view.aMs, state.view.bMs, w);
      if (x < -1 || x > w + 1) {
        ph.style.display = 'none';
      } else {
        ph.style.display = 'block';
        ph.style.transform = `translateX(${x.toFixed(1)}px)`;
      }
    }
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
    // the view moved under the playhead: re-pin it against the fresh
    // range immediately (player's own relayout handler may not have run yet)
    updatePlayheads();
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
      // [Wave|Spec] per-lane view (Phase 3.9); notes always overlay
      if ((lane.view || 'wave') === 'spec') drawSpec(lane);
      else drawWave(lane);
      drawNotes(lane);
    }
  }

  /** LOD lookup/refresh for the current view (Phase 3.9). Returns the
   *  cached per-column {min,max} for this exact [window, column count],
   *  or null while nothing is ready — the draw then falls back to the
   *  overview envelope and the worker reply repaints when it lands.
   *  Cache key = exact view window + column count: any pan, zoom or
   *  resize gets its own entry (invalidation by construction), bounded
   *  by LOD_CACHE_MAX FIFO. */
  function laneLod(lane, w) {
    if (!lane.buffer || w < 32) return null;
    const [aSec, bSec] = viewSec();
    if (bSec - aSec > LOD_SPAN_SEC) return null;
    const key = `${state.view.aMs}|${state.view.bMs}|${w}`;
    const hit = lane.lodCache && lane.lodCache.get(key);
    if (hit) return hit;
    if (lane.lodPending) return null; // one request in flight per lane
    if (!lane.lodCache) lane.lodCache = new Map();
    if (!lane.lodUp) {
      // hand the worker a zero-copy mono transfer once; in sync-fallback
      // mode uploadPcm returns false and a main-thread copy is kept
      lane.lodUp = true;
      lane.lodId = `${state.method}/${lane.key}`;
      if (!lod.uploadPcm(lane.lodId, lane.buffer.getChannelData(0))) {
        lane.pcmMono = new Float32Array(lane.buffer.getChannelData(0));
      }
    }
    lane.lodPending = key;
    lod
      .compute(lane.lodId, lane.pcmMono, lane.buffer.sampleRate, w, aSec, bSec)
      .then((res) => {
        lane.lodPending = null;
        if (!res) return;
        if (lane.lodCache.size >= LOD_CACHE_MAX) {
          lane.lodCache.delete(lane.lodCache.keys().next().value);
        }
        lane.lodCache.set(key, res);
        scheduleDraw(); // repaint pixel-sharp
      });
    return null;
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
    const [aSec, bSec] = viewSec();
    const span = Math.max(bSec - aSec, 1e-6);
    // auto-scale (I2 visuals): normalize to THIS lane's own peak so
    // quiet stems render visible waveforms instead of flat lines
    const k = lane.peak > 1e-6 ? 1 / lane.peak : 0;
    const clamp = (v) => Math.max(-1, Math.min(1, v * k));
    // Silence is NEVER invisible (3.8 D1): a muted lane renders its
    // envelope dimmed (35% alpha), an audible one at full color
    g.fillStyle = hexToRgba(
      lane.color,
      mixer.stripAudible(lane) ? 1 : 0.35
    );
    const lodHit = laneLod(lane, w); // may kick off an async compute
    if (lodHit) {
      // pixel-sharp path: one min/max pair per canvas column, straight
      // from the worker's answer
      for (let px = 0; px < w; px++) {
        const hi = clamp(lodHit.max[px]);
        const lo = clamp(lodHit.min[px]);
        if (hi <= 0 && lo >= 0) continue;
        const y0 = mid - hi * (mid * 0.92);
        const y1 = mid - lo * (mid * 0.92);
        g.fillRect(px, y0, 1, Math.max(1, y1 - y0));
      }
    } else if (lane.peaks) {
      const p = lane.peaks;
      const nB = p.max.length;
      for (let px = 0; px < w; px++) {
        const ta = aSec + (px / w) * span;
        const tb = aSec + ((px + 1) / w) * span;
        // bucketSec lives on the envelope (lane.peaks), not the lane —
        // reading lane.bucketSec yielded undefined -> NaN indexes -> the
        // loop skipped every column and lanes rendered black (3.8 D1)
        let i0 = Math.floor(ta / p.bucketSec);
        let i1 = Math.max(i0 + 1, Math.ceil(tb / p.bucketSec));
        i0 = Math.max(0, Math.min(nB, i0));
        i1 = Math.max(0, Math.min(nB, i1));
        let lo = 0;
        let hi = 0;
        for (let i = i0; i < i1; i++) {
          if (p.min[i] < lo) lo = p.min[i];
          if (p.max[i] > hi) hi = p.max[i];
        }
        if (hi <= 0 && lo >= 0) continue;
        const y0 = mid - clamp(hi) * (mid * 0.92);
        const y1 = mid - clamp(lo) * (mid * 0.92);
        g.fillRect(px, y0, 1, Math.max(1, y1 - y0));
      }
    }
    if (lane.peak > 1e-6) {
      // tiny peak-dB label: what the auto-scale factor is compensating
      g.font = '9px system-ui, sans-serif';
      g.fillStyle = 'rgba(255,255,255,0.4)';
      g.textAlign = 'left';
      g.textBaseline = 'top';
      g.fillText(
        t('lanePeakDb', {
          db: (20 * Math.log10(lane.peak)).toFixed(1),
        }),
        6,
        4
      );
    }
  }

  /** Lane mini-spectrogram (Phase 3.9 [Wave|Spec] toggle): paints the
   *  server-computed quantized semitone matrix (the same Phase 0 row
   *  space as the master heatmap) from the shared offscreen image. The
   *  x mapping is column = time / hopSec against the SAME shared view
   *  range; y spans the full A0..C8 lane pitch space like the notes
   *  overlay. Muting dims it like the waveform (silence never hides). */
  function drawSpec(lane) {
    const cv = lane.waveCv;
    if (!cv || !cv.clientWidth) return;
    const g = fitCanvas(cv);
    const w = cv.clientWidth;
    const h = cv.clientHeight;
    g.clearRect(0, 0, w, h);
    if (lane.specState !== 'ready' || !lane.specImg) {
      // transient loading placeholder; a failure reverts the lane to Wave
      // (with a toast) before the next paint
      g.fillStyle = 'rgba(255,255,255,0.35)';
      g.font = '11px system-ui, sans-serif';
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText(t('laneSpecLoading'), w / 2, h / 2);
      return;
    }
    const { cv: img, hopSec } = lane.specImg;
    const [aSec, bSec] = viewSec();
    // column c covers [c*hopSec, (c+1)*hopSec); map the view window onto
    // source columns (out-of-range source is clipped transparent by
    // drawImage, matching the waveform's blank margins)
    const sx = aSec / hopSec;
    const sw = Math.max(1e-6, (bSec - aSec) / hopSec);
    g.globalAlpha = mixer.stripAudible(lane) ? 1 : 0.35;
    g.imageSmoothingEnabled = false; // honest cells, like the master heatmap
    g.drawImage(img, sx, 0, sw, img.height, 0, 0, w, h);
    g.globalAlpha = 1;
  }

  /** Fetch + decode the stem spec once per lane (shared module cache in
   *  stemspec.js makes repeat lanes/layers free; the master payload's
   *  dbRange is asserted so lane/overlay views share the master's dB
   *  basis — 3.9.1 C). A failure toasts the precise reason and degrades
   *  the lane back to Wave. */
  async function ensureSpec(lane) {
    if (lane.specState === 'loading' || lane.specState === 'ready') return;
    lane.specState = 'loading';
    scheduleDraw(); // show the loading placeholder
    try {
      const spec = await getStemSpec(apiBase, state.method, lane.key, {
        dbRange: data.dbRange,
      });
      lane.specImg = createSpecImage(spec, lane.color);
      lane.specState = 'ready';
    } catch (e) {
      lane.specState = 'failed';
      lane.specErr = e?.message || String(e);
      showToast(t('laneSpecFailed', { msg: lane.specErr }));
      lane.view = 'wave';
      paintViewToggle(lane);
    }
    scheduleDraw();
  }

  /** [Wave|Spec] segmented control, overlaid top-left on the scope (the
   *  Notes chip owns the top-right corner). Spec is lazy: nothing is
   *  fetched until the first click (same discipline as the Notes chip). */
  function buildViewToggle(lane, scope) {
    const seg = document.createElement('div');
    seg.className = 'lane-view-toggle';
    seg.title = t('laneSpecTip');
    lane.viewBtns = {};
    for (const mode of ['wave', 'spec']) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = t(mode === 'wave' ? 'laneWave' : 'laneSpec');
      b.addEventListener('click', () => {
        if (lane.view === mode) return;
        lane.view = mode;
        paintViewToggle(lane);
        if (mode === 'spec') ensureSpec(lane);
        else scheduleDraw();
      });
      lane.viewBtns[mode] = b;
      seg.appendChild(b);
    }
    scope.appendChild(seg);
    paintViewToggle(lane);
    // Phase 3.9 M2: drag handle ("⧉ layer") — dragging it onto the master
    // spectrogram creates an overlay layer of this stem's spectrogram
    // (layers.js). The method is stamped at drag time so the layer keeps
    // serving the right stem even after a method switch.
    if (layers && layers.beginLaneDrag) {
      const handle = document.createElement('button');
      handle.type = 'button';
      handle.className = 'lane-layer-handle';
      handle.textContent = '⧉';
      handle.title = t('layerHandleTip');
      handle.addEventListener('pointerdown', (ev) => {
        ev.preventDefault();
        lane.method = state.method;
        layers.beginLaneDrag(lane, ev);
      });
      scope.appendChild(handle);
    }
  }

  function paintViewToggle(lane) {
    if (!lane.viewBtns) return;
    for (const [mode, b] of Object.entries(lane.viewBtns)) {
      b.classList.toggle('active', (lane.view || 'wave') === mode);
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
      state.rows.find((r) => r.model === mixer.mix)?.syncFader?.();
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

  /** GPU/CPU badge (3.10): the active ORT execution-provider chain from
   *  /api/ping capabilities.ort_providers (the session's readback once a
   *  model is loaded, else the selected chain). GPU = the first non-CPU
   *  provider; tooltip lists the full active chain in order. */
  function providerBadge() {
    const eps =
      state.caps && Array.isArray(state.caps.ort_providers)
        ? state.caps.ort_providers
        : [];
    if (!eps.length) return null;
    const names = eps.map((p) => EP_NAMES[p] || p);
    const gpu = eps.filter((p) => p !== 'CPUExecutionProvider');
    return {
      gpu: gpu.length > 0,
      text: gpu.length
        ? t('providerGpu', { name: EP_NAMES[gpu[0]] || gpu[0] })
        : t('providerCpu'),
      tip: t('providerTip', { chain: names.join(' → ') }),
    };
  }

  /** POST starts the separation task, then poll /api/task/{id} until
   *  done — a cached method short-circuits to its stem list at once.
   *  Two-phase progress (3.8 D3): `downloading` reports model-download
   *  bytes/speed, `running` reports inference progress. `force`
   *  (3.10.3 restart) bypasses the stems cache server-side. The
   *  cooperative cancel (3.10.3): `cancelled` task status unwinds the
   *  poll with a cancelled error (no cache was written). */
  async function requestStems(method, force = false) {
    const r = await fetch(
      `${apiBase}/api/stems?method=${method}&quality=${state.quality}` +
        `&device=${state.device}${force ? '&force=1' : ''}`,
      {
        method: 'POST',
      });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    if (body.cached || !body.task_id) {
      // cache hit: no task was created — the strip goes straight to done
      state.taskState = 'done';
      paintStripButtons();
      return body;
    }
    state.taskId = body.task_id;
    state.taskState = 'running';
    paintStripButtons();
    const taskUrl = `${apiBase}${body.status_url}`;
    for (;;) {
      if (!state.enabled || state.method !== method || state.abortLoad) {
        throw cancelledError();
      }
      const res = await fetch(taskUrl);
      const tb = await res.json();
      if (tb.status === 'done') {
        state.taskState = 'done';
        paintStripButtons();
        return tb;
      }
      if (tb.status === 'cancelled') {
        state.taskState = 'cancelled';
        paintStripButtons();
        throw cancelledError();
      }
      if (tb.status === 'error') {
        state.taskState = 'error';
        paintStripButtons();
        throw new Error(tb.error || 'task failed');
      }
      state.taskState = tb.status === 'downloading'
        ? 'downloading'
        : 'running';
      paintStripButtons();
      if (tb.status === 'downloading') {
        const mb = (n) => (n / 1e6).toFixed(1);
        setStatus(
          t('lanesDownloading', {
            done: mb(tb.bytes_done || 0),
            total: tb.bytes_total ? mb(tb.bytes_total) : '?',
            speed: Number(tb.speed_mbps || 0).toFixed(1),
          }),
          true
        );
      } else {
        setStatus(
          t('lanesSeparating', {
            pct: Math.round((tb.progress || 0) * 100),
            passes: QUALITY_PASSES[state.quality] || 1,
          }),
          true
        );
      }
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

  /** Repaint the matrix on the rows: M/S buttons render STRICTLY from
   *  the model (destructive solo writes real mute states — the gold M
   *  on suppressed rows is the actual state); a lane that is inaudible
   *  is shown `.dimmed` WITH a tooltip explaining why. */
  function paintStates() {
    const anySolo = mixer.anySolo;
    for (const { model, row, mute, solo } of state.rows) {
      mute.classList.toggle('active', model.muted);
      solo.classList.toggle('active', !!model.solo);
      const audible =
        model === mixer.mix ? mixer.mixAudible() : mixer.stripAudible(model);
      row.classList.toggle('dimmed', !audible);
      row.title = audible ? '' : suppressionTip(model, anySolo);
    }
    // mute/solo also re-tints the envelopes (dimmed while inaudible —
    // silence must never hide the waveform)
    scheduleDraw();
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
      // prefer the server's precise reason (uv-sync remedy vs the
      // Python >=3.12 basic-pitch limitation)
      px.title = state.caps.poly_reason || t('polyNeedsBP');
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
          e.kind === 'dl'
            ? t('polyNeedsDL')
            : state.caps?.poly_reason || t('polyNeedsBP')
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
      fader: 'db',
    });
    const { row, scope, vol, mute, solo, db } = parts;
    const { syncFader } = wireMuteSolo({
      mixer,
      model: lane,
      vol,
      mute,
      solo,
      dbEl: db,
      apply: () => mixer.apply(),
      paintFill,
    });
    state.rows.push({ model: lane, row, vol, mute, solo, syncFader });
    const wave = document.createElement('canvas');
    wave.className = 'lane-wave';
    const noteCv = document.createElement('canvas');
    noteCv.className = 'lane-notes';
    noteCv.setAttribute('aria-hidden', 'true');
    // Phase 3.9: per-lane playhead hairline (DOM, above the canvases)
    const phEl = document.createElement('div');
    phEl.className = 'lane-playhead';
    phEl.setAttribute('aria-hidden', 'true');
    scope.append(wave, noteCv, phEl);
    lane.phEl = phEl;
    // Phase 3.9: [Wave|Spec] toggle (view state lives on the lane model
    // so a language-switch renderShell keeps the user's choice)
    buildViewToggle(lane, scope);
    // Notes (扒谱): uniform on every poly-eligible lane (D4); a compact
    // chip overlaid on the lane's own scope — the controls cell stays
    // one tidy [fader M S] line at the fixed grid widths
    if (POLY_LANES.has(lane.key)) {
      const px = document.createElement('button');
      px.type = 'button';
      px.className = 'stem-btn lane-notes-btn';
      px.textContent = '♫';
      px.title = t('laneNotesTitle');
      px.addEventListener('click', () => toggleNotes(lane));
      lane.notesBtn = px;
      scope.appendChild(px);
      applyNotesAvailability(lane);
    }
    lane.waveCv = wave;
    lane.noteCv = noteCv;
    container.appendChild(row);
  }

  function renderShell() {
    panel.innerHTML = '';
    state.rows = [];
    // 3.10.3 D1: the Mix row's right cell IS the control strip —
    // [title][method ▾][quality ▾][device ▾][GPU badge][status] inline
    // on one wrapped flex line instead of stacked full-width rows, and
    // the empty dashed Mix lane placeholder is gone. The Mix row renders
    // in EVERY state (loading/error/ready) so the controls stay
    // reachable while a task runs.
    const mix = trackRow({
      key: 'mix',
      labelText: t('laneMix'),
      color: '#ddd6c8',
      withLane: true,
      fader: 'norm',
    });
    // 3.10.4 D2: the Mix cell is NOT a lane window — drop the .lane-scope
    // chrome entirely so the right cell is ONLY the control strip (no
    // canvas, no dashed placeholder, no lane box)
    mix.row.classList.add('lane-row-mix');
    mix.scope.className = 'mix-controls';
    const title = document.createElement('span');
    title.className = 'stems-title';
    title.textContent = t('lanes');
    const methodSel = document.createElement('select');
    methodSel.className = 'stems-method';
    const dlOk = !!state.caps && state.caps.dl;
    // The FULL separation registry in one dropdown (3.10.1 D1): classic
    // variants grouped first, then the Demucs variants this panel owns.
    const classicGroup = document.createElement('optgroup');
    classicGroup.label = t('methodClassic');
    for (const m of CLASSIC_METHODS) {
      const o = document.createElement('option');
      o.value = m;
      o.textContent = t(`stemsMethod_${m}`);
      classicGroup.appendChild(o);
    }
    const dlGroup = document.createElement('optgroup');
    dlGroup.label = t('methodAI');
    for (const m of Object.keys(DL_METHODS)) {
      const o = document.createElement('option');
      o.value = m;
      o.textContent = t(`lanesMethod_${m}`);
      if (m === state.method) o.selected = true;
      dlGroup.appendChild(o);
    }
    methodSel.append(classicGroup, dlGroup);
    methodSel.disabled = state.loading || !dlOk;
    if (!dlOk) {
      methodSel.title = t('dlNeedsExtra');
    }
    methodSel.addEventListener('change', () => {
      if (methodSel.disabled) return;
      const m = methodSel.value;
      if (m === state.method) return;
      if (DL_METHODS[m]) {
        load(m);
        return;
      }
      // classic variant: hand off to the stems panel — close the lanes
      // panel first so the mix gain hands back before the stems panel
      // re-saves it (the off button's visual state rides along)
      const offBtn = toggle.querySelector('button[data-lanes="off"]');
      toggle
        .querySelectorAll('button')
        .forEach((b) => b.classList.toggle('active', b === offBtn));
      disable();
      openWithMethod(m);
    });
    // Inference quality tier (3.10): demucs shifts — fast = 1 pass,
    // balanced = 2, best = 3. Persisted; a switch re-POSTs, and the
    // server recomputes when the cached render used a different tier.
    const qualitySel = document.createElement('select');
    qualitySel.className = 'stems-method stems-quality';
    qualitySel.title = t('qualityTip');
    for (const q of QUALITY_TIERS) {
      const o = document.createElement('option');
      o.value = q;
      o.textContent = t(QUALITY_LABEL_KEYS[q]);
      if (q === state.quality) o.selected = true;
      qualitySel.appendChild(o);
    }
    qualitySel.disabled = state.loading || !dlOk;
    qualitySel.addEventListener('change', () => {
      if (qualitySel.disabled || qualitySel.value === state.quality) return;
      state.quality = qualitySel.value;
      try {
        localStorage.setItem(QUALITY_KEY, state.quality);
      } catch {
        /* storage failure: keep the session value */
      }
      load(state.method);
    });
    // Compute device (3.10.2): Auto / GPU / CPU next to the tier. The
    // GPU option is disabled when /api/ping reports no GPU EP in the
    // build (ort_providers_available); a persisted 'gpu' choice on such
    // a machine is coerced back to Auto so a stale setting can never
    // poison every POST. Unlike the tier, a device switch does NOT
    // re-POST: the stems' audio is identical either way — the choice
    // applies to the next separation task.
    const gpuAvailable = () =>
      !!state.caps &&
      Array.isArray(state.caps.ort_providers_available) &&
      state.caps.ort_providers_available.some(
        (p) => p !== 'CPUExecutionProvider'
      );
    if (state.device === 'gpu' && !gpuAvailable()) {
      state.device = 'auto';
      try {
        localStorage.setItem(DEVICE_KEY, state.device);
      } catch {
        /* storage failure: keep the session value */
      }
    }
    const deviceSel = document.createElement('select');
    deviceSel.className = 'stems-method stems-device';
    deviceSel.title = t('deviceTip');
    for (const d of DEVICE_CHOICES) {
      const o = document.createElement('option');
      o.value = d;
      o.textContent = t(DEVICE_LABEL_KEYS[d]);
      if (d === state.device) o.selected = true;
      if (d === 'gpu' && !gpuAvailable()) {
        o.disabled = true;
        o.title = t('deviceNoGpu');
      }
      deviceSel.appendChild(o);
    }
    deviceSel.disabled = state.loading || !dlOk;
    deviceSel.addEventListener('change', () => {
      if (deviceSel.disabled || deviceSel.value === state.device) return;
      state.device = deviceSel.value;
      try {
        localStorage.setItem(DEVICE_KEY, state.device);
      } catch {
        /* storage failure: keep the session value */
      }
    });
    const status = document.createElement('span');
    status.className = 'lanes-status stems-status';
    // Stop & Restart (3.10.3 D2, restyled 3.10.4 D1): house 26px square
    // icon-buttons (same chrome as the M/S keys; champagne active fill
    // marks an in-flight task). State machine — stop enabled ONLY while
    // a task is downloading/running; restart enabled when done/error/
    // cancelled and re-submits with force=1 (cache bypassed).
    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'stem-btn';
    stopBtn.textContent = '⏹';
    stopBtn.setAttribute('aria-label', t('stopSep'));
    stopBtn.title = t('stopTip');
    stopBtn.addEventListener('click', () => stopSeparation());
    const restartBtn = document.createElement('button');
    restartBtn.type = 'button';
    restartBtn.className = 'stem-btn';
    restartBtn.textContent = '↻';
    restartBtn.setAttribute('aria-label', t('restartSep'));
    restartBtn.title = t('restartTip');
    restartBtn.addEventListener('click', () => restartSeparation());
    mix.scope.append(title, methodSel, qualitySel, deviceSel, stopBtn,
      restartBtn, status);
    const badge = providerBadge();
    if (badge) {
      const el = document.createElement('span');
      el.className =
        'provider-badge' + (badge.gpu ? ' provider-badge-gpu' : '');
      el.textContent = badge.text;
      el.title = badge.tip;
      mix.scope.insertBefore(el, stopBtn);
    }
    state.stopBtn = stopBtn;
    state.restartBtn = restartBtn;
    paintStripButtons();
    // Mix lane: the player's own playback, ridden by the master gain.
    const { syncFader: mixSync } = wireMuteSolo({
      mixer,
      model: mixer.mix,
      vol: mix.vol,
      mute: mix.mute,
      solo: mix.solo,
      dbEl: mix.db,
      apply: () => mixer.apply(),
      paintFill,
    });
    mixer.mix.volume = player.mixGainNorm();
    mixSync();
    panel.append(mix.row);
    state.rows.push({
      model: mixer.mix,
      row: mix.row,
      vol: mix.vol,
      mute: mix.mute,
      solo: mix.solo,
      syncFader: mixSync,
    });

    for (const lane of state.lanes) buildLaneRow(panel, lane);
    paintStates();
    syncFromPlot(null); // draw into the fresh canvases
  }

  function teardownLanes() {
    stopAll();
    lod.forgetAll(); // drop the worker-side PCM copies of the old lanes
    mixer.unroute();
    state.lanes = [];
  }

  /** Button state machine (3.10.3 D2): stop enabled ONLY while a task
   *  is downloading/running; restart enabled when idle/done/error/
   *  cancelled (it cancels any running task, then re-submits with
   *  force=1). Both stay dead without the DL extra. */
  function paintStripButtons() {
    const active =
      state.taskState === 'running' || state.taskState === 'downloading';
    if (state.stopBtn) {
      state.stopBtn.disabled = !active;
    }
    if (state.restartBtn) {
      state.restartBtn.disabled =
        state.loading || active || !state.caps || !state.caps.dl;
    }
  }

  /** Stop: POST /api/task/{id}/cancel — the server flips the task's
   *  cooperative cancel flag and the job aborts at the next chunk/pass
   *  boundary (ort run() is uninterruptible; bounded by one ~7.8 s
   *  model segment). The poll loop sees status "cancelled" and unwinds
   *  the load; the UI returns to idle and no stem cache is written. */
  async function stopSeparation() {
    if (!state.taskId || state.abortLoad) return;
    const id = state.taskId;
    try {
      await fetch(`${apiBase}/api/task/${id}/cancel`, {
        method: 'POST',
      });
    } catch {
      /* network hiccup: the poll still reports the outcome */
    }
    setStatus(t('lanesCancelling'), true);
  }

  /** Restart: cancel any running task, wait for the in-flight load to
   *  unwind, then re-submit with force=1 — the server bypasses the
   *  stems cache and recomputes from zero. */
  async function restartSeparation() {
    if (state.loading) {
      state.abortLoad = true;
      if (state.taskId) {
        fetch(`${apiBase}/api/task/${state.taskId}/cancel`, {
          method: 'POST',
        }).catch(() => {});
      }
      while (state.loading) {
        await new Promise((r) => setTimeout(r, 50));
      }
      state.abortLoad = false;
    }
    load(state.method, true);
  }

  async function load(method, force = false) {
    if (state.loading) return;
    state.loading = true;
    state.method = method;
    state.ready = false;
    state.taskId = null;
    state.taskState = 'running';
    // spinner on the On button immediately (3.8 D2): a click is never
    // visually dead while the task starts up
    toggle
      .querySelector('button[data-lanes="on"]')
      ?.classList.add('loading');
    teardownLanes();
    renderShell();
    setStatus(t('lanesSeparating', { pct: 0 }), true);
    let lanes = [];
    try {
      const list = await requestStems(method, force);
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
      const pMix = player.mixPeak();
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
          phEl: null,
          lodCache: null, // [view window|columns] -> per-column min/max
          lodPending: null,
          lodUp: false,
          lodId: null,
          pcmMono: null, // sync-fallback copy (only when Workers are gone)
          view: 'wave', // [Wave|Spec] per-lane view
          specState: null, // null | loading | ready | failed
          specImg: null,
          decodeFailed: false,
        });
        lanes.push(lane);
        try {
          lane.buffer = await decodeStem(item.url);
          lane.peaks = buildEnvelope(lane.buffer);
          // gain staging (I2): fader default = make-up gain matching
          // the lane peak to the mix peak; the envelope auto-scale and
          // peak label use the same per-lane peak
          lane.peak = bufferPeak(lane.buffer);
          lane.makeupDb = makeupDb(pMix, lane.peak);
          lane.dbGain = lane.makeupDb;
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
      // 3.10.1 D2 root cause: the flag MUST be cleared before the final
      // render — renderShell builds the method/quality selects with
      // disabled = state.loading, so when this render ran while the
      // flag was still set (it used to be cleared only in finally,
      // AFTER the render) both dropdowns came up permanently dead.
      state.loading = false;
      renderShell();
      mixer.apply();
      drawAll(); // waveforms visible immediately, before Play (D2)
      updatePlayheads(); // pin the lane playheads to the current position
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
        state.loading = false; // same ordering rule: render enabled
        if (e.cancelled) {
          // user stop / restart unwind: back to IDLE, no failure text
          state.taskState = state.abortLoad ? 'cancelled' : state.taskState;
          renderShell();
          setStatus('');
        } else {
          if (state.taskState !== 'error') state.taskState = 'error';
          renderShell();
          setStatus(t('lanesFailed', { msg: e.message }));
        }
      }
    } finally {
      // a failed/partial load leaves no gain nodes wired anywhere
      lanes.forEach((l) => l.gain.disconnect());
      toggle
        .querySelector('button[data-lanes="on"]')
        ?.classList.remove('loading');
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
    state.taskId = null;
    state.taskState = 'idle';
    state.abortLoad = false;
    toggle
      .querySelector('button[data-lanes="on"]')
      ?.classList.remove('loading');
    panel.hidden = true;
    mixer.mix.solo = false;
    // hand the output back to the mix at the pre-enable volume
    if (state.savedMix !== null) {
      player.setMixGain(state.savedMix, { persist: true });
    }
    mixer.mix.volume = state.savedMix ?? mixer.mix.volume;
    mixer.mix.muted = false;
  }

  toggle.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-lanes]');
    if (!btn || btn.disabled) return;
    const want = btn.dataset.lanes === 'on';
    const turning = (want && !state.enabled) || (!want && state.enabled);
    if (!turning) return;
    if (want) {
      // 3.8 D2: the click ALWAYS answers — capabilities are awaited
      // here (cached after the first check), and a missing [dl] extra
      // toasts the precise remedy, disables the button with the same
      // text as its tooltip, and never starts a task
      await state.capsReady;
      if (!state.caps || !state.caps.dl) {
        showToast(t('dlNeedsExtra'));
        const onBtn = toggle.querySelector('button[data-lanes="on"]');
        if (onBtn) {
          onBtn.disabled = true;
          onBtn.title = t('dlNeedsExtra');
        }
        setStatus('');
        return;
      }
    }
    toggle
      .querySelectorAll('button')
      .forEach((b) => b.classList.toggle('active', b === btn));
    if (want) enable();
    else disable();
  });

  // ---- capabilities: resolved ONCE and exposed as a promise so the
  // Notes click can await it lazily (never auto-triggering transcription
  // on panel load); the toggle gets the remedy tooltip + disabled state
  // when DL is missing and existing Notes buttons get their precise
  // state.
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
      if (onBtn) {
        onBtn.disabled = true;
        onBtn.title = t('dlNeedsExtra');
      }
    }
    for (const lane of state.lanes) applyNotesAvailability(lane);
    if (state.enabled) renderShell();
  })();

  // ---- keep the lanes glued to the main spectrogram's time axis ----
  gd.on('plotly_relayout', syncFromPlot);
  // lane playheads ride the master cursor's frame loop (single time source)
  player.onFrame(updatePlayheads);
  new ResizeObserver(throttled(() => scheduleDraw())).observe(panel);

  // Live language switch: rebuild the panel (state is kept in models)
  onChange(() => {
    if (state.enabled) renderShell();
  });

  return {};
}
