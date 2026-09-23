/** Per-stem spectrogram client (Phase 3.9): ONE fetch+decode cache shared
 *  by every consumer (lane [Wave|Spec] views AND the master-plot overlay
 *  layers), keyed by `${method}/${stem}` — the backend disk-caches the
 *  quantized matrix, so this is a cheap one-time fetch per stem.
 *
 *  The 88-row semitone matrix (the SAME row space as the master heatmap's
 *  y axis — what makes lane views and overlay layers line up with the
 *  master) is rendered once into an offscreen canvas with row 0 = TOP =
 *  C8, tinted from near-black to a target rgb (the lane's own color), so
 *  a drawImage with a source rect is the whole view transform. Silence
 *  stays transparent: the lane/plot background shows through, exactly
 *  like the transparent columns of the waveform view.
 */

const cache = new Map(); // `${method}/${stem}` -> Promise<spec body>

function b64ToU8(b64) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

/** Fetch (or recall) the quantized spec of one stem. Rejects with the
 *  server's error message; the rejection is cached so a permanently
 *  unavailable stem does not re-flood the server. */
export function getStemSpec(apiBase, method, stem) {
  const key = `${method}/${stem}`;
  if (!cache.has(key)) {
    cache.set(
      key,
      fetch(
        `${apiBase}/api/stem_spec?method=${encodeURIComponent(method)}` +
          `&stem=${encodeURIComponent(stem)}`
      ).then(async (r) => {
        const body = await r.json().catch(() => ({}));
        if (!r.ok || body.error) {
          throw new Error(body.error || `HTTP ${r.status}`);
        }
        return body;
      })
    );
  }
  return cache.get(key);
}

/** Render the quantized matrix into an offscreen canvas tinted toward
 *  `tint` (css hex). Returns { cv, hopSec, rows, nCols }. */
export function specToImage(spec, tint) {
  const { rows, nCols, hopSec } = spec;
  const q = b64ToU8(spec.spec);
  const img = new ImageData(nCols, rows);
  const px = img.data;
  const v = parseInt(tint.slice(1), 16);
  const tr = (v >> 16) & 255;
  const tg = (v >> 8) & 255;
  const tb = v & 255;
  // near-black base = the lane/plot background the image composites onto
  const br = 10;
  const bg = 10;
  const bb = 13;
  for (let row = 0; row < rows; row++) {
    // backend rows are bottom-up (row 0 = A0); image rows are top-down
    const src = (rows - 1 - row) * nCols;
    const dst = row * nCols * 4;
    for (let c = 0; c < nCols; c++) {
      const t = q[src + c] / 255;
      const o = dst + c * 4;
      px[o] = br + (tr - br) * t;
      px[o + 1] = bg + (tg - bg) * t;
      px[o + 2] = bb + (tb - bb) * t;
      px[o + 3] = 255;
    }
    // NOTE: fully opaque on purpose — silence stays near-black, which is
    // a no-op under the additive 'screen' blend AND occludes the base in
    // replace ('normal') mode, while looking identical to the lane
    // background in the lane view.
  }
  const cv = document.createElement('canvas');
  cv.width = nCols;
  cv.height = rows;
  cv.getContext('2d').putImageData(img, 0, 0);
  return { cv, hopSec, rows, nCols };
}
