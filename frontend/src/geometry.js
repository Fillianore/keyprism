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
 *  runs at import time so the first layout already uses them). */
export function applyPlotGeometry() {
  const s = document.documentElement.style;
  s.setProperty('--plot-left', `${PLOT_LEFT_PX}px`);
  s.setProperty('--plot-right', `${PLOT_RIGHT_PX}px`);
}

applyPlotGeometry();
