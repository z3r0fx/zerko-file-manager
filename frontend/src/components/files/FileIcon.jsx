import {
  Folder, File, FileText, FileSpreadsheet, FileCode2, FileArchive, Image as ImageIcon, Film, Music,
  Disc3, AppWindow, Type, Clapperboard, FileType2,
} from 'lucide-react';
import { cn } from '../../lib/utils';

// One look per kind of file: a soft coloured tile with a line icon. The colour
// does the recognising - you find "the zip" or "the video" without reading.
export const KIND_STYLE = {
  folder:   { icon: Folder,          tint: 'text-amber-300',   bg: 'bg-amber-400/12' },
  image:    { icon: ImageIcon,       tint: 'text-violet-300',  bg: 'bg-violet-400/12' },
  video:    { icon: Film,            tint: 'text-rose-300',    bg: 'bg-rose-400/12' },
  audio:    { icon: Music,           tint: 'text-fuchsia-300', bg: 'bg-fuchsia-400/12' },
  pdf:      { icon: FileType2,       tint: 'text-red-300',     bg: 'bg-red-400/12' },
  document: { icon: FileText,        tint: 'text-sky-300',     bg: 'bg-sky-400/12' },
  sheet:    { icon: FileSpreadsheet, tint: 'text-emerald-300', bg: 'bg-emerald-400/12' },
  code:     { icon: FileCode2,       tint: 'text-cyan-300',    bg: 'bg-cyan-400/12' },
  text:     { icon: FileText,        tint: 'text-zinc-300',    bg: 'bg-zinc-400/12' },
  archive:  { icon: FileArchive,     tint: 'text-orange-300',  bg: 'bg-orange-400/12' },
  disk:     { icon: Disc3,           tint: 'text-slate-300',   bg: 'bg-slate-400/12' },
  app:      { icon: AppWindow,       tint: 'text-indigo-300',  bg: 'bg-indigo-400/12' },
  font:     { icon: Type,            tint: 'text-lime-300',    bg: 'bg-lime-400/12' },
  project:  { icon: Clapperboard,    tint: 'text-teal-300',    bg: 'bg-teal-400/12' },
  other:    { icon: File,            tint: 'text-zinc-400',    bg: 'bg-zinc-400/10' },
};

/** A small inline icon (rows, breadcrumbs, menus). */
export function KindGlyph({ kind, className }) {
  const s = KIND_STYLE[kind] || KIND_STYLE.other;
  const Icon = s.icon;
  return <Icon className={cn(s.tint, kind === 'folder' && 'fill-amber-300/25', className)} strokeWidth={1.75} />;
}

/** A large tile (grid cards, details panel, previews with nothing to show). */
export function KindTile({ kind, ext, className, iconClass }) {
  const s = KIND_STYLE[kind] || KIND_STYLE.other;
  const Icon = s.icon;
  return (
    <div className={cn('relative grid place-items-center', s.bg, className)}>
      <Icon className={cn(s.tint, 'h-1/3 w-1/3 min-h-6 min-w-6', kind === 'folder' && 'fill-amber-300/25', iconClass)} strokeWidth={1.5} />
      {ext && kind !== 'folder' && (
        <span className="absolute bottom-2 right-2 rounded bg-zinc-950/70 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-zinc-400">
          {ext.slice(0, 5)}
        </span>
      )}
    </div>
  );
}
