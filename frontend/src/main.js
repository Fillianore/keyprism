import './style.css';
import Plotly from 'plotly.js-dist-min';
import {
  buildFigure,
  applyPitchRange,
  applyGrid,
  setSub,
  EPOCH_MS,
  iso,
} from './spectrogram.js';
import { registerAdaptiveTicks, registerRangeClamp } from './ticks.js';
import { createNavbar } from './navbar.js';
import { createPlayer } from './player.js';

/** Progress modal: setPhase text / setProgress(done,total) / setIndeterminate */
function showProgressModal(title = '正在更新频谱') {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <h3>${title}</h3>
      <div class="phase"></div>
      <div class="bar"><div class="bar-fill indeterminate"></div></div>
      <div class="pct"></div>
    </div>`;
  document.body.appendChild(overlay);
  const phaseEl = overlay.querySelector('.phase');
  const fill = overlay.querySelector('.bar-fill');
  const pct = overlay.querySelector('.pct');
  return {
    setPhase(t) {
      phaseEl.textContent = t;
    },
    setIndeterminate(on) {
      fill.classList.toggle('indeterminate', on);
      if (on) {
        fill.style.width = '34%';
        pct.textContent = '';
      }
    },
    setProgress(done, total) {
      if (!total) return;
      const p = Math.min(100, Math.round((done / total) * 100));
      fill.style.width = `${p}%`;
      pct.textContent = `${p}% (${done.toLocaleString()}/${total.toLocaleString()})`;
    },
    close() {
      overlay.remove();
    },
  };
}

/** Read the response body and report download progress by Content-Length;
 *  returns the parsed JSON */
async function readJsonWithProgress(resp, onProgress) {
  const total = parseInt(resp.headers.get('Content-Length') || '0', 10);
  const reader = resp.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    onProgress(received, total);
  }
  const buf = new Uint8Array(received);
  let off = 0;
  for (const c of chunks) {
    buf.set(c, off);
    off += c.length;
  }
  return JSON.parse(new TextDecoder().decode(buf));
}

/** Leading+trailing throttle: high-resolution heatmap recoloring is costly,
 *  so lower the restyle rate while a slider is being dragged */
function throttled(fn, ms = 120) {
  let last = 0;
  let timer = null;
  let pending = null;
  const run = () => {
    last = Date.now();
    timer = null;
    fn(...pending);
  };
  return (...args) => {
    pending = args;
    const wait = ms - (Date.now() - last);
    if (wait <= 0) {
      if (timer) clearTimeout(timer);
      run();
    } else if (!timer) {
      timer = setTimeout(run, wait);
    }
  };
}

async function main() {
  const res = await fetch(`./data.json?t=${Date.now()}`);
  if (!res.ok)
    throw new Error(
      `data.json 加载失败 (${res.status}), 请先运行 backend.py`
    );
  const data = await res.json();
  if (!data.specs || !data.noteLabels) {
    throw new Error(
      'data.json 结构过期: 请重新运行 backend.py 并强制刷新页面 (Ctrl+F5)'
    );
  }

  // Decode on demand: keep only the current channel's float matrix resident
  // (decoding all three channels at high resolution would use too much memory)
  const db = data.dbRange;
  const b64ToU8 = (b64) =>
    Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  let specRaw = {}; // channel -> Uint8Array
  for (const [name, b64] of Object.entries(data.specs)) {
    specRaw[name] = b64ToU8(b64);
  }
  let curSub = data.defaultSub || 1;
  let nCols = data.nCols;
  let envelopes = data.envelopes;
  let curChan = 'mix';
  const decodeChannel = (ch) => {
    const bin = specRaw[ch];
    const rows = data.noteLabels.length * curSub;
    const mat = [];
    for (let r = 0; r < rows; r++) {
      const row = new Array(nCols);
      for (let c = 0; c < nCols; c++) {
        row[c] = (bin[r * nCols + c] / 255) * db - db;
      }
      mat.push(row);
    }
    return mat;
  };
  const makeXs = () =>
    Array.from(
      { length: nCols },
      // Authoritative mapping: the full-track duration is split evenly per
      // column (center-aligned), independent of the backend hop field
      (_, i) =>
        iso(
          EPOCH_MS +
            Math.round((i + 0.5) * (data.durationSec / nCols) * 1000)
        )
    );
  const spec = decodeChannel(curChan);
  let xs = makeXs();

  const tick = () => new Promise((res) => setTimeout(res, 0));

  /** Chunked async decoding (with progress callback, avoids blocking the UI
   *  for long stretches) */
  const decodeChannelAsync = async (ch, onProgress) => {
    const bin = specRaw[ch];
    const rows = data.noteLabels.length * curSub;
    const mat = [];
    const BATCH = 48;
    for (let r = 0; r < rows; r++) {
      const row = new Array(nCols);
      for (let c = 0; c < nCols; c++) {
        row[c] = (bin[r * nCols + c] / 255) * db - db;
      }
      mat.push(row);
      if (r % BATCH === BATCH - 1) {
        onProgress(r + 1, rows);
        await tick();
      }
    }
    onProgress(rows, rows);
    return mat;
  };

  const plotEl = document.getElementById('plot');
  const app = document.getElementById('app');

  const { gd } = buildFigure(plotEl, data, xs, spec);
  registerAdaptiveTicks(gd);
  registerRangeClamp(gd, EPOCH_MS, EPOCH_MS + Math.round(data.durationSec * 1000));
  createPlayer(app, gd, {
    // Query string as cache buster: after a track switch, a same-named
    // audio.wav is no longer read from the browser cache
    audioUrl: `./${data.audioFile}?t=${Date.now()}`,
    offsetSec: data.offsetSec,
    endSec: data.endSec,
    durMs: Math.round(data.durationSec * 1000),
  });
  const nav = createNavbar(app, gd, {
    minMs: EPOCH_MS,
    maxMs: EPOCH_MS + Math.round(data.durationSec * 1000),
    initAMs: EPOCH_MS,
    initBMs: EPOCH_MS + Math.round(data.initViewSec * 1000),
    envUrl: envelopes.mix,
  });

  // ---- Top bar: channel switch (mix/left/right) ----
  const chanSel = document.getElementById('chanSelect');
  chanSel.addEventListener('change', () => {
    curChan = chanSel.value;
    Plotly.restyle(gd, { z: [decodeChannel(curChan)] }, [0]);
    if (envelopes[curChan]) nav.setEnv(envelopes[curChan]);
  });

  // ---- Top bar: resolution switch (requires backend --serve mode) ----
  const rateSel = document.getElementById('rateSelect');
  const subSel = document.getElementById('subSelect');
  const resStatus = document.getElementById('resStatus');
  (data.timeRates || [5, 10, 15, 30]).forEach((r) => {
    const o = document.createElement('option');
    o.value = String(r);
    o.textContent = String(r);
    rateSel.appendChild(o);
  });
  (data.subOptions || [1, 5, 10]).forEach((s) => {
    const o = document.createElement('option');
    o.value = String(s);
    o.textContent = String(s);
    subSel.appendChild(o);
  });
  rateSel.value = String(data.defaultRate || 15);
  subSel.value = String(curSub);
  if (!data.apiBase) {
    rateSel.disabled = true;
    subSel.disabled = true;
    resStatus.textContent = '(静态模式, 切换分辨率需后端 --serve)';
  }
  const applyResolution = async () => {
    if (!data.apiBase) return;
    const rate = parseInt(rateSel.value, 10);
    const s = parseInt(subSel.value, 10);
    if (rate === data.defaultRate && s === curSub && !specRaw._remote) return;
    rateSel.disabled = true;
    subSel.disabled = true;
    const modal = showProgressModal();
    try {
      modal.setPhase(`后端计算中: ${rate} 列/s × ${s} 子带/半音 (STFT 重算, 约 2~5 秒)`);
      const r = await fetch(
        `${data.apiBase}/api/spec?rate=${rate}&sub=${s}`
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      modal.setPhase('接收频谱数据');
      const j = await readJsonWithProgress(r, (done, total) =>
        modal.setProgress(done, total)
      );
      if (j.error) throw new Error(j.error);
      nCols = j.nCols;
      envelopes = j.envelopes;
      modal.setPhase('解码频谱矩阵');
      const raws = {};
      const names = Object.keys(j.specs);
      for (let i = 0; i < names.length; i++) {
        raws[names[i]] = b64ToU8(j.specs[names[i]]);
        modal.setProgress(i + 1, names.length);
        await tick();
      }
      specRaw = raws;
      specRaw._remote = true;
      const firstSwitch = curSub !== j.sub;
      curSub = j.sub;
      data.defaultRate = j.rate;
      if (firstSwitch) setSub(gd, curSub, data, nCols);
      xs = makeXs();
      const mat = await decodeChannelAsync(curChan, (done, total) =>
        modal.setProgress(done, total)
      );
      Plotly.restyle(gd, { z: [mat], x: [xs] }, [0]);
      nav.setEnv(envelopes[curChan]);
      resStatus.textContent = `${j.rate} 列/s · ${j.sub} 子带`;
    } catch (e) {
      resStatus.textContent = `失败: ${e.message}`;
    } finally {
      modal.close();
      rateSel.disabled = false;
      subSel.disabled = false;
    }
  };
  rateSel.addEventListener('change', applyResolution);
  subSel.addEventListener('change', applyResolution);

  // ---- Top bar: pick local music (upload to the backend for analysis and
  // track switch, requires --serve mode) ----
  const pickBtn = document.getElementById('pickBtn');
  const fileInput = document.getElementById('fileInput');
  pickBtn.addEventListener('click', () => {
    if (!data.apiBase) {
      resStatus.textContent = '(选择音乐需后端 --serve 模式)';
      return;
    }
    fileInput.click();
  });
  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = ''; // allow picking the same file again
    if (!file) return;
    const modal = showProgressModal('正在导入音乐');
    pickBtn.disabled = true;
    const finish = (msg) => {
      modal.close();
      pickBtn.disabled = false;
      if (msg) resStatus.textContent = msg;
    };
    const xhr = new XMLHttpRequest();
    xhr.open(
      'POST',
      `${data.apiBase}/api/upload?name=${encodeURIComponent(file.name)}`
    );
    xhr.responseType = 'json';
    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) modal.setProgress(e.loaded, e.total);
    });
    xhr.addEventListener('load', () => {
      const body = xhr.response;
      if (xhr.status === 200 && body && !body.error) {
        modal.setPhase('分析完成, 正在刷新页面');
        modal.setIndeterminate(true);
        setTimeout(() => location.reload(), 500);
        return;
      }
      const err = (body && body.error) || `HTTP ${xhr.status}`;
      finish(`导入失败: ${err}`);
    });
    xhr.addEventListener('error', () => finish('导入失败: 网络错误'));
    xhr.addEventListener('abort', () => finish('导入已取消'));
    modal.setPhase(`上传 ${file.name}`);
    modal.setIndeterminate(false); // switch to real upload progress
    xhr.send(file);
  });

  // ---- Top bar: file name / palette / color floor ----
  const badge = document.getElementById('fileBadge');
  badge.textContent = data.file;
  badge.title = data.file;

  const cmapSel = document.getElementById('cmapSelect');
  for (const name of Object.keys(data.colorscales)) {
    const o = document.createElement('option');
    o.value = name;
    o.textContent = name;
    if (name === data.defaultCmap) o.selected = true;
    cmapSel.appendChild(o);
  }

  const floor = document.getElementById('floorSlider');
  const floorVal = document.getElementById('floorVal');
  const floorMin = -Math.min(40, data.dbRange); // slider lower bound (at most -40 dB)
  const floorDefault = Math.max(floorMin, -30); // default -30 dB
  floor.min = String(floorMin);
  floor.max = '-5';
  floor.step = '1';
  const restyleFloor = throttled((c) =>
    Plotly.restyle(gd, { zmin: [c] }, [0])
  );
  const applyFloor = (v, immediate = false) => {
    const c = Math.round(Math.min(Math.max(v, floorMin), -5));
    floor.value = String(c);
    floorVal.dataset.v = String(c);
    floorVal.textContent = `${c} dB`; // numeric label follows immediately
    if (immediate) Plotly.restyle(gd, { zmin: [c] }, [0]);
    else restyleFloor(c);
  };
  floor.addEventListener('input', () => applyFloor(parseFloat(floor.value)));
  applyFloor(floorDefault, true);

  // ---- Highlight boost (gamma): applies a pos^γ power transform to the
  // colorscale anchor positions; data and hover readouts unchanged ----
  const GAMMA_MIN = 0.6;
  const GAMMA_MAX = 5;
  const gammaSlider = document.getElementById('gammaSlider');
  const gammaVal = document.getElementById('gammaVal');
  gammaSlider.min = String(GAMMA_MIN);
  gammaSlider.max = String(GAMMA_MAX);
  gammaSlider.step = '0.05';
  const restyleGamma = throttled((cs) =>
    Plotly.restyle(gd, { colorscale: [cs] }, [0])
  );
  const applyGamma = (v, immediate = false) => {
    const g = Math.min(Math.max(v, GAMMA_MIN), GAMMA_MAX);
    gammaSlider.value = String(g);
    gammaVal.dataset.v = String(g);
    gammaVal.textContent = `γ ${g.toFixed(2)}`; // numeric label follows immediately
    const base = data.colorscales[cmapSel.value];
    const cs = base.map(([p, c]) => [Math.pow(p, g), c]);
    if (immediate) Plotly.restyle(gd, { colorscale: [cs] }, [0]);
    else restyleGamma(cs);
  };

  /** Clicking a numeric label turns it into an input: Enter/blur commits,
   *  Esc cancels */
  function makeEditable(span, apply) {
    span.classList.add('num');
    span.title = '点击输入数值';
    span.addEventListener('click', () => {
      if (span.querySelector('input')) return;
      const cur = parseFloat(span.dataset.v);
      const input = document.createElement('input');
      input.type = 'number';
      input.step = 'any';
      input.className = 'num-input';
      let closed = false;
      const close = (commit) => {
        if (closed) return;
        closed = true;
        const v = parseFloat(input.value);
        if (commit && isFinite(v)) apply(v);
        else apply(cur); // on cancel, re-render with the original value
      };
      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') close(true);
        else if (ev.key === 'Escape') close(false);
        ev.stopPropagation();
      });
      input.addEventListener('blur', () => close(true));
      input.addEventListener('click', (ev) => ev.stopPropagation());
      span.textContent = '';
      span.appendChild(input);
      input.value = String(cur);
      input.focus();
      input.select();
    });
  }

  makeEditable(floorVal, applyFloor);
  makeEditable(gammaVal, applyGamma);
  document.getElementById('floorReset').addEventListener('click', () => {
    applyFloor(floorDefault, true);
  });
  document.getElementById('gammaReset').addEventListener('click', () => {
    applyGamma(1, true);
  });

  const applyColorscale = () => applyGamma(parseFloat(gammaSlider.value));
  cmapSel.addEventListener('change', () =>
    applyGamma(parseFloat(gammaSlider.value), true)
  );
  gammaSlider.addEventListener('input', applyColorscale);
  applyGamma(1, true);
  const loSel = document.getElementById('loSelect');
  const hiSel = document.getElementById('hiSelect');
  const fill = (sel, def) => {
    data.noteLabels.forEach((t, i) => {
      const o = document.createElement('option');
      o.value = String(i);
      o.textContent = t;
      sel.appendChild(o);
    });
    sel.value = String(def);
  };
  fill(loSel, 0);
  fill(hiSel, data.noteLabels.length - 1);
  const applyRange = () => {
    let lo = parseInt(loSel.value, 10);
    let hi = parseInt(hiSel.value, 10);
    if (lo > hi) {
      [lo, hi] = [hi, lo];
      loSel.value = String(lo);
      hiSel.value = String(hi);
    }
    applyPitchRange(gd, data, lo, hi);
  };
  loSel.addEventListener('change', applyRange);
  hiSel.addEventListener('change', applyRange);

  // ---- Top bar: BPM / measure offset / beats per measure ----
  const bpmInput = document.getElementById('bpmInput');
  const offsetInput = document.getElementById('offsetInput');
  const beatsSel = document.getElementById('beatsSel');
  const detectedBpm = data.bpm || 120;
  const detectedOffsetMs = Math.round((data.beatOffsetSec || 0) * 1000);
  bpmInput.value = String(detectedBpm);
  offsetInput.value = String(detectedOffsetMs);
  const maxMs = EPOCH_MS + Math.round(data.durationSec * 1000);
  const applyBpmGrid = () => {
    let bpm = parseFloat(bpmInput.value);
    if (!isFinite(bpm)) bpm = detectedBpm;
    bpm = Math.min(Math.max(bpm, 30), 300);
    let off = parseFloat(offsetInput.value);
    if (!isFinite(off)) off = 0;
    applyGrid(gd, {
      bpm,
      offsetMs: off,
      beats: parseInt(beatsSel.value, 10) || 4,
      minMs: EPOCH_MS,
      maxMs,
    });
  };
  bpmInput.addEventListener('change', applyBpmGrid);
  offsetInput.addEventListener('change', applyBpmGrid);
  beatsSel.addEventListener('change', applyBpmGrid);
  document.getElementById('bpmReset').addEventListener('click', () => {
    bpmInput.value = String(detectedBpm);
    applyBpmGrid();
  });
  document.getElementById('offsetReset').addEventListener('click', () => {
    offsetInput.value = String(detectedOffsetMs);
    applyBpmGrid();
  });

  // ---- Layout sync: force a plotly resize after any container size change,
  // preventing the bottom from being covered ----
  const wrap = document.getElementById('plot-wrap');
  const sync = () => Plotly.Plots.resize(gd);
  new ResizeObserver(sync).observe(wrap);
  sync();
}

main().catch((err) => {
  document.body.insertAdjacentHTML(
    'afterbegin',
    `<div style="padding:20px;font-family:sans-serif;color:#b00">
       加载失败: ${err.message}
     </div>`
  );
  console.error(err);
});
