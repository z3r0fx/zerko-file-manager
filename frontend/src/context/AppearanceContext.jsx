import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

/**
 * Everything the person can change about how the app looks and feels, in one
 * place.
 *
 * The values are written to the document root as CSS custom properties, so
 * the whole UI follows without a single component needing to know a setting
 * exists. Stored per browser rather than per account on purpose: this is
 * about the screen you are sitting at, and the same person on a laptop and a
 * colour-calibrated monitor will not want the same answer.
 */

const KEY = 'zerko.appearance.v1';

export const ACCENTS = [
  { id: 'ember', name: 'Ember', hex: '#ff5c1f' },
  { id: 'amber', name: 'Amber', hex: '#f5a524' },
  { id: 'lime', name: 'Lime', hex: '#8ec53f' },
  { id: 'teal', name: 'Teal', hex: '#12b3a4' },
  { id: 'sky', name: 'Sky', hex: '#3b93f7' },
  { id: 'indigo', name: 'Indigo', hex: '#6366f1' },
  { id: 'violet', name: 'Violet', hex: '#a855f7' },
  { id: 'rose', name: 'Rose', hex: '#f43f5e' },
  { id: 'slate', name: 'Graphite', hex: '#94a3b8' },
];

/** Background tones: the grey scale the whole UI is drawn from (950 is the page,
 *  900 and 800 the panels and cards on it). */
export const TONES = {
  default: {
    name: 'Default',
    v: { 50: '250 250 250', 100: '244 244 245', 200: '228 228 231', 300: '212 212 216', 400: '161 161 170',
         500: '113 113 122', 600: '82 82 91', 700: '63 63 70', 800: '39 39 42', 900: '24 24 27', 950: '9 9 11' },
  },
  black: {
    name: 'Pure black',
    v: { 50: '250 250 250', 100: '244 244 245', 200: '228 228 231', 300: '212 212 216', 400: '161 161 170',
         500: '113 113 122', 600: '72 72 80', 700: '52 52 58', 800: '30 30 33', 900: '14 14 16', 950: '0 0 0' },
  },
  midnight: {
    name: 'Midnight',
    v: { 50: '248 250 252', 100: '241 245 249', 200: '226 232 240', 300: '203 213 225', 400: '148 163 184',
         500: '100 116 139', 600: '71 85 105', 700: '51 65 85', 800: '30 41 59', 900: '15 23 42', 950: '5 9 24' },
  },
  warm: {
    name: 'Warm',
    v: { 50: '250 250 249', 100: '245 245 244', 200: '231 229 228', 300: '214 211 209', 400: '168 162 158',
         500: '120 113 108', 600: '87 83 78', 700: '68 64 60', 800: '41 37 36', 900: '28 25 23', 950: '12 10 9' },
  },
};

export const CORNERS = {
  square: { name: 'Square', radius: '0px' },
  soft: { name: 'Soft', radius: '0.5rem' },
  round: { name: 'Round', radius: '0.9rem' },
};

/** One-click looks: an accent, a background tone and a corner style together. */
export const PRESETS = [
  { id: 'ember', name: 'Ember', accent: '#ff5c1f', tone: 'default', corners: 'soft' },
  { id: 'midnight', name: 'Midnight', accent: '#6366f1', tone: 'midnight', corners: 'soft' },
  { id: 'oled', name: 'OLED', accent: '#f43f5e', tone: 'black', corners: 'soft' },
  { id: 'lagoon', name: 'Lagoon', accent: '#12b3a4', tone: 'midnight', corners: 'round' },
  { id: 'sunset', name: 'Sunset', accent: '#f5a524', tone: 'warm', corners: 'round' },
  { id: 'graphite', name: 'Graphite', accent: '#94a3b8', tone: 'black', corners: 'square' },
];

