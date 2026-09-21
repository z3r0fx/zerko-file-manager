import { Trash2, Undo2, Link2, Copy, ExternalLink, Lock, Clock, Eye, Download, FolderOpen, Loader2 } from 'lucide-react';
import { cn } from '../../lib/utils';
import { formatBytes, formatWhen, parentOf } from '../../lib/files';
import { KindGlyph } from './FileIcon';
import { Button } from './Dialogs';

export function Empty({ icon: Icon, title, hint, children }) {
  return (
    <div className="grid place-items-center px-6 py-20 text-center">
      <div className="max-w-sm">
        <div className="mx-auto grid h-16 w-16 place-items-center rounded-2xl border border-zinc-800 bg-zinc-900">
          <Icon className="h-7 w-7 text-zinc-500" strokeWidth={1.5} />
        </div>
        <p className="mt-5 text-base font-medium text-zinc-200">{title}</p>
        {hint && <p className="mt-1.5 text-sm leading-relaxed text-zinc-500">{hint}</p>}
        {children && <div className="mt-5 flex justify-center gap-2">{children}</div>}
      </div>
    </div>
  );
}

export function Loading() {
  return (
    <div className="space-y-2 py-2">
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="flex items-center gap-4 px-3 py-2">
          <div className="h-7 w-7 animate-pulse rounded-lg bg-zinc-800/70" />
          <div className="h-3.5 animate-pulse rounded bg-zinc-800/70" style={{ width: `${30 + ((i * 37) % 40)}%` }} />
        </div>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ trash
export function TrashView({ data, canPurge, onRestore, onPurge, onEmpty }) {
  if (!data) return <Loading />;
  if (!data.items.length) {
    return <Empty icon={Trash2} title="Trash is empty" hint={`Things you delete wait here for ${data.days} days, so a slip of the finger is never a disaster.`} />;
  }
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
        <p className="text-sm text-zinc-400">
          {data.items.length} item{data.items.length === 1 ? '' : 's'} - {formatBytes(data.total_size)}. Anything here is deleted for good after {data.days} days.
        </p>
        {canPurge && <Button variant="outline" onClick={onEmpty} className="!text-red-400"><Trash2 className="h-4 w-4" /> Empty trash</Button>}
      </div>
      <div className="divide-y divide-zinc-900 rounded-xl border border-zinc-800/80">
        {data.items.map((t) => (
          <div key={t.id} className="group flex items-center gap-4 px-4 py-3">
            <KindGlyph kind={t.kind} className="h-6 w-6 shrink-0" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm text-zinc-100">{t.name}</p>
              <p className="truncate text-xs text-zinc-500">
                From {parentOf(t.orig_path) ? parentOf(t.orig_path).split('/').join(' / ') : 'All files'} - deleted {formatWhen(t.deleted_at)}{t.deleted_by ? ` by ${t.deleted_by}` : ''}
              </p>
            </div>
            <span className="hidden text-xs tabular-nums text-zinc-500 sm:block">{t.is_dir ? '' : formatBytes(t.size)}</span>
            <div className="flex items-center gap-1">
              <Button variant="outline" onClick={() => onRestore([t.id])} className="!px-3 !py-1.5 text-[13px]"><Undo2 className="h-3.5 w-3.5" /> Restore</Button>
              {canPurge && (
                <button onClick={() => onPurge(t)} aria-label="Delete forever" title="Delete forever"
                        className="rounded-lg p-2 text-zinc-500 hover:bg-red-500/10 hover:text-red-400"><Trash2 className="h-4 w-4" /></button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ share links
export const fullLink = (s) => `${window.location.origin}${s.url_path}`;

export function LinksView({ data, onCopy, onRevoke, onReveal, busyId }) {
  if (!data) return <Loading />;
  if (!data.length) {
    return <Empty icon={Link2} title="No shared links yet"
                  hint="Right-click any file or folder and choose Share to make a link you can send to a friend. They will not need an account." />;
  }
  return (
    <div className="divide-y divide-zinc-900 rounded-xl border border-zinc-800/80">
      {data.map((s) => {
        const dead = s.expired || s.revoked;
        return (
          <div key={s.id} className={cn('flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3.5', dead && 'opacity-55')}>
            <KindGlyph kind={s.is_dir ? 'folder' : 'other'} className="h-6 w-6 shrink-0" />
            <div className="min-w-0 flex-1 basis-56">
              <p className="truncate text-sm font-medium text-zinc-100">{s.name}</p>
              <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-zinc-500">
                {s.has_password && <span className="inline-flex items-center gap-1"><Lock className="h-3 w-3" /> Password</span>}
                <span className="inline-flex items-center gap-1"><Clock className="h-3 w-3" /> {s.expired ? 'Expired' : s.expires_at ? `Expires ${formatWhen(s.expires_at)}` : 'Never expires'}</span>
                <span className="inline-flex items-center gap-1"><Eye className="h-3 w-3" /> {s.views}</span>
                <span className="inline-flex items-center gap-1"><Download className="h-3 w-3" /> {s.downloads}</span>
                <span>by {s.created_by}</span>
              </p>
            </div>
            <div className="flex items-center gap-1">
              {!dead && (
                <>
                  <Button variant="outline" onClick={() => onCopy(s)} className="!px-3 !py-1.5 text-[13px]"><Copy className="h-3.5 w-3.5" /> Copy link</Button>
                  <a href={s.url_path} target="_blank" rel="noreferrer" title="Open" aria-label="Open link"
                     className="rounded-lg p-2 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"><ExternalLink className="h-4 w-4" /></a>
                </>
              )}
              <button onClick={() => onReveal(s)} title="Show in folder" aria-label="Show in folder"
                      className="rounded-lg p-2 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"><FolderOpen className="h-4 w-4" /></button>
              {!s.revoked && (
                <button onClick={() => onRevoke(s)} disabled={busyId === s.id}
                        className="rounded-lg px-2.5 py-1.5 text-[13px] text-red-400 hover:bg-red-500/10 disabled:opacity-50">
                  {busyId === s.id ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Turn off'}
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
