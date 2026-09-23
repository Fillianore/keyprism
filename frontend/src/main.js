import './style.css';
import Plotly from 'plotly.js-dist-min';
import {
  buildFigure,
  applyPitchRange,
  applyGrid,
  applyPlotShapes,
  setSub,
  applyHoverLang,
  EPOCH_MS,
  iso,
  pMs,
} from './spectrogram.js';
import { registerAdaptiveTicks, registerRangeClamp } from './ticks.js';
import { createPlayer } from './player.js';
import { createSpecFeed } from './specfeed.js';
import { initNotes } from './notes.js';
import { initStems } from './stems.js';
import { initLanes } from './lanes.js';
import { throttled } from './util.js';
import { t, onChange } from './i18n.js';

/** Progress modal: setPhase text / setProgress(done,total) / setIndeterminate */
function showProgressModal(title = t('updatingSpec')) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <h3>${title}</h3>
      <div class="phase"></div>
      <div class="bar"><div class="bar-fill indeterminate"></div></div>
      <div class="pct"></div>
    </div>`;
  document.body.appendChild(overlay);
  const phaseEl = overlay.querySelector('.phase');
  const fill = overlay.querySelector('.bar-fill');
  const pct = overlay.querySelector('.pct');
  return {
    setPhase(t) {
      phaseEl.textContent = t;
    },
    setIndeterminate(on) {
      fill.classList.toggle('indeterminate', on);
      if (on) {
        fill.style.width = '34%';
        pct.textContent = '';
      }
    },
    setProgress(done, total) {
      if (!total) return;
      const p = Math.min(100, Math.round((done / total) * 100));
      fill.style.width = `${p}%`;
      pct.textContent = `${p}% (${done.toLocaleString()}/${total.toLocaleString()})`;
    },
    close() {
      overlay.remove();
    },
  };
}

/** Read the response body and report download progress by Content-Length;
 *  returns the parsed JSON */
async function readJsonWithProgress(resp, onProgress) {
  const total = parseInt(resp.headers.get('Content-Length') || '0', 10);
  const reader = resp.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    onProgress(received, total);
  }
  const buf = new Uint8Array(received);
  let off = 0;
  for (const c of chunks) {
    buf.set(c, off);
    off += c.length;
  }
  return JSON.parse(new TextDecoder().decode(buf));
}

async function main() {
  const res = await fetch(`./data.json?t=${Date.now()}`);
  if (!res.ok)
    throw new Error(t('dataLoadFailed', { status: res.status }));
  const data = await res.json();
  if (!data.specs || !data.noteLabels) {
    throw new Error(t('dataStale'));
  }

  // Keep the quantized matrices resident as raw bytes; the plot trace
  // receives the full pooled matrix built from them (see specfeed.js —
  // plotly re-rasterizes its whole z matrix on every replot, so the matrix
  // is pooled down to display resolution instead of viewport-sliced: no
  // dynamic loading, nothing to refill while panning)
  const b64ToU8 = (b64) =>
    Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  let specRaw = {}; // channel -> Uint8Array
  for (const [name, b64] of Object.entries(data.specs)) {
    specRaw[name] = b64ToU8(b64);
  }
  let curSub = data.defaultSub || 1;
  let nCols = data.nCols;
  let curChan = 'mix';
  let pitchLoHi = [0, data.noteLabels.length - 1];

  const plotEl = document.getElementById('plot');
  const app = document.getElementById('app');

  const feed = createSpecFeed(data, {
    specRaw: () => specRaw,
    nCols: () => nCols,
    curSub: () => curSub,
    curChan: () => curChan,
    plotH: () => plotEl.clientHeight,
    pitchLoHi: () => pitchLoHi,
  });

  const tick = () => new Promise((res) => setTimeout(res, 0));

  const { gd } = buildFigure(
    plotEl,
    data,
    // Complete pooled matrix: rendered once, panning never reloads
    feed.build()
  );
  registerAdaptiveTicks(gd);
  registerRangeClamp(gd, EPOCH_MS, EPOCH_MS + Math.round(data.durationSec * 1000));
  const player = createPlayer(app, gd, {
    // Query string as cache buster: after a track switch, a same-named
    // audio.wav is no longer read from the browser cache
    audioUrl: `./${data.audioFile}?t=${Date.now()}`,
    offsetSec: data.offsetSec,
    endSec: data.endSec,
    durMs: Math.round(data.durationSec * 1000),
  });

  // ---- Top bar: monophonic note overlay (bass / lead, Phase 1) ----
  initNotes({
    gd,
    data,
    apiBase: data.apiBase,
    getSub: () => curSub,
  });

  // ---- Stem player (classic source separation, Phase 2): shares the
  // player's AudioContext and transport clock ----
  initStems({
    data,
    player,
    apiBase: data.apiBase,
  });

  // ---- Multi-lane DL workspace (Demucs stems + polyphonic notes,
  // Phase 3): same shared AudioContext; canvas rendering synced to the
  // main chart's xaxis range ----
  initLanes({
    gd,
    data,
    player,
    apiBase: data.apiBase,
  });

  // ---- Top bar: channel switch (mix/left/right) ----
  const chanSel = document.getElementById('chanSelect');
  chanSel.addEventListener('change', () => {
    curChan = chanSel.value;
    cancelStream();
    withRenderBusy(() => feed.apply(gd));
  });

  // ---- Top bar: resolution switch (requires backend --serve mode) ----
  const rateSel = document.getElementById('rateSelect');
  const subSel = document.getElementById('subSelect');
  const resStatus = document.getElementById('resStatus');
  (data.timeRates || [15, 30, 60]).forEach((r) => {
    const o = document.createElement('option');
    o.value = String(r);
    o.textContent = String(r);
    rateSel.appendChild(o);
  });
  (data.subOptions || [1, 5, 10]).forEach((s) => {
    const o = document.createElement('option');
    o.value = String(s);
    o.textContent = String(s);
    subSel.appendChild(o);
  });
  rateSel.value = String(data.defaultRate || 15);
  subSel.value = String(curSub);
  if (!data.apiBase) {
    rateSel.disabled = true;
    subSel.disabled = true;
    resStatus.textContent = t('staticMode');
  }
  const applyResolution = async () => {
    if (!data.apiBase) return;
    const rate = parseInt(rateSel.value, 10);
    const s = parseInt(subSel.value, 10);
    if (rate === data.defaultRate && s === curSub && !specRaw._remote) return;
    rateSel.disabled = true;
    subSel.disabled = true;
    const modal = showProgressModal();
    try {
      modal.setPhase(
        t('backendComputing', { rate, sub: s })
      );
      const r = await fetch(
        `${data.apiBase}/api/spec?rate=${rate}&sub=${s}`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      modal.setPhase(t('receivingSpec'));
      const j = await readJsonWithProgress(r, (done, total) =>
        modal.setProgress(done, total)
      );
      if (j.error) throw new Error(j.error);
      nCols = j.nCols;
      modal.setPhase(t('decodingMatrix'));
      const raws = {};
      const names = Object.keys(j.specs);
      for (let i = 0; i < names.length; i++) {
        raws[names[i]] = b64ToU8(j.specs[names[i]]);
        modal.setProgress(i + 1, names.length);
        await tick();
      }
      specRaw = raws;
      specRaw._remote = true;
      const firstSwitch = curSub !== j.sub;
      curSub = j.sub;
      data.defaultRate = j.rate;
      if (firstSwitch) setSub(gd, curSub, data);
      modal.setPhase(t('decodingMatrix'));
      await tick();
      // Swap in the full pooled matrix for the new resolution (one replot,
      // modal covers it)
      cancelStream();
      feed.apply(gd);
      resStatus.textContent = t('resStatus', { rate: j.rate, sub: j.sub });
    } catch (e) {
      resStatus.textContent = t('failed', { msg: e.message });
    } finally {
      modal.close();
      rateSel.disabled = false;
      subSel.disabled = false;
    }
  };
  rateSel.addEventListener('change', applyResolution);
  subSel.addEventListener('change', applyResolution);

  // ---- Top bar: pick local music (upload to the backend for analysis and
  // track switch, requires --serve mode) ----
  const pickBtn = document.getElementById('pickBtn');
  const fileInput = document.getElementById('fileInput');
  pickBtn.addEventListener('click', () => {
    if (!data.apiBase) {
      resStatus.textContent = t('pickNeedsServe');
      return;
    }
    fileInput.click();
  });
  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = ''; // allow picking the same file again
    if (!file) return;
    const modal = showProgressModal(t('importingMusic'));
    pickBtn.disabled = true;
    const finish = (msg) => {
      modal.close();
      pickBtn.disabled = false;
      if (msg) resStatus.textContent = msg;
    };
    const xhr = new XMLHttpRequest();
    xhr.open(
      'POST',
      `${data.apiBase}/api/upload?name=${encodeURIComponent(file.name)}`
    );
    xhr.responseType = 'json';
    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) modal.setProgress(e.loaded, e.total);
    });
    xhr.addEventListener('load', () => {
      const body = xhr.response;
      if (xhr.status === 200 && body && !body.error) {
        modal.setPhase(t('analyzeDone'));
        modal.setIndeterminate(true);
        setTimeout(() => location.reload(), 500);
        return;
      }
      const err = (body && body.error) || `HTTP ${xhr.status}`;
      finish(t('importFailed', { msg: err }));
    });
    xhr.addEventListener('error', () => finish(t('importNetworkError')));
    xhr.addEventListener('abort', () => finish(t('importCancelled')));
    modal.setPhase(t('uploading', { name: file.name }));
    modal.setIndeterminate(false); // switch to real upload progress
    xhr.send(file);
  });

  // ---- Top bar: file name / palette / color floor ----
  const badge = document.getElementById('fileBadge');
  badge.textContent = data.file;
  badge.title = data.file;

  const cmapSel = document.getElementById('cmapSelect');
  for (const name of Object.keys(data.colorscales)) {
    const o = document.createElement('option');
    o.value = name;
    o.textContent = name;
    if (name === data.defaultCmap) o.selected = true;
    cmapSel.appendChild(o);
  }

  const floor = document.getElementById('floorSlider');
  const floorVal = document.getElementById('floorVal');
  const floorMin = -Math.min(40, data.dbRange); // slider lower bound (at most -40 dB)
  const floorDefault = Math.max(floorMin, -30); // default -30 dB
  floor.min = String(floorMin);
  floor.max = '-5';
  floor.step = '1';
  const restyleFloor = throttled((c) =>
    Plotly.restyle(gd, { zmin: [c] }, [0])
  );
  const applyFloor = (v, immediate = false) => {
    const c = Math.round(Math.min(Math.max(v, floorMin), -5));
    floor.value = String(c);
    floorVal.dataset.v = String(c);
    floorVal.textContent = `${c} dB`; // numeric label follows immediately
    if (immediate) Plotly.restyle(gd, { zmin: [c] }, [0]);
    else restyleFloor(c);
  };
  floor.addEventListener('input', () => applyFloor(parseFloat(floor.value)));
  applyFloor(floorDefault, true);

  // ---- Highlight boost (gamma): applies a pos^γ power transform to the
  // colorscale anchor positions; data and hover readouts unchanged ----
  const GAMMA_MIN = 0.6;
  const GAMMA_MAX = 5;
  const gammaSlider = document.getElementById('gammaSlider');
  const gammaVal = document.getElementById('gammaVal');
  gammaSlider.min = String(GAMMA_MIN);
  gammaSlider.max = String(GAMMA_MAX);
  gammaSlider.step = '0.05';
  const restyleGamma = throttled((cs) =>
    Plotly.restyle(gd, { colorscale: [cs] }, [0])
  );
  const applyGamma = (v, immediate = false) => {
    const g = Math.min(Math.max(v, GAMMA_MIN), GAMMA_MAX);
    gammaSlider.value = String(g);
    gammaVal.dataset.v = String(g);
    gammaVal.textContent = `γ ${g.toFixed(2)}`; // numeric label follows immediately
    const base = data.colorscales[cmapSel.value];
    const cs = base.map(([p, c]) => [Math.pow(p, g), c]);
    if (immediate) Plotly.restyle(gd, { colorscale: [cs] }, [0]);
    else restyleGamma(cs);
  };

  /** Clicking a numeric label turns it into an input: Enter/blur commits,
   *  Esc cancels */
  function makeEditable(span, apply) {
    span.classList.add('num');
    span.title = t('clickToEdit');
    span.addEventListener('click', () => {
      if (span.querySelector('input')) return;
      const cur = parseFloat(span.dataset.v);
      const input = document.createElement('input');
      input.type = 'number';
      input.step = 'any';
      input.className = 'num-input';
      let closed = false;
      const close = (commit) => {
        if (closed) return;
        closed = true;
        const v = parseFloat(input.value);
        if (commit && isFinite(v)) apply(v);
        else apply(cur); // on cancel, re-render with the original value
      };
      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') close(true);
        else if (ev.key === 'Escape') close(false);
        ev.stopPropagation();
      });
      input.addEventListener('blur', () => close(true));
      input.addEventListener('click', (ev) => ev.stopPropagation());
      span.textContent = '';
      span.appendChild(input);
      input.value = String(cur);
      input.focus();
      input.select();
    });
  }

  makeEditable(floorVal, applyFloor);
  makeEditable(gammaVal, applyGamma);
  document.getElementById('floorReset').addEventListener('click', () => {
    applyFloor(floorDefault, true);
  });
  document.getElementById('gammaReset').addEventListener('click', () => {
    applyGamma(1, true);
  });

  const applyColorscale = () => applyGamma(parseFloat(gammaSlider.value));
  cmapSel.addEventListener('change', () =>
    applyGamma(parseFloat(gammaSlider.value), true)
  );
  gammaSlider.addEventListener('input', applyColorscale);
  applyGamma(1, true);
  const loSel = document.getElementById('loSelect');
  const hiSel = document.getElementById('hiSelect');
  const fill = (sel, def) => {
    data.noteLabels.forEach((t, i) => {
      const o = document.createElement('option');
      o.value = String(i);
      o.textContent = t;
      sel.appendChild(o);
    });
    sel.value = String(def);
  };
  fill(loSel, 0);
  fill(hiSel, data.noteLabels.length - 1);
  const applyRange = () => {
    let lo = parseInt(loSel.value, 10);
    let hi = parseInt(hiSel.value, 10);
    if (lo > hi) {
      [lo, hi] = [hi, lo];
      loSel.value = String(lo);
      hiSel.value = String(hi);
    }
    pitchLoHi = [lo, hi];
    applyPitchRange(gd, data, lo, hi);
    // Row window changed (fewer semitones -> finer pooling budget)
    withRenderBusy(() => feed.apply(gd));
  };
  loSel.addEventListener('change', applyRange);
  hiSel.addEventListener('change', applyRange);

  // ---- Top bar: BPM / measure offset / beats per measure ----
  const bpmInput = document.getElementById('bpmInput');
  const offsetInput = document.getElementById('offsetInput');
  const beatsSel = document.getElementById('beatsSel');
  const detectedBpm = data.bpm || 120;
  const detectedOffsetMs = Math.round((data.beatOffsetSec || 0) * 1000);
  bpmInput.value = String(detectedBpm);
  offsetInput.value = String(detectedOffsetMs);
  const maxMs = EPOCH_MS + Math.round(data.durationSec * 1000);
  const applyBpmGrid = () => {
    let bpm = parseFloat(bpmInput.value);
    if (!isFinite(bpm)) bpm = detectedBpm;
    bpm = Math.min(Math.max(bpm, 30), 300);
    let off = parseFloat(offsetInput.value);
    if (!isFinite(off)) off = 0;
    applyGrid(gd, {
      bpm,
      offsetMs: off,
      beats: parseInt(beatsSel.value, 10) || 4,
      minMs: EPOCH_MS,
      maxMs,
    });
  };
  bpmInput.addEventListener('change', applyBpmGrid);
  offsetInput.addEventListener('change', applyBpmGrid);
  beatsSel.addEventListener('change', applyBpmGrid);
  document.getElementById('bpmReset').addEventListener('click', () => {
    bpmInput.value = String(detectedBpm);
    applyBpmGrid();
  });
  document.getElementById('offsetReset').addEventListener('click', () => {
    offsetInput.value = String(detectedOffsetMs);
    applyBpmGrid();
  });

  // ---- Layout sync: force a plotly resize after any container size change,
  // preventing the bottom from being covered ----
  const wrap = document.getElementById('plot-wrap');
  const sync = () => Plotly.Plots.resize(gd);
  let resizeT = 0;
  new ResizeObserver(() => {
    sync();
    // Phase 3.9: the keyboard strip is pinned to fixed pixel geometry, but
    // plotly shapes are fractional — after a paper-width change one cheap
    // shapes-only relayout re-pins it (see applyPlotShapes)
    applyPlotShapes(gd);
    // Row pooling budget follows the plot height: rebuild the rows when it
    // changed enough to move the pooling factor (debounced)
    clearTimeout(resizeT);
    resizeT = setTimeout(() => {
      if (feed.rowsStale()) withRenderBusy(() => feed.apply(gd));
    }, 250);
  }).observe(wrap);
  sync();

  // ---- Live language switch: re-render persistent dynamic labels (static
  // DOM is handled by applyStatic inside setLang; transient status/modal
  // messages keep the language they were shown in) ----
  onChange(() => {
    if (!data.apiBase) resStatus.textContent = t('staticMode');
    floorVal.title = t('clickToEdit');
    gammaVal.title = t('clickToEdit');
    applyHoverLang(gd);
  });

  // ---- Wheel over the spectrogram: plain wheel scrubs playback and pans
  // the view along (wheel down = forward, up = backward); Ctrl+wheel zooms
  // the time axis (Plotly's native scrollZoom is disabled, so zoom is
  // implemented here) ----
  const trackStart = EPOCH_MS;
  const trackEnd = EPOCH_MS + Math.round(data.durationSec * 1000);
  const MIN_SPAN_MS = 1000;
  const SEC_PER_NOTCH = 2; // scrub speed for a standard deltaY=100 notch

  /** Time under the cursor (for cursor-anchored zoom), or null if the
   *  geometry is not (yet) available */
  const cursorMs = (e) => {
    try {
      const fl = gd._fullLayout;
      const rect = gd.getBoundingClientRect();
      const [d0, d1] = fl.xaxis.domain;
      const plotW = gd.clientWidth - fl.margin.l - fl.margin.r;
      const f =
        ((e.clientX - rect.left - fl.margin.l) / plotW - d0) / (d1 - d0);
      const [ra, rb] = [pMs(fl.xaxis.range[0]), pMs(fl.xaxis.range[1])];
      return ra + f * (rb - ra);
    } catch {
      return null;
    }
  };

  /** Clamp [a, b] to the track bounds, keeping the span */
  const clampRange = (a, b) => {
    const span = b - a;
    if (span >= trackEnd - trackStart) return [trackStart, trackEnd];
    if (a < trackStart) {
      a = trackStart;
      b = a + span;
    }
    if (b > trackEnd) {
      b = trackEnd;
      a = b - span;
    }
    return [a, b];
  };

  /** Multiply the visible span by `factor`, keeping `anchorMs` stationary;
   *  clamped to the track bounds (registerRangeClamp double-guards the
   *  relayout anyway) */
  const zoomRange = (a, b, factor, anchorMs) => {
    const span = b - a;
    const newSpan = Math.min(
      Math.max(Math.round(span * factor), MIN_SPAN_MS),
      trackEnd - trackStart
    );
    const f =
      anchorMs === null || span <= 0
        ? 0.5
        : Math.min(Math.max((anchorMs - a) / span, 0), 1);
    const na = a + f * (span - newSpan);
    return clampRange(na, na + newSpan);
  };

  /** Shift the visible window by deltaMs, keeping its span and clamping to
   *  the track bounds */
  const panRange = (a, b, deltaMs) => clampRange(a + deltaMs, b + deltaMs);

  // Transform-settle wheel handling: a Plotly relayout re-rasterizes the
  // whole heatmap slice (~tens of ms), so one per wheel notch would freeze
  // the view. While a wheel stream is active, the applied range stays put
  // and the pending pan/zoom is shown by sliding/scaling the trace layer
  // via an SVG transform (the same trick plotly's own drag pan uses). The
  // commit is lazy: it runs after the gesture goes idle, on pointerdown
  // (clicks/drags need an accurate axis), or early when the pending view
  // escapes the loaded slice — whichever comes first.
  const SETTLE_MS = 500;
  let stream = null; // { raf, timer } while a wheel stream is active
  let pendingPanMs = 0;
  let pendingZoom = null; // { factor, anchorMs }

  /** Range the pending ops would produce, on top of the applied one */
  const pendingRange = () => {
    const r = gd._fullLayout.xaxis.range;
    let a = pMs(r[0]);
    let b = pMs(r[1]);
    if (pendingZoom) {
      [a, b] = zoomRange(a, b, pendingZoom.factor, pendingZoom.anchorMs);
    }
    if (pendingPanMs) {
      [a, b] = panRange(a, b, pendingPanMs);
    }
    return [a, b];
  };

  /** SVG translate+scale mapping the applied range onto [na, nb]; the
   *  playhead overlay follows the same affine so it stays glued to the
   *  content while the view slides */
  const applyViewTransform = (na, nb) => {
    const fl = gd._fullLayout;
    const [d0, d1] = fl.xaxis.domain;
    const plotW = gd.clientWidth - fl.margin.l - fl.margin.r;
    const X0 = fl.margin.l + d0 * plotW;
    const domW = (d1 - d0) * plotW; // usable axis width (domain fraction)
    const r = fl.xaxis.range;
    const a = pMs(r[0]);
    const b = pMs(r[1]);
    const s = (b - a) / (nb - na);
    const C = ((a - na) * domW) / (nb - na);
    const tx = X0 * (1 - s) + C;
    const layer = gd.querySelector('.heatmaplayer');
    if (layer) {
      layer.setAttribute(
        'transform',
        `translate(${tx.toFixed(2)},0) scale(${s.toFixed(5)},1)`
      );
    }
    const x = player.cursorX();
    const ph = player.cursorEl();
    if (x !== null && ph) {
      ph.style.transform = `translateX(${(X0 + C + s * (x - X0)).toFixed(1)}px)`;
    }
  };

  const clearViewTransform = () => {
    const layer = gd.querySelector('.heatmaplayer');
    if (layer) layer.removeAttribute('transform');
  };

  /** Modal-style overlay while a heavy replot blocks the main thread
   *  (committing a wheel gesture, swapping channels...): the same visual
   *  language as the resolution-switch progress modal, in a lighter
   *  variant — dimmer backdrop, no input blocking. The sliding bar is a
   *  composited transform animation, so it keeps moving through the block,
   *  but it must be started a couple of frames BEFORE the blocking call.
   *  Engaged only when the pause is perceptible; re-entrant calls run
   *  without the ceremony. */
  const BUSY_CELLS = 120000;
  let busyEl = null;
  let busyDepth = 0;
  const busyCells = () => gd.data[0].z.length * gd.data[0].z[0].length;
  const withRenderBusy = async (op) => {
    if (busyDepth || busyCells() < BUSY_CELLS) {
      await op();
      return;
    }
    busyDepth = 1;
    if (!busyEl) {
      busyEl = document.createElement('div');
      busyEl.className = 'modal-overlay render-busy-overlay';
      busyEl.innerHTML = `
        <div class="modal">
          <h3>${t('renderingSpec')}</h3>
          <div class="bar"><div class="bar-fill indeterminate"></div></div>
        </div>`;
      document.body.appendChild(busyEl);
    }
    busyEl.style.display = 'flex';
    await new Promise((res) =>
      requestAnimationFrame(() => requestAnimationFrame(res))
    );
    try {
      await op();
    } finally {
      busyEl.style.display = 'none';
      busyDepth = 0;
    }
  };

  /** Commit the pending range with one relayout (the pooled matrix is
   *  complete, so no data work follows; player.reposition restores the
   *  playhead via the same event). immediate (pointer-initiated) commits
   *  synchronously so click/drag coordinates resolve against the fresh
   *  range; idle-timer commits show the render-busy spinner and keep the
   *  transform until the relayout has drawn (same task — no flicker). */
  const settleStream = (immediate = false) => {
    if (!stream) return;
    clearTimeout(stream.timer);
    cancelAnimationFrame(stream.raf);
    stream = null;
    const [na, nb] = pendingRange();
    pendingZoom = null;
    pendingPanMs = 0;
    const r = gd._fullLayout.xaxis.range;
    if (na === pMs(r[0]) && nb === pMs(r[1])) {
      clearViewTransform();
      player.refresh();
      return;
    }
    const commit = () =>
      Plotly.relayout(gd, { 'xaxis.range': [iso(na), iso(nb)] });
    if (immediate) {
      clearViewTransform();
      commit();
      return;
    }
    withRenderBusy(async () => {
      await commit();
      clearViewTransform();
    });
  };

  const streamUpdate = () => {
    stream.raf = 0;
    // The trace holds the complete pooled matrix: a gesture never needs
    // data work, only the transient transform
    applyViewTransform(...pendingRange());
  };

  /** Drop an in-flight stream without committing (before external data
   *  swaps like channel/resolution changes) */
  const cancelStream = () => {
    if (!stream) return;
    clearTimeout(stream.timer);
    cancelAnimationFrame(stream.raf);
    stream = null;
    pendingZoom = null;
    pendingPanMs = 0;
    clearViewTransform();
    player.refresh(); // undo the affine playhead shift (no relayout fires)
  };

  // Clicks and drags resolve coordinates against the applied axis range:
  // commit any pending view first (synchronously) so they land accurately
  plotEl.addEventListener(
    'pointerdown',
    () => settleStream(true),
    { capture: true }
  );

  // ---- Wheel over the spectrogram: plain wheel scrubs playback and pans
  // the view; no damping — the pooled matrix is complete, so a gesture is
  // pure transform work ----
  plotEl.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault(); // also blocks the browser's Ctrl+wheel page zoom
      if (e.ctrlKey) {
        const op = {
          factor: Math.exp((e.deltaY / 100) * 0.5),
          anchorMs: cursorMs(e),
        };
        pendingZoom = pendingZoom
          ? { factor: pendingZoom.factor * op.factor, anchorMs: op.anchorMs }
          : op;
      } else {
        const sec = Math.max(
          -5 * SEC_PER_NOTCH,
          Math.min(5 * SEC_PER_NOTCH, (e.deltaY / 100) * SEC_PER_NOTCH)
        );
        if (sec) {
          // Scrub the playhead and shift the spectrogram view by the same
          // applied delta, so the whole timeline scrolls with the wheel
          const before = player.currentTime();
          const after = player.seekBy(sec);
          const deltaMs = Math.round((after - before) * 1000);
          if (deltaMs) pendingPanMs += deltaMs;
        }
      }
      if (pendingPanMs || pendingZoom) {
        if (!stream) stream = { raf: 0, timer: 0 };
        if (!stream.raf) stream.raf = requestAnimationFrame(streamUpdate);
        clearTimeout(stream.timer);
        stream.timer = setTimeout(settleStream, SETTLE_MS);
      }
    },
    { passive: false }
  );
}

main().catch((err) => {
  document.body.insertAdjacentHTML(
    'afterbegin',
    `<div style="padding:20px;font-family:sans-serif;color:#b00">
       ${t('loadFailed', { msg: err.message })}
     </div>`
  );
  console.error(err);
});
