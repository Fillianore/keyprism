import Plotly from 'plotly.js-dist-min';
import { EPOCH_MS, iso, pMs, fmtRel } from './spectrogram.js';

/** Player: fully buffered Web Audio engine + click-to-seek + smooth cursor +
 *  follow viewport
 *
 * Why not the <audio> element:
 *   - seeking in a data:URL MP3 requires a decoding scan, which stutters and
 *     makes currentTime drift proportionally from the real sample position
 *     (VBR duration misestimates can accumulate to seconds)
 *   - after decodeAudioData decodes everything, a seek is just a buffer
 *     offset: instant, and the clock is sample-accurate (ctx.currentTime),
 *     so cursor and sound stay strictly in sync
 */
export function createPlayer(container, gd, opts) {
  const { audioUrl, offsetSec, endSec, durMs } = opts;
  const OFFSET = offsetSec;
  const END = endSec ?? -1;
  const TRUE_DUR = durMs / 1000;

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  const ctx = new AudioCtx();
  const gain = ctx.createGain();
  gain.connect(ctx.destination);

  const VOL_KEY = 'piano-spec-volume';
  const VOL_MAX = Math.pow(10, -10 / 20); // volume ceiling: -10 dB attenuation
  let volSaved = VOL_MAX;
  try {
    const saved = parseFloat(localStorage.getItem(VOL_KEY));
    if (isFinite(saved)) volSaved = Math.min(Math.max(saved, 0), VOL_MAX);
  } catch {
    /* localStorage unavailable: ignore */
  }
  gain.gain.value = volSaved;

  let buffer = null; // decoded PCM
  let src = null; // current BufferSource
  let startCtx = 0; // ctx.currentTime at play start
  let startOffset = 0; // buffer offset at play start
  let savedOffset = 0; // paused position
  let playing = false;
  let endTimer = null;

  const specMs = (t) => EPOCH_MS + (t - OFFSET) * 1000;
  const audioT = (ms) => OFFSET + (ms - EPOCH_MS) / 1000;
  const duration = () => (buffer ? buffer.duration : TRUE_DUR);
  const limit = () =>
    END > 0 ? Math.min(END, duration()) : duration();

  const outLatency = () => {
    const l = ctx.outputLatency || 0;
    return isFinite(l) ? l : 0;
  };

  /** The current "audible" moment: sample clock minus output latency,
   *  aligned with the ear */
  const curTime = () => {
    if (!buffer) return 0;
    let t;
    if (playing) {
      t = startOffset + (ctx.currentTime - startCtx - outLatency());
      t = Math.min(Math.max(t, startOffset), duration());
    } else {
      t = savedOffset;
    }
    return t;
  };

  // ---- Control bar ----
  // Icons: fill and stroke share a color with round joins for fully rounded
  // geometry; the primary key uses engraved dark brown, secondary keys
  // champagne gold
  const ICONS = {
    play:
      '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">' +
      '<defs><linearGradient id="icDark" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#57411d"/>' +
      '<stop offset="0.55" stop-color="#3a2c12"/>' +
      '<stop offset="1" stop-color="#211806"/>' +
      '</linearGradient></defs>' +
      '<path d="M9.2 6v12l9.6-6z" fill="url(#icDark)" stroke="url(#icDark)"' +
      ' stroke-width="2.6" stroke-linejoin="round"/></svg>',
    pause:
      '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">' +
      '<defs><linearGradient id="icDark" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#57411d"/>' +
      '<stop offset="0.55" stop-color="#3a2c12"/>' +
      '<stop offset="1" stop-color="#211806"/>' +
      '</linearGradient></defs>' +
      '<rect x="7" y="5.4" width="3.7" height="13.2" rx="1.85" fill="url(#icDark)"/>' +
      '<rect x="13.3" y="5.4" width="3.7" height="13.2" rx="1.85" fill="url(#icDark)"/></svg>',
    home:
      '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">' +
      '<defs><linearGradient id="icGold" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#f6e7b6"/>' +
      '<stop offset="0.55" stop-color="#dcb46d"/>' +
      '<stop offset="1" stop-color="#a87f3f"/>' +
      '</linearGradient></defs>' +
      '<rect x="4.8" y="5.2" width="2.7" height="13.6" rx="1.35" fill="url(#icGold)"/>' +
      '<path d="M19.2 6v12l-9-6z" fill="url(#icGold)" stroke="url(#icGold)"' +
      ' stroke-width="2.4" stroke-linejoin="round"/></svg>',
  };
  const bar = document.createElement('div');
  bar.className = 'player-bar';
  const btn = document.createElement('button');
  btn.style.minWidth = '42px';
  btn.setAttribute('aria-label', '播放/暂停');
  btn.textContent = '解码中…';
  btn.disabled = true;
  const btnHome = document.createElement('button');
  btnHome.style.minWidth = '38px';
  btnHome.setAttribute('aria-label', '回到开头');
  btnHome.innerHTML = ICONS.home;
  const timeLbl = document.createElement('span');
  timeLbl.className = 'time';
  timeLbl.textContent = '00:00:00 / -:--';
  const seek = document.createElement('input');
  seek.type = 'range';
  seek.className = 'seek';
  let seekHeld = false;
  const folWrap = document.createElement('label');
  const folCb = document.createElement('input');
  folCb.type = 'checkbox';
  folCb.checked = true;
  folWrap.append(folCb, document.createTextNode('跟随播放'));
  const volLbl = document.createElement('span');
  volLbl.textContent = '音量';
  const vol = document.createElement('input');
  vol.type = 'range';
  vol.className = 'vol';
  vol.min = '0';
  vol.max = VOL_MAX.toFixed(3);
  vol.step = '0.005';
  vol.value = volSaved.toFixed(3);
  bar.append(btn, btnHome, timeLbl, seek, folWrap, volLbl, vol);
  container.appendChild(bar);

  // ---- Engine ----
  function stopSource() {
    if (src) {
      src.onended = null;
      try {
        src.stop();
      } catch {
        /* already stopped */
      }
      src.disconnect();
      src = null;
    }
    if (endTimer) {
      clearTimeout(endTimer);
      endTimer = null;
    }
  }

  function play(fromOffset) {
    if (!buffer) return;
    if (ctx.state === 'suspended') ctx.resume();
    stopSource();
    startOffset = Math.min(
      Math.max(fromOffset ?? savedOffset, 0),
      duration() - 0.01
    );
    src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(gain);
    src.start(0, startOffset);
    startCtx = ctx.currentTime;
    playing = true;
    btn.innerHTML = ICONS.pause;
    src.onended = () => {
      if (playing && !src) return;
      if (playing) {
        playing = false;
        savedOffset = duration();
        btn.innerHTML = ICONS.play;
      }
    };
    const lim = limit();
    if (END > 0 && startOffset < lim) {
      endTimer = setTimeout(
        () => {
          if (playing) {
            pause();
            savedOffset = lim;
          }
        },
        Math.max(0, (lim - startOffset) * 1000)
      );
    }
    if (!rafId) rafId = requestAnimationFrame(loop);
  }

  function pause() {
    if (!playing) return;
    savedOffset = curTime();
    playing = false;
    stopSource();
    btn.innerHTML = ICONS.play;
  }

  function seekTo(t) {
    if (!buffer) return;
    const c = Math.min(Math.max(t, 0), duration());
    if (playing) play(c);
    else {
      savedOffset = c;
      reposition();
    }
  }

  // ---- Decode (starts in the background as soon as the page loads) ----
  (async () => {
    try {
      const ab = await (await fetch(audioUrl)).arrayBuffer();
      buffer = await ctx.decodeAudioData(ab);
      btn.disabled = false;
      btn.innerHTML = ICONS.play;
      seek.min = String(OFFSET);
      seek.max = String(limit() || 0);
      seek.step = '0.1';
      timeLbl.textContent = `00:00:00 / ${fmtRel(specMs(limit()))}`;
      reposition();
    } catch (e) {
      btn.textContent = '解码失败';
      btn.title = String(e?.message || e);
      console.error('音频解码失败:', e);
    }
  })();

  // ---- Controls ----
  btn.addEventListener('click', () => {
    if (playing) pause();
    else play(Math.max(savedOffset, OFFSET));
  });
  btnHome.addEventListener('click', () => seekTo(OFFSET));
  seek.addEventListener('input', () => {
    seekTo(parseFloat(seek.value));
  });
  seek.addEventListener('pointerdown', () => {
    seekHeld = true;
  });
  window.addEventListener('pointerup', () => {
    seekHeld = false;
  });
  vol.addEventListener('input', () => {
    gain.gain.value = parseFloat(vol.value);
    try {
      localStorage.setItem(VOL_KEY, String(gain.gain.value));
    } catch {
      /* ignore storage failure */
    }
  });

  // ---- Track the current viewport (for follow panning) ----
  let va = EPOCH_MS;
  let vb = EPOCH_MS + 30000;
  try {
    const r0 = gd._fullLayout.xaxis.range;
    va = pMs(r0[0]);
    vb = pMs(r0[1]);
  } catch {
    /* noop */
  }
  gd.on('plotly_relayout', (e) => {
    const r = e['xaxis.range'];
    if (r) {
      va = pMs(r[0]);
      vb = pMs(r[1]);
    } else if (e['xaxis.range[0]'] !== undefined) {
      va = pMs(e['xaxis.range[0]']);
      vb = pMs(e['xaxis.range[1]']);
    } else if (e['xaxis.autorange']) {
      va = pMs(gd._fullLayout.xaxis.range[0]);
      vb = pMs(gd._fullLayout.xaxis.range[1]);
    }
  });

  // ---- HTML overlay cursor (transform-driven, zero plotly overhead) ----
  const ph = document.createElement('div');
  ph.className = 'playhead';
  gd.parentElement.appendChild(ph);

  function reposition() {
    const fl = gd._fullLayout;
    if (!fl) return;
    const r0 = pMs(fl.xaxis.range[0]);
    const r1 = pMs(fl.xaxis.range[1]);
    const pm = Math.min(Math.max(specMs(curTime()), r0), r1);
    const frac = r1 > r0 ? (pm - r0) / (r1 - r0) : 0;
    const [d0, d1] = fl.xaxis.domain;
    const plotW = gd.clientWidth - fl.margin.l - fl.margin.r;
    const x = fl.margin.l + (d0 + frac * (d1 - d0)) * plotW;
    ph.style.transform = `translateX(${x}px)`;
    ph.style.top = `${fl.margin.t}px`;
    ph.style.bottom = `${fl.margin.b}px`;
  }

  gd.on('plotly_relayout', reposition);
  new ResizeObserver(reposition).observe(gd.parentElement);
  reposition();

  let rafId = null;
  function loop() {
    rafId = null;
    if (!playing) return;
    const t = curTime();
    const pm = specMs(t);
    reposition();
    if (!seekHeld) seek.value = String(t);
    timeLbl.textContent = `${fmtRel(pm)} / ${fmtRel(specMs(limit()))}`;
    if (END > 0 && t >= END - 0.02) {
      pause();
      return;
    }
    if (folCb.checked) {
      const span = vb - va;
      if (pm < va + span * 0.05 || pm > vb - span * 0.15) {
        const na = Math.min(
          Math.max(pm - span * 0.1, EPOCH_MS),
          EPOCH_MS + durMs - span
        );
        Plotly.relayout(gd, { 'xaxis.range': [iso(na), iso(na + span)] });
      }
    }
    rafId = requestAnimationFrame(loop);
  }

  // ---- Spectrogram click: seek playback without autoplay ----
  gd.on('plotly_click', (e) => {
    if (!e.points || !e.points[0]) return;
    const ms = pMs(e.points[0].x);
    if (!isFinite(ms)) return;
    const t = audioT(ms);
    if (t < 0 || t > duration()) return;
    seekTo(Math.max(t, OFFSET));
  });

  return { seekTo, play, pause, bar };
}
