import { useEffect, useState, useSyncExternalStore } from 'react';
import { ChevronDown, ChevronUp, X, Pause, Play, RotateCw, Check, AlertCircle, Loader2, UploadCloud } from 'lucide-react';
import { cn } from '../../lib/utils';
import { uploads, summarize, formatBytes, formatRate, formatEta } from '../../lib/files';
import { KindGlyph } from './FileIcon';

const subscribe = (fn) => uploads.subscribe(fn);
const snap = () => uploads.getSnapshot();

export function useUploads() {
  return useSyncExternalStore(subscribe, snap, snap);
}

function Row({ it }) {
  const pct = it.size ? Math.min(100, (it.sent / it.size) * 100) : it.status === 'done' ? 100 : 0;
  const label = it.relative || it.name;
  return (
    <div className="group flex items-center gap-3 px-4 py-2.5">
      <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-zinc-800/80">
        {it.status === 'done' ? <Check className="h-4 w-4 text-emerald-400" />
          : it.status === 'error' ? <AlertCircle className="h-4 w-4 text-red-400" />
          : <KindGlyph kind="other" className="h-4 w-4" />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className="truncate text-sm text-zinc-100" title={label}>{label}</p>
          <span className="shrink-0 font-mono text-[11px] text-zinc-500">
            {it.status === 'uploading' ? formatRate(it.speed) : ''}
          </span>
        </div>
        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-zinc-800">
          <div className={cn('h-full rounded-full transition-[width] duration-300',
                 it.status === 'error' ? 'bg-red-500' : it.status === 'done' ? 'bg-emerald-500' : it.status === 'paused' ? 'bg-zinc-500' : 'bg-accent')}
               style={{ width: `${pct}%` }} />
        </div>
        <p className={cn('mt-1 truncate text-[11px]', it.status === 'error' ? 'text-red-400' : 'text-zinc-500')}>
          {it.status === 'error' ? it.error
            : it.note ? it.note
            : it.status === 'queued' ? 'Waiting...'
            : it.status === 'paused' ? `Paused - ${formatBytes(it.sent)} of ${formatBytes(it.size)}`
            : it.status === 'done' ? `${formatBytes(it.size)} - uploaded`
            : it.status === 'cancelled' ? 'Cancelled'
            : `${formatBytes(it.sent)} of ${formatBytes(it.size)}`}
        </p>
      </div>
      <div className="flex shrink-0 items-center opacity-0 transition group-hover:opacity-100 focus-within:opacity-100">
        {it.status === 'uploading' && <IconBtn label="Pause" onClick={() => uploads.pause(it.id)}><Pause className="h-3.5 w-3.5" /></IconBtn>}
        {(it.status === 'paused' || it.status === 'error') && (
          <IconBtn label={it.status === 'error' ? 'Retry' : 'Resume'} onClick={() => uploads.resume(it.id)}>
            {it.status === 'error' ? <RotateCw className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
          </IconBtn>
        )}
        {['queued', 'uploading', 'paused', 'error'].includes(it.status) && (
          <IconBtn label="Cancel" onClick={() => uploads.cancel(it.id)}><X className="h-3.5 w-3.5" /></IconBtn>
        )}
      </div>
    </div>
  );
}

function IconBtn({ label, onClick, children }) {
  return (
    <button onClick={onClick} aria-label={label} title={label}
            className="rounded-md p-1.5 text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100">
      {children}
    </button>
  );
}

export default function UploadTray() {
  const items = useUploads();
  const [collapsed, setCollapsed] = useState(false);
  const [dismissed, setDismissed] = useState(0);
  const s = summarize(items);
  const visible = items.filter((i) => i.status !== 'cancelled');

  // A new batch after you closed the tray brings it back.
  useEffect(() => { if (s.working) setDismissed(0); }, [s.working, items.length]);

  if (!visible.length || dismissed === items.length) return null;

  const finished = !s.working && s.active === 0;
  const anyPaused = items.some((i) => i.status === 'paused');
  const title = finished
    ? `${s.done} upload${s.done === 1 ? '' : 's'} complete`
    : anyPaused && !s.working ? `Paused - ${s.active} left`
    : `Uploading ${s.active} item${s.active === 1 ? '' : 's'}`;
  const sub = finished ? formatBytes(s.size)
    : [`${s.pct}%`, formatRate(s.speed), formatEta(s.eta)].filter(Boolean).join(' - ');

  return (
    <div className="fixed bottom-4 right-4 z-[70] w-[min(24rem,calc(100vw-2rem))] overflow-hidden rounded-2xl border border-zinc-700/70 bg-zinc-900/95 shadow-2xl shadow-black/60 backdrop-blur">
      <div className="flex items-center gap-3 border-b border-zinc-800 px-4 py-3">
        <div className={cn('grid h-8 w-8 place-items-center rounded-lg', finished ? 'bg-emerald-500/15' : 'bg-accent/15')}>
          {finished ? <Check className="h-4 w-4 text-emerald-400" />
            : s.working ? <Loader2 className="h-4 w-4 animate-spin text-accent" /> : <UploadCloud className="h-4 w-4 text-accent" />}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-zinc-100">{title}</p>
          <p className="truncate text-[11px] text-zinc-500">{sub}</p>
        </div>
        {!finished && (s.working
          ? <IconBtn label="Pause all" onClick={() => uploads.pauseAll()}><Pause className="h-4 w-4" /></IconBtn>
          : <IconBtn label="Resume all" onClick={() => uploads.resumeAll()}><Play className="h-4 w-4" /></IconBtn>)}
        <IconBtn label={collapsed ? 'Expand' : 'Collapse'} onClick={() => setCollapsed((c) => !c)}>
          {collapsed ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </IconBtn>
        <IconBtn label={finished ? 'Close' : 'Cancel all'}
                 onClick={() => { if (finished) { uploads.clearFinished(); setDismissed(items.length); } else uploads.cancelAll(); }}>
          <X className="h-4 w-4" />
        </IconBtn>
      </div>
      {!finished && (
        <div className="h-0.5 bg-zinc-800"><div className="h-full bg-accent transition-[width] duration-300" style={{ width: `${s.pct}%` }} /></div>
      )}
      {!collapsed && (
        <div className="max-h-72 divide-y divide-zinc-800/60 overflow-y-auto">
          {visible.slice().reverse().map((it) => <Row key={it.id} it={it} />)}
        </div>
      )}
    </div>
  );
}
