/** High-resolution lane waveforms (Phase 3.9): on-demand per-pixel-column
 *  min/max ("level of detail") computed OFF the main thread.
 *
 *  Why: the cached full-track envelope (~1024 columns) is perfect for the
 *  overview but becomes blocky buckets when the visible window shrinks to
 *  a few seconds. Pixel-sharp waveforms need one min/max pair per canvas
 *  column over the visible sample range — O(window samples) per redraw,
 *  far too much for the UI thread at 60 Hz, so the scan runs in a Web
 *  Worker (module type, bundled by Vite) and the main thread only paints
 *  the returned Float32Arrays.
 *
 *  Ownership model: the worker keeps ONE mono copy of each lane's PCM
 *  (keyed by lane id, transferred over zero-copy — the AudioBuffer keeps
 *  its own data for playback). Subsequent LOD requests only ship the
 *  view window, so panning/zooming costs a tiny message, not a 40 MB
 *  copy. Without Worker support the same pure math runs synchronously
 *  on a main-thread copy (correct, just blocking).
 */

/** Per-column min/max over the window [t0, t1) seconds of `pcm`.
 *  Pure and allocation-bounded: two Float32Array(columns) out. Columns
 *  outside the signal stay at (0, 0) = silence, matching the envelope
 *  path. */
export function colMinMax(pcm, sr, columns, t0, t1) {
  const n = pcm.length;
  const min = new Float32Array(columns);
  const max = new Float32Array(columns);
  if (n === 0 || columns <= 0 || !(t1 > t0)) return { min, max };
  // sample-space window, clamped to the buffer
  const s0 = Math.max(0, Math.min(n, Math.floor(t0 * sr)));
  const s1 = Math.max(s0, Math.min(n, Math.ceil(t1 * sr)));
  const span = s1 - s0;
  if (span <= 0) return { min, max };
  for (let px = 0; px < columns; px++) {
    const a = s0 + Math.floor((px * span) / columns);
    let b = s0 + Math.floor(((px + 1) * span) / columns);
    if (b <= a) b = a + 1; // every column covers >= 1 sample
    if (a >= s1) break;
    let lo = 0;
    let hi = 0;
    for (let i = a; i < b && i < s1; i++) {
      const v = pcm[i];
      if (v < lo) lo = v;
      else if (v > hi) hi = v;
    }
    min[px] = lo;
    max[px] = hi;
  }
  return { min, max };
}

/** Worker entry: keeps {laneId: Float32Array} PCM copies, answers
 *  {type:'lod'} requests with transferred min/max arrays. */
export function waveWorkerBody() {
  const pcm = new Map();
  self.onmessage = (e) => {
    const m = e.data;
    if (m.type === 'pcm') {
      pcm.set(m.laneId, m.pcm); // transferred in, owned here
      return;
    }
    if (m.type === 'forget') {
      if (m.laneId) pcm.delete(m.laneId);
      else pcm.clear();
      return;
    }
    if (m.type === 'lod') {
      const d = pcm.get(m.laneId);
      const { min, max } = d
        ? colMinMax(d, m.sr, m.columns, m.t0, m.t1)
        : colMinMax(new Float32Array(0), m.sr, m.columns, m.t0, m.t1);
      self.postMessage({ id: m.id, laneId: m.laneId, min, max }, [
        min.buffer,
        max.buffer,
      ]);
    }
  };
}

/** LOD client: one shared worker (or the sync fallback), promise-based,
 *  replies matched by request id. */
export class WaveLod {
  constructor() {
    this.worker = null;
    this.pending = new Map(); // id -> resolve
    this.nextId = 1;
    try {
      this.worker = new Worker(
        new URL('./wave-worker.js', import.meta.url),
        { type: 'module' }
      );
      this.worker.onmessage = (e) => {
        const { id, min, max } = e.data;
        const resolve = this.pending.get(id);
        if (resolve) {
          this.pending.delete(id);
          resolve({ min, max });
        }
      };
      this.worker.onerror = () => {
        // a dead worker fails every pending request; fall back to sync
        for (const resolve of this.pending.values()) resolve(null);
        this.pending.clear();
        try {
          this.worker.terminate();
        } catch {
          /* already gone */
        }
        this.worker = null;
      };
    } catch {
      this.worker = null; // module workers unsupported: sync fallback
    }
  }

  /** Hand the lane's mono PCM to the worker (transferred, zero-copy).
   *  Returns false when there is no worker (caller keeps a local copy). */
  uploadPcm(laneId, pcm) {
    if (!this.worker) return false;
    // copy: the input may be a view the caller keeps (AudioBuffer data)
    const copy = new Float32Array(pcm);
    this.worker.postMessage({ type: 'pcm', laneId, pcm: copy }, [
      copy.buffer,
    ]);
    return true;
  }

  forgetAll() {
    if (this.worker) this.worker.postMessage({ type: 'forget' });
  }

  /** Compute the LOD for [t0, t1) — resolves null when neither the
   *  worker nor the local fallback can serve (caller keeps the current
   *  drawing). `localPcm` is only consulted in sync-fallback mode. */
  compute(laneId, localPcm, sr, columns, t0, t1) {
    if (this.worker) {
      const id = this.nextId++;
      return new Promise((resolve) => {
        this.pending.set(id, resolve);
        this.worker.postMessage({ type: 'lod', id, laneId, sr, columns, t0, t1 });
      });
    }
    return Promise.resolve(
      localPcm ? colMinMax(localPcm, sr, columns, t0, t1) : null
    );
  }
}
