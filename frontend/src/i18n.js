/** Minimal i18n: EN (default) / ZH, persisted in localStorage.
 *
 *  - Static DOM: elements declare data-i18n / data-i18n-title /
 *    data-i18n-aria; applyStatic() swaps the text in place (the hardcoded
 *    HTML text is the English default, so no flash for first-time visitors)
 *  - Dynamic strings: modules call t(key, params) at render time and
 *    subscribe with onChange() to re-render on live language switches
 */

const LANG_KEY = 'keyprism-lang';

// Exported for the key-completeness guard (scripts/check-i18n.mjs):
// every t('key') usage must exist in BOTH dicts.
export const dict = {
  en: {
    pickMusic: '♪ Select Music',
    pickMusicTitle: 'Pick a local music file and upload it for analysis',
    resolution: 'Resolution',
    colsPerSec: 'cols/s',
    subPerSemitone: 'sub/semitone',
    channel: 'Channel',
    chanMix: 'Mix',
    chanLeft: 'Left',
    chanRight: 'Right',
    pitchRange: 'Range',
    notes: 'Notes',
    notesOff: 'Off',
    notesBass: 'Bass',
    notesLead: 'Lead',
    notesBoth: 'Both',
    notesAria: 'Note tracks',
    midiExport: 'Export MIDI',
    midiExportTitle: 'Download the selected tracks as a MIDI file',
    notesHintCapped:
      '{n} notes in view, showing top 1500 by confidence — zoom in for all',
    notesFailed: 'Notes failed: {msg}',
    notesNeedsServe: '(notes need backend --serve mode)',
    on: 'On',
    stems: 'Stems',
    solo: 'Solo',
    mute: 'Mute',
    stemMix: 'Mix',
    stemHarmonic: 'Harmonic',
    stemPercussive: 'Percussive',
    stemLowRank: 'Low-rank',
    stemSparse: 'Transient',
    stemsMethod_combined: 'Combined',
    stemsMethod_hpss: 'HPSS',
    stemsMethod_rpca: 'RPCA',
    stemsSeparating: 'Separating stems… {done}/{total}',
    stemsFailed: 'Stems failed: {msg}',
    lanes: 'AI Separation (Demucs)',
    lanesAria: 'AI separation lanes (Demucs)',
    lanesMethod_demucs_4: 'Demucs 4-stem',
    lanesMethod_demucs_6: 'Demucs 6-stem',
    methodClassic: 'Classic',
    methodAI: 'AI (Demucs)',
    lanesSeparating: 'DL separating… {pct}%',
    lanesDownloading:
      'Downloading model {done} / {total} MB ({speed} MB/s)',
    lanesFailed: 'Lanes failed: {msg}',
    dlNeedsExtra: 'Requires uv sync --extra dl (onnxruntime)',
    providerGpu: 'GPU: {name}',
    providerCpu: 'CPU',
    providerTip: 'Execution providers: {chain}',
    qualityFast: 'Fast',
    qualityBalanced: 'Balanced',
    qualityBest: 'Best',
    qualityTip:
      'Inference quality (Demucs shifts): more passes = cleaner stems, ~linear compute',
    laneMix: 'Mix',
    laneVocals: 'Vocals',
    laneDrums: 'Drums',
    laneBass: 'Bass',
    lanePiano: 'Piano',
    laneGuitar: 'Guitar',
    laneOther: 'Other',
    laneNotes: 'Notes',
    laneNotesTitle: 'Polyphonic notes (Basic Pitch) overlay on/off',
    laneNotesFailed: 'Poly notes failed: {msg}',
    laneDecodeFailed: 'Waveform decode failed',
    polyNeedsDL: 'Polyphonic notes need the optional DL extra: uv sync --extra dl',
    polyNeedsBP:
      'Polyphonic transcription is unavailable on Python ≥ 3.12; separation is unaffected',
    faderDbTitle:
      'Stem fader (dB); default = make-up gain matching the mix loudness',
    lanePeakDb: 'peak {db} dB',
    laneWave: 'Wave',
    laneSpec: 'Spec',
    laneSpecTip: 'Toggle waveform / spectrogram view for this lane',
    laneSpecLoading: 'Loading spectrogram…',
    laneSpecFailed: 'Spectrogram failed: {msg}',
    layerHandleTip: 'Drag onto the spectrogram to overlay this stem',
    layersTitle: 'Layers',
    layersEmpty: 'Drag a lane onto the spectrogram to add a layer',
    layerBlendAdd: 'Add',
    layerBlendCover: 'Cover',
    layerBlendTitle: '叠加 (screen blend) or 覆盖 (opaque replace)',
    layerOpacity: 'Layer opacity',
    layerGainTitle:
      'Intensity gain (dB) on the layer spectrum before blending — shifts which energies light up',
    layerGammaTitle:
      'Intensity γ on the layer colormap input — master highlight-γ semantics',
    layerVisible: 'Toggle layer visibility',
    layerRemove: 'Remove layer',
    layerReorderTip: 'Drag to reorder layers',
    layerLoading: 'Loading layer…',
    mixerMutedTip: 'Muted by default; unmuting automatically ducks the mix',
    mixerMixDuckedTip:
      'Stems are playing: the mix is ducked to avoid summing into clipping',
    soloSuppressedTip: "Silenced by another track's solo",
    toastDismiss: 'Dismiss',
    bpmResetTitle: 'Reset to detected value',
    offsetMs: 'Offset ms',
    beatsPerBar: 'Beats/bar',
    cmap: 'Palette',
    colorFloor: 'Color floor',
    resetDefault: 'Reset to default',
    highlightGamma: 'Highlight γ',
    langToggleTitle: 'Switch language',

    updatingSpec: 'Updating spectrogram',
    renderingSpec: 'Rendering spectrogram',
    dataLoadFailed:
      'Failed to load data.json ({status}). Start the backend first',
    dataStale:
      'data.json is outdated: re-run the backend and hard-refresh (Ctrl+F5)',
    staticMode: '(static mode: resolution switch needs backend --serve)',
    backendComputing:
      'Backend computing: {rate} cols/s × {sub} sub/semitone (STFT recompute, ~2–5 s)',
    receivingSpec: 'Receiving spectrogram data',
    decodingMatrix: 'Decoding spectrogram matrix',
    resStatus: '{rate} cols/s · {sub} sub',
    failed: 'Failed: {msg}',
    pickNeedsServe: '(music upload needs backend --serve mode)',
    importingMusic: 'Importing music',
    analyzeDone: 'Analysis done, refreshing page',
    importFailed: 'Import failed: {msg}',
    importNetworkError: 'Import failed: network error',
    importCancelled: 'Import cancelled',
    uploading: 'Uploading {name}',
    clickToEdit: 'Click to type a value',
    loadFailed: 'Failed to load: {msg}',

    playPause: 'Play/Pause',
    decoding: 'Decoding…',
    backToStart: 'Back to start',
    followPlayback: 'Follow',
    volume: 'Volume',
    decodeFailed: 'Decode failed',

    hoverTemplate:
      'Time %{x|%M:%S.%L}<br>Note %{text}<br>Level %{z:.1f} dB<extra></extra>',
  },
  zh: {
    pickMusic: '♪ 选择音乐',
    pickMusicTitle: '选择本地音乐文件, 上传到后端解析',
    resolution: '分辨率',
    colsPerSec: '列/s',
    subPerSemitone: '子带/半音',
    channel: '通道',
    chanMix: '混合',
    chanLeft: '左',
    chanRight: '右',
    pitchRange: '音域',
    notes: '音符',
    notesOff: '关',
    notesBass: '贝斯',
    notesLead: '旋律',
    notesBoth: '全部',
    notesAria: '音符音轨',
    midiExport: '导出 MIDI',
    midiExportTitle: '将所选音轨下载为 MIDI 文件',
    notesHintCapped: '可视音符 {n} 个, 仅显示置信度前 1500, 放大可查看全部',
    notesFailed: '音符获取失败: {msg}',
    notesNeedsServe: '(音符功能需后端 --serve 模式)',
    on: '开',
    stems: '分轨',
    solo: '独奏',
    mute: '静音',
    stemMix: '原曲',
    stemHarmonic: '谐波',
    stemPercussive: '打击',
    stemLowRank: '低秩',
    stemSparse: '瞬态',
    stemsMethod_combined: '融合',
    stemsMethod_hpss: 'HPSS',
    stemsMethod_rpca: 'RPCA',
    stemsSeparating: '分轨计算中… {done}/{total}',
    stemsFailed: '分轨失败: {msg}',
    lanes: 'AI 分离 (Demucs)',
    lanesAria: 'AI 分离多轨 (Demucs)',
    lanesMethod_demucs_4: 'Demucs 4 轨',
    lanesMethod_demucs_6: 'Demucs 6 轨',
    methodClassic: '传统',
    methodAI: 'AI 分离 (Demucs)',
    lanesSeparating: 'DL 分离中… {pct}%',
    lanesDownloading: '下载模型 {done} / {total} MB ({speed} MB/s)',
    lanesFailed: '多轨失败: {msg}',
    dlNeedsExtra: '需要 uv sync --extra dl (onnxruntime)',
    providerGpu: 'GPU: {name}',
    providerCpu: 'CPU',
    providerTip: '执行提供者: {chain}',
    qualityFast: '快速',
    qualityBalanced: '均衡',
    qualityBest: '最佳',
    qualityTip: '推理质量 (Demucs shifts): 遍数越多分轨越干净, 计算量约线性增加',
    laneMix: '混音',
    laneVocals: '人声',
    laneDrums: '鼓',
    laneBass: '贝斯',
    lanePiano: '钢琴',
    laneGuitar: '吉他',
    laneOther: '其他',
    laneNotes: '扒谱',
    laneNotesTitle: '多音扒谱 (Basic Pitch) 覆盖层开关',
    laneNotesFailed: '多音扒谱失败: {msg}',
    laneDecodeFailed: '波形解码失败',
    polyNeedsDL: '需要 uv sync --extra dl',
    polyNeedsBP: '多音转录在 Python ≥3.12 不可用；分离功能不受影响',
    faderDbTitle: '分轨推子 (dB)；默认为匹配原曲响度的补偿增益',
    lanePeakDb: '峰值 {db} dB',
    laneWave: '波形',
    laneSpec: '频谱',
    laneSpecTip: '切换该轨的 波形 / 频谱 视图',
    laneSpecLoading: '频谱加载中…',
    laneSpecFailed: '频谱获取失败: {msg}',
    layerHandleTip: '拖到频谱图上叠加该分轨',
    layersTitle: '叠加层',
    layersEmpty: '将下方分轨拖到频谱图上即可叠加',
    layerBlendAdd: '叠加',
    layerBlendCover: '覆盖',
    layerBlendTitle: '叠加 (screen 混合) 或 覆盖 (不透明替换)',
    layerOpacity: '图层不透明度',
    layerGainTitle: '叠加前作用于图层频谱的强度增益 (dB) — 改变哪些能量点亮',
    layerGammaTitle: '图层 colormap 输入的强度 γ — 同主图高光 γ 语义',
    layerVisible: '切换图层可见性',
    layerRemove: '移除图层',
    layerReorderTip: '拖动调整图层顺序',
    layerLoading: '图层加载中…',
    mixerMutedTip: '默认静音；取消静音将自动压低原曲',
    mixerMixDuckedTip: '分离轨播放中：为避免叠加削波，原曲已自动压低',
    soloSuppressedTip: '因其他轨道独奏而被静音',
    toastDismiss: '关闭',
    bpmResetTitle: '还原检测值',
    offsetMs: '偏移ms',
    beatsPerBar: '拍/小节',
    cmap: '配色',
    colorFloor: '色彩下限',
    resetDefault: '还原默认',
    highlightGamma: '高光 γ',
    langToggleTitle: '切换语言',

    updatingSpec: '正在更新频谱',
    renderingSpec: '正在渲染频谱',
    dataLoadFailed: 'data.json 加载失败 ({status}), 请先启动后端',
    dataStale: 'data.json 结构过期: 请重新运行后端并强制刷新页面 (Ctrl+F5)',
    staticMode: '(静态模式, 切换分辨率需后端 --serve)',
    backendComputing:
      '后端计算中: {rate} 列/s × {sub} 子带/半音 (STFT 重算, 约 2~5 秒)',
    receivingSpec: '接收频谱数据',
    decodingMatrix: '解码频谱矩阵',
    resStatus: '{rate} 列/s · {sub} 子带',
    failed: '失败: {msg}',
    pickNeedsServe: '(选择音乐需后端 --serve 模式)',
    importingMusic: '正在导入音乐',
    analyzeDone: '分析完成, 正在刷新页面',
    importFailed: '导入失败: {msg}',
    importNetworkError: '导入失败: 网络错误',
    importCancelled: '导入已取消',
    uploading: '上传 {name}',
    clickToEdit: '点击输入数值',
    loadFailed: '加载失败: {msg}',

    playPause: '播放/暂停',
    decoding: '解码中…',
    backToStart: '回到开头',
    followPlayback: '跟随播放',
    volume: '音量',
    decodeFailed: '解码失败',

    hoverTemplate:
      '时间 %{x|%M:%S.%L}<br>音 %{text}<br>强度 %{z:.1f} dB<extra></extra>',
  },
};

