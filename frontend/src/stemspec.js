/** Per-stem spectrogram client (Phase 3.9, revised 3.9.1): ONE fetch+decode
 *  cache shared by every consumer (lane [Wave|Spec] views AND the master-
 *  plot overlay layers), keyed by `${method}/${stem}` — the backend disk-
 *  caches the quantized matrix, so this is a cheap one-time fetch per stem.
 *
 *  The 88-row semitone matrix (the SAME row space as the master heatmap's
 *  y axis — what makes lane views and overlay layers line up with the
 *  master) is rendered into an offscreen canvas with row 0 = TOP = C8,
 *  tinted from near-black to a target rgb (the lane's own color).
 *
 *  Intensity vs opacity (3.9.1 B): the stored matrix is the dB map
 *  normalized over [-dbRange, 0] (q = 255·(dB + dbRange)/dbRange). The
 *  per-layer INTENSITY controls act on that dB map BEFORE the tint —
 *  `dB' = dB + gain_dB`, then the γ warp of the colormap input with the
 *  MASTER's highlight-γ direction (`v^(1/γ)`, see intensityValue) —
 *  exactly the master's color-floor/γ semantics, while opacity stays an
 *  alpha mix on the composite. The raw q matrix is kept on the image
 *  object so a slider move re-renders the small 88×nCols canvas in place.
 */

const cache = new Map(); // `${method}/${stem}` -> Promise<spec body>

function b64ToU8(b64) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

/** Fetch (or recall) the quantized spec of one stem. Rejects with the
 *  server's error message; the rejection is cached so a permanently
 *  unavailable stem does not re-flood the server. `expect.dbRange`, when
 *  given, must match the server's dB basis (the master payload's
 *  dbRange) — a mismatch throws instead of rendering on a foreign scale. */
export function getStemSpec(apiBase, method, stem, expect = {}) {
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
        if (!body.spec || !body.rows || !body.nCols || !body.hopSec) {
          throw new Error('stem spec: malformed payload');
        }
        if (expect.dbRange !== undefined &&
            Math.abs(body.dbRange - expect.dbRange) > 1e-6) {
          throw new Error(
            `stem spec: dbRange ${body.dbRange} != master ${expect.dbRange}`
          );
        }
        return body;
      })
    );
  }
  return cache.get(key);
}

/** Decode + render the quantized matrix into an offscreen canvas tinted
 *  toward `tint` (css hex) at gain 0 dB / γ 1. The returned object carries
 *  the raw q matrix + basis so intensity re-renders stay cheap. */
export function createSpecImage(spec, tint) {
  const { rows, nCols, hopSec, dbRange } = spec;
  const img = {
    cv: document.createElement('canvas'),
    q: b64ToU8(spec.spec),
    rows,
    nCols,
    hopSec,
    dbRange: dbRange || 70,
  };
  img.cv.width = nCols;
  img.cv.height = rows;
  img.idata = new ImageData(nCols, rows); // reused by every re-render
  renderSpecInto(img, tint, 0, 1);
  return img;
}

/** The per-pixel INTENSITY transform (pure, Node-testable — see
 *  scripts/test-intensity.mjs): a dB shift (the master's color-floor
 *  semantics, clamped at both ends) followed by the γ warp of the
 *  colormap input. γ direction (3.9.1 fix): the MASTER's highlight-γ
 *  (main.js applyGamma) warps the colorscale ANCHORS by p^γ, which is
 *  equivalent to warping the data by v^(1/γ) — so a BIGGER γ reads
 *  BRIGHTER, exactly like the master's slider. (The naive v^γ would
 *  invert the direction: bigger γ = darker.) */
export function intensityValue(q01, gainDb, dbRange, gamma) {
  let t = q01 + gainDb / dbRange;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  return Math.pow(t, 1 / Math.max(0.01, gamma));
}

/** Re-render the tinted image IN PLACE from the raw q matrix with the
 *  given intensity (see intensityValue). Fully independent of any
 *  opacity mixing. */
export function renderSpecInto(img, tint, gainDb, gamma) {
  const { q, rows, nCols, dbRange, idata } = img;
  const px = idata.data;
  const v = parseInt(tint.slice(1), 16);
  const tr = (v >> 16) & 255;
  const tg = (v >> 8) & 255;
  const tb = v & 255;
  // near-black base = the background the image composites onto
  const br = 10;
  const bg = 10;
  const bb = 13;
  for (let row = 0; row < rows; row++) {
    // backend rows are bottom-up (row 0 = A0); image rows are top-down
    const src = (rows - 1 - row) * nCols;
    const dst = row * nCols * 4;
    for (let c = 0; c < nCols; c++) {
      const t = intensityValue(q[src + c] / 255, gainDb, dbRange, gamma);
      const o = dst + c * 4;
      px[o] = br + (tr - br) * t;
      px[o + 1] = bg + (tg - bg) * t;
      px[o + 2] = bb + (tb - bb) * t;
      px[o + 3] = 255;
    }
  }
  // NOTE: fully opaque on purpose — silence stays near-black, which is a
  // no-op under the additive 'screen' blend AND occludes the base in
  // replace ('normal') mode, while looking identical to the lane
  // background in the lane view.
  const g2 = img.cv.getContext('2d');
  g2.clearRect(0, 0, nCols, rows);
  g2.putImageData(idata, 0, 0);
}
