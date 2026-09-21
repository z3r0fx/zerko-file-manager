import { useEffect, useState } from 'react';
import { X, Download, Share2, Pencil, Star, Trash2, Link2, FolderOpen, Loader2, Eye } from 'lucide-react';
import { cn } from '../../lib/utils';
import {
  filesApi, formatBytes, formatWhen, kindLabel, thumbUrl, parentOf,
} from '../../lib/files';
import Thumb from './Thumb';
import { KindGlyph } from './FileIcon';

function Row({ label, children }) {
  if (children === '' || children == null || children === false) return null;
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5 text-[13px]">
      <dt className="shrink-0 text-zinc-500">{label}</dt>
      <dd className="min-w-0 break-words text-right text-zinc-200">{children}</dd>
    </div>
  );
}

function Action({ icon: Icon, label, onClick, danger, disabled }) {
  return (
    <button onClick={onClick} disabled={disabled}
            className={cn('flex flex-col items-center gap-1.5 rounded-xl px-2 py-2.5 text-[11px] transition disabled:opacity-40',
              danger ? 'text-red-400 hover:bg-red-500/10' : 'text-zinc-300 hover:bg-zinc-800')}>
      <Icon className="h-[18px] w-[18px]" />
      {label}
    </button>
  );
}

/** Right-hand panel: what you have selected (or the folder you are in) and what you can do with it. */
export default function DetailsPanel({ selection, folder, view, onClose, actions, can }) {
  const single = selection.length === 1 ? selection[0] : null;
  const [full, setFull] = useState(null);   // folders: real size + counts, computed on demand

  useEffect(() => {
    setFull(null);
    if (!single?.is_dir) return undefined;
    let dead = false;
    filesApi.info(single.path).then((r) => { if (!dead) setFull(r); }).catch(() => {});
    return () => { dead = true; };
  }, [single?.path, single?.is_dir]);

  const total = selection.reduce((s, e) => s + (e.is_dir ? 0 : e.size), 0);

  return (
    <aside className="flex h-full w-full flex-col border-l border-zinc-800/80 bg-zinc-950/60">
      <div className="flex items-center justify-between px-5 py-3.5">
        <h3 className="text-sm font-semibold text-zinc-200">Details</h3>
        <button onClick={onClose} aria-label="Close details" className="rounded-md p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"><X className="h-4 w-4" /></button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5">
        {single && (
          <>
            <div className="overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900">
              <Thumb entry={single} src={single.thumb ? thumbUrl(single.path, 600, single.mtime) : null} className="aspect-[4/3] w-full" />
            </div>
            <div className="mt-4 flex items-start gap-2.5">
              <KindGlyph kind={single.kind} className="mt-0.5 h-5 w-5 shrink-0" />
              <p className="min-w-0 break-words text-[15px] font-medium leading-snug text-zinc-50">{single.name}</p>
            </div>

            <div className="mt-4 grid grid-cols-3 gap-1 rounded-2xl border border-zinc-800 bg-zinc-900/50 p-1">
              <Action icon={Download} label="Download" onClick={() => actions.download([single])} />
              {single.is_dir && <Action icon={FolderOpen} label="Open" onClick={() => actions.open(single)} />}
              {!single.is_dir && <Action icon={Eye} label="Preview" onClick={() => actions.open(single)} />}
              {can.share && <Action icon={Share2} label="Share" onClick={() => actions.share(single)} />}
              {can.write && <Action icon={Pencil} label="Rename" onClick={() => actions.rename(single)} />}
              {can.write && <Action icon={Trash2} label="Delete" danger onClick={() => actions.remove([single])} />}
            </div>

            <dl className="mt-4 divide-y divide-zinc-800/70">
              <Row label="Type">{kindLabel(single)}</Row>
              <Row label="Size">
                {single.is_dir
                  ? (full ? `${formatBytes(full.size)}${full.partial ? '+' : ''} - ${full.files} file${full.files === 1 ? '' : 's'}` : <Loader2 className="ml-auto h-3.5 w-3.5 animate-spin text-zinc-500" />)
                  : `${formatBytes(single.size)} (${single.size.toLocaleString()} bytes)`}
              </Row>
              <Row label="Modified">{formatWhen(single.mtime, { long: true })}</Row>
              <Row label="Location">{parentOf(single.path) ? parentOf(single.path).split('/').join(' / ') : 'All files'}</Row>
              <Row label="Added by">{single.uploaded_by}</Row>
              <Row label="Sharing">{single.shared ? 'Has a public link' : 'Private'}</Row>
            </dl>

            <button onClick={() => actions.star(single)}
                    className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl border border-zinc-800 py-2 text-sm text-zinc-300 hover:bg-zinc-800/70">
              <Star className={cn('h-4 w-4', single.starred && 'fill-amber-300 text-amber-300')} />
              {single.starred ? 'Remove star' : 'Add star'}
            </button>
            {!!single.shared && can.share && (
              <button onClick={() => actions.share(single)}
                      className="mt-2 flex w-full items-center justify-center gap-2 rounded-xl border border-zinc-800 py-2 text-sm text-zinc-300 hover:bg-zinc-800/70">
                <Link2 className="h-4 w-4" /> Manage links
              </button>
            )}
          </>
        )}

        {selection.length > 1 && (
          <>
            <div className="grid aspect-[4/3] place-items-center rounded-2xl border border-zinc-800 bg-zinc-900">
              <div className="text-center">
                <p className="text-4xl font-semibold text-zinc-100">{selection.length}</p>
                <p className="mt-1 text-sm text-zinc-500">items selected</p>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-3 gap-1 rounded-2xl border border-zinc-800 bg-zinc-900/50 p-1">
              <Action icon={Download} label="Download" onClick={() => actions.download(selection)} />
              {can.write && <Action icon={FolderOpen} label="Move to" onClick={() => actions.moveTo(selection)} />}
              {can.write && <Action icon={Trash2} label="Delete" danger onClick={() => actions.remove(selection)} />}
            </div>
            <dl className="mt-4 divide-y divide-zinc-800/70">
              <Row label="Folders">{selection.filter((e) => e.is_dir).length || ''}</Row>
              <Row label="Files">{selection.filter((e) => !e.is_dir).length || ''}</Row>
              <Row label="Size of files">{formatBytes(total)}</Row>
            </dl>
          </>
        )}

        {selection.length === 0 && (
          <div>
            <div className="grid aspect-[4/3] place-items-center rounded-2xl border border-dashed border-zinc-800 bg-zinc-900/40 text-center">
              <div>
                <KindGlyph kind="folder" className="mx-auto h-10 w-10" />
                <p className="mt-3 px-4 text-sm font-medium text-zinc-200">{folder.title}</p>
              </div>
            </div>
            <dl className="mt-4 divide-y divide-zinc-800/70">
              <Row label="Folders">{view.folders}</Row>
              <Row label="Files">{view.files}</Row>
              <Row label="Size of files">{formatBytes(view.size)}</Row>
            </dl>
            <p className="mt-5 text-xs leading-relaxed text-zinc-600">Select something to see its details, or drag files anywhere on this page to upload them.</p>
          </div>
        )}
      </div>
    </aside>
  );
}
