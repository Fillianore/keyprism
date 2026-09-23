/** LOD wave worker (Phase 3.9): computes per-pixel-column min/max for the
 *  lane waveforms off the main thread. Thin shell around the pure
 *  colMinMax in wavelod.js (bundled in by Vite). */
import { waveWorkerBody } from './wavelod.js';

waveWorkerBody();
