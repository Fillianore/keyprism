/** Central DAW-standard mixer state shared by the Phase 2 stem player
 *  (stems.js) and the Phase 3 lane workspace (lanes.js).
 *
 *  Signal chain (Phase 3.7 gain staging):
 *
 *      stem source ──> strip GainNode (dB fader) ──┐
 *      stem source ──> strip GainNode (dB fader) ──┼──> MASTER GainNode
 *      mix (player.js own source) ─────────────────┘     (ceiling −10 dB)
 *                                                        │
 *                                                   BRICKWALL LIMITER
 *                                                        │
 *                                                 ctx.destination
 *
 *  - MASTER caps separated audio at the same −10 dB ceiling the
 *    transport's main volume uses (player.js VOL_MAX), so the mix and
 *    the stems stay comparable at equal fader positions.
 *  - LIMITER (why): per-stem make-up defaults (up to +12 dB, derived
 *    from stem/mix peaks at decode) and the +18 dB fader ceiling mean
 *    several open stems can sum far past full scale AFTER the master
 *    bus. The DynamicsCompressorNode (threshold −1 dB, knee 0,
 *    ratio 20:1, attack 1 ms, release 100 ms) acts as a brickwall:
 *    everything above −1 dBFS leaves at ≤ 1/20 of its overshoot. The
 *    threshold is −1 dB (not 0) so the compressor's 1 ms attack-time
 *    overshoot on sharp transients still cannot clip the destination.
 *
 *  Mute/Solo is a DESTRUCTIVE state machine (Phase 3.7): solo is not a
 *    virtual overlay — it MATERIALIZES as real mute states, so
 *    effective audibility is simply NOT muted and every button renders
 *    strictly from state.
 *
 *  Per strip: {muted, solo}. Invariant I1: never (muted AND solo) —
 *  the M+S-both-gold UI state is unrepresentable by construction.
 *
 *  Transition table (A = pressed strip, B = any other strip, X = the
 *  active soloist; strips include the Mix row):
 *  T1 press S on A (off→on): A.solo=T, A.muted=F; snapshot ← the mutes
 *     at engage (captured ONLY when no solo is active — a solo MOVE
 *     keeps the frozen snapshot so a later release restores the user's
 *     pre-solo mutes, not the solo-forced ones); every B≠A: B.muted=T,
 *     B.solo=F. Pressing S on B while A solo = the solo MOVES to B.
 *  T2 press S on A (on→off): A.solo=F; restore mutes from the snapshot
 *     (except A itself — releasing its own solo leaves it audible).
 *  T3 press M on muted A while any X solo: apply T2 to X first
 *     (restore), then A.muted=F — "unmuting a track releases the solo".
 *  T4 press M on unmuted A: A.muted=T; if A was soloed, apply T2 first
 *     (so a strip can never end up muted AND soloed).
 *  T5 press S on muted A: A.muted=F, then T1.
 *
 *  Mix-row anti-clip ducking (unchanged, gain-level + `.dimmed` row):
 *    the Mix plays only while it is effectively ALONE — any solo except
 *    its own, or any unmuted stem, silences it, because the
 *    complementary stems already sum to ≈ the mix; mix + stems summed
 *    at unity would double the waveform and clip.
 *
 *  All gain changes ramp via setTargetAtTime (~12 ms): click-free by
 *  construction, scheduling is never touched.
 */

/** Volume ceiling for separated audio — mirrors player.js VOL_MAX
 *  (−10 dB). Change both together. */
export const MASTER_CEILING = Math.pow(10, -10 / 20);

/** dB → linear amplitude */
export const dbToLin = (db) => Math.pow(10, db / 20);

/** Max |sample| over every channel of a decoded AudioBuffer — the stem
 *  peak that drives the make-up gain. Pure: no WebAudio objects. */
export function bufferPeak(buffer) {
  let p = 0;
  for (let c = 0; c < buffer.numberOfChannels; c++) {
    const d = buffer.getChannelData(c);
    for (let i = 0; i < d.length; i++) {
      const v = Math.abs(d[i]);
      if (v > p) p = v;
    }
  }
  return p;
}

/** Make-up gain that matches a stem's peak to the mix peak, clamped to
 *  [0, +12] dB: a stem already louder than the mix gets NO attenuation
 *  (0 dB), a quiet stem at most +12 dB so the fader keeps headroom. */
export function makeupDb(pMix, pStem) {
  const db = 20 * Math.log10(Math.max(pMix, 1e-9) / Math.max(pStem, 1e-6));
  return Math.min(12, Math.max(0, db));
}

export class MixerState {
  /** @param {AudioContext} ctx the ONE shared AudioContext
   *  @param {(norm: number, opts?: object) => void} mixApply
   *    drives the Mix row's gain (player.setMixGain) */
  constructor(ctx, mixApply) {
    this.ctx = ctx;
    this.mixApply = mixApply;
    // Brickwall limiter between the master bus and the destination
    // (rationale in the module docstring)
    this.limiter = ctx.createDynamicsCompressor();
    this.limiter.threshold.value = -1;
    this.limiter.knee.value = 0;
    this.limiter.ratio.value = 20;
    this.limiter.attack.value = 0.001;
    this.limiter.release.value = 0.1;
    this.master = ctx.createGain();
    this.master.gain.value = MASTER_CEILING;
    this.master.connect(this.limiter);
    this.limiter.connect(ctx.destination);
    this.mix = { id: 'mix', volume: 1, muted: false, solo: false };
    this.strips = []; // {id, gain, buffer, src, volume, muted, solo, dbGain, makeupDb, peak}
    this.snapshot = null; // Map id -> user mute at solo engage
    this.repaints = new Set(); // UI paint hooks (buttons + row dimming)
  }

