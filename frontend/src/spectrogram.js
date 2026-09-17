import Plotly from 'plotly.js-dist-min';

export const EPOCH_MS = Date.UTC(2020, 0, 1);
export const N_ROWS = 88;

// 键盘贴条几何常量 (paper / y 轴域分数)
const KEY_STRIP = [0.004, 0.048];
const KEY_BLACK_W = 0.62;
const KEY_BLACK_H = 0.58;
const KEY_WHITE_FILL = '#F4EFE2';
const KEY_BLACK_FILL = '#121215';
const KEY_LINE_LIGHT = '#CFC8B4';
const KEY_LINE_STRONG = '#928A72';

export function iso(ms) {
  return new Date(ms).toISOString();
}

export function pMs(v) {
  if (typeof v === 'number') return v;
  // 兼容 ISO (T 分隔, 可带 Z) 与 plotly 内部格式 (空格分隔, 无 Z), 一律按 UTC
  const m =
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(Z)?$/.exec(v);
  if (m) {
    const ms = m[7] ? Math.round(parseFloat(`0.${m[7]}`) * 1000) : 0;
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], ms);
  }
  return Date.parse(v);
}

/** mm:ss:mmm 格式 (分:秒:毫秒) */
export function fmtRel(ms) {
  const total = Math.max(0, Math.round(ms - EPOCH_MS));
  const mm = Math.floor(total / 60000);
  const ss = Math.floor((total % 60000) / 1000);
  const mmm = total % 1000;
  return `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}:${String(
    mmm
  ).padStart(3, '0')}`;
}

/** 真实钢琴几何: 白键全高, 黑键 58% 行高居中, 白键分隔线在每行边界
 *  (E|F、B|C 加深)。lo/hi 为可见半音行范围, 分数相对该范围计算 */
function keyboardShapes(lo = 0, hi = N_ROWS - 1) {
  const [l, r] = KEY_STRIP;
  const bw = (r - l) * KEY_BLACK_W;
  const n = hi - lo + 1;
  const shapes = [
    {
      type: 'rect',
      xref: 'paper',
      yref: 'y domain',
      x0: l,
      x1: r,
      y0: 0.0,
      y1: 1.0,
      fillcolor: KEY_WHITE_FILL,
      line: { width: 0 },
    },
  ];
  for (let m = lo; m < hi; m++) {
    const strong = [4, 11].includes(m % 12);
    shapes.push({
      type: 'line',
      xref: 'paper',
      yref: 'y domain',
      x0: l,
      x1: r,
      y0: (m + 1 - lo) / n,
      y1: (m + 1 - lo) / n,
      line: {
        width: strong ? 1.6 : 1.0,
        color: strong ? KEY_LINE_STRONG : KEY_LINE_LIGHT,
      },
    });
  }
  const half = KEY_BLACK_H / (2 * n);
  for (let i = lo; i <= hi; i++) {
    if ([1, 3, 6, 8, 10].includes(i % 12)) {
      const c = (i + 0.5 - lo) / n;
      shapes.push({
        type: 'rect',
        xref: 'paper',
        yref: 'y domain',
        x0: l,
        x1: l + bw,
        y0: c - half,
        y1: c + half,
        fillcolor: KEY_BLACK_FILL,
        line: { width: 0.5, color: '#000000' },
      });
    }
  }
  shapes.push({
    type: 'rect',
    xref: 'paper',
    yref: 'y domain',
    x0: l,
    x1: r,
    y0: 0.0,
    y1: 1.0,
    fillcolor: 'rgba(0,0,0,0)',
    line: { width: 1, color: '#948B76' },
  });
  return shapes;
}

/** 小节网格状态 (BPM / 偏移 / 每小节拍数), 由 applyGrid 更新 */
let gridState = null;
let pitchRange = [0, N_ROWS - 1]; // 半音范围
let sub = 1; // 每半音子带数 (行数 = N_ROWS * sub)

/** y 轴半音刻度: 半音 m 的中心行 = m*sub + (sub-1)/2 */
const rowOf = (m) => m * sub + (sub - 1) / 2;

function yaxisConfig(data) {
  const [lo, hi] = pitchRange;
  const cIdx = data.cTickIdx.filter((i) => i >= lo && i <= hi);
  return {
    range: [lo * sub - 0.5, (hi + 1) * sub - 0.5],
    fixedrange: true,
    tickvals: cIdx.map(rowOf),
    ticktext: cIdx.map((i) => data.noteLabels[i]),
    tickfont: { color: '#9a9282', size: 9 },
    linecolor: '#3b3833',
    gridcolor: 'rgba(255,255,255,0.04)',
  };
}

