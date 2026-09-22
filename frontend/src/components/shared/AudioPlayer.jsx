import { useState, useRef, useEffect, useCallback } from 'react';
import { Play, Pause, Repeat, Repeat1, Volume2, VolumeX } from 'lucide-react';
import { cn } from '../../lib/utils';

/**
 * The audio tile: a live equaliser with a scrub bar, drawn to fill the 16:9
 * frame of a library tile (the filename and details sit underneath, so they
 * are not repeated here).
 *
 *  - While it plays the bars really do move: they read the sound through a Web
 *    Audio analyser. If the browser will not give us one, they fall back to a
 *    gentle simulated bounce so the tile still feels alive.
 *  - Bars left of the playhead are coloured, so the equaliser is also the
 *    progress display. Click or drag anywhere across it to seek.
 *  - Heights are written straight to the DOM inside an animation frame, never
 *    through React state, so a grid full of audio tiles stays light.
 *  - Only one plays at a time: starting a clip stops the previous one.
 */

const BARS = 28;
let current = null;              // { pause } of whichever tile is playing

const fmt = (s) => {
  if (!Number.isFinite(s) || s < 0) return '0:00';
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
};

/** Stable pseudo-random resting shape per clip (Math.random() here re-drew it on every render). */
function restingShape(id) {
  let x = (Number(id) || 1) * 2654435761 % 4294967296;
  return Array.from({ length: BARS }, () => {
    x = (x * 1664525 + 1013904223) % 4294967296;
    const base = 0.22 + (x / 4294967296) * 0.5;
    return base;
  });
}

