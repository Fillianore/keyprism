#!/usr/bin/env node
/** Layer intensity transform test (3.9.1 gamma-direction regression).
 *
 *  Locks intensityValue (stemspec.js) to the MASTER's highlight-γ
 *  semantics: the master warps colorscale anchors by p^γ (main.js
 *  applyGamma), which is equivalent to warping the data by v^(1/γ) —
 *  so a BIGGER γ must read BRIGHTER. The naive v^γ inverts this
 *  (bigger γ = darker), which is exactly the reported bug.
 *
 *  Pure Node: stemspec.js touches the DOM only inside functions.
 */

import { intensityValue } from '../src/stemspec.js';

let failures = 0;
const ok = (cond, msg) => {
  if (cond) {
    console.log(`  ok  ${msg}`);
  } else {
    failures++;
    console.error(`  FAIL  ${msg}`);
  }
};

const DB = 70;
const mid = 0.5;

// ---- 1. identity + clamps ------------------------------------------------
ok(intensityValue(mid, 0, DB, 1) === mid, 'γ=1, gain=0: identity');
ok(intensityValue(0, 0, DB, 2) === 0, 'silence stays black (bottom clamp)');
ok(intensityValue(1, 0, DB, 0.3) === 1, 'full scale stays full (top clamp)');
ok(intensityValue(0.2, -24, DB, 1) === 0, 'gain -24 dB floors to black');
// 0.7 == -21 dB: +24 dB exceeds full scale, so the ceiling clamps
ok(intensityValue(0.7, 24, DB, 1) === 1, 'gain +24 dB ceilings to full');

// ---- 2. gamma DIRECTION: bigger γ = brighter (master highlight-γ) --------
ok(intensityValue(mid, 0, DB, 2) > intensityValue(mid, 0, DB, 1),
  'γ 2.0 brighter than γ 1.0 at mid-tone (master direction)');
ok(intensityValue(mid, 0, DB, 1) > intensityValue(mid, 0, DB, 0.5),
  'γ 1.0 brighter than γ 0.5 at mid-tone');
ok(intensityValue(mid, 0, DB, 3) > intensityValue(mid, 0, DB, 2) &&
   intensityValue(mid, 0, DB, 2) > intensityValue(mid, 0, DB, 1) &&
   intensityValue(mid, 0, DB, 1) > intensityValue(mid, 0, DB, 0.5) &&
   intensityValue(mid, 0, DB, 0.5) > intensityValue(mid, 0, DB, 0.3),
  'strictly monotonic brightness over the whole slider range (0.3 → 3.0)');

// ---- 3. gain DIRECTION: bigger gain = brighter ----------------------------
ok(intensityValue(mid, 6, DB, 1) > intensityValue(mid, 0, DB, 1) &&
   intensityValue(mid, 0, DB, 1) > intensityValue(mid, -6, DB, 1),
  'gain direction: +6 dB brighter, -6 dB darker (orthogonal to γ)');

// ---- 4. equivalence with the master's colorscale-anchor warp -------------
// master applyGamma: anchors (p, c) -> (p^γ, c). On an identity ramp
// (c = p) the color shown at data value v is exactly u = v^(1/γ) whenever
// v sits on a warped anchor (between anchors plotly interpolates linearly
// in warp space, so anchors are the exact points). The layer's data warp
// must reproduce those positions.
{
  const g = 1.6;
  let maxErr = 0;
  for (let k = 1; k < 32; k++) {
    const p = k / 32;
    const v = Math.pow(p, g); // a warped anchor position
    maxErr = Math.max(maxErr, Math.abs(intensityValue(v, 0, DB, g) - p));
  }
  ok(maxErr < 1e-12,
    `layer warp inverts the master anchor warp exactly at anchors (max err ${maxErr.toExponential(2)})`);
}

if (failures) {
  console.error(`test:intensity FAILED (${failures} failures)`);
  process.exit(1);
}
console.log('test:intensity OK: gain/γ directions match the master (bigger γ = brighter)');
