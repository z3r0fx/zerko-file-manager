import { Check, Star, Link2, ArrowUp, ArrowDown, MoreVertical } from 'lucide-react';
import { cn } from '../../lib/utils';
import { formatBytes, formatWhen, kindLabel, thumbUrl, parentOf, DND_TYPE } from '../../lib/files';
import Thumb from './Thumb';
import { KindGlyph } from './FileIcon';

const dropProps = (entry, h) => (entry.is_dir && h.canDrop ? {
  'data-drop-path': entry.path,
  onDragOver: (e) => {
    const t = [...e.dataTransfer.types];
    if (!t.includes(DND_TYPE) && !t.includes('Files')) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = t.includes(DND_TYPE) ? (e.ctrlKey || e.altKey ? 'copy' : 'move') : 'copy';
    h.setDropTarget(entry.path);
  },
  onDragLeave: () => h.setDropTarget((d) => (d === entry.path ? null : d)),
  onDrop: (e) => { e.preventDefault(); e.stopPropagation(); h.setDropTarget(null); h.onDropItems(e, entry.path); },
} : {});

function common(entry, h) {
  const selected = h.selected.has(entry.path);
  return {
    'data-path': entry.path,
    draggable: h.canDrag,
    onDragStart: (e) => h.onDragStart(e, entry),
    onClick: (e) => h.onClick(e, entry),
    onDoubleClick: () => h.onOpen(entry),
    onContextMenu: (e) => { e.preventDefault(); h.onContext(e, entry); },
    ...dropProps(entry, h),
    selected,
    over: h.dropTarget === entry.path,
    cut: h.cut.has(entry.path),
    focused: h.focus === entry.path,
  };
}

function Check_({ on, onClick, className }) {
  return (
    <button type="button" tabIndex={-1} aria-label={on ? 'Deselect' : 'Select'}
            onClick={(e) => { e.stopPropagation(); onClick(e); }}
            onDoubleClick={(e) => e.stopPropagation()}
            className={cn('grid h-5 w-5 place-items-center rounded-md border transition',
              on ? 'border-accent bg-accent text-accent-foreground opacity-100' : 'border-zinc-500 bg-zinc-950/70 text-transparent opacity-0 hover:border-zinc-300 group-hover:opacity-100', className)}>
      <Check className="h-3.5 w-3.5" strokeWidth={3} />
    </button>
  );
}

