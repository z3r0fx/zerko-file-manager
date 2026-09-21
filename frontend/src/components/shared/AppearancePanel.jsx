import { useEffect, useState } from 'react';
import { X, RotateCcw, Check, Palette, LayoutPanelLeft, MousePointer2, Sparkles } from 'lucide-react';
import {
  useAppearance, ACCENTS, TONES, CORNERS, PRESETS, DEFAULTS, hexToRgb, readableOn,
} from '../../context/AppearanceContext';

function Row({ label, hint, children }) {
  return (
    <div className="border-b border-zinc-800/80 py-4 last:border-0">
      <div className="mb-2.5 flex items-baseline justify-between gap-4">
        <span className="text-sm font-medium text-zinc-200">{label}</span>
        {hint && <span className="text-right text-[11px] leading-tight text-zinc-500">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

function Choice({ options, value, onChange }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={`rounded-md px-3 py-1.5 text-xs font-medium transition active:scale-95 ${
            value === o.value
              ? 'bg-accent text-accent-foreground'
              : 'bg-zinc-800/70 text-zinc-300 hover:bg-zinc-800'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Slider({ label, value, min, max, step, onChange, format }) {
  return (
    <div>
      <div className="mb-1 flex justify-between text-[11px] text-zinc-500">
        <span>{label}</span><span className="font-mono">{format ? format(value) : value}</span>
      </div>
      <input type="range" min={min} max={max} step={step} value={value}
             onChange={(e) => onChange(Number(e.target.value))} className="w-full accent-accent" />
    </div>
  );
}

function Toggle({ on, onChange, label, hint }) {
  return (
    <button type="button" role="switch" aria-checked={on} onClick={() => onChange(!on)}
            className="flex w-full items-center justify-between gap-4 rounded-lg px-1 py-2 text-left transition hover:bg-zinc-900">
      <span>
        <span className="block text-sm text-zinc-200">{label}</span>
        {hint && <span className="block text-[11px] leading-snug text-zinc-500">{hint}</span>}
      </span>
      <span className={`relative h-5 w-9 shrink-0 rounded-full transition ${on ? 'bg-accent' : 'bg-zinc-700'}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${on ? 'left-[18px]' : 'left-0.5'}`} />
      </span>
    </button>
  );
}

/** A colour choice: a few named swatches plus "pick your own". */
function ColourChoice({ value, onChange, options, accent }) {
  const named = options.map((o) => o.value);
  const isCustom = !named.includes(value);
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {options.map((o) => (
        <button key={o.value} onClick={() => onChange(o.value)} title={o.label}
                className={`flex items-center gap-2 rounded-md px-2.5 py-1.5 text-xs font-medium transition active:scale-95 ${
                  value === o.value ? 'bg-zinc-100 text-zinc-900' : 'bg-zinc-800/70 text-zinc-300 hover:bg-zinc-800'}`}>
          <span className="h-3 w-3 rounded-full border border-zinc-500/60"
                style={{ background: o.value === 'accent' ? accent : o.value === 'dark' ? '#0a0a0b' : o.value === 'light' ? '#f4f4f5' : o.value }} />
          {o.label}
        </button>
      ))}
      <label className={`flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-1.5 text-xs font-medium transition ${
        isCustom ? 'bg-zinc-100 text-zinc-900' : 'bg-zinc-800/70 text-zinc-300 hover:bg-zinc-800'}`}>
        <input type="color" value={isCustom && /^#[0-9a-f]{6}$/i.test(value) ? value : '#ffffff'}
               onChange={(e) => onChange(e.target.value)} className="h-4 w-4 cursor-pointer rounded border-0 bg-transparent p-0" />
        Custom
      </label>
    </div>
  );
}

const TABS = [
  { id: 'theme', label: 'Theme', icon: Palette },
  { id: 'layout', label: 'Layout', icon: LayoutPanelLeft },
  { id: 'cursor', label: 'Cursor', icon: MousePointer2 },
  { id: 'effects', label: 'Effects', icon: Sparkles },
];

export default function AppearancePanel({ onClose }) {
  const a = useAppearance();
  const [tab, setTab] = useState(() => { try { return sessionStorage.getItem('zerko.appearance.tab') || 'theme'; } catch { return 'theme'; } });
  const [custom, setCustom] = useState(
    ACCENTS.some((c) => c.hex.toLowerCase() === (a.accent || '').toLowerCase()) ? a.accent : a.accent,
  );

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const pick = (id) => { setTab(id); try { sessionStorage.setItem('zerko.appearance.tab', id); } catch { /* fine */ } };
  const ink = `rgb(${readableOn(hexToRgb(a.accent))})`;
  const isPreset = (p) => a.accent.toLowerCase() === p.accent && a.tone === p.tone && a.corners === p.corners;

  return (
    <div
      className="animate-shade-in fixed inset-0 z-[9998] flex items-start justify-end bg-black/40 p-0 sm:p-4"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="animate-pop-in flex h-full w-full flex-col border-zinc-800 bg-zinc-950 sm:h-auto sm:max-h-[92vh] sm:w-[28rem] sm:rounded-2xl sm:border sm:shadow-2xl">
        <div className="flex items-center justify-between px-5 pb-2 pt-4">
          <h2 className="text-base font-semibold text-zinc-100">Appearance</h2>
          <div className="flex items-center gap-1">
            <button onClick={a.reset} title="Back to defaults"
                    className="rounded-md p-2 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200">
              <RotateCcw className="h-4 w-4" />
            </button>
            <button onClick={onClose} className="rounded-md p-2 text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-200">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="mx-5 mb-1 flex gap-1 rounded-xl bg-zinc-900 p-1" role="tablist">
          {TABS.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => pick(t.id)}
                    className={`flex flex-1 items-center justify-center gap-1.5 rounded-[9px] py-1.5 text-xs font-medium transition ${
                      tab === t.id ? 'bg-zinc-700 text-zinc-50' : 'text-zinc-500 hover:text-zinc-200'}`}>
              <t.icon className="h-3.5 w-3.5" /> {t.label}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
          {tab === 'theme' && (
            <>
              <Row label="Looks" hint="Accent, background and corners in one click">
                <div className="grid grid-cols-3 gap-2">
                  {PRESETS.map((p) => {
                    const t = TONES[p.tone].v;
                    return (
                      <button key={p.id} onClick={() => a.set({ accent: p.accent, tone: p.tone, corners: p.corners })}
                              className={`group overflow-hidden rounded-xl border text-left transition active:scale-95 ${
                                isPreset(p) ? 'border-accent' : 'border-zinc-800 hover:border-zinc-600'}`}>
                        <div className="p-2" style={{ background: `rgb(${t[950]})` }}>
                          <div className="h-8 rounded-md p-1.5" style={{ background: `rgb(${t[800]})`, borderRadius: CORNERS[p.corners].radius === '0px' ? 3 : 8 }}>
                            <div className="h-full w-1/2 rounded-sm" style={{ background: p.accent, borderRadius: CORNERS[p.corners].radius === '0px' ? 2 : 6 }} />
                          </div>
                        </div>
                        <div className="bg-zinc-900 px-2 py-1 text-[11px] text-zinc-300">{p.name}</div>
                      </button>
                    );
                  })}
                </div>
              </Row>

              <Row label="Accent colour" hint="Buttons, highlights and the active state">
                <div className="flex flex-wrap gap-2">
                  {ACCENTS.map((c) => {
                    const on = c.hex.toLowerCase() === (a.accent || '').toLowerCase();
                    return (
                      <button key={c.id} title={c.name} onClick={() => { a.set({ accent: c.hex }); setCustom(c.hex); }}
                              style={{ backgroundColor: c.hex }}
                              className={`grid h-8 w-8 place-items-center rounded-full transition active:scale-90 ${
                                on ? 'ring-2 ring-zinc-100 ring-offset-2 ring-offset-zinc-950' : 'hover:scale-110'}`}>
                        {on && <Check className="h-4 w-4" style={{ color: ink }} />}
                      </button>
                    );
                  })}
                </div>
                <label className="mt-3 flex items-center gap-2.5">
                  <input type="color" value={/^#[0-9a-f]{6}$/i.test(custom) ? custom : a.accent}
                         onChange={(e) => { setCustom(e.target.value); a.set({ accent: e.target.value }); }}
                         className="h-8 w-8 cursor-pointer rounded border border-zinc-700 bg-transparent p-0.5" />
                  <span className="text-xs text-zinc-400">or pick your own</span>
                </label>
              </Row>

              <Row label="Background" hint="The dark the whole app is drawn in">
                <div className="grid grid-cols-4 gap-2">
                  {Object.entries(TONES).map(([id, t]) => (
                    <button key={id} onClick={() => a.set({ tone: id })}
                            className={`overflow-hidden rounded-xl border text-center transition active:scale-95 ${
                              a.tone === id ? 'border-accent' : 'border-zinc-800 hover:border-zinc-600'}`}>
                      <div className="flex h-9">
                        <span className="flex-1" style={{ background: `rgb(${t.v[950]})` }} />
                        <span className="flex-1" style={{ background: `rgb(${t.v[900]})` }} />
                        <span className="flex-1" style={{ background: `rgb(${t.v[800]})` }} />
                      </div>
                      <div className="bg-zinc-900 py-1 text-[11px] text-zinc-300">{t.name}</div>
                    </button>
                  ))}
                </div>
              </Row>

              <Row label="Corners" hint="How round buttons, tabs and panels are">
                <Choice value={a.corners} onChange={(v) => a.set({ corners: v })}
                        options={Object.entries(CORNERS).map(([value, c]) => ({ value, label: c.name }))} />
              </Row>
            </>
          )}

          {tab === 'layout' && (
            <>
              <Row label="Density" hint="Text, spacing and how big tiles are">
                <Choice value={a.density} onChange={(v) => a.set({ density: v })}
                        options={[{ value: 'compact', label: 'Compact' }, { value: 'normal', label: 'Normal' }, { value: 'roomy', label: 'Roomy' }]} />
              </Row>

              <Row label="Text size" hint="Scales the text and spacing together">
                <Slider label="Size" min={0.85} max={1.3} step={0.05} value={a.textScale}
                        onChange={(v) => a.set({ textScale: v })} format={(v) => `${Math.round(v * 100)}%`} />
              </Row>

              <Row label="Thumbnails" hint="Every tile is 16:9">
                <Choice value={a.thumbFit} onChange={(v) => a.set({ thumbFit: v })}
                        options={[{ value: 'cover', label: 'Fill the frame' }, { value: 'contain', label: 'Show whole picture' }]} />
              </Row>

              <Row label="Clicking a video" hint="Like a file browser, or straight to the player">
                <Choice value={a.clickAction} onChange={(v) => a.set({ clickAction: v })}
                        options={[{ value: 'open', label: 'Open it' }, { value: 'select', label: 'Select it - double-click opens' }]} />
                <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">
                  Either way, the dot in a tile's corner selects it. Click one dot, hold <kbd className="rounded bg-zinc-800 px-1">Shift</kbd> and click another to pick everything in between,
                  or on a touch screen press a dot and drag across the tiles. <kbd className="ml-1 rounded bg-zinc-800 px-1">Ctrl</kbd> adds or removes one.
                  Press and hold anything for its right-click menu.
                </p>
              </Row>

              <Row label="Sidebar" hint="Drag its right edge">
                <div className="flex items-center justify-between gap-3 rounded-lg bg-zinc-900 px-3 py-2.5">
                  <span className="text-xs text-zinc-400">
                    Hold and drag the line between the sidebar and the page. Double-click it to reset.
                    <span className="ml-1 font-mono text-zinc-500">{a.sidebarWidth}px</span>
                  </span>
                  <button onClick={() => a.set({ sidebarWidth: DEFAULTS.sidebarWidth })}
                          className="shrink-0 rounded-md bg-zinc-800 px-2.5 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-700 active:scale-95">
                    Reset
                  </button>
                </div>
              </Row>
            </>
          )}

          {tab === 'cursor' && (
            <>
              <Row label="Cursor" hint="Text fields always keep the normal I-beam">
                <div className="grid grid-cols-2 gap-2">
                  {[
                    ['system', 'System', 'Your normal pointer'],
                    ['arrow', 'Arrow', 'Any colour and size'],
                    ['dot', 'Dot', 'A small dot'],
                    ['ring', 'Dot + ring', 'Dot with a trailing ring'],
                  ].map(([id, name, sub]) => (
                    <button key={id} onClick={() => a.set({ cursor: id })}
                            className={`rounded-xl border px-3 py-2 text-left transition active:scale-95 ${
                              a.cursor === id ? 'border-accent bg-accent/10' : 'border-zinc-800 hover:border-zinc-600'}`}>
                      <span className="block text-sm font-medium text-zinc-100">{name}</span>
                      <span className="block text-[11px] text-zinc-500">{sub}</span>
                    </button>
                  ))}
                </div>
              </Row>

              {a.cursor === 'arrow' && (
                <Row label="Arrow" hint="Outlined so it shows on any background">
                  <div className="space-y-3">
                    <Slider label="Size" min={0.6} max={2} step={0.1} value={a.arrowSize}
                            onChange={(v) => a.set({ arrowSize: v })} format={(v) => `${Math.round(v * 24)}px`} />
                    <ColourChoice value={a.arrowColor} onChange={(v) => a.set({ arrowColor: v })} accent={a.accent}
                                  options={[{ value: 'dark', label: 'Black' }, { value: 'light', label: 'White' }, { value: 'accent', label: 'Accent' }]} />
                  </div>
                </Row>
              )}

              {(a.cursor === 'dot' || a.cursor === 'ring') && (
                <Row label={a.cursor === 'ring' ? 'Dot + ring' : 'Dot'} hint="Size, colour and feel">
                  <div className="space-y-3">
                    <Slider label="Size" min={0.6} max={1.8} step={0.1} value={a.cursorSize}
                            onChange={(v) => a.set({ cursorSize: v })} format={(v) => `${Math.round(v * 100)}%`} />
                    {a.cursor === 'ring' && (
                      <Slider label="Ring follows" min={0.08} max={0.6} step={0.02} value={a.ringSmooth}
                              onChange={(v) => a.set({ ringSmooth: v })}
                              format={(v) => (v < 0.16 ? 'floaty' : v < 0.32 ? 'smooth' : 'snappy')} />
                    )}
                    <ColourChoice value={a.cursorColor} onChange={(v) => a.set({ cursorColor: v })} accent={a.accent}
                                  options={[{ value: 'accent', label: 'Accent' }, { value: 'light', label: 'White' }, { value: 'dark', label: 'Black' }]} />
                  </div>
                </Row>
              )}

              {a.cursor === 'system' && (
                <p className="pt-4 text-[11px] leading-relaxed text-zinc-500">
                  The system cursor is your operating system's own and can't be recoloured from a web page. Choose
                  <b className="text-zinc-300"> Arrow </b> to get the same pointer in black, white or any colour, at any size.
                </p>
              )}
            </>
          )}

          {tab === 'effects' && (
            <>
              <Row label="Motion" hint="Slides, fades and hover easing">
                <Choice value={a.motion} onChange={(v) => a.set({ motion: v })}
                        options={[{ value: 'on', label: 'Smooth' }, { value: 'off', label: 'Instant' }]} />
              </Row>
              <div className="py-3">
                <Toggle on={a.ripple} onChange={(v) => a.set({ ripple: v })} label="Click ripple"
                        hint="A ring pulses out from wherever you click" />
                <Toggle on={a.glow} onChange={(v) => a.set({ glow: v })} label="Pointer glow"
                        hint="A soft accent light follows your mouse" />
                {a.glow && (
                  <div className="space-y-3 px-1 pb-2 pt-1">
                    <Slider label="Glow size" min={60} max={400} step={10} value={a.glowSize}
                            onChange={(v) => a.set({ glowSize: v })} format={(v) => `${v}px`} />
                    <Slider label="Brightness" min={0.05} max={0.5} step={0.05} value={a.glowStrength}
                            onChange={(v) => a.set({ glowStrength: v })} format={(v) => `${Math.round(v * 200)}%`} />
                  </div>
                )}
              </div>
            </>
          )}

          <p className="pt-3 text-[11px] leading-relaxed text-zinc-500">
            Saved in this browser, not on your account - a laptop and a colour-calibrated monitor rarely want the same answer.
            The reset arrow at the top puts everything back.
          </p>
        </div>
      </div>
    </div>
  );
}
