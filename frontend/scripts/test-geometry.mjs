#!/usr/bin/env node
/** Shared pitch->pixel mapping test (3.9.1 D).
 *
 *  The master heatmap's y axis and every overlay canvas MUST place a
 *  semitone on the same pixel rows. The master renders from
 *  geometry.keyRangeUnits (the plotly yaxis range) with row centers at
 *  geometry.rowCenterUnit; overlays render via geometry.unitToPlotFraction
 *  band mapping (the arithmetic in layers.js drawLayer). This test locks
 *  the two to each other: for a range of (sub, key-range, plot-height)
 *  combinations, an isolated bright semitone in the stem matrix must land
 *  within 1 px of the master heatmap's row for that pitch.
 *
 *  Pure Node: geometry.js imports nothing browser-bound (the CSS side
 *  effect is guarded), so no DOM is needed.
 */

import {
  keyRangeUnits,
  rowCenterUnit,
  unitToPlotFraction,
} from '../src/geometry.js';

let failures = 0;
const ok = (cond, msg) => {
  if (cond) {
    console.log(`  ok  ${msg}`);
  } else {
    failures++;
    console.error(`  FAIL  ${msg}`);
  }
};

// ---- 1. range convention: exactly the plotly yaxisConfig values ----------
{
  const [ylo, yhi] = keyRangeUnits(5, 12, 60);
  ok(ylo === 12 * 5 - 0.5 && yhi === 61 * 5 - 0.5,
    'keyRangeUnits == [lo*sub-0.5, (hi+1)*sub-0.5] (plotly yaxis range)');
  ok(unitToPlotFraction(keyRangeUnits(1, 0, 87)[1], 1, 0, 87) === 0,
    'fraction 0 at the TOP of the key range (canvas top-down)');
  ok(unitToPlotFraction(keyRangeUnits(1, 0, 87)[0], 1, 0, 87) === 1,
    'fraction 1 at the BOTTOM of the key range');
  ok(rowCenterUnit(21, 1) === 21 && rowCenterUnit(60, 10) === 604.5,
    'rowCenterUnit == m*sub + (sub-1)/2');
}

// ---- 2. overlay-vs-master row alignment (<= 1 px) -------------------------
/** Master heatmap: pixel y of semitone m's center at plot height H
 *  (the plotly yaxis maps row units linearly onto the plot area). */
function masterRowY(m, sub, lo, hi, H) {
  const [ylo, yhi] = keyRangeUnits(sub, lo, hi);
  return (H * (yhi - rowCenterUnit(m, sub))) / (yhi - ylo);
}

/** Overlay: pixel band of stem-spec source row s under the drawLayer
 *  drawImage arithmetic (full visible key range mapped via
 *  unitToPlotFraction; image row 0 = top = C8). Returns [y0, y1]. */
function overlayRowBand(s, sub, lo, hi, H, rows = 88) {
  const fTop = unitToPlotFraction((hi + 1) * sub - 0.5, sub, lo, hi);
  const fBot = unitToPlotFraction(lo * sub - 0.5, sub, lo, hi);
  const yTop = fTop * H;
  const yH = (fBot - fTop) * H;
  const sy = Math.max(0, rows - 1 - hi);
  const sh = Math.min(rows - sy, hi - lo + 1);
  const y0 = yTop + (s - sy) * (yH / sh);
  return [y0, y0 + yH / sh];
}

{
  let worst = 0;
  let cases = 0;
  for (const sub of [1, 5, 10]) {
    for (const [lo, hi] of [
      [0, 87],
      [21, 69],
      [33, 45],
      [55, 87],
    ]) {
      for (const H of [360, 640, 900]) {
        for (const m of [lo, hi, Math.floor((lo + hi) / 2)]) {
          const master = masterRowY(m, sub, lo, hi, H);
          const [b0, b1] = overlayRowBand(87 - m, sub, lo, hi, H);
          const delta = Math.abs((b0 + b1) / 2 - master);
          worst = Math.max(worst, delta);
          cases++;
          if (delta > 1) {
            console.error(
              `  FAIL  sub=${sub} lo=${lo} hi=${hi} H=${H} m=${m}: ` +
                `overlay center ${(b0 + b1) / 2} vs master ${master}`
            );
            failures++;
          }
        }
      }
    }
  }
  ok(failures === 0,
    `row centers match (worst |Δ| = ${worst.toFixed(6)} px over ${cases} cases, <= 1 px)`);
}

// ---- 3. isolated-note scenario: the bright stem row lands on the ----------
//      master's row for that pitch (QA #4, end to end over the mapping)
{
  const sub = 1;
  const lo = 0;
  const hi = 87;
  const H = 640;
  const rows = 88;
  const PITCH = 45; // A2 — a strong isolated bass note
  // stem spec matrix: silence everywhere except the note's row
  const q = new Uint8Array(rows);
  q[PITCH] = 220;
  const s = rows - 1 - PITCH; // image row 0 = top = C8
  const [b0, b1] = overlayRowBand(s, sub, lo, hi, H, rows);
  const brightCenter = (b0 + b1) / 2;
  const master = masterRowY(PITCH, sub, lo, hi, H);
  ok(Math.abs(brightCenter - master) <= 1,
    `isolated note (pitch ${PITCH}): overlay bright-row y ${brightCenter.toFixed(2)} ` +
      `== master row y ${master.toFixed(2)} (|Δ| = ${Math.abs(brightCenter - master).toFixed(3)} px)`);
}

if (failures) {
  console.error(`test:geometry FAILED (${failures} failures)`);
  process.exit(1);
}
console.log('test:geometry OK: shared pitch->pixel mapping, overlay rows within 1 px');
