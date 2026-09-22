/** Stem player (Phase 2 classic source separation).
 *
 *  Fetches /api/stems for the current track, decodes every stem WAV into
 *  the SHARED AudioContext owned by player.js and drives all stem sources
 *  from the main transport events. Gain architecture lives in the shared
 *  MixerState (mixer.js): every strip feeds the MASTER GainNode (−10 dB
 *  ceiling) before the destination — nothing can clip, and the DAW
 *  mute/solo matrix is computed in ONE place.
 *
 *  Sync design (the whole point of this module):
 *  - one shared AudioContext: all stems + the mix live on the same
 *    hardware clock, so there is nothing to drift apart;
 *  - the transport emits ('play', {offset, when}) with ONE absolute
 *    timestamp `when = ctx.currentTime + START_LEAD`; every stem's
 *    AudioBufferSourceNode (and the mix source in player.js) is started
 *    with source.start(when, offset) — never serial awaits, never
 *    setTimeout — so all channels begin on the same audio frame and stay
 *    sample-locked for the whole track;
 *  - stem WAVs are rendered from the same cached complex STFT at the same
 *    sample rate, so identical offsets address identical sample positions;
 *  - volume/mute/solo never touch scheduling: they only ramp GainNodes
 *    (setTargetAtTime, ~12 ms), which is click- and pop-free.
 *
 *  Mixer defaults (anti-clipping, destructive solo — Phase 3.7):
 *  - loading stems does NOT change what the user hears: the Mix keeps
 *    playing at its current volume, every separated stem starts MUTED
 *    (gain 0) until explicitly unmuted/soloed;
 *  - each stem fader defaults to its MAKE-UP gain (mix peak / stem
 *    peak, clamped to +12 dB), so unmuting a stem auditions it at
 *    mix-comparable loudness; the brickwall limiter on the master bus
 *    (mixer.js) holds the sum with every fader wide open;
 *  - unmuting any stem hands the lead to the stems: the Mix is silenced
 *    at gain level (its content is already inside the stems — summing
 *    both would double the waveform and clip) and its row is shown
 *    `.dimmed`; soloing a stem force-mutes every other row (the M
 *    buttons render that real state) and unmuting any row releases it.
 */

import { t, onChange } from './i18n.js';
import { MixerState, bufferPeak, makeupDb } from './mixer.js';
import { trackRow, wireMuteSolo } from './controls.js';

const START_LEAD = 0.06; // keep identical to player.js START_LEAD
const METHODS = ['combined', 'hpss', 'rpca'];
const POLL_MS = 700;

/** Fixed stem contract per method (mirrors keyprism.stems.STEM_SPECS —
 *  the frontend never reads Python data). The /api/stems response MUST
 *  match EXACTLY this list, in this order, for every file length: a
 *  server that answers chunk-count-dependent stems is a contract
 *  violation and fails loudly here instead of rendering mystery rows. */
const EXPECTED_STEMS = {
  combined: ['harmonic', 'percussive'],
  hpss: ['harmonic', 'percussive'],
  rpca: ['lowrank', 'sparse'],
};

/** i18n labels + overlay colors per stem key (mirrors the visual
 *  language; the frontend never reads Python data) */
const STEM_META = {
  harmonic: { labelKey: 'stemHarmonic', color: '#e2c48a' },
  percussive: { labelKey: 'stemPercussive', color: '#8fb8d8' },
  lowrank: { labelKey: 'stemLowRank', color: '#e2c48a' },
  sparse: { labelKey: 'stemSparse', color: '#8fb8d8' },
};

