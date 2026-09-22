/**
 * Little diagrams for the geometry sliders, in place of coloured tracks.
 *
 * Those tracks were borrowed from Temperature and Tint, where a warm/cool or
 * green/magenta gradient genuinely tells you what the slider does. On
 * Vertical, Horizontal and Lens distortion they say nothing at all - there is
 * no colour meaning to keystone. A picture of the frame being pushed is what
 * a person can actually read at a glance, which is why Lightroom draws one.
 *
 * Each glyph shows the shape the frame takes at that end of the slider. Put
 * the minus glyph at the left of the track and the plus glyph at the right,
 * and give the track itself a plain neutral fill with a centre detent.
 *
 *   <GeometryGlyph kind="vertical" side="min" />
 *
 * Sized in em so they follow the label's font size.
 */

const S = {
  width: '1.15em',
  height: '1.15em',
  display: 'block',
  flex: 'none',
};

function Frame({ children, title }) {
  return (
    <svg viewBox="0 0 24 24" style={S} fill="none" aria-hidden="true" focusable="false">
      <title>{title}</title>
      {children}
    </svg>
  );
}

const stroke = {
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinejoin: 'round',
  strokeLinecap: 'round',
};
const ghost = { ...stroke, strokeWidth: 1, opacity: 0.28, strokeDasharray: '2 2' };

// The reference rectangle every glyph is deformed away from.
const REF = <rect x="4" y="5" width="16" height="14" rx="1" {...ghost} />;

export const GEOMETRY_GLYPHS = {
  // Vertical keystone: the top edge widens or narrows - what you get when the
  // camera is tilted up or down and the walls stop being parallel.
  vertical: {
    min: (
      <Frame title="Top narrows">
        {REF}
        <path d="M7.5 5 H16.5 L20 19 H4 Z" {...stroke} />
      </Frame>
    ),
    max: (
      <Frame title="Top widens">
        {REF}
        <path d="M4 5 H20 L16.5 19 H7.5 Z" {...stroke} />
      </Frame>
    ),
  },
  // Horizontal keystone: the left or right edge stretches - the camera was
  // off to one side of the wall it was pointed at.
  horizontal: {
    min: (
      <Frame title="Left edge stretches">
        {REF}
        <path d="M4 4 V20 L20 17 V7 Z" {...stroke} />
      </Frame>
    ),
    max: (
      <Frame title="Right edge stretches">
        {REF}
        <path d="M20 4 V20 L4 17 V7 Z" {...stroke} />
      </Frame>
    ),
  },
  // Lens distortion: barrel bulges out, pincushion pulls in.
  distortion: {
    min: (
      <Frame title="Pincushion — edges pull in">
        {REF}
        <path d="M5 5 Q12 8 19 5 Q16 12 19 19 Q12 16 5 19 Q8 12 5 5 Z" {...stroke} />
      </Frame>
    ),
    max: (
      <Frame title="Barrel — edges bulge out">
        {REF}
        <path d="M5 5 Q12 2 19 5 Q22 12 19 19 Q12 22 5 19 Q2 12 5 5 Z" {...stroke} />
      </Frame>
    ),
  },
  // Straighten: rotation about the centre.
  straighten: {
    min: (
      <Frame title="Rotate anticlockwise">
        {REF}
        <g transform="rotate(-9 12 12)">
          <rect x="4" y="5" width="16" height="14" rx="1" {...stroke} />
        </g>
      </Frame>
    ),
    max: (
      <Frame title="Rotate clockwise">
        {REF}
        <g transform="rotate(9 12 12)">
          <rect x="4" y="5" width="16" height="14" rx="1" {...stroke} />
        </g>
      </Frame>
    ),
  },
  // Scale: how far into the frame the crop sits.
  scale: {
    min: (
      <Frame title="Whole frame">
        <rect x="3" y="4.5" width="18" height="15" rx="1" {...stroke} />
      </Frame>
    ),
    max: (
      <Frame title="Zoomed in">
        {REF}
        <rect x="7.5" y="8" width="9" height="8" rx="1" {...stroke} />
        <path d="M9.5 10.5 L14.5 10.5 M9.5 13.5 L14.5 13.5" {...ghost} />
      </Frame>
    ),
  },
};

export default function GeometryGlyph({ kind, side = 'min', className = '' }) {
  const set = GEOMETRY_GLYPHS[kind];
  if (!set) return null;
  return (
    <span className={`shrink-0 text-zinc-500 ${className}`} style={{ lineHeight: 0 }}>
      {set[side] || set.min}
    </span>
  );
}

/**
 * A geometry slider row: label and value on top, then glyph - track - glyph.
 * The track is deliberately plain; the meaning lives in the two glyphs.
 */
export function GeometrySlider({
  kind, label, value, onChange, min = -1, max = 1, step = 0.001,
  format = (v) => Number(v).toFixed(3),
}) {
  const bipolar = min < 0 && max > 0;
  return (
    <div className="mb-3">
      <div className="mb-1 flex items-baseline justify-between">
        <span className="text-xs text-zinc-300">{label}</span>
        <span className="font-mono text-[11px] text-zinc-400">{format(value)}</span>
      </div>
      <div className="flex items-center gap-2">
        <GeometryGlyph kind={kind} side="min" />
        <div className="relative flex-1">
          {bipolar && (
            <span
              aria-hidden="true"
              className="pointer-events-none absolute left-1/2 top-1/2 h-2.5 w-px -translate-x-1/2 -translate-y-1/2 bg-zinc-600"
            />
          )}
          <input
            type="range"
            min={min} max={max} step={step}
            value={value}
            onChange={(e) => onChange?.(Number(e.target.value))}
            onDoubleClick={() => onChange?.(bipolar ? 0 : min)}
            className="w-full accent-accent"
            title="Double-click to reset"
          />
        </div>
        <GeometryGlyph kind={kind} side="max" />
      </div>
    </div>
  );
}