/** 切换每半音子带数 (行数变化), 重新应用 y 轴 (shape 用轴域分数不受影响) */
export function setSub(gd, s, data) {
  sub = s;
  Plotly.relayout(gd, { yaxis: yaxisConfig(data) });
}

function gridShapes() {
  if (!gridState) return [];
  const { bpm, offsetMs, beats, minMs, maxMs } = gridState;
  const beatMs = 60000 / bpm;
  const barMs = beatMs * beats;
  // 归一化偏移到 [0, barMs)
  let t0 = minMs + (((offsetMs % barMs) + barMs) % barMs);
  const out = [];
  for (let t = t0; t <= maxMs; t += barMs) {
    out.push({
      type: 'line',
      xref: 'x',
      yref: 'paper',
      x0: iso(t),
      x1: iso(t),
      y0: 0,
      y1: 1,
      line: { width: 1.4, color: 'rgba(226,196,138,0.5)' },
    });
    for (let b = 1; b < beats && t + b * beatMs < maxMs; b++) {
      const tb = t + b * beatMs;
      out.push({
        type: 'line',
        xref: 'x',
        yref: 'paper',
        x0: iso(tb),
        x1: iso(tb),
        y0: 0,
        y1: 1,
        line: { width: 1, color: 'rgba(226,196,138,0.15)' },
      });
    }
  }
  return out;
}

/** 汇总当前所有 shape: 键盘 + 小节网格 (播放光标由 HTML 覆盖层实现, 不占 shape) */
function currentShapes(gd, xs) {
  return [
    ...keyboardShapes(pitchRange[0], pitchRange[1]),
    ...gridShapes(),
  ];
}

/** 应用小节网格 (BPM / 偏移 ms / 每小节拍数) */
export function applyGrid(gd, opts) {
  gridState = opts;
  Plotly.relayout(gd, { shapes: currentShapes(gd, null) });
}

/** 应用音域范围: y 轴裁剪 + shape 全量重建 (保留网格与光标) */
export function applyPitchRange(gd, data, lo, hi) {
  pitchRange = [lo, hi];
  Plotly.relayout(gd, {
    yaxis: yaxisConfig(data),
    shapes: currentShapes(gd, null),
  });
}

/** 创建热图与布局, 返回 { gd, playheadIdx } */
export function buildFigure(el, data, xs, spec) {
  const initViewMs = data.initViewSec * 1000;
  if (data.bpm) {
    // 初始网格: 使用后端估计的 BPM 与首拍偏移, 每小节 4 拍
    gridState = {
      bpm: data.bpm,
      offsetMs: (data.beatOffsetSec || 0) * 1000,
      beats: 4,
      minMs: EPOCH_MS,
      maxMs: EPOCH_MS + Math.round(data.durationSec * 1000),
    };
  }

  const traces = [
    {
      type: 'heatmap',
      z: spec,
      x: xs,
      y: Array.from({ length: spec.length }, (_, i) => i), // 数值行号
      colorscale: data.colorscales[data.defaultCmap],
      zmin: -data.dbRange,
      zmax: 0,
      showscale: false,
      hovertemplate:
        '时间 %{x|%M:%S.%L}<br>行 %{y}<br>强度 %{z:.1f} dB<extra></extra>',
      zsmooth: false,
    },
  ];

  const tickStyle = { color: '#9a9282', size: 10 };
  const layout = {
    dragmode: 'pan', // 拖拽 = 平移, 不做框选缩放
    font: { family: 'Microsoft YaHei, sans-serif', color: '#d3ccbd' },
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    margin: { l: 20, r: 20, t: 16, b: 40 },
    shapes: currentShapes(null, xs),
    xaxis: {
      type: 'date',
      domain: [0.062, 1.0],
      range: [xs[0], iso(EPOCH_MS + initViewMs)],
      tickformat: '%M:%S',
      dtick: pickDtickMs(initViewMs),
      tickfont: tickStyle,
      linecolor: '#3b3833',
      gridcolor: 'rgba(255,255,255,0.05)',
      zerolinecolor: '#3b3833',
    },
    yaxis: yaxisConfig(data),
  };

  Plotly.newPlot(el, traces, layout, {
    responsive: true,
    scrollZoom: true, // 滚轮缩放 (y 轴已被 fixedrange 锁定, 仅 x 生效)
  });
  return { gd: el };
}

const STEPS_MS = [
  1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000,
];

export function pickDtickMs(spanMs) {
  for (const s of STEPS_MS) {
    if (spanMs / s <= 16) return s;
  }
  return STEPS_MS[STEPS_MS.length - 1];
}