  onRepaint(fn) {
    this.repaints.add(fn);
  }

  /** All strips INCLUDING the Mix row — the state machine treats them
   *  uniformly (soloing a stem force-mutes the Mix row too). */
  all() {
    return [this.mix, ...this.strips];
  }

  get anySolo() {
    return this.all().some((s) => s.solo);
  }

  soloist() {
    return this.all().find((s) => s.solo) || null;
  }

  /** Restore user mutes captured at solo engage, skipping `except`
   *  (the strip releasing its own solo stays in its current state). */
  restore(exceptId) {
    if (!this.snapshot) return;
    for (const s of this.all()) {
      if (s.id === exceptId) continue;
      if (this.snapshot.has(s.id)) s.muted = this.snapshot.get(s.id);
    }
  }

  /** Press the S button on strip `a` (T1/T2/T5 + solo move) */
  pressSolo(a) {
    if (a.solo) {
      // T2: release — restore others' user mutes, A stays audible
      a.solo = false;
      this.restore(a.id);
      this.snapshot = null;
      return;
    }
    // T5: soloing a muted strip unmutes it first
    // snapshot only at ENGAGE — during a solo move the current mutes
    // are solo-forced, not user choices, so the frozen snapshot is kept
    const snap = this.anySolo
      ? this.snapshot
      : new Map(this.all().map((s) => [s.id, s.muted]));
    a.muted = false;
    for (const b of this.all()) {
      if (b === a) continue;
      b.muted = true; // T1: solo materializes as real mutes
      b.solo = false; // a previous soloist hands the solo over
    }
    a.solo = true;
    this.snapshot = snap;
  }

  /** Press the M button on strip `a` (T3/T4) */
  pressMute(a) {
    const soloist = this.soloist();
    if (a.muted) {
      // unmute press
      if (soloist && soloist !== a) {
        // T3: unmuting a track releases the solo first
        soloist.solo = false;
        this.restore(soloist.id);
        this.snapshot = null;
      }
      a.muted = false;
    } else {
      // mute press
      if (a.solo) {
        // T4: muting the soloist releases its solo first
        a.solo = false;
        this.restore(a.id);
        this.snapshot = null;
      }
      a.muted = true;
    }
  }

  /** Effective audibility of a strip: purely NOT muted (destructive
   *  solo already wrote itself into the mute states). */
  stripAudible(s) {
    return !s.muted;
  }

  /** Mix audible only while effectively alone (anti-clipping rule) */
  mixAudible() {
    if (this.anySolo) return this.mix.solo && !this.mix.muted;
    return !this.mix.muted && !this.strips.some((s) => !s.muted);
  }

  /** Ramp every strip + the mix to its audible gain. Strip gain is the
   *  dB fader (default = the strip's make-up gain); the Mix row keeps
   *  its normalized 0..1 volume (the transport's own −10 dB ceiling
   *  applies on the player side). */
  apply() {
    const t = this.ctx.currentTime;
    for (const s of this.strips) {
      s.gain.gain.setTargetAtTime(
        s.muted ? 0 : dbToLin(s.dbGain || 0),
        t,
        0.012
      );
    }
    this.mixApply(this.mixAudible() ? this.mix.volume : 0, {
      persist: false,
    });
    this.repaint();
  }

  repaint() {
    for (const fn of this.repaints) fn();
  }

  /** Fresh strip in the DEFAULT state: muted (gain 0) with the fader
   *  pre-set to its make-up gain once the buffer peak is known —
   *  loading stems never changes what the user currently hears (the Mix
   *  keeps playing); separated lanes join silently until unmuted, then
   *  audition at mix-comparable loudness. */
  makeStrip(id, extra = {}) {
    const gain = this.ctx.createGain();
    gain.gain.value = 0; // muted until the first apply() opens it
    return {
      id,
      buffer: null,
      gain,
      src: null,
      volume: 1,
      muted: true,
      solo: false,
      dbGain: 0,
      makeupDb: 0,
      peak: 0,
      ...extra,
    };
  }

  /** Wire strips into the master bus. Called only after a FULLY
   *  successful load, so a failed decode can never leave half-wired
   *  gain nodes behind (memory/pipeline hygiene). A reload retires any
   *  active solo snapshot (the strips it described are gone). */
  route(strips) {
    this.unroute();
    this.strips = strips;
    this.snapshot = null;
    for (const s of strips) s.gain.connect(this.master);
  }

  unroute() {
    for (const s of this.strips) s.gain.disconnect();
    this.strips = [];
    this.snapshot = null;
  }

  dispose() {
    this.unroute();
    this.master.disconnect();
    this.limiter.disconnect();
  }
}
