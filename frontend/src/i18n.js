/** Minimal i18n: EN (default) / ZH, persisted in localStorage.
 *
 *  - Static DOM: elements declare data-i18n / data-i18n-title /
 *    data-i18n-aria; applyStatic() swaps the text in place (the hardcoded
 *    HTML text is the English default, so no flash for first-time visitors)
 *  - Dynamic strings: modules call t(key, params) at render time and
 *    subscribe with onChange() to re-render on live language switches
 */

const LANG_KEY = 'keyprism-lang';

const dict = {
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
    bpmResetTitle: 'Reset to detected value',
    offsetMs: 'Offset ms',
    beatsPerBar: 'Beats/bar',
    cmap: 'Palette',
    colorFloor: 'Color floor',
    resetDefault: 'Reset to default',
    highlightGamma: 'Highlight γ',
    langToggleTitle: 'Switch language',

    updatingSpec: 'Updating spectrogram',
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
    bpmResetTitle: '还原检测值',
    offsetMs: '偏移ms',
    beatsPerBar: '拍/小节',
    cmap: '配色',
    colorFloor: '色彩下限',
    resetDefault: '还原默认',
    highlightGamma: '高光 γ',
    langToggleTitle: '切换语言',

    updatingSpec: '正在更新频谱',
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
