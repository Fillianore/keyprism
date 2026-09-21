import Plotly from 'plotly.js-dist-min';
import { EPOCH_MS, iso, pMs } from './spectrogram.js';
import { t } from './i18n.js';
import { throttled } from './util.js';

/** Note overlay (Phase 1 monophonic transcription).
 *
 *  Fetches /api/notes for the selected track(s), renders the note events
 *  as layout.shapes rectangles on the EXISTING heatmap figure (no new
 *  plotly traces) and re-culls to the visible time window on every
 *  relayout through the shared leading+trailing throttle (util.js).
 *
 *  Design points:
 *  - shapes are keyed with the name prefix below so we can add/remove them
 *    without touching the keyboard/grid shapes owned by spectrogram.js
 *    (we filter them out of gd.layout.shapes and relayout the union);
 *  - layer 'above' puts the rects over the heatmap while the playback
 *    cursor stays an HTML overlay above everything;
 *  - only notes intersecting the visible x-range become shapes; beyond
 *    MAX_VISIBLE the top-confidence ones are kept and an i18n hint shows;
 *  - y coordinates follow the subband row grid: semitone m spans sub rows,
 *    center row (m-21)*sub + (sub-1)/2, rect half-height 0.45 key units.
 */

const NOTE_NAME = 'kp-note';
const MAX_VISIBLE = 1500;

// Mirror of keyprism.tracks.MONO_TRACKS colors (the frontend never reads
// Python data; keep the two in sync when presets change)
const TRACK_COLORS = { bass: '#5AC8FA', lead: '#FFB74D' };

function hexToRgba(hex, alpha) {
  const v = parseInt(hex.slice(1), 16);
  return `rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},${alpha})`;
}

/** Notes of one track -> culled, sorted, capped list for the view [a, b]
 *  (ms on the epoch axis) */
function visibleNotes(notes, aMs, bMs) {
  const spanMs = Math.max(1, bMs - aMs);
  const inView = notes.filter(
    (n) => EPOCH_MS + n.end * 1000 >= aMs && EPOCH_MS + n.start * 1000 <= bMs
  );
  if (inView.length <= MAX_VISIBLE) return { list: inView, capped: false };
  const top = [...inView]
    .sort((x, y) => y.conf - x.conf)
    .slice(0, MAX_VISIBLE)
    .sort((x, y) => x.start - y.start);
  return { list: top, capped: true, total: inView.length };
}

export function initNotes({ gd, data, apiBase, getSub }) {
  if (!apiBase) {
    // static mode: the backend serves notes, so without it the whole
    // control stays disabled
    const group = document.getElementById('notesToggle');
    const hint = document.getElementById('notesHint');
    if (group) group.querySelectorAll('button').forEach((b) => (b.disabled = true));
    if (hint) hint.textContent = t('notesNeedsServe');
    return {};
  }

  let selection = 'off';
  let pending = null; // in-flight fetch promise while spinner shows
  let cache = {}; // selection -> notes dict {"bass": [...], "lead": [...]}
  let lastShapesJson = null; // guard against relayout feedback loops

  const group = document.getElementById('notesToggle');
  const spin = document.getElementById('notesSpin');
  const hint = document.getElementById('notesHint');
  const midiBtn = document.getElementById('midiBtn');

  const setBusy = (on) => {
    if (spin) spin.hidden = !on;
    group.classList.toggle('busy', on);
  };

  /** Shapes currently owned by us (from the user layout store) */
  const ourShapes = () =>
    (gd.layout.shapes || []).filter(
      (s) => typeof s.name === 'string' && s.name.startsWith(NOTE_NAME)
    );

  function applyShapes(noteShapes) {
    const json = JSON.stringify(noteShapes);
    if (json === lastShapesJson) return;
    const others = (gd.layout.shapes || []).filter(
      (s) => !(typeof s.name === 'string' && s.name.startsWith(NOTE_NAME))
    );
    lastShapesJson = json;
    Plotly.relayout(gd, { shapes: [...others, ...noteShapes] });
  }

  function clearShapes() {
    if (ourShapes().length === 0) {
      lastShapesJson = null;
      return;
    }
    lastShapesJson = null;
    applyShapes([]);
  }

  /** Build + apply the overlay for the current selection and view */
  function render() {
    if (selection === 'off' || !cache[selection]) return;
    const range = gd._fullLayout?.xaxis?.range;
    const a = range ? pMs(range[0]) : EPOCH_MS;
    const b = range ? pMs(range[1]) : EPOCH_MS + data.durationSec * 1000;
    const wanted = selection === 'both'
      ? Object.keys(cache[selection])
      : [selection];
    const sub = getSub();
    const shapes = [];
    let capped = false;
    let total = 0;
    for (const name of wanted) {
      const { list, capped: cap, total: tot } = visibleNotes(
        cache[selection][name] || [], a, b
      );
      capped = capped || cap;
      total = Math.max(total, tot);
      const color = TRACK_COLORS[name] || '#FFFFFF';
      for (const n of list) {
        const c = (n.pitch - 21) * sub + (sub - 1) / 2;
        shapes.push({
          type: 'rect',
          xref: 'x',
          yref: 'y',
          x0: iso(EPOCH_MS + Math.round(n.start * 1000)),
          x1: iso(EPOCH_MS + Math.round(n.end * 1000)),
          y0: c - 0.45 * sub,
          y1: c + 0.45 * sub,
          fillcolor: hexToRgba(color, 0.3),
          line: { color: hexToRgba(color, 0.9), width: 1 },
          layer: 'above',
          name: `${NOTE_NAME}-${name}`,
        });
      }
    }
    if (hint) {
      hint.textContent = capped
        ? t('notesHintCapped', { n: total })
        : '';
    }
    applyShapes(shapes);
  }

  const renderThrottled = throttled(render, 120);

  async function fetchSelection(sel) {
    const r = await fetch(`${apiBase}/api/notes?track=${sel}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    if (body.error) throw new Error(body.error);
    return body.notes;
  }

  async function setSelection(sel) {
    selection = sel;
    group.querySelectorAll('button').forEach((btn) =>
      btn.classList.toggle('active', btn.dataset.notes === sel)
    );
    midiBtn.hidden = sel === 'off';
    if (sel === 'off') {
      if (hint) hint.textContent = '';
      clearShapes();
      return;
    }
    if (pending) return; // a fetch is already running; it re-renders
    setBusy(true);
    pending = (async () => {
      try {
        cache[sel] = await fetchSelection(sel);
        if (selection === sel) {
          if (hint) hint.textContent = '';
          render();
        }
      } catch (e) {
        if (hint) hint.textContent = t('notesFailed', { msg: e.message });
      } finally {
        pending = null;
        setBusy(false);
      }
    })();
  }

  group.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-notes]');
    if (!btn || btn.disabled) return;
    setSelection(btn.dataset.notes);
  });

  midiBtn.addEventListener('click', () => {
    if (selection === 'off') return;
    const a = document.createElement('a');
    a.href = `${apiBase}/api/midi?track=${selection}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  // Re-cull on every relayout (pan/zoom/follow/range clamp) through the
  // shared leading+trailing throttle; renders are idempotent (signature
  // guard), so the extra pass after our own relayout is a no-op.
  gd.on('plotly_relayout', renderThrottled);

  return { render };
}
