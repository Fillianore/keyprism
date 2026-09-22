/** Shared track-row factory (Phase 3.6 D3/6, Phase 3.7 compact grid):
 *  ONE builder for the Phase 2 stem panel (label | controls) and the
 *  Phase 3 lane workspace (label | controls | scope) so labels, icons,
 *  sliders and M/S buttons can never drift apart between the two
 *  panels — the "bare gray dot" vs styled-slider inconsistency class
 *  is structurally impossible now.
 *
 *  Phase 3.7 fader: stem rows get a dB fader (−40…+18, step 0.5, 0 dB
 *  detent) with a live dB readout under the slider; the Mix row keeps
 *  the normalized 0..1 fader (it IS the transport volume, ceiling
 *  −10 dB on the player side) with its real output dB shown.
 *
 *  DOM construction + event wiring only; the mute/solo STATE MACHINE
 *  lives in mixer.js (pressMute/pressSolo) — buttons never flip model
 *  flags themselves, they call the mixer and re-render strictly from
 *  the model (so the M+S-both-gold state is unrepresentable); the lane
 *  scope cell is left EMPTY for the caller to fill with its canvases
 *  (canvas ownership + redraw stays in lanes.js). */

import { trackIcon } from './trackIcons.js';
import { t } from './i18n.js';
import { MASTER_CEILING } from './mixer.js';

/** Live readout text: dB faders show their value; the Mix fader is a
 *  0..1 norm over the −10 dB ceiling, so show the REAL output dB. */
export function faderDbText(model, fader) {
  if (fader === 'db') {
    const v = model.dbGain || 0;
    return `${v > 0 ? '+' : ''}${v.toFixed(1)}`;
  }
  const lin = (model.volume || 0) * MASTER_CEILING;
  if (lin <= 1e-4) return '\u2212\u221E';
  const db = 20 * Math.log10(lin);
  return `${db > 0 ? '+' : ''}${db.toFixed(1)}`;
}

export function trackRow({ key, labelText, color, withLane = false,
                           fader = 'norm' }) {
  const row = document.createElement('div');
  row.className = withLane ? 'lane-row' : 'stem-row';

  // label cell: <icon><text> (D1)
  const label = document.createElement('span');
  label.className = 'stem-name';
  const ico = document.createElement('span');
  ico.className = 'stem-ico';
  ico.setAttribute('aria-hidden', 'true');
  ico.innerHTML = trackIcon(key); // static SVG constants, never user data
  const txt = document.createElement('span');
  txt.className = 'stem-label';
  txt.textContent = labelText;
  label.append(ico, txt);
  if (color) label.style.setProperty('--stem-color', color);

  // controls cell: one shared fader + M/S hardware keys + dB readout
  const controls = document.createElement('div');
  controls.className = 'stem-controls';
  const vol = document.createElement('input');
  vol.type = 'range';
  vol.className = 'stem-vol';
  if (fader === 'db') {
    vol.min = '-40';
    vol.max = '18';
    vol.step = '0.5';
    vol.title = t('faderDbTitle');
  } else {
    vol.min = '0';
    vol.max = '1';
    vol.step = '0.01';
  }
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
  const db = document.createElement('span');
  db.className = 'stem-db';
  controls.append(vol, mute, solo, db);

  const parts = { row, label, controls, vol, mute, solo, db };
  if (withLane) {
    const scope = document.createElement('div');
    scope.className = 'lane-scope';
    row.append(label, controls, scope);
    parts.scope = scope;
  } else {
    row.append(label, controls);
  }
  return parts;
}

/** Shared M/S/fader wiring: M/S clicks run the mixer state machine
 *  (mixer.pressMute / pressSolo — buttons re-render strictly from the
 *  model via the panel's apply() repaint); the fader writes dB (stems)
 *  or normalized volume (Mix row) into the model. Returns syncFader()
 *  to (re)paint slider + readout from the model without fighting an
 *  in-progress drag. paintFill is the panel's slider fill painter. */
export function wireMuteSolo({ mixer, model, vol, mute, solo, dbEl,
                               apply, paintFill, fader = 'norm' }) {
  const setDb = () => {
    if (dbEl) dbEl.textContent = faderDbText(model, fader);
  };
  const syncFader = () => {
    if (document.activeElement !== vol) {
      vol.value = String(
        fader === 'db' ? model.dbGain || 0 : model.volume
      );
    }
    paintFill(vol);
    setDb();
  };
  mute.addEventListener('click', () => {
    mixer.pressMute(model);
    apply();
  });
  solo.addEventListener('click', () => {
    mixer.pressSolo(model);
    apply();
  });
  vol.addEventListener('input', () => {
    let v = parseFloat(vol.value);
    if (fader === 'db' && v !== 0 && Math.abs(v) < 0.75) {
      v = 0; // 0 dB detent: the ±0.5 dB positions snap to exactly 0
      vol.value = '0';
    }
    if (fader === 'db') model.dbGain = v;
    else model.volume = v;
    paintFill(vol);
    setDb();
    apply();
  });
  syncFader();
  return { syncFader };
}