let lang = 'en';
try {
  const saved = localStorage.getItem(LANG_KEY);
  if (saved === 'zh' || saved === 'en') lang = saved;
} catch {
  /* localStorage unavailable: keep default */
}

const listeners = new Set();
let toggleWired = false;

export function getLang() {
  return lang;
}

/** Translate `key` in the active language, interpolating {placeholder}s */
export function t(key, params) {
  let s = dict[lang][key] ?? dict.en[key] ?? key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = s.replaceAll(`{${k}}`, String(v));
    }
  }
  return s;
}

function applyDocLang() {
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
}

/** Swap text/title/aria of all declared elements and refresh the toggle */
export function applyStatic() {
  applyDocLang();
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-title]').forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
  document.querySelectorAll('[data-i18n-aria]').forEach((el) => {
    el.setAttribute('aria-label', t(el.dataset.i18nAria));
  });
  document.querySelectorAll('#langToggle [data-lang]').forEach((b) => {
    b.classList.toggle('active', b.dataset.lang === lang);
  });
  if (!toggleWired) {
    toggleWired = true;
    const tg = document.getElementById('langToggle');
    if (tg) {
      tg.addEventListener('click', (ev) => {
        const b = ev.target.closest('[data-lang]');
        if (b) setLang(b.dataset.lang);
      });
    }
  }
}

/** Switch language, persist it, and notify all live-UI subscribers */
export function setLang(next) {
  if (next !== lang && (next === 'en' || next === 'zh')) {
    lang = next;
    try {
      localStorage.setItem(LANG_KEY, lang);
    } catch {
      /* ignore storage failure */
    }
    applyStatic();
    listeners.forEach((fn) => fn(lang));
  }
}

/** Subscribe a callback invoked after each language switch */
export function onChange(fn) {
  listeners.add(fn);
}
