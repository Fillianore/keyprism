import Plotly from 'plotly.js-dist-min';
import { t } from './i18n.js';
import {
  PLOT_LEFT_PX,
  PLOT_RIGHT_PX,
  keyRangeUnits,
  rowCenterUnit,
} from './geometry.js';

export const EPOCH_MS = Date.UTC(2020, 0, 1);
export const N_ROWS = 88;

// Keyboard strip + pitch labels: BOTH live inside the empty left plot
// margin [0, PLOT_LEFT_PX) — pitch labels leftmost, the keyboard filling
// the remainder and hugging the plot-area left edge (small gap only), so
// the plot area [PLOT_LEFT_PX, W - PLOT_RIGHT_PX] contains ONLY spectrum.
// CRITICAL plotly fact (3.9.1 QA root cause): 'paper' shape/annotation
// coordinates span the PLOTTING AREA INSIDE THE MARGINS, so the margin
// zone is NEGATIVE paper territory — the fractions below are derived from
// the pixel geometry at draw time (see keyStripFrac / pitchLabelX) and
// re-pinned on resize by applyPlotShapes (main.js resize observer).
const KEY_LABEL_X_PX = 10; // pitch labels (C8..C1) start here (leftmost)
const KEY_X0_PX = 40; // keyboard strip left edge (right of the labels)
const KEY_GAP_PX = 4; // keyboard hugs the plot area: only this gap remains
const KEY_BLACK_W = 0.62; // black key length (horizontal), like a real piano
const KEY_BLACK_H = 1.0; // black key width (vertical): full row, same as white keys
const KEY_WHITE_FILL = '#F4EFE2';
const KEY_BLACK_FILL = '#121215';
const KEY_LINE_LIGHT = '#CFC8B4';
const KEY_LINE_STRONG = '#928A72';
const KEY_PC0 = 9; // row 0 is A0 (MIDI 21): pitch class of row m = (m + 9) % 12
const KEY_FONT = { size: 9, color: '#9a9282' };
const pcOf = (m) => (m + KEY_PC0) % 12;

/** Current figure width in px (set by buildFigure / applyPlotShapes);
 *  margin-zone paper fractions are computed against it. */
let paperW = 0;

/** Width of the plotting area (inside the fixed margins) in px: the base
 *  that 'paper' coordinates are normalized against. */
function plotW() {
  return Math.max(1, (paperW > 0 ? paperW : 1280) - PLOT_LEFT_PX -
    PLOT_RIGHT_PX);
}

/** Keyboard strip [left, right] as paper fractions: pinned to the pixel
 *  geometry [KEY_X0_PX, PLOT_LEFT_PX - KEY_GAP_PX] — inside the left
 *  margin, hence NEGATIVE paper coordinates. */
function keyStripFrac() {
  const l = (KEY_X0_PX - PLOT_LEFT_PX) / plotW();
  return [l, -KEY_GAP_PX / plotW()];
}

/** Pitch-label annotation x as a paper fraction (left-anchored): pins the
 *  labels to KEY_LABEL_X_PX in the margin, leftmost of the keyboard. */
function pitchLabelX() {
  return (KEY_LABEL_X_PX - PLOT_LEFT_PX) / plotW();
}

export function iso(ms) {
  return new Date(ms).toISOString();
}

export function pMs(v) {
  if (typeof v === 'number') return v;
  // Accept both ISO (T separator, optional Z) and plotly's internal format
  // (space separator, no Z); always interpreted as UTC
  const m =
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(Z)?$/.exec(v);
  if (m) {
    const ms = m[7] ? Math.round(parseFloat(`0.${m[7]}`) * 1000) : 0;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], ms);
  }
  return Date.parse(v);
}

/** mm:ss:mmm format (minutes:seconds:milliseconds) */
export function fmtRel(ms) {
  const total = Math.max(0, Math.round(ms - EPOCH_MS));
  const mm = Math.floor(total / 60000);
  const ss = Math.floor((total % 60000) / 1000);
  const mmm = total % 1000;
  return `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}:${String(
    mmm
  ).padStart(3, '0')}`;
}

/** Virtual keyboard strip: white keys span whole rows; black keys keep the
 *  real-piano 62% horizontal length but span their full semitone row
 *  vertically (virtual keyboard, no need for the shorter real-piano keys).
 *  White-key group separators: E|F and B|C sit on the direct row boundary,
 *  the other five (C|D, D|E, F|G, G|A, A|B) pass through the center of the
 *  black key between the two white keys (hidden under it, visible only in
 *  the slivers above/below).
 *  lo/hi are the visible semitone row range; fractions are relative to it */
