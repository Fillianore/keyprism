/** Stem player (Phase 2 classic source separation).
 *
 *  Fetches /api/stems for the current track, decodes every stem WAV into
 *  the SHARED AudioContext owned by player.js and drives all stem sources
 *  from the main transport events.
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
 */

import { t, onChange } from './i18n.js';

const START_LEAD = 0.06; // keep identical to player.js START_LEAD
const METHODS = ['combined', 'hpss', 'rpca'];
const POLL_MS = 700;

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
  const state = {
    enabled: false,
    loading: false,
    ready: false,
    method: 'combined',
    savedMix: null,
    pollTimer: 0,
    stems: [], // {key, url, buffer, gain, src, volume, muted, solo}
  };

  // ------------------------------------------------------------ engine
  function applyGains() {
    const anySolo = state.stems.some((s) => s.solo);
    for (const s of state.stems) {
      const audible = anySolo ? s.solo : !s.muted;
      s.gain.gain.setTargetAtTime(
        audible ? s.volume : 0,
        ctx.currentTime,
        0.012
      );
    }
    // the Mix row rides the master gain
    const mixAudible = anySolo ? false : !mixRow.muted;
    player.setMixGain(mixAudible ? mixRow.volume : 0, { persist: false });
  }

  function startAll(offset, when) {
    stopAll();
    for (const s of state.stems) {
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
    for (const s of state.stems) {
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

  const mixRow = { volume: 1, muted: true, solo: false, slider: null };

  player.onTransport((type, info) => {
    if (!state.enabled) return;
    if (type === 'play' && state.ready) {
      startAll(info.offset, info.when);
    } else if (type === 'pause') {
      stopAll();
    } else if (type === 'mixgain' && mixRow.slider && !mixRow.muted) {
      // user moved the main volume slider: mirror it into the Mix row
      mixRow.volume = info.norm;
      if (document.activeElement !== mixRow.slider) {
        mixRow.slider.value = String(info.norm);
        paintFill(mixRow.slider);
      }
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

  function buildRow(container, nameText, color, model, onVolume) {
    const row = document.createElement('div');
    row.className = 'stem-row';
    const name = document.createElement('span');
    name.className = 'stem-name';
    name.textContent = nameText;
    if (color) name.style.setProperty('--stem-color', color);
    const vol = document.createElement('input');
    vol.type = 'range';
    vol.className = 'stem-vol';
    vol.min = '0';
    vol.max = '1';
    vol.step = '0.01';
    vol.value = String(model.volume);
    const mute = document.createElement('button');
    mute.type = 'button';
    mute.className = 'stem-btn';
    mute.textContent = 'M';
    mute.title = t('mute');
    mute.setAttribute('aria-label', t('mute'));
    const solo = document.createElement('button');
    solo.type = 'button';
    solo.className = 'stem-btn';
    solo.textContent = 'S';
    solo.title = t('solo');
    solo.setAttribute('aria-label', t('solo'));
    mute.classList.toggle('active', model.muted);
    solo.classList.toggle('active', model.solo);
    mute.addEventListener('click', () => {
      model.muted = !model.muted;
      mute.classList.toggle('active', model.muted);
      applyGains();
    });
    solo.addEventListener('click', () => {
      model.solo = !model.solo;
      solo.classList.toggle('active', model.solo);
      applyGains();
    });
    vol.addEventListener('input', () => {
      model.volume = parseFloat(vol.value);
      paintFill(vol);
      onVolume();
    });
    row.append(name, vol, mute, solo);
    container.appendChild(row);
    paintFill(vol);
    return { row, vol, mute, solo };
  }

  function renderPanel() {
    panel.innerHTML = '';
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

    const mixUi = buildRow(
      panel,
      t('stemMix'),
      '#ddd6c8',
      mixRow,
      () => applyGains()
    );
    mixUi.row.classList.add('stem-row-mix');
    mixRow.slider = mixUi.vol;
    mixUi.vol.value = String(player.mixGainNorm());
    paintFill(mixUi.vol);

    for (const s of state.stems) {
      const meta = STEM_META[s.key] || {};
      buildRow(panel, t(meta.labelKey || s.key), meta.color, s, () =>
        applyGains()
      );
    }
  }

  async function load(method) {
    if (state.loading) return;
    state.loading = true;
    state.method = method;
    state.ready = false;
    renderPanel();
    setStatus(t('stemsSeparating', { done: 0, total: '?' }), true);
    const poll = pollProgress(method);
    try {
      const body = await fetchStemList(method);
      const stemsList = [];
      for (const item of body.stems) {
        const buffer = await decodeStem(item.url);
        const gain = ctx.createGain();
        gain.connect(ctx.destination);
        stemsList.push({
          key: item.key,
          buffer,
          gain,
          src: null,
          volume: 1,
          muted: false,
          solo: false,
        });
      }
      if (state.method !== method) return; // switched away mid-load
      state.stems.forEach((s) => s.gain.disconnect());
      state.stems = stemsList;
      state.ready = true;
      mixRow.muted = true; // stems take over the transport's output
      mixRow.solo = false;
      renderPanel();
      applyGains();
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
    state.stems.forEach((s) => s.gain.disconnect());
    state.stems = [];
    state.ready = false;
    state.loading = false;
    panel.hidden = true;
    // hand the output back to the mix at the pre-enable volume
    mixRow.muted = true;
    if (state.savedMix !== null) {
      player.setMixGain(state.savedMix, { persist: true });
    }
  }

  toggle.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-stems]');
    if (!btn || btn.disabled) return;
    const want = btn.dataset.stems === 'on';
    toggle
      .querySelectorAll('button')
      .forEach((b) => b.classList.toggle('active', b === btn));
    if (want && !state.enabled) enable();
    else if (!want && state.enabled) disable();
  });

  // Live language switch: rebuild the panel (state is kept in models)
  onChange(() => {
    if (state.enabled) renderPanel();
  });

  return {};
}
