/** Shared track-row factory (Phase 3.6 D3/D6): ONE builder for the
 *  Phase 2 stem panel (label | controls) and the Phase 3 lane workspace
 *  (label | controls | scope) so labels, icons, sliders and M/S buttons
 *  can never drift apart between the two panels — the "bare gray dot"
 *  vs styled-slider inconsistency class is structurally impossible now.
 *
 *  Pure DOM construction: event wiring and the gain matrix stay with
 *  the callers (stems.js / lanes.js drive mixer.apply() themselves);
 *  the lane scope cell is left EMPTY for the caller to fill with its
 *  canvases (canvas ownership + redraw stays in lanes.js). */

import { trackIcon } from './trackIcons.js';
import { t } from './i18n.js';

export function trackRow({ key, labelText, color, withLane = false }) {
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

  // controls cell: one shared slider + M/S hardware keys
  const controls = document.createElement('div');
  controls.className = 'stem-controls';
  const vol = document.createElement('input');
  vol.type = 'range';
  vol.className = 'stem-vol';
  vol.min = '0';
  vol.max = '1';
  vol.step = '0.01';
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
  controls.append(vol, mute, solo);

  const parts = { row, label, controls, vol, mute, solo };
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

/** Shared M/S/slider wiring: buttons flip the model flags and call the
 *  panel's apply() (the mixer matrix); the slider mirrors model.volume.
 *  paintFill is the panel's slider fill painter. */
export function wireMuteSolo({ model, vol, mute, solo, apply, paintFill }) {
  mute.classList.toggle('active', model.muted);
  solo.classList.toggle('active', model.solo);
  vol.value = String(model.volume);
  mute.addEventListener('click', () => {
    model.muted = !model.muted;
    mute.classList.toggle('active', model.muted);
    apply();
  });
  solo.addEventListener('click', () => {
    model.solo = !model.solo;
    solo.classList.toggle('active', model.solo);
    apply();
  });
  vol.addEventListener('input', () => {
    model.volume = parseFloat(vol.value);
    paintFill(vol);
    apply();
  });
}
