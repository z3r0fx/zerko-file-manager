import { useEffect, useRef } from 'react';
import { useAppearance } from '../../context/AppearanceContext';

/**
 * Two optional flourishes, both off by default:
 *   ripple - a ring pulses out from every click
 *   glow   - a soft accent light follows the pointer
 * They draw over the page but never catch the mouse, and the glow's position
 * is written straight to the DOM in an animation frame rather than through
 * React state.
 */
export default function PointerEffects() {
  const { ripple, glow, glowSize, glowStrength } = useAppearance();
  const size = Math.max(60, Math.min(400, Number(glowSize) || 150));
  const strength = Math.max(0.05, Math.min(0.5, Number(glowStrength) || 0.2));
  const sizeRef = useRef(size);
  sizeRef.current = size;
  const glowRef = useRef(null);

  useEffect(() => {
    if (!ripple) return undefined;
    const onDown = (e) => {
      if (e.button !== 0) return;
      const d = document.createElement('div');
      d.className = 'click-ripple';
      d.style.left = `${e.clientX}px`;
      d.style.top = `${e.clientY}px`;
      document.body.appendChild(d);
      d.addEventListener('animationend', () => d.remove(), { once: true });
      setTimeout(() => d.remove(), 900);
    };
    window.addEventListener('pointerdown', onDown, { capture: true, passive: true });
    return () => window.removeEventListener('pointerdown', onDown, { capture: true });
  }, [ripple]);

  useEffect(() => {
    if (!glow) return undefined;
    if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return undefined;
    let x = -999; let y = -999; let raf = 0;
    const onMove = (e) => { x = e.clientX; y = e.clientY; };
    const tick = () => {
      if (glowRef.current) glowRef.current.style.transform = `translate3d(${x - sizeRef.current / 2}px, ${y - sizeRef.current / 2}px, 0)`;
      raf = requestAnimationFrame(tick);
    };
    window.addEventListener('pointermove', onMove, { passive: true });
    raf = requestAnimationFrame(tick);
    return () => { window.removeEventListener('pointermove', onMove); cancelAnimationFrame(raf); };
  }, [glow]);

  if (!glow) return null;
  return (
    <div
      ref={glowRef}
      aria-hidden="true"
      className="pointer-events-none fixed left-0 top-0 z-[60] rounded-full"
      style={{
        width: size,
        height: size,
        background: `radial-gradient(closest-side, rgb(var(--accent-rgb) / ${strength}), rgb(var(--accent-rgb) / ${(strength * 0.4).toFixed(3)}) 55%, transparent)`,
        mixBlendMode: 'screen',
        willChange: 'transform',
        transform: 'translate3d(-999px,-999px,0)',
      }}
    />
  );
}