export function initStems({ data, player, apiBase }) {
  const toggle = document.getElementById('stemsToggle');
  const panel = document.getElementById('stemsPanel');
  if (!toggle || !panel) return {};
  if (!apiBase) {
    // static mode: separation is a backend feature
    toggle
      .querySelectorAll('button')
      .forEach((b) => (b.disabled = true));
    return {};
  }

  const ctx = player.getContext();
  const mixer = new MixerState(ctx, (norm, opts) =>
    player.setMixGain(norm, opts)
  );
  const state = {
    enabled: false,
    loading: false,
    ready: false,
    method: 'combined',
    savedMix: null,
    rows: [], // {model, row, vol, mute, solo} mirrors for repaint
  };

  // ------------------------------------------------------------ engine
  function startAll(offset, when) {
    stopAll();
    for (const s of mixer.strips) {
      if (!s.buffer) continue;
      const end = s.buffer.duration - 0.005;
      if (offset >= end) continue;
      const src = ctx.createBufferSource();
      src.buffer = s.buffer;
      src.connect(s.gain);
      // THE sync primitive: one absolute timestamp for every source
      src.start(when, Math.min(Math.max(offset, 0), end));
      s.src = src;
    }
  }

  function stopAll() {
    for (const s of mixer.strips) {
      if (!s.src) continue;
      s.src.onended = null;
      try {
        s.src.stop();
      } catch {
        /* already stopped */
      }
      s.src.disconnect();
      s.src = null;
    }
  }

  player.onTransport((type, info) => {
    if (!state.enabled) return;
    if (type === 'play' && state.ready) {
      startAll(info.offset, info.when);
    } else if (type === 'pause') {
      stopAll();
    } else if (type === 'mixgain' && mixer.mix && !mixer.mix.muted) {
      // user moved the main volume slider: mirror it into the Mix row
      mixer.mix.volume = info.norm;
      state.rows.find((r) => r.model === mixer.mix)?.syncFader?.();
    }
  });

  // -------------------------------------------------------------- data
  async function fetchStemList(method) {
    const r = await fetch(`${apiBase}/api/stems?method=${method}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    if (body.error) throw new Error(body.error);
    return body;
  }

  async function decodeStem(url) {
    const ab = await (await fetch(url)).arrayBuffer();
    return await ctx.decodeAudioData(ab); // shared context: same clock
  }

  function setStatus(text, busy = false) {
    const el = panel.querySelector('.stems-status');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('busy', busy);
  }

  async function pollProgress(method) {
    while (state.loading && state.method === method) {
      try {
        const r = await fetch(
          `${apiBase}/api/stems?method=${method}&progress=1`
        );
        const body = await r.json();
        if (body.total > 0) {
          setStatus(
            t('stemsSeparating', { done: body.done, total: body.total }),
            true
          );
        }
      } catch {
        /* progress is best-effort */
      }
      await new Promise((res) => setTimeout(res, POLL_MS));
    }
  }

  // --------------------------------------------------------------- UI
  function paintFill(el) {
    const min = parseFloat(el.min);
    const max = parseFloat(el.max);
    const v = parseFloat(el.value);
    const p =
      isFinite(min) && isFinite(max) && max > min && isFinite(v)
        ? ((v - min) / (max - min)) * 100
        : 0;
    el.style.setProperty('--fill', `${Math.min(Math.max(p, 0), 100)}%`);
  }

  /** Why a row is currently inaudible (tooltip copy for D6) */
  function suppressionTip(model, anySolo) {
    if (anySolo && !model.solo) return t('soloSuppressedTip');
    return model === mixer.mix ? t('mixerMixDuckedTip') : t('mixerMutedTip');
  }

  /** Repaint the matrix on the rows: M/S buttons render STRICTLY from
   *  the model (destructive solo writes real mute states, so the gold
   *  M on suppressed rows is the actual state, never flipped behind
   *  the user's back); a strip that is inaudible is shown `.dimmed`
   *  WITH a tooltip explaining why. */
  function paintStates() {
    const anySolo = mixer.anySolo;
    for (const { model, row, mute, solo } of state.rows) {
      mute.classList.toggle('active', model.muted);
      solo.classList.toggle('active', !!model.solo);
      const audible =
        model === mixer.mix ? mixer.mixAudible() : mixer.stripAudible(model);
      row.classList.toggle('dimmed', !audible);
      row.title = audible ? '' : suppressionTip(model, anySolo);
    }
  }
  mixer.onRepaint(paintStates);

  /** One row via the shared factory (controls.js): <icon><label> cell +
   *  fader/M/S controls cell — identical construction for the Mix row
   *  and every stem row (D3). Stem rows get the dB fader; the Mix row
   *  keeps the normalized transport volume. */
  function buildRow(container, key, nameText, color, model) {
    const parts = trackRow({
      key,
      labelText: nameText,
      color,
      fader: model === mixer.mix ? 'norm' : 'db',
    });
    const { syncFader } = wireMuteSolo({
      mixer,
      model,
      vol: parts.vol,
      mute: parts.mute,
      solo: parts.solo,
      dbEl: parts.db,
      apply: () => mixer.apply(),
      paintFill,
    });
    container.appendChild(parts.row);
    const entry = {
      model,
      row: parts.row,
      vol: parts.vol,
      mute: parts.mute,
      solo: parts.solo,
      syncFader,
    };
    state.rows.push(entry);
    return entry;
  }

  function renderPanel() {
    panel.innerHTML = '';
    state.rows = [];
    const title = document.createElement('span');
    title.className = 'stems-title';
    title.textContent = t('stems');
    const methodSel = document.createElement('select');
    methodSel.className = 'stems-method';
    for (const m of METHODS) {
      const o = document.createElement('option');
      o.value = m;
      o.textContent = t(`stemsMethod_${m}`);
      if (m === state.method) o.selected = true;
      methodSel.appendChild(o);
    }
    methodSel.addEventListener('change', () => {
      if (!methodSel.disabled && methodSel.value !== state.method) {
        load(methodSel.value);
      }
    });
    const status = document.createElement('span');
    status.className = 'stems-status';
    panel.append(title, methodSel, status);
    methodSel.disabled = state.loading;

    if (!state.ready) return;

    // Mix row: the transport's own playback, ridden by the master gain
    const mixUi = buildRow(panel, 'mix', t('stemMix'), '#ddd6c8', mixer.mix);
    mixUi.row.classList.add('stem-row-mix');
    mixer.mix.volume = player.mixGainNorm();
    mixUi.syncFader();

    for (const s of mixer.strips) {
      const meta = STEM_META[s.key] || {};
      buildRow(panel, s.key, t(meta.labelKey || s.key), meta.color, s);
    }
    paintStates();
  }

  async function load(method) {
    if (state.loading) return;
    state.loading = true;
    state.method = method;
    state.ready = false;
    stopAll(); // a method switch retires the previous strips' sources
    renderPanel();
    setStatus(t('stemsSeparating', { done: 0, total: '?' }), true);
    const poll = pollProgress(method);
    let strips = [];
    try {
      const body = await fetchStemList(method);
      // Strict stem contract (fixed names per method, any file length)
      const keys = body.stems.map((s) => s.key);
      const expected = EXPECTED_STEMS[method];
      if (
        keys.length !== expected.length ||
        keys.some((k, i) => k !== expected[i])
      ) {
        throw new Error(
          `${t('stemsFailed', { msg: method })}: ${keys.join(', ')}`
        );
      }
      const pMix = player.mixPeak();
      for (const item of body.stems) {
        // makeStrip defaults: MUTED (gain 0) — loading stems never
        // changes what the user currently hears
        const strip = mixer.makeStrip(item.key, { key: item.key });
        strips.push(strip);
        strip.buffer = await decodeStem(item.url);
        // gain staging (I2): fader default = make-up gain matching the
        // stem peak to the mix peak, so an unmuted stem auditions at
        // mix-comparable loudness
        strip.peak = bufferPeak(strip.buffer);
        strip.makeupDb = makeupDb(pMix, strip.peak);
        strip.dbGain = strip.makeupDb;
      }
      if (state.method !== method) return; // switched away mid-load
      mixer.route(strips); // wire into the master bus only when complete
      strips = [];
      state.ready = true;
      // The Mix keeps playing at its current volume; the Mix row fader
      // mirrors it (stems stay muted until the user opens one)
      mixer.mix.volume = player.mixGainNorm();
      mixer.mix.muted = false;
      mixer.mix.solo = false;
      renderPanel();
      mixer.apply();
      if (player.isPlaying()) {
        // jump in synced at the current position (short 60 ms handover)
        startAll(player.currentTime(), ctx.currentTime + START_LEAD);
      }
      setStatus('');
    } catch (e) {
      if (state.method === method) {
        state.ready = false;
        renderPanel();
        setStatus(t('stemsFailed', { msg: e.message }));
      }
    } finally {
      // a failed/partial load leaves no gain nodes wired anywhere
      strips.forEach((s) => s.gain.disconnect());
      state.loading = false; // ends the progress poll loop
    }
    await poll;
  }

  function enable() {
    state.enabled = true;
    state.savedMix = player.mixGainNorm();
    panel.hidden = false;
    load(state.method);
  }

  function disable() {
    state.enabled = false;
    stopAll();
    mixer.unroute();
    state.ready = false;
    state.loading = false;
    panel.hidden = true;
    mixer.mix.solo = false;
    // hand the output back to the mix at the pre-enable volume
    if (state.savedMix !== null) {
      player.setMixGain(state.savedMix, { persist: true });
    }
    mixer.mix.volume = state.savedMix ?? mixer.mix.volume;
    mixer.mix.muted = false;
  }

  toggle.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-stems]');
    if (!btn || btn.disabled) return;
    const want = btn.dataset.stems === 'on';
    if ((want && !state.enabled) || (!want && state.enabled)) {
      toggle
        .querySelectorAll('button')
        .forEach((b) => b.classList.toggle('active', b === btn));
      if (want) enable();
      else disable();
    }
  });

  // Live language switch: rebuild the panel (state is kept in models)
  onChange(() => {
    if (state.enabled) renderPanel();
  });

  return {};
}