// ------------------------------------------------------------------ grid
function FolderCard({ entry, h }) {
  const { selected, over, cut, focused, ...rest } = common(entry, h);
  return (
    <div {...rest} tabIndex={-1}
         className={cn('group relative flex cursor-default select-none items-center gap-3 rounded-2xl border px-3.5 py-3 transition',
           selected ? 'border-accent/60 bg-accent/15' : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-700 hover:bg-zinc-900',
           over && 'border-accent bg-accent/25 ring-2 ring-accent/60', cut && 'opacity-50', focused && !selected && 'ring-1 ring-zinc-500')}>
      <KindGlyph kind="folder" className="h-6 w-6 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-zinc-100" title={entry.name}>{entry.name}</p>
        {h.showLocation && <p className="truncate text-[11px] text-zinc-500">{parentOf(entry.path) || 'All files'}</p>}
      </div>
      {!!entry.shared && <Link2 className="h-3.5 w-3.5 shrink-0 text-sky-400" />}
      {!!entry.starred && <Star className="h-3.5 w-3.5 shrink-0 fill-amber-300 text-amber-300" />}
      <div className={cn('absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1 rounded-lg bg-zinc-900/95 pl-2 shadow-[-8px_0_8px_-2px_rgb(24_24_27/0.95)] transition',
             selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 max-md:opacity-100')}>
        <Check_ on={selected} onClick={() => h.onCheck(entry)} className="!opacity-100" />
        <button type="button" tabIndex={-1} aria-label="More" onClick={(e) => { e.stopPropagation(); h.onContext(e, entry); }}
                className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-zinc-500 transition hover:bg-zinc-800 hover:text-zinc-100">
          <MoreVertical className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

function FileCard({ entry, h }) {
  const { selected, over, cut, focused, ...rest } = common(entry, h);
  return (
    <div {...rest} tabIndex={-1}
         className={cn('group relative cursor-default select-none overflow-hidden rounded-2xl border transition',
           selected ? 'border-accent/70 bg-accent/10 ring-1 ring-accent/50' : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-600 hover:bg-zinc-900',
           cut && 'opacity-50', focused && !selected && 'ring-1 ring-zinc-500')}>
      <div className="p-2 pb-0">
        <Thumb entry={entry} src={entry.thumb ? thumbUrl(entry.path, 360, entry.mtime) : null}
               className="aspect-[4/3] w-full rounded-xl" />
      </div>
      <Check_ on={selected} onClick={() => h.onCheck(entry)} className="absolute left-3.5 top-3.5" />
      <div className="absolute right-3.5 top-3.5 flex items-center gap-1">
        {!!entry.shared && <span className="grid h-6 w-6 place-items-center rounded-full bg-zinc-950/75"><Link2 className="h-3.5 w-3.5 text-sky-400" /></span>}
        {!!entry.starred && <span className="grid h-6 w-6 place-items-center rounded-full bg-zinc-950/75"><Star className="h-3.5 w-3.5 fill-amber-300 text-amber-300" /></span>}
      </div>
      <div className="flex items-center gap-2 px-3 py-2.5">
        <KindGlyph kind={entry.kind} className="h-4 w-4 shrink-0" />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium text-zinc-100" title={entry.name}>{entry.name}</p>
          <p className="truncate text-[11px] text-zinc-500">
            {h.showLocation ? (parentOf(entry.path) || 'All files') : `${formatBytes(entry.size)} - ${formatWhen(entry.mtime)}`}
          </p>
        </div>
        <button type="button" tabIndex={-1} aria-label="More" onClick={(e) => { e.stopPropagation(); h.onContext(e, entry); }}
                className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-zinc-500 opacity-0 transition hover:bg-zinc-800 hover:text-zinc-100 group-hover:opacity-100 max-md:opacity-100">
          <MoreVertical className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

export function GridView({ entries, h }) {
  const folders = entries.filter((e) => e.is_dir);
  const files = entries.filter((e) => !e.is_dir);
  return (
    <div className="space-y-6">
      {folders.length > 0 && (
        <section>
          {files.length > 0 && <h3 className="mb-2.5 text-xs font-medium uppercase tracking-wider text-zinc-500">Folders</h3>}
          <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(calc(13.5rem_*_var(--tile-scale,1)),1fr))]">
            {folders.map((e) => <FolderCard key={e.path} entry={e} h={h} />)}
          </div>
        </section>
      )}
      {files.length > 0 && (
        <section>
          {folders.length > 0 && <h3 className="mb-2.5 text-xs font-medium uppercase tracking-wider text-zinc-500">Files</h3>}
          <div className="grid gap-3.5 [grid-template-columns:repeat(auto-fill,minmax(calc(11.5rem_*_var(--tile-scale,1)),1fr))]">
            {files.map((e) => <FileCard key={e.path} entry={e} h={h} />)}
          </div>
        </section>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ list
function Header({ sort, onSort, showLocation }) {
  const Th = ({ k, children, className }) => (
    <button type="button" onClick={() => onSort(k)}
            className={cn('flex items-center gap-1 text-left text-xs font-medium uppercase tracking-wider transition hover:text-zinc-200',
              sort.key === k ? 'text-zinc-200' : 'text-zinc-500', className)}>
      {children}
      {sort.key === k && (sort.dir === 'asc' ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />)}
    </button>
  );
  return (
    <div className="sticky top-0 z-10 grid items-center gap-4 border-b border-zinc-800 bg-zinc-950/95 px-3 py-2 backdrop-blur [grid-template-columns:2rem_minmax(0,1fr)_7rem] md:[grid-template-columns:2rem_minmax(0,1fr)_9rem_6rem_9rem]">
      <span />
      <Th k="name">Name</Th>
      <Th k="modified" className="max-md:hidden">Modified</Th>
      <Th k="size" className="justify-end max-md:hidden">Size</Th>
      <Th k="kind" className="max-md:hidden">Type</Th>
      <span className="hidden max-md:block" />
    </div>
  );
}

function ListRow({ entry, h }) {
  const { selected, over, cut, focused, ...rest } = common(entry, h);
  return (
    <div {...rest} tabIndex={-1}
         className={cn('group grid cursor-default select-none items-center gap-4 border-b border-zinc-900 px-3 py-2 transition [grid-template-columns:2rem_minmax(0,1fr)_7rem] md:[grid-template-columns:2rem_minmax(0,1fr)_9rem_6rem_9rem]',
           selected ? 'bg-accent/15' : 'hover:bg-zinc-900/80', over && 'bg-accent/25 ring-1 ring-inset ring-accent',
           cut && 'opacity-50', focused && !selected && 'shadow-[inset_0_0_0_1px_rgb(113_113_122)]')}>
      <div className="relative grid h-8 w-8 place-items-center">
        <div className={cn('transition', selected && 'opacity-0', 'group-hover:opacity-0')}>
          <KindGlyph kind={entry.kind} className="h-[22px] w-[22px]" />
        </div>
        <Check_ on={selected} onClick={() => h.onCheck(entry)} className="absolute" />
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm text-zinc-100" title={entry.name}>{entry.name}</span>
          {!!entry.starred && <Star className="h-3.5 w-3.5 shrink-0 fill-amber-300 text-amber-300" />}
          {!!entry.shared && <Link2 className="h-3.5 w-3.5 shrink-0 text-sky-400" />}
        </div>
        {h.showLocation && <p className="truncate text-[11px] text-zinc-500">{parentOf(entry.path) || 'All files'}</p>}
      </div>
      <span className="truncate text-[13px] text-zinc-400 max-md:hidden">{formatWhen(entry.mtime)}</span>
      <span className="text-right text-[13px] tabular-nums text-zinc-400 max-md:hidden">{entry.is_dir ? '' : formatBytes(entry.size)}</span>
      <span className="truncate text-[13px] text-zinc-500 max-md:hidden">{kindLabel(entry)}</span>
      <div className="flex items-center justify-end gap-2 md:hidden">
        <span className="text-[11px] text-zinc-500">{entry.is_dir ? '' : formatBytes(entry.size)}</span>
        <button type="button" tabIndex={-1} aria-label="More" onClick={(e) => { e.stopPropagation(); h.onContext(e, entry); }}
                className="grid h-7 w-7 place-items-center rounded-md text-zinc-500 hover:bg-zinc-800"><MoreVertical className="h-4 w-4" /></button>
      </div>
    </div>
  );
}

export function ListView({ entries, h, sort, onSort }) {
  return (
    <div className="-mx-3">
      <Header sort={sort} onSort={onSort} showLocation={h.showLocation} />
      {entries.map((e) => <ListRow key={e.path} entry={e} h={h} />)}
    </div>
  );
}
