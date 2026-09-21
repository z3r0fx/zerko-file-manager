import { useState, useEffect, useRef, useLayoutEffect } from 'react';
import { X, ChevronLeft, ChevronRight, Tag, RotateCw, RotateCcw, Download, ZoomIn, ZoomOut, Maximize2, Crop } from 'lucide-react';
import { RATIO, slideAxis, objectPosition, ratioFromResolution } from '../../lib/framing';
import { useCrop, saveCrops } from '../../lib/photoCrops';
import StatusBadge from './StatusBadge';
import TagSelector from './TagSelector';
import NotesPanel from './NotesPanel';

/**
 * Full-screen stills viewer.
 *
 * Layout is a single flex column - header / stage / footer - rather than a
 * stack of absolutely-positioned bars. The previous version had the info bar
 * and the filmstrip both pinned to bottom-0, so the filmstrip (later in the
 * DOM) sat on top of the status control, the tag button and the notes box and
 * ate every click aimed at them.
 *
 * The stage is a flex child with min-h-0, which is what gives the <img> a real
 * height to resolve max-h-full against. Without it the percentage resolved to
 * auto, the image rendered at natural size and overflow-hidden cropped it -
 * that is why tall and 4:3 frames came out chopped.
 */
export default function PhotoLightbox({ photos, initialIndex, onClose, onUpdateStatus }) {
  const [currentIndex, setCurrentIndex] = useState(initialIndex);
  const [showTagSelector, setShowTagSelector] = useState(false);
  const [rotation, setRotation] = useState(0);
  const [zoom, setZoom] = useState(1);
  const stageRef = useRef(null);
  const fitRef = useRef(null);
  const [fitBox, setFitBox] = useState({ w: 0, h: 0 });

  const photo = photos[currentIndex];

  // 16:9 view (default): the photo fills a 16:9 frame - no bars - using the
  // framing saved for it; drag to change it. "Full photo" shows it uncropped.
  const [frame169, setFrame169] = useState(() => {
    try { return localStorage.getItem('zerko_lightbox_169') !== '0'; } catch { return true; }
  });
  const toggleFrame = () => setFrame169((v) => {
    try { localStorage.setItem('zerko_lightbox_169', v ? '0' : '1'); } catch { /* storage blocked */ }
    return !v;
  });
  const savedY = useCrop(photo ? photo.id : null);
  const [dragY, setDragY] = useState(null);       // while dragging
  const [loadedRatio, setLoadedRatio] = useState(null);
  const dragRef = useRef(null);
  useEffect(() => { setLoadedRatio(null); setDragY(null); }, [currentIndex]);
  const ratio = loadedRatio || ratioFromResolution(photo?.resolution);
  const axis = slideAxis(ratio);
  const yNow = dragY ?? savedY;
  const token = typeof localStorage !== 'undefined' ? localStorage.getItem('token') : '';

  useEffect(() => {
    setRotation(0);
    setZoom(1);
    if (stageRef.current) stageRef.current.scrollTo({ top: 0, left: 0 });
  }, [currentIndex]);

  useEffect(() => {
    const handleKey = (e) => {
      // Don't hijack keys while the user is writing a note or renaming.
      const t = e.target;
      const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
      if (e.key === 'Escape') { onClose(); return; }
      if (typing) return;

      const prev = () => setCurrentIndex((i) => (i > 0 ? i - 1 : photos.length - 1));
      const next = () => setCurrentIndex((i) => (i < photos.length - 1 ? i + 1 : 0));
      if (e.key === 'ArrowLeft') { e.preventDefault(); prev(); }
      if (e.key === 'ArrowRight') { e.preventDefault(); next(); }
      if (e.key === 'Tab') { e.preventDefault(); e.shiftKey ? prev() : next(); }
      if (e.key === 'Home') { e.preventDefault(); setCurrentIndex(0); }
      if (e.key === 'End') { e.preventDefault(); setCurrentIndex(photos.length - 1); }
      if (e.key === '+' || e.key === '=') { e.preventDefault(); setZoom((z) => Math.min(z + 0.25, 4)); }
      if (e.key === '-') { e.preventDefault(); setZoom((z) => Math.max(z - 0.25, 1)); }
      if (e.key === '0') { e.preventDefault(); setZoom(1); setRotation(0); }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [photos.length, onClose]);

  useLayoutEffect(() => {
    const el = fitRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const measure = () => setFitBox({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (!photo) return null;

  const downloadPhoto = () => {
    const link = document.createElement('a');
    link.href = `/api/video-file/${photo.id}?token=${token}`;
    link.download = photo.filename;
    link.click();
  };

  const goPrev = () => setCurrentIndex((i) => (i > 0 ? i - 1 : photos.length - 1));
  const goNext = () => setCurrentIndex((i) => (i < photos.length - 1 ? i + 1 : 0));

  const toolBtn =
    'p-2 rounded-lg bg-white/5 hover:bg-white/15 text-white/80 hover:text-white transition border border-white/10';

  // Rotating 90/270 swaps the frame's effective axes. A CSS transform does not
  // affect layout, so the constraint has to be swapped by hand or a portrait
  // shot rotated upright spills straight out of a landscape stage.
  const quarterTurned = ((rotation / 90) % 2 + 2) % 2 === 1;
  const boxW = quarterTurned ? fitBox.h : fitBox.w;
  const boxH = quarterTurned ? fitBox.w : fitBox.h;
  const frameW = boxW && boxH ? Math.min(boxW, boxH * RATIO) : 0;

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-[#08080A]/98 backdrop-blur-sm">
      {/* header */}
      <div className="shrink-0 flex items-center justify-between gap-4 px-4 py-3">
        <div className="flex gap-2">
          <button onClick={() => setRotation((r) => r - 90)} className={toolBtn} title="Rotate left"><RotateCcw className="w-5 h-5" /></button>
          <button onClick={() => setRotation((r) => r + 90)} className={toolBtn} title="Rotate right"><RotateCw className="w-5 h-5" /></button>
          <button onClick={() => setZoom((z) => Math.min(z + 0.25, 4))} className={toolBtn} title="Zoom in (+)"><ZoomIn className="w-5 h-5" /></button>
          <button onClick={() => setZoom((z) => Math.max(z - 0.25, 1))} className={toolBtn} title="Zoom out (-)"><ZoomOut className="w-5 h-5" /></button>
          <button onClick={() => { setZoom(1); setRotation(0); }} className={toolBtn} title="Fit to screen (0)"><Maximize2 className="w-5 h-5" /></button>
          <button
            onClick={toggleFrame}
            className={toolBtn + (frame169 ? ' !border-accent/60 !bg-accent/20 !text-white' : '')}
            title={frame169 ? '16:9 view - click to see the whole photo' : 'Whole photo - click for the 16:9 view'}
          >
            <Crop className="w-5 h-5" />
          </button>
          <button onClick={downloadPhoto} className={toolBtn} title="Download"><Download className="w-5 h-5" /></button>
          {zoom !== 1 && (
            <span className="self-center ml-1 font-mono text-[11px] text-white/45">{Math.round(zoom * 100)}%</span>
          )}
        </div>
        <button onClick={onClose} className={toolBtn} title="Close (Esc)"><X className="w-5 h-5" /></button>
      </div>

      {/* stage - min-h-0 is what lets max-h-full actually bind */}
      <div className="relative flex-1 min-h-0 flex items-center justify-center">
        <button
          onClick={goPrev}
          className="absolute left-2 z-10 p-2 rounded-full bg-black/40 hover:bg-black/70 text-white/60 hover:text-white transition"
          title="Previous"
        >
          <ChevronLeft className="w-8 h-8" />
        </button>

        <div
          ref={stageRef}
          className={
            'h-full w-full px-16 py-2 ' +
            (zoom > 1 ? 'overflow-auto' : 'overflow-hidden')
          }
        >
          <div ref={fitRef} className="h-full w-full flex items-center justify-center">
            {frame169 ? (
              <div
                className={'group relative overflow-hidden rounded-lg bg-black shadow-2xl shadow-black/60 transition-transform duration-200 ' +
                  (axis ? (axis === 'y' ? 'cursor-ns-resize' : 'cursor-ew-resize') : '')}
                style={{
                  width: frameW ? `${frameW}px` : '100%',
                  height: frameW ? `${frameW / RATIO}px` : 'auto',
                  aspectRatio: '16 / 9',
                  touchAction: 'none',
                  transform: `rotate(${rotation}deg) scale(${zoom})`,
                  transformOrigin: 'center center',
                }}
                title={axis ? 'Drag to choose which part of the photo is shown' : 'Already 16:9'}
                onPointerDown={(e) => {
                  if (!axis || zoom !== 1 || rotation % 360 !== 0) return;
                  const box = e.currentTarget.getBoundingClientRect();
                  const shown = axis === 'y' ? box.width / ratio : box.height * ratio;      // photo size along the slide axis
                  const slack = shown - (axis === 'y' ? box.height : box.width);
                  if (slack < 2) return;
                  e.currentTarget.setPointerCapture?.(e.pointerId);
                  dragRef.current = { start: axis === 'y' ? e.clientY : e.clientX, y0: yNow, slack, moved: false };
                }}
                onPointerMove={(e) => {
                  const d = dragRef.current; if (!d) return;
                  const delta = (axis === 'y' ? e.clientY : e.clientX) - d.start;
                  if (Math.abs(delta) > 2) d.moved = true;
                  setDragY(Math.max(0, Math.min(1, d.y0 - delta / d.slack)));
                }}
                onPointerUp={(e) => {
                  const d = dragRef.current; dragRef.current = null;
                  e.currentTarget.releasePointerCapture?.(e.pointerId);
                  if (d && d.moved && dragY != null) saveCrops({ [photo.id]: dragY }).catch(() => {});
                }}
              >
                <img
                  key={photo.id}
                  src={`/api/photo-preview/${photo.id}?token=${token}`}
                  alt={photo.filename}
                  draggable={false}
                  onLoad={(e) => setLoadedRatio(e.currentTarget.naturalWidth / (e.currentTarget.naturalHeight || 1))}
                  className="h-full w-full select-none object-cover"
                  style={{ objectPosition: objectPosition(ratio, yNow) }}
                />
                {axis && (
                  <span className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full bg-black/55 px-3 py-1 text-[11px] text-white/70 opacity-0 transition-opacity duration-300 group-hover:opacity-100">
                    {axis === 'y' ? 'Drag up or down to reframe' : 'Drag left or right to reframe'}
                  </span>
                )}
              </div>
            ) : (
            <img
              key={photo.id}
              src={`/api/photo-preview/${photo.id}?token=${token}`}
              alt={photo.filename}
              draggable={false}
              onLoad={(e) => setLoadedRatio(e.currentTarget.naturalWidth / (e.currentTarget.naturalHeight || 1))}
              className="select-none transition-transform duration-200"
              style={{
                // Whole frame, never a crop: the natural aspect is preserved and
                // the longer side is what meets the edge of the stage.
                maxWidth: boxW ? `${boxW}px` : '100%',
                maxHeight: boxH ? `${boxH}px` : '100%',
                width: 'auto',
                height: 'auto',
                objectFit: 'contain',
                transform: `rotate(${rotation}deg) scale(${zoom})`,
                transformOrigin: 'center center',
              }}
            />
            )}
          </div>
        </div>

        <button
          onClick={goNext}
          className="absolute right-2 z-10 p-2 rounded-full bg-black/40 hover:bg-black/70 text-white/60 hover:text-white transition"
          title="Next"
        >
          <ChevronRight className="w-8 h-8" />
        </button>
      </div>

      {/* footer - one layer, so nothing here is buried under anything else */}
      <div className="shrink-0 border-t border-white/10 bg-black/60">
        <div className="flex flex-wrap items-start gap-x-6 gap-y-3 px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-sm font-medium text-white" title={photo.filename}>{photo.filename}</h2>
            <p className="mt-0.5 font-mono text-[11px] text-white/45">
              {currentIndex + 1} of {photos.length}
              {photo.file_size_formatted ? ` · ${photo.file_size_formatted}` : ''}
              {photo.uploaded_at ? ` · ${new Date(photo.uploaded_at).toLocaleDateString()}` : ''}
              <span className="ml-3 text-white/25">← → or Tab to move</span>
            </p>
          </div>

          <div className="flex items-center gap-3">
            <StatusBadge status={photo.status} onChange={(s) => onUpdateStatus?.(photo.id, s)} />
            <button
              onClick={() => setShowTagSelector(true)}
              className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-white/80 hover:bg-white/15 hover:text-white transition"
            >
              <Tag className="w-4 h-4" /> Tags
            </button>
          </div>

          <div className="w-72 max-h-32 overflow-y-auto">
            <NotesPanel videoId={photo.id} />
          </div>
        </div>

        {/* filmstrip */}
        <div className="flex gap-1.5 overflow-x-auto px-4 pb-3 snap-x">
          {photos.map((ph, i) => (
            <button
              key={ph.id}
              type="button"
              onClick={(e) => { e.stopPropagation(); setCurrentIndex(i); }}
              ref={(el) => { if (el && i === currentIndex) el.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' }); }}
              className={
                'relative h-12 w-20 shrink-0 overflow-hidden rounded snap-center transition ' +
                (i === currentIndex ? 'ring-2 ring-accent opacity-100' : 'opacity-45 hover:opacity-80')
              }
              title={ph.filename}
            >
              {ph.thumbnail_path
                ? <img src={ph.thumbnail_path} alt="" loading="lazy" className="h-full w-full object-cover" />
                : <span className="grid h-full w-full place-items-center bg-zinc-800 text-[9px] text-zinc-500">no preview</span>}
            </button>
          ))}
        </div>
      </div>

      {showTagSelector && <TagSelector video={photo} onClose={() => setShowTagSelector(false)} />}
    </div>
  );
}
