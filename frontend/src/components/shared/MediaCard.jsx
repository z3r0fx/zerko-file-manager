import { useState, useContext, useRef } from 'react';
import { cn } from '../../lib/utils';
import { DataContext } from '../../context/DataContext';
import StatusBadge from './StatusBadge';
import AudioPlayer from './AudioPlayer';
import { API_BASE } from '../../lib/api';
import TiltCard from './TiltCard';

export default function MediaCard({ media, onClick, onContextMenu, onStatusChange, isSelected, onToggleSelect, onRangeSelect, totalSelectedCount = 0, searchTerm = '' }) {
  const [imageError, setImageError] = useState(false);
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

  const handleClick = (e) => {
    e.stopPropagation();
    if (isAudio && togglePlayRef.current) {
        togglePlayRef.current();
        return;
    }
    if (e.ctrlKey || e.metaKey) {
      onToggleSelect?.(media);
    } else if (e.shiftKey) {
      onRangeSelect?.(media);
    } else {
      onClick?.(media);
    }
  };

  const isPhoto = media.media_type === 'photo';
  const isAudio = media.media_type === 'audio';
  const isVideo = media.media_type === 'video';

  const thumbnailUrl = media.thumbnail_path
    ? (media.thumbnail_path.startsWith('http') ? media.thumbnail_path : `${API_BASE}${media.thumbnail_path}`)
    : null;

  const handleDragStart = (e) => {
    // Any card can be dragged. Dragging one that's part of a selection moves
    // the whole selection; dragging an unselected card moves just that one.
    const ids = isSelected && totalSelectedCount > 1 ? null : [media.id];
    e.dataTransfer.setData('mediaId', media.id);
    if (ids) e.dataTransfer.setData('mediaIds', JSON.stringify(ids));
    e.dataTransfer.effectAllowed = 'move';
    
    if (totalSelectedCount > 1) {
        const dragPreview = document.createElement('div');
        dragPreview.className = 'fixed top-0 left-0 bg-red-600 text-white px-2 py-1 rounded text-xs font-bold z-[9999] pointer-events-none';
        dragPreview.innerText = `Moving ${totalSelectedCount} items`;
        document.body.appendChild(dragPreview);
        e.dataTransfer.setDragImage(dragPreview, 0, 0);
        setTimeout(() => document.body.removeChild(dragPreview), 0);
    }
  };

  return (
    <TiltCard
      data-id={media.id}
      disabled={isAudio}
      className={cn(
        'bg-zinc-900 rounded-lg border',
        'group relative cursor-pointer',
        // outline, not ring: rings are box-shadows and the tilt effect writes
        // box-shadow inline. outline also costs no layout space.
        isSelected
          ? 'border-[#ff5c1f] outline outline-2 -outline-offset-2 outline-[#ff5c1f]'
          : 'border-zinc-800/60 hover:border-[#ff5c1f]/60',
        isPhoto ? 'aspect-square' : 'aspect-video'
      )}
      draggable
      onDragStart={handleDragStart}
      onClick={handleClick}
      onContextMenu={handleContextMenu}
    >
      {/* Selection state: visible without hovering, and on top of everything */}
      {isSelected && (
        <>
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 z-20 rounded-lg bg-[#ff5c1f]/15"
          />
          <div className="pointer-events-none absolute right-2 top-2 z-30 flex h-6 w-6 items-center justify-center rounded-full bg-[#ff5c1f] shadow-lg">
            <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="#0b0b0d" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 12l5 5L20 7" />
            </svg>
          </div>
        </>
      )}
      {/* Thumbnail */}
      {isPhoto && thumbnailUrl && !imageError ? (
        /* Stills show the whole frame - a contact sheet you can trust. The tile
           stays square so the grid lines up; the photo is letterboxed inside it
           rather than centre-cropped, and hover brightens instead of scaling
           (scaling a contained image would crop it back again on hover). */
        <div className="w-full h-full overflow-hidden rounded-lg relative bg-black/50 transition duration-300 group-hover:brightness-110">
           <img
            src={thumbnailUrl}
            alt={media.filename}
            className="w-full h-full object-contain"
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
        <div className="w-full h-full overflow-hidden rounded-lg relative transition-transform duration-300 group-hover:scale-105 group-hover:brightness-110">
          <img
            src={thumbnailUrl}
            alt={media.filename}
            className="w-full h-full object-cover"
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
        <div className="p-4 flex flex-col h-full justify-between relative">
            <h3 className="text-sm font-medium text-zinc-100 truncate">{media.filename}</h3>
            <div className="absolute top-2 left-2 flex gap-0.5 bg-black/50 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity">
                {[1, 2, 3, 4, 5].map(r => (
                    <button type="button" key={r} onClick={(e) => { e.stopPropagation(); e.preventDefault(); handleRating(e, r); }} className={cn("text-xs transition-transform hover:scale-150 active:scale-75", r <= (media.rating || 0) ? "text-yellow-500" : "text-zinc-600")}>★</button>
                ))}
            </div>
            <AudioPlayer audio={media} volume={0.5} onTogglePlay={(fn) => togglePlayRef.current = fn} />
        </div>
      ) : (
        /* Default card */
        <div className="w-full h-full bg-zinc-800 flex items-center justify-center relative">
            <h3 className="text-sm text-zinc-400 truncate px-2">{media.filename}</h3>
             <div className="absolute top-2 left-2 flex gap-0.5 bg-black/50 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity">
                {[1, 2, 3, 4, 5].map(r => (
                    <button type="button" key={r} onClick={(e) => { e.stopPropagation(); e.preventDefault(); handleRating(e, r); }} className={cn("text-xs transition-transform hover:scale-150 active:scale-75", r <= (media.rating || 0) ? "text-yellow-500" : "text-zinc-600")}>★</button>
                ))}
            </div>
        </div>
      )}

      {/* Info Section (bottom overlay for photos) */}
      <div className={cn("p-3", isPhoto ? "absolute bottom-0 inset-x-0 bg-black/60 rounded-b-lg" : "")}>
        {!isPhoto && <h3 className="text-sm font-medium text-zinc-100 truncate mb-1">{media.filename}</h3>}
        
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
