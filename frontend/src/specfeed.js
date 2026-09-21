import Plotly from 'plotly.js-dist-min';
import { EPOCH_MS, iso } from './spectrogram.js';

/** Full-track pooled heatmap feed.
 *
 *  Why pooling: plotly re-rasterizes its entire z matrix into a canvas
 *  image on EVERY replot (there is no partial redraw), so the matrix must
 *  stay as small as the display can resolve:
 *    - rows: the y axis never pans; when the full subband row count exceeds
 *      what the plot height can show (~one row per screen pixel), subband
 *      rows are max-pooled by a divisor of `sub` — peaks stay visible and
 *      replot cost drops proportionally. Narrow pitch ranges get k=1 (full
 *      subband detail) automatically.
 *    - columns: capped at MAX_COLS via max-pooling for very long tracks
 *      (keeps plotly's raster canvas inside browser limits).
 *
 *  The pooled trace is COMPLETE: no viewport slicing, no dynamic loading,
 *  nothing to refill while panning — the whole spectrogram is rendered
 *  once per data/color-affecting change, and view changes during a gesture
 *  are a transient SVG transform (main.js). Pooling keeps geometry exact:
 *  pooled cells sit at their band centers, and a heatmap brick edge is the
 *  midpoint of adjacent centers, so pooled bricks tile exactly like the
 *  un-pooled ones. */

const MAX_COLS = 16000;

/** Ascending divisors of s (candidate row pooling factors) */
function divisors(s) {
  const out = [];
  for (let d = 1; d <= s; d++) {
    if (s % d === 0) out.push(d);
  }
  return out;
}

export function createSpecFeed(data, get) {
  let curRows = { lo: 0, hi: 87, k: 1 }; // row window + pooling factor

  /** Plot-area height in px (row budget); margins t16+b40 subtracted */
  const rowBudget = () =>
    Math.max(120, (get.plotH ? get.plotH() : 600) - 56);

  /** Row window + pooling factor for the current pitch range / sub */
  function rowSpec() {
    const s = get.curSub();
    const loHi = get.pitchLoHi
      ? get.pitchLoHi()
      : [0, data.noteLabels.length - 1];
    const lo = loHi[0];
    const hi = loHi[1];
    const visible = (hi - lo + 1) * s;
    const budget = rowBudget();
    let k = s;
    for (const d of divisors(s)) {
      k = d;
      if (visible / d <= budget) break;
    }
    return { lo, hi, k };
  }

  /** Whether the row window / pooling factor changed (caller should
   *  rebuild after pitch-range, sub or plot-height changes) */
  function rowsStale() {
    const r = rowSpec();
    return r.lo !== curRows.lo || r.hi !== curRows.hi || r.k !== curRows.k;
  }

  /** Build the complete pooled matrix, row centers, column centers and
   *  hover labels */
  function build() {
    curRows = rowSpec();
    const { lo, hi, k } = curRows;
    const s = get.curSub();
    const r0 = lo * s;
    const r1 = (hi + 1) * s;
    const rows = (r1 - r0) / k;
    const nCols = get.nCols();
    const kc = Math.max(1, Math.ceil(nCols / MAX_COLS));
    const cols = Math.ceil(nCols / kc);
    const db = data.dbRange;
    const bin = get.specRaw()[get.curChan()];
    const labels = data.noteLabels;
    const z = new Array(rows);
    const y = new Array(rows);
    const text = new Array(rows);
    for (let r = 0; r < rows; r++) {
      const i0 = r0 + r * k; // first input row of the pooled band
      const row = new Array(cols);
      for (let c = 0; c < cols; c++) {
        const j0 = c * kc;
        const j1 = Math.min(nCols, j0 + kc);
        let v = -Infinity;
        for (let j = j0; j < j1; j++) {
          for (let b = 0; b < k; b++) {
            const q = (bin[(i0 + b) * nCols + j] / 255) * db - db;
            if (q > v) v = q;
          }
        }
        row[c] = v;
      }
      z[r] = row;
      // center of the pooled band, in input-row units (k divides s, so a
      // band never crosses a semitone boundary)
      y[r] = i0 + (k - 1) / 2;
      const m = Math.floor(i0 / s);
      const label =
        k === 1 && s > 1 ? `${labels[m]} (${(i0 % s) + 1}/${s})` : labels[m];
      text[r] = new Array(cols).fill(label);
    }
    const cm = (data.durationSec * 1000) / nCols;
    const xs = new Array(cols);
    for (let c = 0; c < cols; c++) {
      // center of the pooled column group, in ms
      xs[c] = iso(EPOCH_MS + Math.round((c * kc + kc / 2) * cm));
    }
    return { z, y, xs, text };
  }

  /** Rebuild and swap into the trace (data/color-affecting changes only:
   *  channel, subband, pitch range, plot height, resolution) */
  function apply(gd) {
    const b = build();
    Plotly.restyle(gd, { z: [b.z], x: [b.xs], y: [b.y], text: [b.text] }, [0]);
  }

  return { build, apply, rowsStale };
}
