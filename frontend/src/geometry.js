/** Shared plot-area geometry (Phase 3.9): ONE source of truth for the
 *  horizontal pixel boundaries of the master spectrogram plot area AND
 *  every lane canvas plot area.
 *
 *  Why: a drum hit at t=5 s must land on the SAME vertical line in the
 *  master heatmap and in every lane waveform below it. That only holds
 *  when both panels map time onto identical absolute pixel columns, i.e.
 *  identical left/right plot-area boundaries. Plotly expresses its plot
 *  area as (margins + fractional domain), CSS as layout boxes — the two
 *  are pinned together here:
 *
 *  - PLOT_LEFT_PX / PLOT_RIGHT_PX are the exported JS consts consumed by
 *    spectrogram.js (master figure margins + domain [0, 1], so the plot
 *    area is EXACTLY [PLOT_LEFT_PX, W - PLOT_RIGHT_PX] in pixels) and by
 *    layers.js (overlay canvases cover exactly that rect);
 *  - applyPlotGeometry() mirrors them into the --plot-left /
 *    --plot-right CSS custom properties, from which style.css derives
 *    the lane grid (label + controls columns + gaps sum to --plot-left,
 *    so the 1fr scope column — the lane canvas — starts at PLOT_LEFT_PX
 *    and ends at W - PLOT_RIGHT_PX by construction).
 *
 *  Keep the arithmetic in style.css (.lane-row / .lanes-panel) in sync:
 *  plot-left = panel-pad + label-w + gap + ctl-w + gap = 280 px.
 */

export const PLOT_LEFT_PX = 280;
export const PLOT_RIGHT_PX = 16;

/** Mirror the pixel consts into the CSS custom properties (idempotent;
 *  runs at import time so the first layout already uses them). Guarded so
 *  the module stays importable in pure-Node tests (test-geometry.mjs). */
export function applyPlotGeometry() {
  if (typeof document === 'undefined') return;
  const s = document.documentElement.style;
  s.setProperty('--plot-left', `${PLOT_LEFT_PX}px`);
  s.setProperty('--plot-right', `${PLOT_RIGHT_PX}px`);
}

applyPlotGeometry();

// ---- Shared pitch -> pixel mapping (3.9.1 D) ----------------------------
// ONE definition of the key-range row geometry, consumed by BOTH the
// master heatmap (yaxisConfig in spectrogram.js — plotly renders the axis
// from it) AND every overlay canvas (drawLayer in layers.js). Semitone m
// spans row units [m*sub - 0.5, (m+1)*sub - 0.5]; its center row is
// m*sub + (sub-1)/2. Fractions are 0 at the TOP of the key range and 1 at
// the bottom (plotly y is bottom-up, canvas y is top-down — the fraction
// is deliberately expressed top-down because every canvas consumer maps
// it directly to pixels).

/** The master y-axis range in row units for the visible key range */
export function keyRangeUnits(sub, lo, hi) {
  return [lo * sub - 0.5, (hi + 1) * sub - 0.5];
}

/** Row-unit value of semitone m's center row */
export function rowCenterUnit(m, sub) {
  return m * sub + (sub - 1) / 2;
}

/** Top-down fraction (0 = top of the key range, 1 = bottom) of a row-unit
 *  value within the visible key range [lo, hi] at `sub` subbands */
export function unitToPlotFraction(r, sub, lo, hi) {
  const [ylo, yhi] = keyRangeUnits(sub, lo, hi);
  return (yhi - r) / (yhi - ylo);
}
