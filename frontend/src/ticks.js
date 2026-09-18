import Plotly from 'plotly.js-dist-min';
import { iso, pMs, pickDtickMs } from './spectrogram.js';

/** Adaptive time-axis ticks: the wider the window, the sparser the ticks;
 *  one tick per second for windows within 16s */
export function registerAdaptiveTicks(gd) {
  gd.on('plotly_relayout', (e) => {
    const r = e['xaxis.range'];
    let a;
    let b;
    if (r) {
      a = r[0];
      b = r[1];
    } else if (e['xaxis.range[0]'] !== undefined) {
      a = e['xaxis.range[0]'];
      b = e['xaxis.range[1]'];
    } else {
      return;
    }
    const span = pMs(b) - pMs(a);
    if (!isFinite(span) || span <= 0) return;
    const dt = pickDtickMs(span);
    if (gd._fullLayout.xaxis.dtick !== dt) {
      Plotly.relayout(gd, { 'xaxis.dtick': dt });
    }
  });
}

/** Range clamping: the time window cannot be dragged outside
 *  [minMs, maxMs] (hard stop at the track's start/end) */
export function registerRangeClamp(gd, minMs, maxMs) {
  const clamp = (a, b) => {
    if (!isFinite(a) || !isFinite(b) || b <= a) return null;
    const span = b - a;
    if (span >= maxMs - minMs) return [minMs, maxMs];
    let na = a;
    let nb = b;
    if (na < minMs) {
      na = minMs;
      nb = na + span;
    }
    if (nb > maxMs) {
      nb = maxMs;
      na = nb - span;
    }
    return na !== a || nb !== b ? [na, nb] : null;
  };

  gd.on('plotly_relayout', (e) => {
    const r = e['xaxis.range'];
    let pair = null;
    if (r) {
      pair = clamp(pMs(r[0]), pMs(r[1]));
    } else if (e['xaxis.range[0]'] !== undefined) {
      pair = clamp(pMs(e['xaxis.range[0]']), pMs(e['xaxis.range[1]']));
    } else if (e['xaxis.autorange']) {
      const fr = gd._fullLayout.xaxis.range;
      pair = clamp(pMs(fr[0]), pMs(fr[1]));
    }
    if (pair) {
      Plotly.relayout(gd, { 'xaxis.range': [iso(pair[0]), iso(pair[1])] });
    }
  });
}
