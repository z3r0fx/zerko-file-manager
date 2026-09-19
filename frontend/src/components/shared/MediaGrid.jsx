import { useState, useEffect, useRef } from 'react';
import MediaCard from './MediaCard';
import VideoTable from './VideoTable';
import Skeleton from './Skeleton';
import { cn } from '../../lib/utils';

export default function MediaGrid({ mediaItems = [], viewMode = 'grid', gridSize = 'medium', loading, onVideoClick, onContextMenu, onStatusChange, onTagClick, onMove, onDelete, selectedVideoIds = new Set(), onToggleSelect, onRangeSelect, totalSelectedCount = 0, searchTerm = '' }) {
  // Render a screenful at a time. Mounting 1,200 cards at once pinned the main
  // thread hard enough to make the cursor itself lag.
  const PAGE = 60;
  const [shown, setShown] = useState(PAGE);
  const sentinelRef = useRef(null);

  // mediaItems is rebuilt on EVERY render of BrowsePage (it is filtered
  // inline), so depending on the array itself re-ran this on every render and
  // slammed `shown` back to 60 - which is what yanked you to the top and made
  // the bottom rows flicker. The stats poll every 5s guaranteed it kept
  // happening. Depend on a signature of the list instead of its identity.
  const listSignature = `${viewMode}|${gridSize}|${mediaItems.length}|${mediaItems[0]?.id ?? ''}|${mediaItems[mediaItems.length - 1]?.id ?? ''}`;
  useEffect(() => { setShown(PAGE); }, [listSignature]);

  // Grow the window as the sentinel comes into view.
  //
  // The previous version listed `shown` as a dependency, so every growth tore
  // the observer down and built a new one - and a fresh IntersectionObserver
  // fires immediately for whatever is already on screen. That re-triggered
  // instantly, cascading 60 -> 120 -> 180 -> ... in one burst until the entire
  // library was mounted, which is what flung the page to the bottom and then
  // ground to a halt. The observer is now created ONCE and reads live values
  // through refs, with a cooldown so one pass can only add one page.
  const totalRef = useRef(mediaItems.length);
  const busy = useRef(false);
  totalRef.current = mediaItems.length;

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return undefined;

    const io = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      if (busy.current) return;
      busy.current = true;
      setShown((n) => Math.min(n + PAGE, totalRef.current));
      // let the new rows lay out (and push the sentinel away) before we listen
      // again, otherwise we just re-trigger on the same intersection
      window.setTimeout(() => { busy.current = false; }, 400);
    }, { rootMargin: '250px' });

    io.observe(node);
    return () => io.disconnect();
  }, [listSignature]);


  if (loading) {
    return (
      <div className={cn("grid gap-4", viewMode === 'grid' ? getGridClasses(gridSize) : 'grid-cols-1')}>
        {[...Array(12)].map((_, i) => (
            <Skeleton key={i} className="aspect-video" />
        ))}
      </div>
    );
  }

  function getGridClasses(size) {
    switch(size) {
        case 'small': return 'grid-cols-4 sm:grid-cols-6 md:grid-cols-8 lg:grid-cols-10';
        case 'large': return 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3';
        default: return 'grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6';
    }
  }

  if (viewMode === 'list') {
      return (
        <VideoTable 
            videos={mediaItems} 
            onVideoClick={onVideoClick} 
            onStatusChange={onStatusChange} 
            selectedVideoIds={selectedVideoIds} 
            onToggleSelect={onToggleSelect} 
            onRangeSelect={onRangeSelect}
        />
      );
  }

  return (
    <>
    <div className={cn("grid gap-4", getGridClasses(gridSize))}>
      {mediaItems.slice(0, shown).map((media, index) => (
        <div
            key={media.id}
            className={index < 24 ? 'animate-stagger-fade-in' : undefined}
            style={{
              // Only the first screenful animates in - staggering 1,200 cards
              // meant 1,200 simultaneous animations.
              ...(index < 24 ? { '--delay': `${index * 0.04}s` } : null),
              // NOTE: content-visibility was removed here. Its guessed
              // intrinsic size did not match the real card, so rows kept
              // resizing as they entered and left view and the browser's
              // scroll anchoring yanked the viewport around. Windowing plus
              // lazy images already does the heavy lifting.
            }}
        >
            <MediaCard
            media={media}
            onClick={onVideoClick}
            onContextMenu={onContextMenu}
            onStatusChange={onStatusChange}
            onTagClick={onTagClick}
            onMove={onMove}
            onDelete={onDelete}
            isSelected={selectedVideoIds.has(media.id)}
            onToggleSelect={onToggleSelect}
            onRangeSelect={onRangeSelect}
            totalSelectedCount={totalSelectedCount}
            searchTerm={searchTerm}
            />
        </div>
      ))}
    </div>
    {shown < mediaItems.length && (
      <div ref={sentinelRef} className="h-16 flex items-center justify-center text-xs text-zinc-600">
        Loading more… ({shown} of {mediaItems.length})
      </div>
    )}
    </>
  );
}
