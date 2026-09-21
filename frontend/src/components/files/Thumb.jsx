import { useState } from 'react';
import { cn } from '../../lib/utils';
import { KindTile } from './FileIcon';

/** A picture for images/videos (made by the server, cached), a coloured tile for everything else. */
export default function Thumb({ entry, src, className, iconClass }) {
  const [state, setState] = useState(entry.thumb ? 'loading' : 'none');
  if (state === 'none' || state === 'failed' || !src) {
    return <KindTile kind={entry.kind} ext={entry.ext} className={className} iconClass={iconClass} />;
  }
  return (
    <div className={cn('relative overflow-hidden bg-zinc-900', className)}>
      {state === 'loading' && <div className="absolute inset-0 animate-pulse bg-zinc-800/60" />}
      <img src={src} alt="" loading="lazy" decoding="async" draggable={false}
           onLoad={() => setState('ready')} onError={() => setState('failed')}
           className={cn('h-full w-full object-cover transition-opacity duration-300', state === 'ready' ? 'opacity-100' : 'opacity-0')} />
      {entry.kind === 'video' && state === 'ready' && (
        <span className="absolute bottom-2 right-2 rounded bg-zinc-950/70 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-zinc-300">{entry.ext}</span>
      )}
    </div>
  );
}
