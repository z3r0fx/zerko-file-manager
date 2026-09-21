import { useState, useContext, useRef } from 'react';
import { cn } from '../../lib/utils';
import { DataContext } from '../../context/DataContext';
import StatusBadge from './StatusBadge';
import AudioPlayer from './AudioPlayer';
import { API_BASE } from '../../lib/api';
import TiltCard from './TiltCard';
import { useAppearance } from '../../context/AppearanceContext';
import { startMediaDrag } from '../../lib/mediaDrag';
import { useCrop } from '../../lib/photoCrops';
import { objectPosition, ratioFromResolution } from '../../lib/framing';

export default function MediaCard({ media, onClick, onContextMenu, onStatusChange, isSelected, onToggleSelect, onRangeSelect, onSelectOnly, selectedIds, totalSelectedCount = 0, searchTerm = '' }) {
  const { clickAction, thumbFit } = useAppearance();
  const fit = thumbFit === 'contain' ? 'object-contain' : 'object-cover';
  const [imageError, setImageError] = useState(false);
  // a finger uses press-and-hold for the menu, so tiles are not HTML5-draggable there
  const coarsePointer = typeof window !== 'undefined' && !!window.matchMedia?.('(pointer: coarse)').matches;
  const [loadedRatio, setLoadedRatio] = useState(null);
  // where this photo sits in its 16:9 tile (saved from the export dialog / viewer)
  const cropY = useCrop(media.media_type === 'photo' ? media.id : null);
  const togglePlayRef = useRef(null);
  const { updateVideoMetadata } = useContext(DataContext);

  const getTranscriptSnippet = () => {
    // 1. Prioritize search_matches (from transcript-search API)
    if (media.search_matches && media.search_matches.length > 0) {
        return media.search_matches[0].text;
    }
    // 2. Fallback to local transcription string search
    if (!searchTerm || !media.transcription) return null;
    const index = media.transcription.toLowerCase().indexOf(searchTerm.toLowerCase());
    if (index === -1) return null;

    const start = Math.max(0, index - 40);
    const end = Math.min(media.transcription.length, index + searchTerm.length + 60);
    let snippet = media.transcription.substring(start, end);
    
    if (start > 0) snippet = '...' + snippet;
    if (end < media.transcription.length) snippet = snippet + '...';

    return snippet;
  };

  const transcriptSnippet = getTranscriptSnippet();

  const handleContextMenu = (e) => {
    e.preventDefault();
    e.stopPropagation();
    onContextMenu?.(e, media);
  };

  const handleRating = async (e, rating) => {
    e.stopPropagation();
    await updateVideoMetadata(media.id, { rating });
  };

  // Shift = everything between the last one you clicked and this one.
  // Ctrl/Cmd = add or remove just this one. A plain click either selects
  // (double-click opens, like a file browser) or opens straight away,
  // depending on the "Clicking a video" setting. Touch screens have no
  // double-click, so a tap opens - or, once something is selected, toggles.
  const handleClick = (e) => {
    e.stopPropagation();
    if (e.shiftKey) { onRangeSelect?.(media, e.ctrlKey || e.metaKey); return; }
    if (e.ctrlKey || e.metaKey) { onToggleSelect?.(media); return; }
    const coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
    if (coarse && totalSelectedCount > 0) { onToggleSelect?.(media); return; }
    if (isAudio && togglePlayRef.current) { togglePlayRef.current(); return; }
    if (clickAction === 'select' && !coarse && onSelectOnly) { onSelectOnly(media); return; }
    onClick?.(media);
  };

  const handleDoubleClick = (e) => {
    if (clickAction !== 'select' || isAudio || e.shiftKey || e.ctrlKey || e.metaKey) return;
    e.stopPropagation();
    onClick?.(media);
  };

  // Touch: press the dot and drag across tiles to select them all (BrowsePage
  // does the sweeping). The pointer-up click that follows must not toggle again.
  const swept = useRef(false);
  const handleCheckPointerDown = (e) => {
    e.stopPropagation();
    if (e.pointerType === 'mouse') return;
    swept.current = true;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    window.dispatchEvent(new CustomEvent('zerko-sweep-select', { detail: { phase: 'start', id: media.id } }));
  };
  const handleCheckPointerMove = (e) => {
    if (!swept.current) return;
    const el = document.elementFromPoint(e.clientX, e.clientY)?.closest?.('[data-id]');
    if (el) window.dispatchEvent(new CustomEvent('zerko-sweep-select', { detail: { phase: 'move', id: Number(el.getAttribute('data-id')) } }));
  };
  const handleCheckPointerUp = (e) => {
    if (swept.current) e.currentTarget.releasePointerCapture?.(e.pointerId);
    setTimeout(() => { swept.current = false; }, 0);
  };

  const handleCheck = (e) => {
    e.stopPropagation();
    e.preventDefault();
    if (swept.current) return;
    if (e.shiftKey) onRangeSelect?.(media, e.ctrlKey || e.metaKey);
    else onToggleSelect?.(media);
  };

  const isPhoto = media.media_type === 'photo';
  const isAudio = media.media_type === 'audio';
  const isVideo = media.media_type === 'video';

  const thumbnailUrl = media.thumbnail_path
    ? (media.thumbnail_path.startsWith('http') ? media.thumbnail_path : `${API_BASE}${media.thumbnail_path}`)
    : null;

  const handleDragStart = (e) => {
    // Dragging a card that is part of a selection moves the whole selection;
    // dragging an unselected card moves just that one.
    const ids = isSelected && selectedIds && selectedIds.size > 1 ? [...selectedIds] : [media.id];
    startMediaDrag(e, ids, media.id);
  };

  return (
    <TiltCard
      data-id={media.id}
      disabled={isAudio}
      restScale={isSelected ? 1.03 : 1}
      className={cn(
        'bg-zinc-900 rounded-lg border',
        'group relative flex flex-col cursor-pointer hover:z-10',
        isSelected && 'z-[5]',
        // outline, not ring: rings are box-shadows and the tilt effect writes
        // box-shadow inline. outline also costs no layout space.
        isSelected
          ? 'border-accent outline outline-2 -outline-offset-2 outline-accent'
          : 'border-zinc-800/60 hover:border-accent/60',
        ''
      )}
      draggable={!coarsePointer}
      onDragStart={handleDragStart}
      onMouseDown={(e) => { if (e.shiftKey) e.preventDefault(); }}
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      onContextMenu={handleContextMenu}
    >
      {/* Selection state: visible without hovering, and on top of everything */}
      {isSelected && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 z-20 rounded-lg bg-accent/15"
        />
      )}
      <button
        type="button"
        aria-label={isSelected ? 'Deselect' : 'Select'}
        aria-pressed={isSelected}
        data-no-longpress
        onClick={handleCheck}
        onPointerDown={handleCheckPointerDown}
        onPointerMove={handleCheckPointerMove}
        onPointerUp={handleCheckPointerUp}
        onPointerCancel={handleCheckPointerUp}
        onDoubleClick={(e) => e.stopPropagation()}
        className={cn(
          'absolute right-2 top-2 z-30 flex h-6 w-6 touch-none items-center justify-center rounded-full border transition active:scale-90',
          isSelected
            ? 'border-accent bg-accent text-accent-foreground shadow-lg'
            : 'border-white/70 bg-black/45 text-transparent hover:border-white hover:bg-black/60 hover:text-white/80',
          isSelected || totalSelectedCount > 0 ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 max-md:opacity-100'
        )}
      >
        <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 12l5 5L20 7" />
        </svg>
      </button>
      {/* Picture: always a 16:9 frame, so every tile in the grid lines up */}
      <div className="relative aspect-video w-full shrink-0 overflow-hidden rounded-t-[inherit] bg-zinc-950">
      {isPhoto && thumbnailUrl && !imageError ? (
        /* Stills fill the 16:9 tile like everything else. The framing you chose
           (drag it in Generate proxy, or in the viewer) decides which part of a
           4:3 photo the tile shows; centred until you say otherwise. */
        <div className="absolute inset-0 overflow-hidden bg-zinc-950 transition duration-300 group-hover:brightness-110">
           <img
            src={thumbnailUrl}
            alt={media.filename}
            className={`h-full w-full ${fit}`}
            style={{ objectPosition: objectPosition(loadedRatio || ratioFromResolution(media.resolution), cropY) }}
            onLoad={(e) => setLoadedRatio(e.currentTarget.naturalWidth / (e.currentTarget.naturalHeight || 1))}
            loading="lazy"
            decoding="async"
            onError={() => setImageError(true)}
          />
           <div className="absolute top-2 left-2 flex gap-0.5 bg-black/50 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity">
                {[1, 2, 3, 4, 5].map(r => (
                    <button type="button" key={r} onClick={(e) => { e.stopPropagation(); e.preventDefault(); handleRating(e, r); }} className={cn("text-xs transition-transform hover:scale-150 active:scale-75", r <= (media.rating || 0) ? "text-yellow-500" : "text-zinc-600")}>★</button>
                ))}
            </div>
        </div>
      ) : isVideo && thumbnailUrl && !imageError ? (
        <div className="absolute inset-0 overflow-hidden transition duration-300 group-hover:brightness-110">
          <img
            src={thumbnailUrl}
            alt={media.filename}
            className={`h-full w-full ${fit}`}
            loading="lazy"
            decoding="async"
            onError={() => setImageError(true)}
          />
          <div className="absolute top-2 left-2 flex gap-0.5 bg-black/50 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity">
                {[1, 2, 3, 4, 5].map(r => (
                    <button type="button" key={r} onClick={(e) => { e.stopPropagation(); e.preventDefault(); handleRating(e, r); }} className={cn("text-xs transition-transform hover:scale-150 active:scale-75", r <= (media.rating || 0) ? "text-yellow-500" : "text-zinc-600")}>★</button>
                ))}
            </div>
        </div>
      ) : isAudio ? (
        <>
          <AudioPlayer audio={media} volume={0.5} onTogglePlay={(fn) => { togglePlayRef.current = fn; }} />
          <div className="absolute top-2 left-2 z-10 flex gap-0.5 rounded bg-black/50 p-1 opacity-0 transition-opacity group-hover:opacity-100">
            {[1, 2, 3, 4, 5].map(r => (
              <button type="button" key={r} onClick={(e) => { e.stopPropagation(); e.preventDefault(); handleRating(e, r); }} className={cn("text-xs transition-transform hover:scale-150 active:scale-75", r <= (media.rating || 0) ? "text-yellow-500" : "text-zinc-600")}>★</button>
            ))}
          </div>
        </>
      ) : (
        /* Default card */
        <div className="absolute inset-0 flex items-center justify-center bg-zinc-800">
            <h3 className="text-sm text-zinc-400 truncate px-2">{media.filename}</h3>
             <div className="absolute top-2 left-2 flex gap-0.5 bg-black/50 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity">
                {[1, 2, 3, 4, 5].map(r => (
                    <button type="button" key={r} onClick={(e) => { e.stopPropagation(); e.preventDefault(); handleRating(e, r); }} className={cn("text-xs transition-transform hover:scale-150 active:scale-75", r <= (media.rating || 0) ? "text-yellow-500" : "text-zinc-600")}>★</button>
                ))}
            </div>
        </div>
      )}

      </div>

      {/* Details */}
      <div className="p-3">
        <h3 className="mb-1 truncate text-sm font-medium text-zinc-100" title={media.filename}>{media.filename}</h3>
        
        {transcriptSnippet && (
            <p className="text-[10px] text-zinc-400 line-clamp-2 italic mb-2 leading-tight">
                {transcriptSnippet.split(new RegExp(`(${searchTerm})`, 'gi')).map((part, i) => 
                    part.toLowerCase() === searchTerm.toLowerCase() 
                        ? <span key={i} className="bg-yellow-500/30 text-yellow-200 rounded-sm px-0.5">{part}</span> 
                        : part
                )}
            </p>
        )}

        <div className="flex items-center justify-between">
            <StatusBadge status={media.status} onChange={(s) => onStatusChange?.(media.id, s)} />
            <span className="text-xs text-zinc-500">{media.file_size_formatted}</span>
        </div>
      </div>
    </TiltCard>
  );
}