export default function AudioPlayer({ audio, volume = 0.5, onTogglePlay }) {
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState(false);
  const [muted, setMuted] = useState(false);
  const [time, setTime] = useState({ t: 0, d: 0 });

  const elRef = useRef(null);
  const ctxRef = useRef(null);
  const anaRef = useRef(null);
  const dataRef = useRef(null);
  const barRefs = useRef([]);
  const rafRef = useRef(0);
  const progressRef = useRef(0);
  const playingRef = useRef(false);
  const rest = useRef(restingShape(audio.id));
  const scrubbing = useRef(false);
  const rootRef = useRef(null);
  const fillRef = useRef(null);
  const knobRef = useRef(null);
  const [compact, setCompact] = useState(false);

  const ensure = useCallback(() => {
    if (elRef.current) return elRef.current;
    const el = new Audio(`/api/video-file/${audio.id}?token=${localStorage.getItem('token')}`);
    el.preload = 'metadata';
    el.volume = volume;
    el.addEventListener('timeupdate', () => {
      const d = el.duration || 0;
      progressRef.current = d ? el.currentTime / d : 0;
      if (!scrubbing.current) setTime({ t: el.currentTime, d });
    });
    el.addEventListener('loadedmetadata', () => setTime((x) => ({ ...x, d: el.duration || 0 })));
    el.addEventListener('ended', () => { playingRef.current = false; setPlaying(false); });
    el.addEventListener('pause', () => { playingRef.current = false; setPlaying(false); });
    el.addEventListener('play', () => { playingRef.current = true; setPlaying(true); });
    elRef.current = el;
    return el;
  }, [audio.id, volume]);

  const hookAnalyser = () => {
    if (ctxRef.current || !elRef.current) return;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      const src = ctx.createMediaElementSource(elRef.current);
      const ana = ctx.createAnalyser();
      ana.fftSize = 128;
      ana.smoothingTimeConstant = 0.78;
      src.connect(ana);
      ana.connect(ctx.destination);
      ctxRef.current = ctx; anaRef.current = ana;
      dataRef.current = new Uint8Array(ana.frequencyBinCount);
    } catch { /* simulated bars instead */ }
  };

  const pause = useCallback(() => { elRef.current?.pause(); }, []);

  const toggle = useCallback(() => {
    const el = ensure();
    if (!el.paused) { el.pause(); return; }
    if (current && current.pause !== pause) current.pause();
    current = { pause };
    hookAnalyser();
    ctxRef.current?.resume?.();
    el.play().catch(() => { playingRef.current = false; setPlaying(false); });
  }, [ensure, pause]);

  // hand the tile a way to toggle playback when it is clicked
  useEffect(() => { if (onTogglePlay) onTogglePlay(toggle); }, [toggle, onTogglePlay]);

  useEffect(() => { if (elRef.current) { elRef.current.loop = loop; } }, [loop]);
  useEffect(() => { if (elRef.current) { elRef.current.volume = volume; elRef.current.muted = muted; } }, [volume, muted]);

  // narrow or short tiles: keep the times, drop the loop / mute buttons
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => setCompact(el.clientWidth < 250 || el.clientHeight < 128));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // the equaliser
  useEffect(() => {
    let t0 = performance.now();
    const draw = (now) => {
      const p = progressRef.current;
      const live = playingRef.current;
      const ana = anaRef.current;
      if (live && ana && dataRef.current) ana.getByteFrequencyData(dataRef.current);
      const bins = dataRef.current ? dataRef.current.length : 0;
      const sec = (now - t0) / 1000;
      for (let i = 0; i < BARS; i += 1) {
        const bar = barRefs.current[i];
        if (!bar) continue;
        let h = rest.current[i];
        if (live) {
          if (ana && bins) {
            // spread the lower ~70% of the spectrum (where music lives) across the bars
            const v = dataRef.current[Math.min(bins - 1, Math.floor((i / BARS) * bins * 0.7))] / 255;
            h = 0.12 + v * 0.88;
          } else {
            h = 0.25 + 0.55 * Math.abs(Math.sin(sec * 3.1 + i * 0.7) * Math.cos(sec * 1.7 + i * 0.35));
          }
        }
        bar.style.transform = `scaleY(${h.toFixed(3)})`;
        const passed = (i + 0.5) / BARS <= p;
        bar.style.opacity = passed ? '1' : (live ? '0.55' : '0.45');
        bar.style.backgroundColor = passed ? 'rgb(var(--accent-rgb))' : 'rgb(var(--z-500))';
      }
      const pct = `${(Math.max(0, Math.min(1, p)) * 100).toFixed(2)}%`;
      if (fillRef.current) fillRef.current.style.width = pct;
      if (knobRef.current) knobRef.current.style.left = pct;
      rafRef.current = requestAnimationFrame(draw);
    };
    rafRef.current = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(rafRef.current);
  }, []);

  useEffect(() => () => {
    const el = elRef.current;
    if (el) { el.pause(); el.removeAttribute('src'); }
    if (current && current.pause === pause) current = null;
    try { ctxRef.current?.close?.(); } catch { /* already closed */ }
  }, [pause]);

  // click / drag across the equaliser to seek
  const seekFrom = (e) => {
    const el = ensure();
    const box = e.currentTarget.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (e.clientX - box.left) / box.width));
    const d = el.duration || time.d || audio.duration || 0;
    if (!d) return;
    el.currentTime = f * d;
    progressRef.current = f;
    setTime({ t: f * d, d });
  };
  const stop = (e) => e.stopPropagation();

  const total = time.d || audio.duration || 0;
  const shown = playing || time.t > 0 ? fmt(time.t) : '0:00';

  return (
    <div ref={rootRef} className="absolute inset-0 flex flex-col bg-gradient-to-b from-zinc-900 to-zinc-950 px-3 pb-2 pt-3"
         onClick={stop} onDoubleClick={stop} draggable={false}>
      {/* equaliser / scrub bar */}
      <div
        className="group/eq relative flex min-h-0 flex-1 cursor-pointer touch-none items-end gap-[3px]"
        role="slider" aria-label="Seek" aria-valuemin={0} aria-valuemax={100}
        aria-valuenow={Math.round(progressRef.current * 100)}
        onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); }}
        onDragStart={(e) => { e.preventDefault(); e.stopPropagation(); }}
        onPointerDown={(e) => { e.stopPropagation(); scrubbing.current = true; e.currentTarget.setPointerCapture?.(e.pointerId); seekFrom(e); }}
        onPointerMove={(e) => { if (scrubbing.current) seekFrom(e); }}
        onPointerUp={(e) => { scrubbing.current = false; e.currentTarget.releasePointerCapture?.(e.pointerId); }}
      >
        {Array.from({ length: BARS }, (_, i) => (
          <span key={i} className="h-full flex-1 origin-bottom rounded-full"
                ref={(n) => { barRefs.current[i] = n; }}
                style={{ transform: `scaleY(${rest.current[i]})`, backgroundColor: 'rgb(var(--z-500))', opacity: 0.45 }} />
        ))}
      </div>

      {/* glowing progress bar - click or drag to seek */}
      <div
        className="group/bar mt-1.5 flex h-4 shrink-0 cursor-pointer touch-none items-center"
        role="slider" aria-label="Position" aria-valuemin={0} aria-valuemax={100}
        onMouseDown={(e) => { e.preventDefault(); e.stopPropagation(); }}
        onDragStart={(e) => { e.preventDefault(); e.stopPropagation(); }}
        onPointerDown={(e) => { e.stopPropagation(); scrubbing.current = true; e.currentTarget.setPointerCapture?.(e.pointerId); seekFrom(e); }}
        onPointerMove={(e) => { if (scrubbing.current) seekFrom(e); }}
        onPointerUp={(e) => { scrubbing.current = false; e.currentTarget.releasePointerCapture?.(e.pointerId); }}
      >
        <div className="relative h-1.5 w-full rounded-full bg-zinc-800/90">
          <div ref={fillRef} className="absolute inset-y-0 left-0 rounded-full"
               style={{
                 width: '0%',
                 background: 'linear-gradient(90deg, rgb(var(--accent-rgb) / 0.55), rgb(var(--accent-rgb)))',
                 boxShadow: playing
                   ? '0 0 12px 2px rgb(var(--accent-rgb) / 0.85), 0 0 3px rgb(var(--accent-rgb))'
                   : '0 0 8px 1px rgb(var(--accent-rgb) / 0.5)',
                 transition: 'box-shadow 300ms',
               }} />
          <div ref={knobRef}
               className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 scale-90 rounded-full bg-white transition-transform group-hover/bar:scale-110"
               style={{ left: '0%', boxShadow: '0 0 10px 3px rgb(var(--accent-rgb) / 0.8)' }} />
        </div>
      </div>

      {/* transport: play, the two times (never cut off), and - when there is room - loop and mute */}
      <div className="mt-1 flex shrink-0 items-center gap-2">
        <button type="button" aria-label={playing ? 'Pause' : 'Play'}
                onClick={(e) => { e.stopPropagation(); toggle(); }}
                className={cn('grid shrink-0 place-items-center rounded-full bg-accent text-accent-foreground shadow-lg shadow-accent/25 transition hover:scale-110 active:scale-90',
                              compact ? 'h-7 w-7' : 'h-8 w-8')}>
          {playing ? <Pause className="h-4 w-4" fill="currentColor" /> : <Play className="ml-0.5 h-4 w-4" fill="currentColor" />}
        </button>
        <span className="shrink-0 whitespace-nowrap font-mono text-[11px] tabular-nums text-zinc-200">{shown}</span>
        <span className="min-w-0 flex-1" />
        <span className="shrink-0 whitespace-nowrap font-mono text-[11px] tabular-nums text-zinc-400">{total ? fmt(total) : (audio.duration_formatted || '--:--')}</span>
        {!compact && (
          <>
            <button type="button" aria-label="Loop" onClick={(e) => { e.stopPropagation(); setLoop((l) => !l); }}
                    className={cn('shrink-0 rounded-md p-1 transition hover:bg-zinc-800', loop ? 'text-accent' : 'text-zinc-500 hover:text-zinc-200')}>
              {loop ? <Repeat1 className="h-3.5 w-3.5" /> : <Repeat className="h-3.5 w-3.5" />}
            </button>
            <button type="button" aria-label={muted ? 'Unmute' : 'Mute'} onClick={(e) => { e.stopPropagation(); setMuted((m) => !m); }}
                    className={cn('shrink-0 rounded-md p-1 transition hover:bg-zinc-800', muted ? 'text-accent' : 'text-zinc-500 hover:text-zinc-200')}>
              {muted ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