export const DEFAULTS = {
  accent: '#ff5c1f',
  tone: 'default',          // default | black | midnight | warm
  corners: 'soft',          // square | soft | round
  density: 'normal',        // compact | normal | roomy
  textScale: 1,             // 0.85 - 1.3
  motion: 'on',             // on | off
  thumbFit: 'cover',        // cover (fill the 16:9 frame) | contain (show the whole picture)
  clickAction: 'open',      // open (the dot at the tile's corner selects) | select (double-click opens)
  v: 2,                     // settings version, for one-off migrations
  cursor: 'ring',           // system | arrow | dot | ring
  cursorSize: 1,            // dot / ring: 0.6 - 1.8
  cursorColor: 'accent',    // dot / ring: 'accent' | 'light' | 'dark' | #hex
  ringSmooth: 0.2,          // ring follow speed: 0.08 (floaty) - 0.6 (snappy)
  arrowSize: 1,             // arrow: 0.6 - 2
  arrowColor: 'dark',       // arrow: 'dark' | 'light' | 'accent' | #hex
  ripple: false,            // a ring pulses out from every click
  glow: false,              // a soft accent light follows the pointer
  glowSize: 150,            // its diameter in px (60 - 400)
  glowStrength: 0.2,        // how bright, 0.05 - 0.5
  sidebarWidth: 260,
};

export const SIDEBAR_MIN = 190;
export const SIDEBAR_MAX = 560;

// --- colour helpers --------------------------------------------------------

export function hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(String(hex || '').trim());
  if (!m) return [255, 92, 31];
  return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
}

const clamp = (n) => Math.max(0, Math.min(255, Math.round(n)));

/** Lighter/darker steps for hover and pressed states. Mixing toward white and
 *  black keeps hue stable, which straight multiplication does not. */
function shift(rgb, amount) {
  const towards = amount > 0 ? 255 : 0;
  const k = Math.abs(amount);
  return rgb.map((c) => clamp(c + (towards - c) * k));
}

function lightness(rgb) {
  const [r, g, b] = rgb.map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** Perceived lightness, so text on an accent-filled button stays readable when
 *  someone picks a pale accent. */
export function readableOn(rgb) {
  return lightness(rgb) > 0.42 ? '9 9 11' : '250 250 250';
}

const toHex = (rgb) => `#${rgb.map((c) => c.toString(16).padStart(2, '0')).join('')}`;

// --- the "arrow" cursor ----------------------------------------------------
// A real cursor image, so it keeps the operating system's zero-lag pointer
// (nothing chases the mouse in JavaScript) - it is just drawn in the colour and
// size you chose. Outlined in the opposite tone so it reads on any background.

function arrowColorHex(setting, accentHex) {
  if (setting === 'light') return '#f4f4f5';
  if (setting === 'dark') return '#0a0a0b';
  if (setting === 'accent') return accentHex;
  return /^#[0-9a-f]{6}$/i.test(setting || '') ? setting : '#0a0a0b';
}

const ARROW_PATH = 'M4 2.5V20l4.6-4.2 3 6.7 3-1.3-3-6.6H19z';
// index finger up, thumb tucked - the "this is clickable" hand
const HAND_PATH = 'M9.6 2.6c-.85 0-1.5.65-1.5 1.5v8.3l-1.55-1.3c-.6-.5-1.5-.45-2 .15-.5.6-.45 1.45.1 1.95l4.6 5.2c.9 1.05 2.2 1.75 3.7 1.75h1.4c2.7 0 4.6-1.9 4.6-4.5v-4.6c0-.8-.6-1.4-1.4-1.4-.4 0-.75.15-1 .45-.2-.55-.7-.9-1.3-.9-.5 0-.95.25-1.2.65-.25-.5-.75-.8-1.3-.8-.3 0-.6.1-.8.25V4.1c0-.85-.65-1.5-1.5-1.5z';

function cursorUrl(path, px, fill, stroke, hx, hy) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px}" viewBox="0 0 24 24">`
    + `<path d="${path}" fill="${fill}" stroke="${stroke}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}") ${Math.round(hx * px / 24)} ${Math.round(hy * px / 24)}`;
}

// Density changes three things at once: the size of all text and spacing
// (scale), how big a tile in the grids wants to be (tile - so compact really
// fits more per row and roomy fewer), and the gaps between them.
const DENSITY = {
  compact: { scale: 0.84, row: '1.5rem', gap: '0.3rem', tile: 0.74, spread: 0.55 },
  normal: { scale: 1, row: '1.85rem', gap: '0.5rem', tile: 1, spread: 1 },
  roomy: { scale: 1.14, row: '2.3rem', gap: '0.8rem', tile: 1.32, spread: 1.5 },
};

