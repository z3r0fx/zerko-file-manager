import { useEffect, useRef, useContext } from 'react';
import { AuthContext } from '../../context/AuthContext';
import { useAppearance } from '../../context/AppearanceContext';

/**
 * Three cursors, chosen in Appearance.
 *
 *   system - the operating system's own. Nothing is drawn and nothing is
 *            hidden, so text fields get an I-beam and resize handles look
 *            like resize handles.
 *   dot    - a small filled dot. Crisp, no lag, gets out of the way.
 *   ring   - the dot with a trailing ring that swells over anything clickable.
 *
 * Position is written straight to the DOM inside a requestAnimationFrame
 * loop. An earlier version called setState on every mousemove and listed the
 * position in its effect deps, so it tore down and re-registered the window
 * listener every frame - which is what made the UI feel heavy over a large
 * grid.
 */
export default function CustomCursor() {
  const { user } = useContext(AuthContext);
  const { cursor, cursorSize, ringSmooth } = useAppearance();

  const dotRef = useRef(null);
  const ringRef = useRef(null);
  const target = useRef({ x: -100, y: -100 });
  const ring = useRef({ x: -100, y: -100 });
  const hot = useRef(false);
  const text = useRef(false);
  const down = useRef(false);
  const visible = useRef(false);
  const raf = useRef(null);

  const enabled = !!user && (cursor === 'dot' || cursor === 'ring');

  useEffect(() => {
    if (!enabled) return undefined;
    if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return undefined;

    const INTERACTIVE = 'button, a, input, select, textarea, [role="button"], [data-cursor="hot"]';
    const TEXT = 'input:not([type="checkbox"]):not([type="radio"]):not([type="range"]), textarea, [contenteditable="true"]';
    const scale = Math.max(0.6, Math.min(2, Number(cursorSize) || 1));
    const withRing = cursor === 'ring';
    const follow = Math.max(0.05, Math.min(0.9, Number(ringSmooth) || 0.2));

    const onMove = (e) => {
      target.current.x = e.clientX;
      target.current.y = e.clientY;
      visible.current = true;
      const el = e.target;
      hot.current = !!(el && el.closest && el.closest(INTERACTIVE));
      // Over a text field the native I-beam is genuinely more useful than
      // anything we can draw, so we step aside and let it through.
      text.current = !!(el && el.closest && el.closest(TEXT));
    };
    const onDown = () => { down.current = true; };
    const onUp = () => { down.current = false; };

    // Only hide when the pointer genuinely leaves the window. Deleting the
    // element under the cursor (closing a menu, removing a tag chip) fires
    // mouseout/mouseleave that bubble to document - parking the cursor
    // off-screen on those is what made it vanish until you left the page.
    const onLeave = (e) => {
      if (e && e.relatedTarget) return;
      visible.current = false;
    };
    const onEnter = () => { visible.current = true; };

    const tick = () => {
      const show = visible.current && !text.current;
      const op = show ? '1' : '0';

      if (ringRef.current) {
        if (withRing) {
          ring.current.x += (target.current.x - ring.current.x) * follow;
          ring.current.y += (target.current.y - ring.current.y) * follow;
          const size = (hot.current ? 34 : 22) * scale;
          const s = down.current ? 0.84 : 1;
          ringRef.current.style.width = `${size}px`;
          ringRef.current.style.height = `${size}px`;
          ringRef.current.style.transform =
            `translate3d(${ring.current.x - size / 2}px, ${ring.current.y - size / 2}px, 0) scale(${s})`;
          ringRef.current.style.opacity = op;
        } else {
          ringRef.current.style.opacity = '0';
        }
      }

      if (dotRef.current) {
        const d = (hot.current ? 9 : 6) * scale;
        dotRef.current.style.width = `${d}px`;
        dotRef.current.style.height = `${d}px`;
        dotRef.current.style.transform =
          `translate3d(${target.current.x - d / 2}px, ${target.current.y - d / 2}px, 0)`;
        dotRef.current.style.opacity = op;
      }

      raf.current = requestAnimationFrame(tick);
    };

    // pointerover also fires when the element under a STATIONARY pointer
    // changes, so the cursor recovers by itself after a dialog closes.
    window.addEventListener('pointermove', onMove, { passive: true, capture: true });
    window.addEventListener('pointerover', onMove, { passive: true, capture: true });
    window.addEventListener('pointerdown', onDown);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('mouseenter', onEnter, true);
    document.addEventListener('mouseleave', onLeave);
    raf.current = requestAnimationFrame(tick);

    return () => {
      window.removeEventListener('pointermove', onMove, { capture: true });
      window.removeEventListener('pointerover', onMove, { capture: true });
      window.removeEventListener('pointerdown', onDown);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('mouseenter', onEnter, true);
      document.removeEventListener('mouseleave', onLeave);
      cancelAnimationFrame(raf.current);
    };
  }, [enabled, cursor, cursorSize, ringSmooth]);

  if (!enabled) return null;

  return (
    <>
      <div
        ref={ringRef}
        className="fixed left-0 top-0 pointer-events-none z-[99999] rounded-full border"
        style={{
          width: 22,
          height: 22,
          borderWidth: '1.5px',
          borderColor: 'rgb(var(--cursor-rgb) / 0.55)',
          backgroundColor: 'rgb(var(--cursor-rgb) / 0.06)',
          transition: 'width 140ms ease, height 140ms ease',
          willChange: 'transform',
          opacity: 0,
        }}
      />
      <div
        ref={dotRef}
        className="fixed left-0 top-0 pointer-events-none z-[99999] rounded-full"
        style={{
          width: 6,
          height: 6,
          backgroundColor: 'rgb(var(--cursor-rgb))',
          boxShadow: '0 0 0 1.5px rgb(0 0 0 / 0.35)',
          willChange: 'transform',
          opacity: 0,
        }}
      />
    </>
  );
}
