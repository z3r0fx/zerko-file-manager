import { useRef, useCallback } from 'react';

/**
 * Wraps a card so it leans toward the pointer, with a soft highlight that
 * tracks the cursor across the artwork.
 *
 * The transform is written straight to the node in the mousemove handler -
 * no React state - so dragging the mouse across a grid of 1,200 cards does
 * not trigger a single re-render.
 */
export default function TiltCard({
  children,
  className = '',
  maxTilt = 10,
  lift = 18,
  disabled = false,
  ...rest
}) {
  const ref = useRef(null);
  const glowRef = useRef(null);
  const raf = useRef(null);

  const onMouseMove = useCallback((e) => {
    if (disabled || !ref.current) return;
    const node = ref.current;
    const r = node.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width;
    const py = (e.clientY - r.top) / r.height;

    if (raf.current) cancelAnimationFrame(raf.current);
    raf.current = requestAnimationFrame(() => {
      const rx = (0.5 - py) * maxTilt;
      const ry = (px - 0.5) * maxTilt * 1.15;
      node.style.willChange = 'transform';
      node.style.transform =
        `perspective(1000px) rotateX(${rx.toFixed(2)}deg) rotateY(${ry.toFixed(2)}deg) translateZ(${lift}px)`;
      node.style.transition = 'box-shadow 260ms ease, border-color 200ms ease';
      node.style.boxShadow = '0 26px 46px -18px rgba(0,0,0,0.85)';
      if (glowRef.current) {
        glowRef.current.style.opacity = '1';
        glowRef.current.style.background =
          `radial-gradient(240px circle at ${(px * 100).toFixed(1)}% ${(py * 100).toFixed(1)}%, rgba(255,255,255,0.18), rgba(255,255,255,0) 62%)`;
      }
    });
  }, [disabled, maxTilt, lift]);

  const onMouseLeave = useCallback(() => {
    if (!ref.current) return;
    if (raf.current) cancelAnimationFrame(raf.current);
    const node = ref.current;
    node.style.transition =
      'transform 520ms cubic-bezier(0.22, 1, 0.36, 1), box-shadow 300ms ease, border-color 200ms ease';
    node.style.transform = 'perspective(1000px) rotateX(0deg) rotateY(0deg) translateZ(0)';
    // Empty string REMOVES the inline declaration. Setting it to a transparent
    // shadow instead would keep overriding any class-based box-shadow - which
    // is exactly what was wiping the selection ring off hovered cards.
    node.style.boxShadow = '';
    if (glowRef.current) glowRef.current.style.opacity = '0';
    // Drop the layer again once the card settles, or every card in the grid
    // stays permanently promoted and the compositor grinds to a halt.
    window.setTimeout(() => { if (node) node.style.willChange = 'auto'; }, 560);
  }, []);

  return (
    <div
      ref={ref}
      className={className}
      style={{ transformStyle: 'preserve-3d' }}
      onMouseMove={onMouseMove}
      onMouseLeave={onMouseLeave}
      {...rest}
    >
      {children}
      <div
        ref={glowRef}
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 rounded-lg"
        style={{ opacity: 0, transition: 'opacity 240ms ease' }}
      />
    </div>
  );
}
