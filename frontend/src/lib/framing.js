export const RATIO = 16 / 9;

/** Width / height of a photo from a "4000x3000" style string, or null. */
export function ratioFromResolution(res) {
  const m = /^\s*(\d{2,6})\s*[x×]\s*(\d{2,6})/i.exec(res || '');
  return m ? Number(m[1]) / Number(m[2]) : null;
}

/** Which way the 16:9 window can slide inside a photo: 'y', 'x', or null when it is already 16:9. */
export function slideAxis(ratio) {
  if (!ratio) return 'y';
  if (Math.abs(ratio - RATIO) < 0.012) return null;
  return ratio < RATIO ? 'y' : 'x';
}

/** CSS object-position for a photo shown in a 16:9 frame. */
export function objectPosition(ratio, y) {
  const p = `${(Math.max(0, Math.min(1, y)) * 100).toFixed(2)}%`;
  return slideAxis(ratio) === 'x' ? `${p} 50%` : `50% ${p}`;
}
