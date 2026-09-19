import { useEffect, useRef, useContext } from 'react';
import { AuthContext } from '../../context/AuthContext';

/**
 * Dot + trailing ring.
 *
 * Everything is animated by writing transforms straight to the DOM inside a
 * requestAnimationFrame loop. The previous version called setState on every
 * mousemove AND listed position/ringPosition in its effect deps, so it tore
 * down and re-registered the window listener on every single frame - that is
 * why the UI felt heavy once the grid got large.
 */
export default function CustomCursor() {
  const { user } = useContext(AuthContext);

  const dotRef = useRef(null);
  const ringRef = useRef(null);
  const target = useRef({ x: -100, y: -100 });
  const ring = useRef({ x: -100, y: -100 });
  const hot = useRef(false);
  const down = useRef(false);
  const visible = useRef(false);
  const raf = useRef(null);

  useEffect(() => {
    if (!user) return undefined;
    if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return undefined;

    const INTERACTIVE = 'button, a, input, select, textarea, [role="button"], [data-cursor="hot"]';

    const onMove = (e) => {
      target.current.x = e.clientX;
      target.current.y = e.clientY;
      visible.current = true;
      const el = e.target;
      hot.current = !!(el && el.closest && el.closest(INTERACTIVE));
    };
    const onDown = () => { down.current = true; };
    const onUp = () => { down.current = false; };

    // Only hide when the pointer genuinely leaves the window. Deleting the
    // element under the cursor (closing a menu, removing a tag chip) fires
    // mouseout/mouseleave that bubble to document - parking the cursor
    // off-screen on those is what made it vanish until you left the page.
    const onLeave = (e) => {
      if (e && e.relatedTarget) return;      // moved to another element, not out
      visible.current = false;
    };
    const onEnter = () => { visible.current = true; };

    const tick = () => {
      ring.current.x += (target.current.x - ring.current.x) * 0.18;
      ring.current.y += (target.current.y - ring.current.y) * 0.18;

      const size = hot.current ? 44 : 26;
      const scale = down.current ? 0.82 : 1;
      const op = visible.current ? '1' : '0';

      if (ringRef.current) {
        ringRef.current.style.width = `${size}px`;
        ringRef.current.style.height = `${size}px`;
        ringRef.current.style.transform =
          `translate3d(${ring.current.x - size / 2}px, ${ring.current.y - size / 2}px, 0) scale(${scale})`;
        ringRef.current.style.borderColor = hot.current
          ? 'rgba(255, 92, 31, 0.9)'
          : 'rgba(236, 236, 239, 0.45)';
        ringRef.current.style.backgroundColor = hot.current
          ? 'rgba(255, 92, 31, 0.08)'
          : 'transparent';
        ringRef.current.style.opacity = op;
      }

      if (dotRef.current) {
        const d = hot.current ? 5 : 6;
        dotRef.current.style.width = `${d}px`;
        dotRef.current.style.height = `${d}px`;
        dotRef.current.style.transform =
          `translate3d(${target.current.x - d / 2}px, ${target.current.y - d / 2}px, 0)`;
        dotRef.current.style.backgroundColor = hot.current ? '#ff5c1f' : '#ececef';
        dotRef.current.style.opacity = op;
      }

      raf.current = requestAnimationFrame(tick);
    };

    // pointermove also fires when the element under a STATIONARY pointer
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
  }, [user]);

  if (!user) return null;

  return (
    <>
      <div
        ref={ringRef}
        className="fixed left-0 top-0 pointer-events-none z-[99999] rounded-full border"
        style={{
          width: 26,
          height: 26,
          borderWidth: '1px',
          transition: 'width 140ms ease, height 140ms ease, border-color 160ms ease, background-color 160ms ease',
          willChange: 'transform',
        }}
      />
      <div
        ref={dotRef}
        className="fixed left-0 top-0 pointer-events-none z-[99999] rounded-full"
        style={{ width: 6, height: 6, willChange: 'transform' }}
      />
    </>
  );
}
