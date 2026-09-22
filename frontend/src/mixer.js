/** Central DAW-standard mixer state shared by the Phase 2 stem player
 *  (stems.js) and the Phase 3 lane workspace (lanes.js).
 *
 *  Signal chain (anti-clipping architecture):
 *
 *      stem source ──> strip GainNode ──┐
 *      stem source ──> strip GainNode ──┼──> MASTER GainNode ──> destination
 *      mix (player.js own source) ──────┘     (ceiling −10 dB)
 *
 *  The master bus caps separated audio at the same −10 dB ceiling the
 *  transport's main volume uses (player.js VOL_MAX), so equal fader
 *  positions are equally loud on mix and stems and NOTHING can push the
 *  output past the ceiling even with every fader wide open.
 *
 *  Mute/Solo matrix (professional DAW semantics, one source of truth):
 *    - strip audible = soloed ? true : (!anySolo && !muted)
 *      Solo anywhere mutes every non-soloed strip at the GAIN level,
 *      regardless of its Mute button; Mute only matters when nobody
 *      solos.
 *    - Mix row: plays only while it is effectively ALONE — any solo
 *      (except its own) or any unmuted stem silences it, because the
 *      complementary stems already sum to ≈ the mix; mix + stems summed
 *      at unity would double the waveform and clip. The Mix row keeps
 *      its own Mute/Solo buttons and shows the forced silence via the
 *      `.dimmed` row style, never by flipping its button states.
 *
 *  All gain changes ramp via setTargetAtTime (~12 ms): click-free by
 *  construction, scheduling is never touched.
 */

/** Volume ceiling for separated audio — mirrors player.js VOL_MAX
 *  (−10 dB). Change both together. */
export const MASTER_CEILING = Math.pow(10, -10 / 20);

export class MixerState {
  /** @param {AudioContext} ctx the ONE shared AudioContext
   *  @param {(norm: number, opts?: object) => void} mixApply
   *    drives the Mix row's gain (player.setMixGain) */
  constructor(ctx, mixApply) {
    this.ctx = ctx;
    this.mixApply = mixApply;
    this.master = ctx.createGain();
    this.master.gain.value = MASTER_CEILING;
    this.master.connect(ctx.destination);
    this.mix = { id: 'mix', volume: 1, muted: false, solo: false };
    this.strips = []; // {id, gain, buffer, src, volume, muted, solo}
    this.repaints = new Set(); // UI paint hooks (row dimming)
  }

  onRepaint(fn) {
    this.repaints.add(fn);
  }

  get anySolo() {
    return this.mix.solo || this.strips.some((s) => s.solo);
  }

  /** Matrix for a separated strip (see module docstring) */
  stripAudible(s) {
    if (this.anySolo) return s.solo;
    return !s.muted;
  }

  /** Mix audible only while effectively alone (anti-clipping rule) */
  mixAudible() {
    if (this.anySolo) return this.mix.solo && !this.mix.muted;
    return !this.mix.muted && !this.strips.some((s) => !s.muted);
  }

  /** Ramp every strip + the mix to its matrix-audible gain */
  apply() {
    const t = this.ctx.currentTime;
    for (const s of this.strips) {
      s.gain.gain.setTargetAtTime(
        this.stripAudible(s) ? s.volume : 0,
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

  /** Fresh strip in the DEFAULT state: muted (gain 0) at unity volume —
   *  loading stems never changes what the user currently hears (the Mix
   *  keeps playing); separated lanes join silently until unmuted. */
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
      ...extra,
    };
  }

  /** Wire strips into the master bus. Called only after a FULLY
   *  successful load, so a failed decode can never leave half-wired
   *  gain nodes behind (memory/pipeline hygiene). */
  route(strips) {
    this.unroute();
    this.strips = strips;
    for (const s of strips) s.gain.connect(this.master);
  }

  unroute() {
    for (const s of this.strips) s.gain.disconnect();
    this.strips = [];
  }

  dispose() {
    this.unroute();
    this.master.disconnect();
  }
}
