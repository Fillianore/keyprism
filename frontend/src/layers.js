/** Layer compositor (Phase 3.9 M2): drag a lane onto the master
 *  spectrogram to create an OVERLAY LAYER of that stem's spectrogram.
 *
 *  Geometry: every overlay canvas covers EXACTLY the master plot area —
 *  the shared [PLOT_LEFT_PX, W - PLOT_RIGHT_PX] x [margin.t, H - margin.b]
 *  rect from geometry.js — x maps from the master's applied xaxis range
 *  (read fresh on every draw, same view range as the lanes), y maps the
 *  88-row stem spec onto the master heatmap's y range (semitone m spans
 *  row units [m*sub - 0.5, (m+1)*sub - 0.5] within [lo*sub - 0.5,
 *  (hi+1)*sub - 0.5]), so overlay cells sit precisely on the base's
 *  pitch/time grid. Redraws happen ONLY on view-range change (throttled)
 *  and resize — playback moves the DOM playhead on its own layer above
 *  the stack and never repaints a single overlay pixel.
 *
 *  Blending is CSS mix-blend-mode (compositor work, no per-pixel JS):
 *  - 叠加 (additive): blend 'screen' + per-layer opacity — dark pixels
 *    of the tinted spec image are no-ops under screen, so the layer
 *    lightens exactly where the stem has energy;
 *  - 覆盖 (replace): blend 'normal' + opacity 1 — the layer occludes the
 *    base (the spec image is opaque by construction, see stemspec.js).
 *
 *  Layer manager: a compact panel at the master's top-right (below the
 *  plotly modebar) listing active layers topmost-first, with per-layer
 *  blend, opacity, visibility, drag-to-reorder (array order == z-order ==
 *  stack DOM order) and remove. The panel only exists while layers do.
 */

import { t, onChange } from './i18n.js';
import { EPOCH_MS, pMs } from './spectrogram.js';
import { PLOT_LEFT_PX, PLOT_RIGHT_PX } from './geometry.js';
import { getStemSpec, createSpecImage, renderSpecInto } from './stemspec.js';
import { showToast } from './toast.js';
import { throttled } from './util.js';

/** Default intensity/opacity for a new additive layer: unity gain, master
 *  γ semantics, opacity high enough to read but not blind. */
const DEFAULT_OPACITY = 0.85;
const GAIN_MIN = -24;
const GAIN_MAX = 24;
const GAMMA_MIN = 0.3;
const GAMMA_MAX = 3;

let nextLayerId = 1;