function keyboardShapes(lo = 0, hi = N_ROWS - 1) {
  const [l, r] = keyStripFrac();
  const bw = (r - l) * KEY_BLACK_W;
  const n = hi - lo + 1;
  const shapes = [
    {
      type: 'rect',
      xref: 'paper',
      yref: 'y domain',
      x0: l,
      x1: r,
      y0: 0.0,
      y1: 1.0,
      fillcolor: KEY_WHITE_FILL,
      line: { width: 0 },
    },
  ];
  for (let m = lo; m <= hi; m++) {
    const pc = pcOf(m);
    if ([1, 3, 6, 8, 10].includes(pc)) {
      // boundary between the flanking white keys, through the black key's
      // center (the black key drawn later covers its middle)
      const c = (m + 0.5 - lo) / n;
      shapes.push({
        type: 'line',
        xref: 'paper',
        yref: 'y domain',
        x0: l,
        x1: r,
        y0: c,
        y1: c,
        line: { width: 1.0, color: KEY_LINE_LIGHT },
      });
    } else if ([4, 11].includes(pc) && m + 1 <= hi) {
      // E|F and B|C: direct white-white row boundary
      const y = (m + 1 - lo) / n;
      shapes.push({
        type: 'line',
        xref: 'paper',
        yref: 'y domain',
        x0: l,
        x1: r,
        y0: y,
        y1: y,
        line: { width: 1.6, color: KEY_LINE_STRONG },
      });
    }
  }
  const half = KEY_BLACK_H / (2 * n);
  for (let i = lo; i <= hi; i++) {
    if ([1, 3, 6, 8, 10].includes(pcOf(i))) {
      const c = (i + 0.5 - lo) / n;
      shapes.push({
        type: 'rect',
        xref: 'paper',
        yref: 'y domain',
        x0: l,
        x1: l + bw,
        y0: c - half,
        y1: c + half,
        fillcolor: KEY_BLACK_FILL,
        line: { width: 0.5, color: '#000000' },
      });
    }
  }
  shapes.push({
    type: 'rect',
    xref: 'paper',
    yref: 'y domain',
    x0: l,
    x1: r,
    y0: 0.0,
    y1: 1.0,
    fillcolor: 'rgba(0,0,0,0)',
    line: { width: 1, color: '#948B76' },
  });
  return shapes;
}

/** Measure grid state (BPM / offset / beats per measure), updated by
 *  applyGrid */
let gridState = null;
let pitchRange = [0, N_ROWS - 1]; // semitone range
let sub = 1; // subbands per semitone (row count = N_ROWS * sub)

/** y-axis semitone row of semitone m's center — the shared geometry
 *  mapping (geometry.js, 3.9.1 D: ONE pitch→pixel definition serves the
 *  master axis AND the overlay canvases) */
const rowOf = (m) => rowCenterUnit(m, sub);

/** Pitch labels (C-note names) live in the left margin as ANNOTATIONS:
 *  plotly axis tick labels can only hug the axis edge, but the labels must
 *  sit LEFTMOST in the margin zone (the keyboard fills the rest). Data
 *  coords y keep every label glued to its row center (setSub moves the
 *  range, the annotations follow automatically). */
let axisLabels = null; // { cTickIdx, noteLabels } (set in buildFigure)

function pitchLabelAnnotations() {
  if (!axisLabels) return [];
  const [lo, hi] = pitchRange;
  const x = pitchLabelX();
  return axisLabels.cTickIdx
    .filter((i) => i >= lo && i <= hi)
    .map((i) => ({
      xref: 'paper', // negative = the left margin zone
      yref: 'y',
      x,
      y: rowOf(i),
      xanchor: 'left',
      yanchor: 'middle',
      showarrow: false,
      text: axisLabels.noteLabels[i],
      font: KEY_FONT,
    }));
}

/** Per-cell note labels for the hover tooltip are built by the spec feed
 *  (specfeed.js) together with z — pooled width keeps the allocation tiny
 *  at high resolutions */

function yaxisConfig() {
  const [lo, hi] = pitchRange;
  return {
    range: keyRangeUnits(sub, lo, hi),
    fixedrange: true,
    // pitch labels are annotations in the margin (pitchLabelAnnotations)
    showticklabels: false,
    linecolor: '#3b3833',
    gridcolor: 'rgba(255,255,255,0.04)',
  };
}

/** Switch subbands per semitone (row count changes); re-apply the y axis
 *  (shapes use axis-domain fractions so they are unaffected). The per-cell
 *  hover text and z/y are rebuilt by the spec feed (specfeed.js) */
export function setSub(gd, s, data) {
  sub = s;
  Plotly.relayout(gd, { yaxis: yaxisConfig() });
}

/** Re-apply the hover template after a language switch */
export function applyHoverLang(gd) {
  Plotly.restyle(gd, { hovertemplate: [t('hoverTemplate')] }, [0]);
}

function gridShapes() {
  if (!gridState) return [];
  const { bpm, offsetMs, beats, minMs, maxMs } = gridState;
  const beatMs = 60000 / bpm;
  const barMs = beatMs * beats;
  // normalize the offset into [0, barMs)
  let t0 = minMs + (((offsetMs % barMs) + barMs) % barMs);
  const out = [];
  for (let t = t0; t <= maxMs; t += barMs) {
    out.push({
      type: 'line',
      xref: 'x',
      yref: 'paper',
      x0: iso(t),
      x1: iso(t),
      y0: 0,
      y1: 1,
      line: { width: 1.4, color: 'rgba(226,196,138,0.5)' },
    });
    for (let b = 1; b < beats && t + b * beatMs < maxMs; b++) {
      const tb = t + b * beatMs;
      out.push({
        type: 'line',
        xref: 'x',
        yref: 'paper',
        x0: iso(tb),
        x1: iso(tb),
        y0: 0,
        y1: 1,
        line: { width: 1, color: 'rgba(226,196,138,0.15)' },
      });
    }
  }
  return out;
}

