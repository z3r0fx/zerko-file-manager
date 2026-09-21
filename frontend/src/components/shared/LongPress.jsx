import { useEffect } from 'react';

/**
 * Press and hold = right-click, everywhere.
 *
 * A finger has no right button, so holding still on anything for about half a
 * second sends that element a real "contextmenu" event - every menu the app
 * already has for the mouse opens from a hold, with no per-screen wiring.
 * Moving the finger (scrolling, dragging) cancels it, and so does lifting it.
 * Text fields, sliders and anything marked data-no-longpress keep the
 * browser's own behaviour.
 */
const HOLD_MS = 500;
const SLOP = 10;
const OWN = 'input,textarea,select,[contenteditable="true"],[role="slider"],[data-no-longpress]';

export default function LongPress() {
  useEffect(() => {
    let timer = null;
    let start = null;
    let firedAt = 0;
    let lastTouch = 0;

    const cancel = () => { clearTimeout(timer); timer = null; start = null; };

    const down = (e) => {
      if (e.pointerType === 'mouse' || e.isPrimary === false) return;
      lastTouch = Date.now();
      if (e.target.closest?.(OWN)) return;
      cancel();
      start = { x: e.clientX, y: e.clientY, target: e.target };
      timer = setTimeout(() => {
        if (!start) return;
        const { x, y } = start;
        const el = document.elementFromPoint(x, y) || start.target;
        cancel();
        firedAt = Date.now();
        try { navigator.vibrate?.(12); } catch { /* not supported */ }
        el.dispatchEvent(new MouseEvent('contextmenu', {
          bubbles: true, cancelable: true, composed: true, view: window,
          clientX: x, clientY: y, screenX: x, screenY: y, button: 2, buttons: 2,
        }));
      }, HOLD_MS);
    };

    const move = (e) => {
      if (!start) return;
      if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > SLOP) cancel();
    };

    // The lift that ends a hold is not a tap: swallow the click it would make.
    const click = (e) => {
      if (Date.now() - firedAt < 700) { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); firedAt = 0; }
    };

    // Android also raises its own contextmenu on a hold; ours replaces it, so a
    // second one would open the menu twice (or the browser's over the top).
    const native = (e) => {
      if (!e.isTrusted || Date.now() - lastTouch > 1500) return;
      if (e.target.closest?.(OWN)) return;
      e.preventDefault();
      e.stopImmediatePropagation();
    };

    window.addEventListener('pointerdown', down, { passive: true, capture: true });
    window.addEventListener('pointermove', move, { passive: true, capture: true });
    window.addEventListener('pointerup', cancel, { capture: true });
    window.addEventListener('pointercancel', cancel, { capture: true });
    window.addEventListener('scroll', cancel, { capture: true, passive: true });
    window.addEventListener('click', click, true);
    window.addEventListener('contextmenu', native, true);
    return () => {
      cancel();
      window.removeEventListener('pointerdown', down, true);
      window.removeEventListener('pointermove', move, true);
      window.removeEventListener('pointerup', cancel, true);
      window.removeEventListener('pointercancel', cancel, true);
      window.removeEventListener('scroll', cancel, true);
      window.removeEventListener('click', click, true);
      window.removeEventListener('contextmenu', native, true);
    };
  }, []);
  return null;
}