export function initLayers({ gd, apiBase, getSub, getPitchLoHi }) {
  // static mode: layers need the backend stem_spec endpoint
  if (!apiBase) return {};

  const wrap = gd.parentElement; // #plot-wrap

  // ---- state -----------------------------------------------------------
  const layers = []; // bottom -> top; DOM order mirrors it
  const stack = document.createElement('div');
  stack.className = 'layer-stack';
  stack.setAttribute('aria-hidden', 'true');
  wrap.appendChild(stack);
  let manager = null;
  let managerList = null;

  // ---- geometry --------------------------------------------------------
  function fitCanvas(cv) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, stack.clientWidth);
    const h = Math.max(1, stack.clientHeight);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    return g;
  }

  /** Pin the stack over the master plot area (shared pixel geometry) */
  function placeStack() {
    stack.style.left = `${PLOT_LEFT_PX}px`;
    stack.style.right = `${PLOT_RIGHT_PX}px`;
    const fl = gd._fullLayout;
    const mt = fl ? fl.margin.t : 16;
    const mb = fl ? fl.margin.b : 40;
    stack.style.top = `${mt}px`;
    stack.style.bottom = `${mb}px`;
  }

  /** Master y range -> canvas y of a row-unit value (top-down) */
  function yOfUnit(r, h, ylo, yhi) {
    return (h * (yhi - r)) / (yhi - ylo);
  }

  // ---- drawing ---------------------------------------------------------
  function drawLayer(layer) {
    const g = fitCanvas(layer.canvas);
    const w = stack.clientWidth;
    const h = stack.clientHeight;
    g.clearRect(0, 0, w, h);
    if (!layer.visible) return;
    if (!layer.img) {
      // transient loading state; failure removes the layer with a toast
      g.fillStyle = 'rgba(243,236,220,0.6)';
      g.font = '12px system-ui, sans-serif';
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText(t('layerLoading'), w / 2, h / 2);
      return;
    }
    const fl = gd._fullLayout;
    if (!fl) return;
    const r = fl.xaxis.range;
    const aMs = pMs(r[0]);
    const bMs = pMs(r[1]);
    const aSec = (aMs - EPOCH_MS) / 1000;
    const span = Math.max(1e-6, (bMs - aMs) / 1000);
    const { cv: img, hopSec, rows } = layer.img;
    // x: view window -> source columns (drawImage clips out-of-range
    // source transparently, matching the lanes' blank margins)
    const sx = aSec / hopSec;
    const sw = span / hopSec;
    // y: the master heatmap's key range (semitone indices [lo, hi] on a
    // sub-band row grid) -> the 88-row spec's band
    const sub = Math.max(1, getSub ? getSub() : 1);
    const [lo, hi] = getPitchLoHi ? getPitchLoHi() : [0, rows - 1];
    const ylo = lo * sub - 0.5;
    const yhi = (hi + 1) * sub - 0.5;
    const yTop = yOfUnit((hi + 1) * sub - 0.5, h, ylo, yhi);
    const yH = yOfUnit(lo * sub - 0.5, h, ylo, yhi) - yTop;
    const sy = Math.max(0, rows - 1 - hi); // image row 0 = top = C8
    const sh = Math.min(rows - sy, hi - lo + 1);
    g.imageSmoothingEnabled = false; // honest cells, like the master
    g.drawImage(img, sx, sy, sw, sh, 0, yTop, w, yH);
  }

  function redraw() {
    placeStack();
    for (const layer of layers) drawLayer(layer);
  }
  const redrawThrottled = throttled(redraw, 120);

  // ---- DOM -------------------------------------------------------------
  function syncStack() {
    for (const layer of layers) stack.appendChild(layer.canvas);
  }

  function paintLayerRow(layer) {
    layer.rowBlend.classList.toggle('active', layer.blend === 'normal');
    layer.rowBlendScreen.classList.toggle('active', layer.blend === 'screen');
    if (layer.blend === 'normal') {
      layer.canvas.style.mixBlendMode = 'normal';
      layer.canvas.style.opacity = '1';
      layer.rowOp.value = '1';
      layer.rowOp.disabled = true;
    } else {
      layer.canvas.style.mixBlendMode = 'screen';
      layer.canvas.style.opacity = String(layer.opacity);
      layer.rowOp.value = String(layer.opacity);
      layer.rowOp.disabled = false;
    }
    paintFill(layer.rowOp);
    // intensity controls (independent of opacity): values + readouts
    layer.rowGain.value = String(layer.gainDb);
    layer.rowGamma.value = String(layer.gamma);
    layer.rowGainVal.textContent = fmtGain(layer.gainDb);
    layer.rowGammaVal.textContent = layer.gamma.toFixed(2);
    paintFill(layer.rowGain);
    paintFill(layer.rowGamma);
    layer.canvas.style.display = layer.visible ? 'block' : 'none';
    layer.rowEye.textContent = layer.visible ? '◉' : '◌';
    layer.rowEye.classList.toggle('off', !layer.visible);
    layer.rowEl.classList.toggle('hidden-layer', !layer.visible);
  }

  const fmtGain = (db) => `${db > 0 ? '+' : ''}${db}`;

  /** INTENSITY (3.9.1 B): re-render the layer's tinted image from the raw
   *  q matrix with dB' = dB + gain and v' = v^gamma, then repaint. This
   *  shifts WHICH energies light up (spectral redistribution); opacity
   *  only alpha-mixes the whole result — visibly different controls. */
  function applyIntensity(layer) {
    if (!layer.img) return;
    renderSpecInto(layer.img, layer.color, layer.gainDb, layer.gamma);
    drawLayer(layer);
  }

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

  function renderManager() {
    if (!layers.length) {
      if (manager) manager.remove();
      manager = null;
      managerList = null;
      return;
    }
    if (!manager) {
      manager = document.createElement('div');
      manager.className = 'layer-manager';
      const title = document.createElement('div');
      title.className = 'layer-manager-title';
      manager.appendChild(title);
      managerList = document.createElement('div');
      managerList.className = 'layer-manager-list';
      manager.appendChild(managerList);
      wrap.appendChild(manager);
    }
    manager.querySelector('.layer-manager-title').textContent =
      `${t('layersTitle')} (${layers.length})`;
    managerList.textContent = '';
    // topmost layer first (array is bottom -> top)
    for (let i = layers.length - 1; i >= 0; i--) {
      managerList.appendChild(buildRow(layers[i]));
    }
  }

  function buildRow(layer) {
    const row = document.createElement('div');
    row.className = 'layer-row';

    // line 1: grip | dot | name | blend | eye | remove
    const top = document.createElement('div');
    top.className = 'layer-row-top';
    const grip = document.createElement('button');
    grip.type = 'button';
    grip.className = 'layer-grip';
    grip.textContent = '☰';
    grip.title = t('layerReorderTip');
    const dot = document.createElement('span');
    dot.className = 'layer-dot';
    dot.style.setProperty('--layer-color', layer.color);
    const name = document.createElement('span');
    name.className = 'layer-name';
    name.textContent = t(layer.labelKey);
    top.append(grip, dot, name);

    const blend = document.createElement('div');
    blend.className = 'layer-blend';
    const bScreen = document.createElement('button');
    bScreen.type = 'button';
    bScreen.textContent = t('layerBlendAdd');
    const bNormal = document.createElement('button');
    bNormal.type = 'button';
    bNormal.textContent = t('layerBlendCover');
    blend.title = t('layerBlendTitle');
    blend.append(bScreen, bNormal);
    top.appendChild(blend);

    // opacity: alpha-mix on the composited layer — NOT intensity; kept on
    // the top line so the two controls read as different things
    const op = document.createElement('input');
    op.type = 'range';
    op.className = 'layer-op';
    op.min = '0';
    op.max = '1';
    op.step = '0.01';
    op.title = t('layerOpacity');
    top.appendChild(op);

    const eye = document.createElement('button');
    eye.type = 'button';
    eye.className = 'layer-eye';
    eye.title = t('layerVisible');
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'layer-x';
    rm.textContent = '×';
    rm.title = t('layerRemove');
    top.append(eye, rm);
    row.appendChild(top);

    // line 2: INTENSITY — gain(dB) + γ on the layer's dB matrix before the
    // tint (master color-floor/γ semantics); orthogonal to opacity
    const fx = document.createElement('div');
    fx.className = 'layer-row-fx';
    const gainLab = document.createElement('span');
    gainLab.className = 'fx-lab';
    gainLab.textContent = 'dB';
    const gain = document.createElement('input');
    gain.type = 'range';
    gain.className = 'layer-fx-slider';
    gain.min = String(GAIN_MIN);
    gain.max = String(GAIN_MAX);
    gain.step = '1';
    gain.title = t('layerGainTitle');
    const gainVal = document.createElement('span');
    gainVal.className = 'fx-val';
    const gammaLab = document.createElement('span');
    gammaLab.className = 'fx-lab';
    gammaLab.textContent = 'γ';
    const gamma = document.createElement('input');
    gamma.type = 'range';
    gamma.className = 'layer-fx-slider';
    gamma.min = String(GAMMA_MIN);
    gamma.max = String(GAMMA_MAX);
    gamma.step = '0.05';
    gamma.title = t('layerGammaTitle');
    const gammaVal = document.createElement('span');
    gammaVal.className = 'fx-val';
    fx.append(gainLab, gain, gainVal, gammaLab, gamma, gammaVal);
    row.appendChild(fx);

    // keep the widget references the painter needs
    layer.rowEl = row;
    layer.rowOp = op;
    layer.rowEye = eye;
    layer.rowBlend = blend;
    layer.rowBlendScreen = bScreen;
    layer.rowGain = gain;
    layer.rowGainVal = gainVal;
    layer.rowGamma = gamma;
    layer.rowGammaVal = gammaVal;

    grip.addEventListener('pointerdown', (ev) => beginReorder(layer, ev));
    bScreen.addEventListener('click', () => {
      layer.blend = 'screen';
      paintLayerRow(layer);
    });
    bNormal.addEventListener('click', () => {
      layer.blend = 'normal'; // replace: occludes (opacity forced to 1)
      paintLayerRow(layer);
    });
    op.addEventListener('input', () => {
      if (layer.blend === 'screen') {
        layer.opacity = Math.min(1, Math.max(0, parseFloat(op.value)));
        layer.canvas.style.opacity = String(layer.opacity);
      }
      paintFill(op);
    });
    gain.addEventListener('input', () => {
      layer.gainDb = Math.min(
        GAIN_MAX,
        Math.max(GAIN_MIN, Math.round(parseFloat(gain.value)))
      );
      applyIntensity(layer);
      paintLayerRow(layer);
    });
    gamma.addEventListener('input', () => {
      layer.gamma = Math.min(
        GAMMA_MAX,
        Math.max(GAMMA_MIN, parseFloat(gamma.value) || 1)
      );
      applyIntensity(layer);
      paintLayerRow(layer);
    });
    eye.addEventListener('click', () => {
      layer.visible = !layer.visible;
      paintLayerRow(layer);
      drawLayer(layer);
    });
    rm.addEventListener('click', () => removeLayer(layer));
    paintLayerRow(layer);
    return row;
  }

  function removeLayer(layer) {
    const i = layers.indexOf(layer);
    if (i >= 0) layers.splice(i, 1);
    layer.canvas.remove();
    renderManager();
    redraw();
  }

  // ---- drag-to-reorder ---------------------------------------------------
  function beginReorder(layer, ev) {
    ev.preventDefault();
    const move = (e) => {
      if (!managerList) return; // manager gone (all layers removed mid-drag)
      const rows = [...managerList.children];
      const target = rows.find((r) => {
        const rc = r.getBoundingClientRect();
        return e.clientY >= rc.top && e.clientY <= rc.bottom && r !== layer.rowEl;
      });
      if (!target) return;
      const ti = layers.findIndex((l) => l.rowEl === target);
      const from = layers.indexOf(layer);
      if (ti < 0 || ti === from) return;
      layers.splice(from, 1);
      layers.splice(ti, 0, layer);
      syncStack(); // array order == z-order == stack DOM order
      renderManager(); // re-render the list (paints widget refs anew)
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  // ---- drag-to-overlay (lane handle -> drop on the master) ---------------
  function beginLaneDrag(lane, ev) {
    const info = {
      stem: lane.key,
      method: lane.method || undefined, // set by lanes.js at drag time
      color: lane.color,
      labelKey: lane.labelKey,
    };
    const ghost = document.createElement('div');
    ghost.className = 'layer-drag-ghost';
    ghost.style.setProperty('--layer-color', info.color);
    ghost.textContent = `⧉ ${t(info.labelKey)}`;
    document.body.appendChild(ghost);
    const moveGhost = (e) => {
      ghost.style.left = `${e.clientX + 12}px`;
      ghost.style.top = `${e.clientY + 10}px`;
      const rc = wrap.getBoundingClientRect();
      const over =
        e.clientX >= rc.left && e.clientX <= rc.right &&
        e.clientY >= rc.top && e.clientY <= rc.bottom;
      wrap.classList.toggle('drop-ok', over);
      ghost.classList.toggle('drop-ok', over);
    };
    moveGhost(ev);
    const cleanup = () => {
      window.removeEventListener('pointermove', moveGhost);
      window.removeEventListener('pointerup', up);
      window.removeEventListener('keydown', onKey);
      ghost.remove();
      wrap.classList.remove('drop-ok');
    };
    const onKey = (e) => {
      if (e.key === 'Escape') cleanup();
    };
    const up = (e) => {
      const rc = wrap.getBoundingClientRect();
      const over =
        e.clientX >= rc.left && e.clientX <= rc.right &&
        e.clientY >= rc.top && e.clientY <= rc.bottom;
      cleanup();
      if (over) addLayer(info);
    };
    window.addEventListener('pointermove', moveGhost);
    window.addEventListener('pointerup', up);
    window.addEventListener('keydown', onKey);
  }

  async function addLayer(info) {
    const layer = {
      id: nextLayerId++,
      stem: info.stem,
      method: info.method,
      color: info.color,
      labelKey: info.labelKey,
      opacity: DEFAULT_OPACITY,
      blend: 'screen', // 叠加 default; 覆盖 is one click away
      gainDb: 0, // INTENSITY (3.9.1 B): dB shift before the tint
      gamma: 1, // INTENSITY: colormap-input exponent (master γ semantics)
      visible: true,
      img: null,
      canvas: document.createElement('canvas'),
    };
    layer.canvas.className = 'layer-canvas';
    stack.appendChild(layer.canvas);
    layers.push(layer);
    renderManager();
    redraw();
    try {
      const spec = await getStemSpec(apiBase, layer.method, layer.stem);
      layer.img = createSpecImage(spec, layer.color);
    } catch (e) {
      showToast(t('laneSpecFailed', { msg: e?.message || String(e) }));
      removeLayer(layer);
      return;
    }
    // a layer removed while loading must not paint into a detached canvas
    if (layers.includes(layer)) {
      applyIntensity(layer); // re-render under the current gain/γ, then draw
      renderManager();
    }
  }

  // ---- wiring ------------------------------------------------------------
  // overlays redraw only on view-range/geometry change (never per frame)
  gd.on('plotly_relayout', redrawThrottled);
  new ResizeObserver(throttled(redraw, 120)).observe(wrap);
  onChange(() => {
    if (manager) renderManager(); // re-translate labels
  });
  placeStack();
  redraw();

  return { beginLaneDrag };
}