/** Assemble all current shapes: keyboard + measure grid (the playback cursor
 *  is an HTML overlay and does not occupy a shape) */
function currentShapes() {
  return [
    ...keyboardShapes(pitchRange[0], pitchRange[1]),
    ...gridShapes(),
  ];
}

/** Re-pin the pixel-anchored margin furniture (keyboard strip + pitch
 *  labels) after a figure-width change: plotly shape/annotation
 *  coordinates are fractional, so a resize rescales them; one cheap
 *  shapes+annotations relayout keeps everything glued to the fixed pixel
 *  geometry. No-op when the width did not change. */
export function applyPlotShapes(gd) {
  const w = gd.clientWidth;
  if (!w || w === paperW) return;
  paperW = w;
  Plotly.relayout(gd, {
    shapes: currentShapes(),
    annotations: pitchLabelAnnotations(),
  });
}

/** Apply the measure grid (BPM / offset in ms / beats per measure) */
export function applyGrid(gd, opts) {
  gridState = opts;
  Plotly.relayout(gd, { shapes: currentShapes() });
}

/** Apply the pitch range: clip the y axis + full rebuild of the margin
 *  furniture (keyboard rows and pitch labels follow the visible window) */
export function applyPitchRange(gd, data, lo, hi) {
  pitchRange = [lo, hi];
  Plotly.relayout(gd, {
    yaxis: yaxisConfig(),
    shapes: currentShapes(),
    annotations: pitchLabelAnnotations(),
  });
}

/** Create the heatmap and layout; `spec` is the complete pooled matrix
 *  built by the spec feed (specfeed.js). Returns { gd } */
export function buildFigure(el, data, spec) {
  const initViewMs = data.initViewSec * 1000;
  sub = data.defaultSub || 1;
  axisLabels = { cTickIdx: data.cTickIdx, noteLabels: data.noteLabels };
  if (data.bpm) {
    // Initial grid: backend-estimated BPM and first-beat offset, 4 beats per
    // measure
    gridState = {
      bpm: data.bpm,
      offsetMs: (data.beatOffsetSec || 0) * 1000,
      beats: 4,
      minMs: EPOCH_MS,
      maxMs: EPOCH_MS + Math.round(data.durationSec * 1000),
    };
  }

  const traces = [
    {
      type: 'heatmap',
      z: spec.z,
      x: spec.xs,
      // pooled row centers in input-row units (see specfeed.js)
      y: spec.y,
      colorscale: data.colorscales[data.defaultCmap],
      zmin: -data.dbRange,
      zmax: 0,
      showscale: false,
      text: spec.text,
      hovertemplate: t('hoverTemplate'),
      zsmooth: false,
    },
  ];

  const tickStyle = { color: '#9a9282', size: 10 };
  paperW = el.clientWidth || 0;
  const layout = {
    dragmode: 'pan', // drag = pan, no box-select zoom
    font: { family: 'Microsoft YaHei, sans-serif', color: '#d3ccbd' },
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    // Phase 3.9 shared geometry: fixed pixel margins + full-domain axis so
    // the plot area is EXACTLY [PLOT_LEFT_PX, W - PLOT_RIGHT_PX] — the
    // identical boundaries every lane canvas maps time onto (geometry.js).
    // autoexpand off: nothing may grow the margins and break the pinning.
    margin: {
      l: PLOT_LEFT_PX,
      r: PLOT_RIGHT_PX,
      t: 16,
      b: 40,
      autoexpand: false,
    },
    shapes: currentShapes(),
    // pitch labels live in the left margin (leftmost, keyboard fills the
    // remainder up to the plot edge — see the KEY_* constants above)
    annotations: pitchLabelAnnotations(),
    xaxis: {
      type: 'date',
      domain: [0.0, 1.0],
      range: [spec.xs[0], iso(EPOCH_MS + initViewMs)],
      tickformat: '%M:%S',
      dtick: pickDtickMs(initViewMs),
      tickfont: tickStyle,
      linecolor: '#3b3833',
      gridcolor: 'rgba(255,255,255,0.05)',
      zerolinecolor: '#3b3833',
    },
    yaxis: yaxisConfig(),
  };

  Plotly.newPlot(el, traces, layout, {
    responsive: true,
    // Wheel zoom is handled manually in main.js: plain wheel scrubs
    // playback, Ctrl+wheel zooms the time axis (y is locked by fixedrange)
    scrollZoom: false,
  });
  return { gd: el };
}

const STEPS_MS = [
  1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000,
];

export function pickDtickMs(spanMs) {
  for (const s of STEPS_MS) {
    if (spanMs / s <= 16) return s;
  }
  return STEPS_MS[STEPS_MS.length - 1];
}