function load() {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const saved = JSON.parse(raw);
    // v2: a plain click opens; selecting is the dot in the tile's corner.
    // Older saved settings had "select" as the default, so reset that once.
    if (saved.v !== 2) { saved.clickAction = 'open'; saved.v = 2; }
    return { ...DEFAULTS, ...saved };
  } catch {
    return { ...DEFAULTS };
  }
}

export const AppearanceContext = createContext(null);

export function AppearanceProvider({ children }) {
  const [settings, setSettings] = useState(load);

  useEffect(() => {
    const root = document.documentElement;
    const base = hexToRgb(settings.accent);
    const hi = shift(base, 0.22);
    const lo = shift(base, -0.28);

    root.style.setProperty('--accent-rgb', base.join(' '));
    root.style.setProperty('--accent-hi-rgb', hi.join(' '));
    root.style.setProperty('--accent-lo-rgb', lo.join(' '));
    root.style.setProperty('--accent-ink', readableOn(base));

    const d = DENSITY[settings.density] || DENSITY.normal;
    root.style.setProperty('--ui-scale', String(d.scale));
    root.style.setProperty('--row-h', d.row);
    root.style.setProperty('--row-gap', d.gap);
    root.style.setProperty('--tile-scale', String(d.tile));
    root.style.setProperty('--gap-scale', String(d.spread));
    root.style.setProperty('--text-scale', String(settings.textScale || 1));

    const tone = TONES[settings.tone] || TONES.default;
    Object.entries(tone.v).forEach(([k, v]) => root.style.setProperty(`--z-${k}`, v));
    root.style.setProperty('--radius', (CORNERS[settings.corners] || CORNERS.square).radius);
    root.dataset.motion = settings.motion === 'off' ? 'off' : 'on';

    // dot / ring colour
    const cursorRgb = settings.cursorColor === 'accent'
      ? base.join(' ')
      : settings.cursorColor === 'dark'
        ? '9 9 11'
        : settings.cursorColor === 'light'
          ? '236 236 239'
          : hexToRgb(settings.cursorColor).join(' ');
    root.style.setProperty('--cursor-rgb', cursorRgb);
    root.style.setProperty('--cursor-scale', String(settings.cursorSize));

    // arrow
    if (settings.cursor === 'arrow') {
      const fillHex = arrowColorHex(settings.arrowColor, toHex(base));
      const fillRgb = hexToRgb(fillHex);
      const stroke = lightness(fillRgb) > 0.4 ? '#0a0a0b' : '#fafafa';
      const px = Math.max(14, Math.min(48, Math.round(24 * (Number(settings.arrowSize) || 1))));
      root.style.setProperty('--cur-arrow', `${cursorUrl(ARROW_PATH, px, fillHex, stroke, 4, 2.5)}, default`);
      root.style.setProperty('--cur-hand', `${cursorUrl(HAND_PATH, px, fillHex, stroke, 9.6, 2.6)}, pointer`);
      root.classList.add('cur-arrow');
    } else {
      root.classList.remove('cur-arrow');
    }

    root.style.setProperty('--sidebar-w', `${settings.sidebarWidth}px`);
    root.dataset.density = settings.density;

    try {
      localStorage.setItem(KEY, JSON.stringify(settings));
    } catch {
      /* private window, or storage full - the look still applies for this session */
    }
  }, [settings]);

  const set = useCallback((patch) => {
    setSettings((s) => ({ ...s, ...patch }));
  }, []);

  const reset = useCallback(() => setSettings({ ...DEFAULTS }), []);

  const value = useMemo(() => ({ ...settings, set, reset }), [settings, set, reset]);
  return <AppearanceContext.Provider value={value}>{children}</AppearanceContext.Provider>;
}

export function useAppearance() {
  const ctx = useContext(AppearanceContext);
  if (!ctx) throw new Error('useAppearance must be used inside AppearanceProvider');
  return ctx;
}
