import Plotly from 'plotly.js-dist-min';
import { iso, pMs } from './spectrogram.js';

/** 底部导航条: 背景为全曲能量包络, 取景窗口拖动时宽度恒定,
 *  左右手柄调整宽度, 点击空白处跳转 */
export function createNavbar(container, gd, { minMs, maxMs, initAMs, initBMs, envUrl }) {
  let a = initAMs;
  let b = initBMs;

  const wrap = document.createElement('div');
  wrap.className = 'navbar';
  wrap.style.backgroundImage = `url("${envUrl}")`;

  const win = document.createElement('div');
  win.className = 'window';
  const hL = document.createElement('div');
  hL.className = 'handle lo';
  const hR = document.createElement('div');
  hR.className = 'handle hi';
  win.append(hL, hR);

  const labL = document.createElement('span');
  labL.className = 'lab left';
  labL.textContent = '00:00:00';
  const labR = document.createElement('span');
  labR.className = 'lab right';
  labR.textContent = fmt(maxMs);
  wrap.append(win, labL, labR);
  container.appendChild(wrap);

  function fmt(ms) {
    const total = Math.round(ms - minMs);
    const mm = Math.floor(total / 60000);
    const ss = Math.floor((total % 60000) / 1000);
    const mmm = total % 1000;
    return `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}:${String(
      mmm
    ).padStart(3, '0')}`;
  }

  function render() {
    win.style.left = `${(100 * (a - minMs)) / (maxMs - minMs)}%`;
    win.style.width = `${(100 * (b - a)) / (maxMs - minMs)}%`;
    win.title = `${fmt(a)} - ${fmt(b)}`;
  }

  function relayout() {
    Plotly.relayout(gd, { 'xaxis.range': [iso(a), iso(b)] });
  }

  function drag(el, mode, jump) {
    el.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      el.setPointerCapture(ev.pointerId);
      const x0 = ev.clientX;
      const a0 = a;
      const b0 = b;
      if (jump) {
        const rect = wrap.getBoundingClientRect();
        const t = minMs + ((ev.clientX - rect.left) / rect.width) * (maxMs - minMs);
        const span0 = b0 - a0;
        a = Math.min(Math.max(t - span0 / 2, minMs), maxMs - span0);
        b = a + span0;
        render();
        relayout();
        return;
      }
      const onMove = (e2) => {
        const w = wrap.getBoundingClientRect().width;
        const dt = ((e2.clientX - x0) / w) * (maxMs - minMs);
        if (mode === 'move') {
          const span = b0 - a0;
          a = Math.min(Math.max(a0 + dt, minMs), maxMs - span);
          b = a + span;
        } else if (mode === 'lo') {
          a = Math.min(Math.max(a0 + dt, minMs), b - 1000);
        } else {
          b = Math.max(Math.min(b0 + dt, maxMs), a + 1000);
        }
        render();
        relayout();
      };
      const onUp = () => {
        el.removeEventListener('pointermove', onMove);
        el.removeEventListener('pointerup', onUp);
      };
      el.addEventListener('pointermove', onMove);
      el.addEventListener('pointerup', onUp);
    });
  }

  drag(win, 'move');
  drag(hL, 'lo');
  drag(hR, 'hi');
  drag(wrap, 'move', true);

  // 主图缩放/平移时同步窗口
  gd.on('plotly_relayout', (e) => {
    const r = e['xaxis.range'];
    let a2;
    let b2;
    if (r) {
      a2 = r[0];
      b2 = r[1];
    } else if (e['xaxis.range[0]'] !== undefined) {
      a2 = e['xaxis.range[0]'];
      b2 = e['xaxis.range[1]'];
    } else if (e['xaxis.autorange']) {
      a = minMs;
      b = maxMs;
      render();
      return;
    }
    if (a2 !== undefined) {
      a = pMs(a2);
      b = pMs(b2);
      if (isFinite(a) && isFinite(b) && b > a) render();
    }
  });

  render();
  return {
    el: wrap,
    setEnv: (url) => {
      wrap.style.backgroundImage = `url("${url}")`;
    },
  };
}
