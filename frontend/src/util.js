/** Leading+trailing throttle: shared utility (extracted from main.js so
 *  note-overlay re-culling reuses the exact same throttle as heatmap
 *  recoloring instead of reinventing setTimeout bookkeeping). High-rate
 *  expensive redraws (heatmap recolor, shape re-culling) get coalesced:
 *  the leading call fires immediately, further calls collapse into one
 *  trailing call after `ms` of quiet. */
export function throttled(fn, ms = 120) {
  let last = 0;
  let timer = null;
  let pending = null;
  const run = () => {
    last = Date.now();
    timer = null;
    fn(...pending);
  };
  return (...args) => {
    pending = args;
    const wait = ms - (Date.now() - last);
    if (wait <= 0) {
      if (timer) clearTimeout(timer);
      run();
    } else if (!timer) {
      timer = setTimeout(run, wait);
    }
  };
}
