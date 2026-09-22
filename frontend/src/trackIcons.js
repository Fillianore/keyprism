/** Per-track inline SVG glyphs (15 px, stroke = currentColor) for the
 *  stem/lane labels (Phase 3.6 D1): every row renders <icon><text> from
 *  the fixed stem registries — Mix/Vocals/Drums/Bass/Other/Piano/Guitar
 *  and the HPSS/RPCA stem names. Static constants only, never user
 *  data; icons inherit the row's --stem-color. */

const S = (inner) =>
  `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" ` +
  `stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" ` +
  `aria-hidden="true">${inner}</svg>`;

const ICONS = {
  // speaker with one sound arc
  mix: S(
    '<path d="M2.5 6.2v3.6h2.5L9 13V3L5 6.2H2.5z"/>' +
      '<path d="M11 5.6a3.3 3.3 0 0 1 0 4.8"/>'
  ),
  // studio microphone
  vocals: S(
    '<rect x="6" y="1.8" width="4" height="7.6" rx="2"/>' +
      '<path d="M3.8 7.8a4.2 4.2 0 0 0 8.4 0"/>' +
      '<path d="M8 12v2.4"/>'
  ),
  // drum shell + crossed sticks
  drums: S(
    '<ellipse cx="8" cy="9.2" rx="5" ry="2.2"/>' +
      '<path d="M3 9.2v3c0 1.2 2.2 2.2 5 2.2s5-1 5-2.2v-3"/>' +
      '<path d="M5.4 6.8 2.6 3.2M10.6 6.8l2.8-3.6"/>'
  ),
  // one deep low-frequency swell (thicker stroke)
  bass: S(
    '<path d="M1.5 8c2-6 3.9-6 5.9 0s3.9 6 5.9 0" stroke-width="2"/>'
  ),
  // generic level bars (uncategorized content)
  other: S(
    '<path d="M2.5 13.5v-3M6 13.5v-6M9.5 13.5V9M13 13.5V4.5"/>'
  ),
  // piano keys
  piano: S(
    '<rect x="2" y="4" width="12" height="8" rx="1"/>' +
      '<path d="M5.8 4v4.6M10.2 4v4.6"/>'
  ),
  // guitar pick
  guitar: S(
    '<path d="M8 13.6C4.6 11 3.1 8.5 3.1 6.3 3.1 4.1 5.1 2.5 8 2.5' +
    's4.9 1.6 4.9 3.8c0 2.2-1.5 4.7-4.9 7.3z"/>'
  ),
  // smooth harmonic sine
  harmonic: S('<path d="M1.5 8q1.6-5 3.2 0t3.2 0 3.2 0 3.2 0"/>'),
  // sharp transient spikes (click train)
  percussive: S('<path d="M2 11.5 4 5.5l2 6 2-9 2 9 2-6 2 6.5"/>'),
  // low-rank: few dominant components
  lowrank: S('<path d="M3.5 12.5V8M8 12.5V5M12.5 12.5V2.8"/>'),
  // sparse: scattered dots
  sparse: S(
    '<path d="M2.8 11.2h.01M6.6 4.8h.01M10.4 10.4h.01M13.4 4h.01"/>'
  ),
};

export function trackIcon(key) {
  return ICONS[key] || ICONS.other;
}
